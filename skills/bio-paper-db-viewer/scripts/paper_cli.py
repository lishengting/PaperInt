#!/usr/bin/env python3
"""
Bio Paper DB Viewer CLI — view papers and statistics from the SQLite database.

Usage:
  paper_cli.py stats
  paper_cli.py list [-s STATUS] [--source SOURCE] [-k KEYWORD] [-n N] [--offset OFFSET]
  paper_cli.py show -p PAPER_ID
  paper_cli.py delete -p PAPER_ID
  paper_cli.py set-status -p PAPER_ID [PAPER_ID ...] -s STATUS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

# Reach up three levels to the project-root scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', '..', '..', 'scripts'))
from paper_db import get_conn, get_stats, get_paper, _row_to_dict


# ---------------------------------------------------------------------------
# Config loading (shared pattern across all paper_cli.py files)
# ---------------------------------------------------------------------------

def _simple_yaml(text: str) -> dict:
    """Minimal YAML parser — fallback when PyYAML is unavailable."""
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            k, v = line.split(':', 1)
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
    return out


def load_config(path: str) -> dict:
    """Load YAML config (PyYAML preferred, simple fallback)."""
    with open(path, 'r', encoding='utf-8') as fh:
        text = fh.read()
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return _simple_yaml(text)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def cmd_stats(args, config):
    conn = get_conn(config)
    status_counts = get_stats(conn)
    total = sum(status_counts.values())

    print(f"Papers database: {config.get('db', {}).get('path', 'data/papers.db')}")
    print(f"{'─' * 50}")
    print(f"Total papers: {total}")
    print()

    # Status breakdown
    print("By status:")
    for st in ('searched', 'downloaded', 'download_failed', 'interpreted'):
        cnt = status_counts.get(st, 0)
        if cnt > 0 or st in ('searched', 'downloaded'):
            print(f"  {st:<20} {cnt:>4}")
    print()

    # Source breakdown
    print("By source:")
    for row in conn.execute(
        'SELECT source, COUNT(*) as cnt FROM papers GROUP BY source ORDER BY cnt DESC'
    ):
        print(f"  {row['source']:<20} {row['cnt']:>4}")
    print()

    # Date range
    row = conn.execute(
        'SELECT MIN(search_date) as mn, MAX(search_date) as mx FROM papers WHERE search_date IS NOT NULL'
    ).fetchone()
    if row and row['mn']:
        mn = row['mn'][:19] if len(row['mn']) > 19 else row['mn']
        mx = row['mx'][:19] if len(row['mx']) > 19 else row['mx']
        print(f"Date range: {mn} — {mx}")

    # Recent counts
    for label, days in (('Last 7 days', 7), ('Last 30 days', 30)):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cnt = conn.execute(
            'SELECT COUNT(*) as cnt FROM papers WHERE search_date >= ?', (cutoff,)
        ).fetchone()['cnt']
        print(f"  {label}: {cnt}")

    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args, config):
    conn = get_conn(config)

    if args.all:
        args.limit = sys.maxsize

    if args.json:
        json_sql = 'SELECT * FROM papers WHERE 1=1'
        json_params: list = []
        if args.status:
            json_sql += ' AND status = ?'
            json_params.append(args.status)
        if args.source:
            json_sql += ' AND source = ?'
            json_params.append(args.source)
        if args.keyword:
            json_sql += ' AND title LIKE ?'
            json_params.append(f'%{args.keyword}%')

        json_sql += ' ORDER BY search_date DESC, paper_id LIMIT ? OFFSET ?'
        json_params.extend([args.limit, args.offset])
        rows = conn.execute(json_sql, json_params).fetchall()

        results = []
        for r in rows:
            paper = _row_to_dict(r)
            paper.pop('metadata_json', None)
            results.append(paper)

        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        return 0

    sql = 'SELECT paper_id, doi, title, source, status, search_date, path_prefix, metadata_json FROM papers WHERE 1=1'
    params: list = []

    if args.status:
        sql += ' AND status = ?'
        params.append(args.status)
    if args.source:
        sql += ' AND source = ?'
        params.append(args.source)
    if args.keyword:
        sql += ' AND title LIKE ?'
        params.append(f'%{args.keyword}%')

    # Parse journal from metadata_json for each row
    def _get_journal(metadata_json):
        if not metadata_json:
            return ''
        try:
            meta = json.loads(metadata_json)
            return meta.get('journal', '') or ''
        except Exception:
            return ''

    def _get_issn(metadata_json):
        if not metadata_json:
            return ''
        try:
            meta = json.loads(metadata_json)
            return meta.get('issn', '') or ''
        except Exception:
            return ''

    # ---- CNSP helpers ----
    def _load_cnsp_map():
        cnsp_cfg = config.get('cnsp', {})
        flagships = {'Nature', 'Science', 'Cell', 'PLOS Biology', 'PLOS Medicine'}
        journal_map = {}
        for key, letter in [('nature_journals', 'n'), ('science_journals', 's'),
                            ('cell_journals', 'c'), ('plos_journals', 'p')]:
            rel = cnsp_cfg.get(key, '')
            if not rel:
                continue
            jpath = rel if os.path.isabs(rel) else os.path.join(
                os.path.dirname(os.path.abspath(args.config)), rel)
            if not os.path.exists(jpath):
                continue
            for j in json.load(open(jpath)):
                name = (j.get('name', '') or '').replace(' (partner)', '')
                if not name:
                    continue
                code = letter.upper() if name in flagships else letter
                journal_map[name.lower()] = code
                abbrev = j.get('abbrev', '')
                if abbrev:
                    journal_map[abbrev.lower()] = code
        return journal_map

    def _load_cns_map():
        cnsp_cfg = config.get('cnsp', {})
        flagships = {'Nature', 'Science', 'Cell'}
        journal_map = {}
        for key, letter in [('nature_journals', 'n'), ('science_journals', 's'),
                            ('cell_journals', 'c')]:
            rel = cnsp_cfg.get(key, '')
            if not rel:
                continue
            jpath = rel if os.path.isabs(rel) else os.path.join(
                os.path.dirname(os.path.abspath(args.config)), rel)
            if not os.path.exists(jpath):
                continue
            for j in json.load(open(jpath)):
                name = (j.get('name', '') or '').replace(' (partner)', '')
                if not name:
                    continue
                code = letter.upper() if name in flagships else letter
                journal_map[name.lower()] = code
                abbrev = j.get('abbrev', '')
                if abbrev:
                    journal_map[abbrev.lower()] = code
        return journal_map

    cnsp_map = _load_cnsp_map()
    cns_map = _load_cns_map() if args.cns else None

    def _get_cnsp(journal_name):
        if not journal_name:
            return ''
        key = journal_name.lower().replace('&amp;', '&')
        return cnsp_map.get(key, '')

    # If --cnsp or --cns flag, load all matching rows and post-filter
    if args.cnsp or args.cns:
        which = 'CNS' if args.cns else 'CNSP'
        lookup_map = cns_map if args.cns else cnsp_map
        all_rows = conn.execute(sql + ' ORDER BY search_date DESC, paper_id', params).fetchall()
        filtered = []
        for r in all_rows:
            journal = _get_journal(r['metadata_json'])
            code = _get_cnsp(journal) if not args.cns else lookup_map.get(
                (journal or '').lower().replace('&amp;', '&'), '')
            if code:
                filtered.append((r, code, journal))
        total = len(filtered)
        # Apply pagination after filtering
        page = filtered[args.offset:args.offset + args.limit]
        rows = [r for r, code, journal in page]
    else:
        count_sql = sql.replace(
            'SELECT paper_id, doi, title, source, status, search_date, path_prefix, metadata_json',
            'SELECT COUNT(*) as cnt', 1
        )
        total = conn.execute(count_sql, params).fetchone()['cnt']

        sql += ' ORDER BY search_date DESC, paper_id LIMIT ? OFFSET ?'
        params.extend([args.limit, args.offset])
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        which = 'CNS ' if args.cns else ('CNSP ' if args.cnsp else '')
        print(f"No {which}papers found{f' matching filters' if (args.status or args.source or args.keyword) else ''}.")
        return 0

    # Column widths
    doi_w = 28
    id_w = 36
    src_w = 8
    st_w = 17
    pp_w = 40
    date_w = 19
    cnsp_w = 4
    j_w = 20
    issn_w = 10

    header = f"{'DOI':<{doi_w}} {'Paper ID':<{id_w}} {'Title':<50} {'Source':<{src_w}} {'CNSP':<{cnsp_w}} {'Status':<{st_w}} {'Path':<{pp_w}} {'Date':<{date_w}} {'Journal':<{j_w}} {'ISSN':<{issn_w}}"
    sep = f"{'─' * doi_w} {'─' * id_w} {'─' * 50} {'─' * src_w} {'─' * cnsp_w} {'─' * st_w} {'─' * pp_w} {'─' * date_w} {'─' * j_w} {'─' * issn_w}"
    print(header)
    print(sep)

    for r in rows:
        doi = r['doi'] or ''
        pid = r['paper_id'] or ''
        title = r['title'] or ''
        source = r['source'] or ''
        status = r['status'] or ''
        pp = r['path_prefix'] or ''
        date_str = r['search_date'] or ''
        journal = _get_journal(r['metadata_json'])
        cnsp = _get_cnsp(journal)
        issn = _get_issn(r['metadata_json'])

        if not args.no_truncate:
            if len(doi) > doi_w - 2:
                doi = doi[:doi_w - 5] + '...'
            if len(pid) > id_w - 2:
                pid = pid[:id_w - 5] + '...'
            if len(title) > 48:
                title = title[:47] + '...'
            if len(source) > src_w:
                source = source[:src_w - 1]
            if len(pp) > pp_w - 2:
                pp = pp[:pp_w - 5] + '...'
            if len(date_str) > date_w:
                date_str = date_str[:date_w]
            if len(journal) > j_w:
                journal = journal[:j_w - 2] + '..'
            if len(issn) > issn_w:
                issn = issn[:issn_w]

        print(f"{doi:<{doi_w}} {pid:<{id_w}} {title:<50} {source:<{src_w}} {cnsp:<{cnsp_w}} {status:<{st_w}} {pp:<{pp_w}} {date_str:<{date_w}} {journal:<{j_w}} {issn:<{issn_w}}")

    showing = min(args.limit, len(rows))
    after = args.offset + showing
    suffix = f" (CNS)" if args.cns else (f" (CNSP)" if args.cnsp else "")
    if total > args.limit:
        print(f"\nShowing {showing} of {total}{suffix} papers (page {(args.offset // args.limit) + 1})")
    else:
        print(f"\n{total}{suffix} paper(s)")

    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def cmd_show(args, config):
    conn = get_conn(config)
    paper = get_paper(conn, args.paper_id)

    if not paper:
        print(f"Paper not found: {args.paper_id}", file=sys.stderr)
        return 1

    # Fields to display in order, with labels
    field_order = [
        ('paper_id', 'Paper ID'),
        ('title', 'Title'),
        ('authors', 'Authors'),
        ('abstract', 'Abstract'),
        ('doi', 'DOI'),
        ('pmid', 'PMID'),
        ('arxiv_id', 'arXiv ID'),
        ('source', 'Source'),
        ('issn', 'ISSN'),
        ('source_url', 'Source URL'),
        ('pdf_url', 'PDF URL'),
        ('dir_name', 'Directory'),
        ('status', 'Status'),
        ('search_date', 'Search Date'),
        ('download_date', 'Download Date'),
        ('interpret_date', 'Interpret Date'),
        ('oa_has_pdf', 'Open Access PDF'),
        ('error_message', 'Error'),
    ]

    print(f"{'─' * 70}")
    for field, label in field_order:
        value = paper.get(field)
        if value is None or value == '' or value == 0:
            continue
        if isinstance(value, str) and len(value) > 500:
            value = value[:500] + '...'
        print(f"{label}: {value}")

    # Show parsed JSON fields if present
    for json_field, label in (('relevance', 'Relevance'), ('matched_tags', 'Tags')):
        value = paper.get(json_field)
        if value and isinstance(value, (dict, list)):
            print(f"\n{label}:")
            print(json.dumps(value, indent=2, ensure_ascii=False))

    print(f"{'─' * 70}")
    return 0


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def cmd_delete(args, config):
    conn = get_conn(config)
    paper = get_paper(conn, args.paper_id)
    if not paper:
        print(f"Paper not found: {args.paper_id}", file=sys.stderr)
        return 1
    title = (paper.get('title') or '')[:60]
    conn.execute("DELETE FROM papers WHERE paper_id = ?", (args.paper_id,))
    conn.commit()
    print(f"Deleted: {args.paper_id} — {title}")
    return 0


# ---------------------------------------------------------------------------
# set-status
# ---------------------------------------------------------------------------

VALID_STATUSES = {'searched', 'downloaded', 'download_failed', 'interpreted',
                  'interpret_failed'}


def cmd_set_status(args, config):
    conn = get_conn(config)
    updated = 0
    not_found = 0
    for pid in args.paper_id:
        paper = get_paper(conn, pid)
        if not paper:
            print(f"Not found: {pid}", file=sys.stderr)
            not_found += 1
            continue
        old_status = paper['status']
        conn.execute(
            "UPDATE papers SET status = ?, updated_at = datetime('now') WHERE paper_id = ?",
            (args.status, pid))
        conn.commit()
        title = (paper.get('title') or '')[:60]
        print(f"{pid}  {old_status} -> {args.status}  {title}")
        updated += 1
    print(f"\n{updated} updated" + (f", {not_found} not found" if not_found else ""))
    return 0 if not_found == 0 else 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  # Show database summary statistics
  paper_cli.py stats

  # List all papers (most recent 20)
  paper_cli.py list

  # List all papers without limit
  paper_cli.py list --all

  # List papers by status
  paper_cli.py list -s downloaded -n 10

  # List papers by source with keyword filter
  paper_cli.py list --source nature -k "microbiome"

  # Paginate through results
  paper_cli.py list -n 10 --offset 20

  # Show full details for a paper
  paper_cli.py show -p s41467-026-70776-7
"""


