#!/usr/bin/env python3
"""
Bio Paper DB Viewer CLI — view papers and statistics from the SQLite database.

Usage:
  paper_cli.py stats
  paper_cli.py list [-s STATUS] [--source SOURCE] [-k KEYWORD] [--found-by KEYWORD] [-n N] [--offset OFFSET] [--id-only]
  paper_cli.py show -p PAPER_ID
  paper_cli.py delete -p PAPER_ID
  paper_cli.py set-status -p PAPER_ID [PAPER_ID ...] -s STATUS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

# Reach up three levels to the project-root scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', '..', '..', 'scripts'))
from paper_db import (get_conn, get_stats, get_paper, get_search_history, _row_to_dict,
                      load_cns_journal_set, load_cnsp_journal_set,
                      build_custom_journal_set, filter_papers_by_journals,
                      normalize_issn_values, paper_matches_journal_set)


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
    for st in ('searched', 'downloaded', 'download_failed', 'interpreted', 'interpret_failed'):
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

KEYWORD_SEARCH_COLUMNS = (
    'title', 'abstract', 'source', 'doi', 'pmid', 'paper_id', 'arxiv_id',
    'path_prefix', 'dir_name', 'source_url', 'pdf_url', 'source_terms_text',
)


def _local_keyword_clause(keyword: str) -> tuple[str, list]:
    pattern = f'%{keyword}%'
    clauses = [f"COALESCE({column}, '') LIKE ?" for column in KEYWORD_SEARCH_COLUMNS]
    clauses.append(
        "COALESCE("
        "CASE WHEN metadata_json IS NOT NULL AND json_valid(metadata_json) "
        "THEN json_extract(metadata_json, '$.journal') ELSE '' END, ''"
        ") LIKE ?"
    )
    return '(' + ' OR '.join(clauses) + ')', [pattern] * len(clauses)


def _single_keyword_provenance_clause(keyword: str) -> tuple[str, list]:
    simple_query = f'all sources: {keyword}'
    return """EXISTS (
        SELECT 1
        FROM paper_search_hits h
        JOIN search_runs r ON r.id = h.run_id
        WHERE h.paper_id = papers.paper_id
          AND (
              CASE
                  WHEN json_valid(r.keywords_json) THEN
                      CASE
                          WHEN json_type(r.keywords_json) = 'array'
                               AND json_array_length(r.keywords_json) = 1
                          THEN LOWER(COALESCE(json_extract(r.keywords_json, '$[0]'), ''))
                          ELSE ''
                      END
                  ELSE ''
              END = LOWER(?)
              OR LOWER(TRIM(COALESCE(r.keywords_json, ''))) = LOWER(?)
              OR LOWER(TRIM(COALESCE(r.query, ''))) = LOWER(?)
              OR LOWER(TRIM(COALESCE(r.query, ''))) = LOWER(?)
          )
    )""", [keyword, keyword, keyword, simple_query]


def _any_provenance_keyword_clause(keyword: str) -> tuple[str, list]:
    pattern = f'%{keyword}%'
    return """EXISTS (
        SELECT 1
        FROM paper_search_hits h
        JOIN search_runs r ON r.id = h.run_id
        WHERE h.paper_id = papers.paper_id
          AND (
              COALESCE(r.keywords_json, '') LIKE ?
              OR COALESCE(r.query, '') LIKE ?
              OR COALESCE(r.query_translation, '') LIKE ?
              OR COALESCE(h.matched_keywords_json, '') LIKE ?
          )
    )""", [pattern, pattern, pattern, pattern]


def _append_keyword_filter(sql: str, params: list, keyword: str,
                           provenance_mode: str = 'single') -> str:
    local_sql, local_params = _local_keyword_clause(keyword)
    if provenance_mode == 'any':
        provenance_sql, provenance_params = _any_provenance_keyword_clause(keyword)
    else:
        provenance_sql, provenance_params = _single_keyword_provenance_clause(keyword)
    params.extend(local_params + provenance_params)
    return sql + f' AND ({local_sql} OR {provenance_sql})'


def _append_found_by_filter(sql: str, params: list, keyword: str) -> str:
    provenance_sql, provenance_params = _any_provenance_keyword_clause(keyword)
    params.extend(provenance_params)
    return sql + f' AND {provenance_sql}'


NO_ISSN_JOURNAL_ID = '__no_issn__'
ISSN_METADATA_KEYS = ('issn', 'issn_print', 'issn_electronic', 'issns')
JOURNAL_LABEL_KEYS = ('journal', 'container-title', 'category', 'abbrev')


def _flatten_values(value):
    if value is None:
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten_values(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_values(item)
        return
    yield value


def _canonical_issn_ids(value) -> set[str]:
    ids = set()
    for item in _flatten_values(value):
        for variant in normalize_issn_values(item):
            compact = variant.replace('-', '').upper()
            if len(compact) == 8:
                ids.add(compact)
    return ids


def _display_issn(compact: str) -> str:
    compact = str(compact or '').replace('-', '').upper()
    if len(compact) == 8:
        return f'{compact[:4]}-{compact[4:]}'
    return compact


def _metadata_from_viewer_paper(paper: dict) -> dict:
    meta = paper.get('metadata_json')
    if isinstance(meta, dict):
        return meta
    if not isinstance(meta, str):
        return {}
    try:
        return json.loads(meta)
    except (json.JSONDecodeError, TypeError):
        return {}


def _paper_issn_ids(paper: dict) -> set[str]:
    ids = set()
    metadata = _metadata_from_viewer_paper(paper)
    for key in ISSN_METADATA_KEYS:
        ids.update(_canonical_issn_ids(paper.get(key)))
        ids.update(_canonical_issn_ids(metadata.get(key)))
    return ids


def _label_values(value) -> list[str]:
    labels = []
    for item in _flatten_values(value):
        if item is None:
            continue
        text = ' '.join(str(item).replace('&amp;', '&').replace(' (partner)', '').split())
        if text:
            labels.append(text)
    return labels


def _journal_from_source_terms(source_terms_text, issn='') -> str:
    terms = [' '.join(term.split()) for term in str(source_terms_text or '').split('|')]
    terms = [term for term in terms if term]
    if not terms:
        return ''
    known_issns = normalize_issn_values(issn)
    term_issns = set()
    for term in terms:
        term_issns.update(normalize_issn_values(term))
    if known_issns and not known_issns.intersection(term_issns):
        return ''
    if not known_issns and not term_issns:
        return ''
    for term in terms:
        if not normalize_issn_values(term):
            return term
    return ''


def _paper_journal_fallback(paper: dict) -> str:
    metadata = _metadata_from_viewer_paper(paper)
    return _journal_from_source_terms(
        paper.get('source_terms_text') or metadata.get('source_terms_text'),
        paper.get('issn') or metadata.get('issn'),
    )


def _apply_journal_fallback(paper: dict) -> dict:
    fallback = _paper_journal_fallback(paper)
    if fallback and not paper.get('journal'):
        paper['journal'] = fallback
    if fallback and not paper.get('category'):
        paper['category'] = fallback
    return paper


def _apply_resolved_pdf_fallback(paper: dict) -> dict:
    pdf_import = paper.get('_pdf_import')
    if not isinstance(pdf_import, dict) or pdf_import.get('resolver_status') != 'resolved':
        return paper
    for key in ('local_pdf_id', 'virtual_doi', 'virtual_issn', 'resolver_error'):
        paper.pop(key, None)
    paper['local_unresolved'] = False
    paper['issn_is_virtual'] = False
    paper['doi_is_virtual'] = False
    return paper


def _normalize_viewer_paper(paper: dict) -> dict:
    _apply_journal_fallback(paper)
    _apply_resolved_pdf_fallback(paper)
    return paper


def _paper_journal_labels(paper: dict) -> list[str]:
    labels = []
    metadata = _metadata_from_viewer_paper(paper)
    for key in JOURNAL_LABEL_KEYS:
        labels.extend(_label_values(paper.get(key)))
        labels.extend(_label_values(metadata.get(key)))
    fallback = _paper_journal_fallback(paper)
    if fallback:
        labels.append(fallback)
    return labels


def _normalize_selected_journal_ids(selected_journals, include_no_issn: bool = False):
    if selected_journals is None and not include_no_issn:
        return None, False
    if selected_journals is None:
        raw_values = []
    elif isinstance(selected_journals, str):
        raw_values = selected_journals.split(',')
    else:
        raw_values = list(selected_journals)

    selected_ids = set()
    include_no_issn = bool(include_no_issn)
    for raw in raw_values:
        token = str(raw or '').strip()
        if not token:
            continue
        if token == NO_ISSN_JOURNAL_ID:
            include_no_issn = True
            continue
        ids = _canonical_issn_ids(token)
        if ids:
            selected_ids.update(ids)
            continue
        compact = token.replace('-', '').upper()
        if len(compact) == 8:
            selected_ids.add(compact)
    return selected_ids, include_no_issn


def _filter_by_selected_journals(papers: list[dict], selected_ids: set[str],
                                 include_no_issn: bool) -> list[dict]:
    filtered = []
    for paper in papers:
        issn_ids = _paper_issn_ids(paper)
        if issn_ids and issn_ids.intersection(selected_ids):
            filtered.append(paper)
        elif not issn_ids and include_no_issn:
            filtered.append(paper)
    return filtered


def _append_viewer_filters(sql: str, params: list, *, status=None, source=None,
                           keyword=None, keyword_provenance='single', found_by=None) -> str:
    if status and str(status).upper() != 'ALL':
        sql += ' AND status = ?'
        params.append(status)
    if source:
        sql += ' AND source = ?'
        params.append(source)
    if keyword:
        sql = _append_keyword_filter(sql, params, keyword, keyword_provenance)
    if found_by:
        sql = _append_found_by_filter(sql, params, found_by)
    return sql


def query_papers_for_viewer(config: dict, *, config_path: str = 'config.yaml',
                            status=None, source=None, keyword=None,
                            keyword_provenance: str = 'single', found_by=None,
                            selected_journals=None, include_no_issn: bool = False,
                            limit: int = 10, offset: int = 0) -> dict:
    conn = get_conn(config)
    limit = max(0, int(limit or 10))
    offset = max(0, int(offset or 0))
    selected_ids, include_no_issn = _normalize_selected_journal_ids(
        selected_journals, include_no_issn)
    needs_journal_filter = selected_ids is not None or include_no_issn

    params: list = []
    sql = _append_viewer_filters(
        'SELECT * FROM papers WHERE 1=1', params, status=status, source=source,
        keyword=keyword, keyword_provenance=keyword_provenance, found_by=found_by)

    if needs_journal_filter:
        rows = conn.execute(sql + ' ORDER BY search_date DESC, paper_id', params).fetchall()
        papers = [_row_to_dict(row) for row in rows]
        papers = _filter_by_selected_journals(papers, selected_ids or set(), include_no_issn)
        total = len(papers)
        page = papers[offset:offset + limit]
    else:
        count_sql = sql.replace('SELECT *', 'SELECT COUNT(*) as cnt', 1)
        total = conn.execute(count_sql, params).fetchone()['cnt']
        rows = conn.execute(
            sql + ' ORDER BY search_date DESC, paper_id LIMIT ? OFFSET ?',
            params + [limit, offset]).fetchall()
        page = [_row_to_dict(row) for row in rows]

    items = []
    for paper in page:
        _normalize_viewer_paper(paper)
        paper.pop('metadata_json', None)
        items.append(paper)
    return {'items': items, 'total': total}


def get_journal_options_for_viewer(config: dict, *, config_path: str = 'config.yaml') -> list[dict]:
    conn = get_conn(config)
    cns_journals = load_cns_journal_set(config, config_path)
    rows = conn.execute('SELECT * FROM papers ORDER BY search_date DESC, paper_id').fetchall()
    buckets = {}
    no_issn_count = 0

    for row in rows:
        paper = _row_to_dict(row)
        issn_ids = _paper_issn_ids(paper)
        labels = _paper_journal_labels(paper)
        if not issn_ids:
            no_issn_count += 1
            continue
        default_selected = paper_matches_journal_set(paper, cns_journals)
        for issn_id in issn_ids:
            bucket = buckets.setdefault(issn_id, {
                'count': 0,
                'labels': Counter(),
                'default_selected': False,
            })
            bucket['count'] += 1
            bucket['default_selected'] = bucket['default_selected'] or default_selected
            for label in labels:
                bucket['labels'][label] += 1

    options = []
    for issn_id, bucket in buckets.items():
        display_issn = _display_issn(issn_id)
        label_counts = bucket['labels']
        journal = label_counts.most_common(1)[0][0] if label_counts else display_issn
        aliases = [label for label, _ in label_counts.most_common(8)]
        options.append({
            'id': issn_id,
            'issn': display_issn,
            'journal': journal,
            'label': f'{journal} ({display_issn})' if journal != display_issn else display_issn,
            'aliases': aliases,
            'count': bucket['count'],
            'default_selected': bucket['default_selected'],
            'is_preprint': False,
        })

    if no_issn_count:
        options.append({
            'id': NO_ISSN_JOURNAL_ID,
            'issn': None,
            'journal': 'Preprint / no ISSN',
            'label': 'Preprint / no ISSN',
            'aliases': [],
            'count': no_issn_count,
            'default_selected': False,
            'is_preprint': True,
        })

    return sorted(options, key=lambda item: (
        not item['default_selected'], item['is_preprint'], item['journal'].lower(), item['id']))


def _print_json_section(label: str, value) -> None:
    if not value:
        return
    print(f"\n{label}:")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _filter_explain_matches(matches, explain):
    if not explain or not isinstance(matches, dict):
        return matches
    needle = explain.lower()
    return {
        key: value for key, value in matches.items()
        if needle in key.lower() or needle in json.dumps(value, ensure_ascii=False).lower()
    }


def cmd_list(args, config):
    conn = get_conn(config)

    if args.all:
        args.limit = sys.maxsize

    def _apply_journal_filters(papers):
        if args.cns or args.cnsp:
            journal_set = (
                load_cns_journal_set(config, args.config)
                if args.cns else load_cnsp_journal_set(config, args.config)
            )
            papers = filter_papers_by_journals(papers, journal_set)
        if args.journals:
            custom_set = build_custom_journal_set(
                args.journals, load_cnsp_journal_set(config, args.config)
            )
            if custom_set['names'] or custom_set['abbrevs'] or custom_set['issns']:
                papers = filter_papers_by_journals(papers, custom_set)
        return papers

    needs_post_filter = bool(args.cnsp or args.cns or args.journals)

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
            json_sql = _append_keyword_filter(json_sql, json_params, args.keyword,
                                              args.keyword_provenance)
        if args.found_by:
            json_sql = _append_found_by_filter(json_sql, json_params, args.found_by)

        if needs_post_filter:
            json_sql += ' ORDER BY search_date DESC, paper_id'
            rows = conn.execute(json_sql, json_params).fetchall()
            papers = [_row_to_dict(r) for r in rows]
            papers = _apply_journal_filters(papers)
            page = papers[args.offset:args.offset + args.limit]
        else:
            json_sql += ' ORDER BY search_date DESC, paper_id LIMIT ? OFFSET ?'
            json_params.extend([args.limit, args.offset])
            rows = conn.execute(json_sql, json_params).fetchall()
            page = [_row_to_dict(r) for r in rows]

        results = []
        for paper in page:
            _normalize_viewer_paper(paper)
            paper.pop('metadata_json', None)
            results.append(paper)

        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        return 0

    sql = 'SELECT paper_id, doi, title, source, status, search_date, path_prefix, metadata_json, source_terms_text FROM papers WHERE 1=1'
    params: list = []

    if args.status:
        sql += ' AND status = ?'
        params.append(args.status)
    if args.source:
        sql += ' AND source = ?'
        params.append(args.source)
    if args.keyword:
        sql = _append_keyword_filter(sql, params, args.keyword,
                                     args.keyword_provenance)
    if args.found_by:
        sql = _append_found_by_filter(sql, params, args.found_by)

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

    cnsp_map = _load_cnsp_map()

    def _get_cnsp(journal_name):
        if not journal_name:
            return ''
        key = journal_name.lower().replace('&amp;', '&')
        return cnsp_map.get(key, '')

    if needs_post_filter:
        all_rows = conn.execute(sql + ' ORDER BY search_date DESC, paper_id', params).fetchall()
        papers = [_row_to_dict(r) for r in all_rows]
        papers = _apply_journal_filters(papers)
        total = len(papers)
        rows = papers[args.offset:args.offset + args.limit]
    else:
        count_sql = sql.replace(
            'SELECT paper_id, doi, title, source, status, search_date, path_prefix, metadata_json, source_terms_text',
            'SELECT COUNT(*) as cnt', 1
        )
        total = conn.execute(count_sql, params).fetchone()['cnt']

        sql += ' ORDER BY search_date DESC, paper_id LIMIT ? OFFSET ?'
        params.extend([args.limit, args.offset])
        rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]

    if args.id_only:
        print(' '.join(str(paper.get('paper_id') or '') for paper in rows))
        return 0

    if not rows:
        which = 'CNS ' if args.cns else ('CNSP ' if args.cnsp else '')
        print(f"No {which}papers found{f' matching filters' if (args.status or args.source or args.keyword or args.found_by or args.journals) else ''}.")
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

    for paper in rows:
        _normalize_viewer_paper(paper)
        doi = paper.get('doi') or ''
        pid = paper.get('paper_id') or ''
        title = paper.get('title') or ''
        source = paper.get('source') or ''
        status = paper.get('status') or ''
        pp = paper.get('path_prefix') or ''
        date_str = paper.get('search_date') or ''
        issn = paper.get('issn') or ''
        journal = paper.get('journal') or ''
        cnsp = _get_cnsp(journal)

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
        ('failure_phase', 'Failure Phase'),
        ('failure_category', 'Failure Category'),
        ('failure_subtype', 'Failure Subtype'),
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
    for json_field, label in (
        ('relevance', 'Relevance'),
        ('matched_tags', 'Tags'),
        ('failure_tags', 'Failure Tags'),
        ('failure_metadata', 'Failure Metadata'),
    ):
        value = paper.get(json_field)
        if value and isinstance(value, (dict, list)):
            print(f"\n{label}:")
            print(json.dumps(value, indent=2, ensure_ascii=False))

    for json_field, label in (
        ('mesh_headings', 'PubMed MeSH Headings'),
        ('pubmed_keywords', 'PubMed Keywords'),
        ('publication_types', 'Publication Types'),
        ('chemicals', 'Chemicals'),
    ):
        _print_json_section(label, paper.get(json_field))

    history = get_search_history(conn, paper['paper_id'])
    if history:
        print("\nSearch provenance:")
        for item in history:
            rank = f" rank {item['rank']}" if item.get('rank') else ''
            print(f"  {item.get('searched_at', '')}  {item.get('source', '')}{rank}")
            if item.get('keywords'):
                print(f"    keywords: {', '.join(str(k) for k in item['keywords'])}")
            if item.get('query'):
                print(f"    query: {item['query']}")
            if item.get('query_translation'):
                print(f"    query translation: {item['query_translation']}")
            if item.get('start_date') or item.get('end_date'):
                print(f"    date range: {item.get('start_date') or '?'} — {item.get('end_date') or '?'}")
            matches = _filter_explain_matches(item.get('matched_keywords'), getattr(args, 'explain', None))
            if matches:
                print("    matched keyword evidence:")
                print(json.dumps(matches, indent=6, ensure_ascii=False))

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
                        description='List papers from the database with optional filtering by status, source, keyword, and search provenance.')
    lp.add_argument('-s', '--status', default=None,
                    help='Filter by status: searched, downloaded, download_failed, interpreted, interpret_failed')
    lp.add_argument('--source', default=None,
                    help='Filter by source: arxiv, biorxiv, medrxiv, pubmed, scholar, nature, science, cell, plos')
    lp.add_argument('-k', '--keyword', default=None,
                    help='Filter by local metadata plus single-keyword search provenance by default')
    lp.add_argument('--keyword-provenance', default='single', choices=['single', 'any'],
                    help='For -k, use single-keyword provenance only (default) or any multi-keyword provenance match')
    lp.add_argument('--found-by', default=None,
                    help='Filter papers saved by a search whose query, keywords, or matched evidence contains this keyword')
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
    lp.add_argument('--id-only', action='store_true',
                    help='Output only space-separated paper IDs')
    lp.add_argument('--cnsp', action='store_true',
                    help='Only show papers whose journal is in CNSP (Nature/Science/Cell/PLOS)')
    lp.add_argument('--cns', action='store_true',
                    help='Only show papers whose journal is in CNS (Nature/Science/Cell, excluding PLOS)')
    lp.add_argument('--journals', default=None,
                    help='Filter by comma-separated journal names, abbreviations, or ISSNs')

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
    sp.add_argument('--explain', default=None,
                    help='When showing search provenance, focus matched evidence on this keyword')

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