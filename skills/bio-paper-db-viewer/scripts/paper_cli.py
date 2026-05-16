#!/usr/bin/env python3
"""
Bio Paper DB Viewer CLI — view papers and statistics from the SQLite database.

Usage:
  paper_cli.py stats
  paper_cli.py list [-s STATUS] [--source SOURCE] [-k KEYWORD] [-n N] [--offset OFFSET]
  paper_cli.py show -p PAPER_ID
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
from paper_db import get_conn, get_stats, get_paper


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
    for st in ('searched', 'downloaded', 'download_failed', 'interpreted', 'skipped'):
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

    sql = 'SELECT paper_id, title, source, status, search_date, metadata_json FROM papers WHERE 1=1'
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

    # Total matching count (before LIMIT/OFFSET)
    count_sql = sql.replace(
        'SELECT paper_id, title, source, status, search_date, metadata_json',
        'SELECT COUNT(*) as cnt', 1
    )
    total = conn.execute(count_sql, params).fetchone()['cnt']

    sql += ' ORDER BY search_date DESC LIMIT ? OFFSET ?'
    params.extend([args.limit, args.offset])

    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print(f"No papers found{f' matching filters' if (args.status or args.source or args.keyword) else ''}.")
        return 0

    # Parse journal from metadata_json for each row
    def _get_journal(metadata_json):
        if not metadata_json:
            return ''
        try:
            meta = json.loads(metadata_json)
            return meta.get('journal', '') or ''
        except Exception:
            return ''

    # Column widths
    id_w = 36
    src_w = 8
    st_w = 17
    date_w = 19
    cnsp_w = 4
    j_w = 20

    def _load_cnsp_map():
        cnsp_cfg = config.get('cnsp', {})
        flagships = {'Nature', 'Science', 'Cell'}
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
                if name:
                    journal_map[name.lower()] = letter.upper() if name in flagships else letter
        return journal_map

    cnsp_map = _load_cnsp_map()

    def _get_cnsp(journal_name):
        if not journal_name:
            return ''
        return cnsp_map.get(journal_name.lower(), '')

    header = f"{'Paper ID':<{id_w}} {'Title':<60} {'Source':<{src_w}} {'CNSP':<{cnsp_w}} {'Status':<{st_w}} {'Date':<{date_w}} {'Journal':<{j_w}}"
    sep = f"{'─' * id_w} {'─' * 60} {'─' * src_w} {'─' * cnsp_w} {'─' * st_w} {'─' * date_w} {'─' * j_w}"
    print(header)
    print(sep)

    for r in rows:
        pid = r['paper_id'] or ''
        if len(pid) > id_w - 2:
            pid = pid[:id_w - 5] + '...'
        title = r['title'] or ''
        if len(title) > 58:
            title = title[:57] + '...'
        source = r['source'] or ''
        if len(source) > src_w:
            source = source[:src_w - 1]
        status = r['status'] or ''
        date_str = r['search_date'] or ''
        if len(date_str) > date_w:
            date_str = date_str[:date_w]
        journal = _get_journal(r['metadata_json'])
        cnsp = _get_cnsp(journal)
        if len(journal) > j_w:
            journal = journal[:j_w - 2] + '..'

        print(f"{pid:<{id_w}} {title:<60} {source:<{src_w}} {cnsp:<{cnsp_w}} {status:<{st_w}} {date_str:<{date_w}} {journal:<{j_w}}")

    showing = min(args.limit, len(rows))
    if total > args.limit:
        print(f"\nShowing {showing} of {total} papers (page {(args.offset // args.limit) + 1})")
    else:
        print(f"\n{total} paper(s)")

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
# CLI entry point
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  # Show database summary statistics
  paper_cli.py stats

  # List all papers (most recent 20)
  paper_cli.py list

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
                           description='"stats", "list", or "show"')

    # ---- stats ----
    sub.add_parser('stats', help='Show database summary statistics',
                   description='Display total papers, counts by status and source, date range, and recent activity.')

    # ---- list ----
    lp = sub.add_parser('list', help='List papers with optional filters',
                        description='List papers from the database with optional filtering by status, source, and keyword.')
    lp.add_argument('-s', '--status', default=None,
                    help='Filter by status: searched, downloaded, download_failed, interpreted, skipped')
    lp.add_argument('--source', default=None,
                    help='Filter by source: arxiv, biorxiv, medrxiv, pubmed, scholar, nature, science, cell, plos')
    lp.add_argument('-k', '--keyword', default=None,
                    help='Filter papers whose title contains the keyword')
    lp.add_argument('-n', '--limit', type=int, default=20,
                    help='Maximum results (default: 20)')
    lp.add_argument('--offset', type=int, default=0,
                    help='Pagination offset (default: 0)')

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
    return 1


if __name__ == '__main__':
    sys.exit(main())