def main():
    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Bio Paper DB Viewer — view papers and statistics from the SQLite database.',
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--config', default='config.yaml',
                   help='Path to shared YAML config file (default: config.yaml)')
    p.add_argument('--db', default=None,
                   help='Path to SQLite database (default: from config.yaml)')

    sub = p.add_subparsers(dest='cmd', required=True,
                           title='commands',
                           description='"stats", "list", "show", "delete", or "set-status"')

    # ---- stats ----
    sub.add_parser('stats', help='Show database summary statistics',
                   description='Display total papers, counts by status and source, date range, and recent activity.')

    # ---- list ----
    lp = sub.add_parser('list', help='List papers with optional filters',
                        description='List papers from the database with optional filtering by status, source, and keyword.')
    lp.add_argument('-s', '--status', default=None,
                    help='Filter by status: searched, downloaded, download_failed, interpreted')
    lp.add_argument('--source', default=None,
                    help='Filter by source: arxiv, biorxiv, medrxiv, pubmed, scholar, nature, science, cell, plos')
    lp.add_argument('-k', '--keyword', default=None,
                    help='Filter papers whose title contains the keyword')
    lp.add_argument('-n', '--limit', type=int, default=20,
                    help='Maximum results (default: 20)')
    lp.add_argument('--offset', type=int, default=0,
                    help='Pagination offset (default: 0)')
    lp.add_argument('--all', action='store_true',
                    help='List all matching papers (overrides -n)')
    lp.add_argument('--no-truncate', action='store_true',
                    help='Show full field values without truncation')
    lp.add_argument('--json', action='store_true',
                    help='Output as JSON with full paper details')
    lp.add_argument('--cnsp', action='store_true',
                    help='Only show papers whose journal is in CNSP (Nature/Science/Cell/PLOS)')
    lp.add_argument('--cns', action='store_true',
                    help='Only show papers whose journal is in CNS (Nature/Science/Cell, excluding PLOS)')

    # ---- delete ----
    dp = sub.add_parser('delete', help='Delete a paper record',
                        description='Remove a paper from the database by its ID.')
    dp.add_argument('-p', '--paper-id', required=True,
                    help='Paper ID to delete')

    # ---- set-status ----
    ssp = sub.add_parser('set-status', help='Set status for one or more papers',
                         description='Change the status of one or more papers by ID.')
    ssp.add_argument('-p', '--paper-id', required=True, nargs='+',
                     help='Paper ID(s) to update')
    ssp.add_argument('-s', '--status', required=True, choices=sorted(VALID_STATUSES),
                     help='Target status')

    # ---- show ----
    sp = sub.add_parser('show', help='Show full details for a paper',
                        description='Display all available fields for a single paper by its ID.')
    sp.add_argument('-p', '--paper-id', required=True,
                    help='Paper ID from the database (e.g., DOI, arXiv ID)')

    args = p.parse_args()
    config = load_config(args.config)
    if args.db:
        config.setdefault('db', {})['path'] = args.db

    if args.cmd == 'stats':
        return cmd_stats(args, config)
    elif args.cmd == 'list':
        return cmd_list(args, config)
    elif args.cmd == 'show':
        return cmd_show(args, config)
    elif args.cmd == 'delete':
        return cmd_delete(args, config)
    elif args.cmd == 'set-status':
        return cmd_set_status(args, config)
    return 1


if __name__ == '__main__':
    sys.exit(main())