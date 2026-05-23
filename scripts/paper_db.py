#!/usr/bin/env python3
"""
Shared SQLite database module for the PaperInt skills pipeline.

Used by: bio-paper-search, bio-paper-downloader, bio-paper-interpreter

Schema: a single `papers` table tracking each paper through the pipeline:
  searched -> downloaded (or download_failed) -> interpreted (or skipped)
"""

import json
import os
import sqlite3
from datetime import datetime


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        TEXT NOT NULL UNIQUE,
    title           TEXT,
    authors         TEXT,
    abstract        TEXT,
    doi             TEXT,
    pmid            TEXT,
    arxiv_id        TEXT,
    source          TEXT,
    source_url      TEXT,
    pdf_url         TEXT,
    dir_name        TEXT,
    status          TEXT NOT NULL DEFAULT 'searched',
    search_date     TEXT,
    download_date   TEXT,
    interpret_date  TEXT,
    metadata_json   TEXT,
    oa_has_pdf      INTEGER DEFAULT 0,
    error_message   TEXT,
    relevance       TEXT,
    matched_tags    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_paper_id ON papers(paper_id);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_search_date ON papers(search_date);
"""

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_conn = None
_db_path = None


def get_db_path(config: dict) -> str:
    """Return the database path from config, defaulting to 'data/papers.db'."""
    return config.get('db', {}).get('path', 'data/papers.db')


def get_conn(config: dict) -> sqlite3.Connection:
    """Open (or return cached) SQLite connection, ensuring schema exists."""
    global _conn, _db_path
    path = get_db_path(config)
    if _conn is not None and _db_path == path:
        return _conn
    if _conn is not None:
        _conn.close()
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    _conn = sqlite3.connect(path)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    _conn.commit()
    _db_path = path
    return _conn


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Search skill operations
# ---------------------------------------------------------------------------

def insert_search_results(conn: sqlite3.Connection, papers: list[dict]) -> int:
    """Insert search results with status='searched'. Skips duplicates. Returns count inserted."""
    count = 0
    now = _now()
    for p in papers:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO papers
                   (paper_id, title, authors, abstract, doi, pmid, arxiv_id,
                    source, source_url, pdf_url, status, search_date, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'searched', ?, ?)""",
                (
                    p.get('paper_id', ''),
                    p.get('title', ''),
                    p.get('authors', ''),
                    p.get('abstract', ''),
                    p.get('doi', ''),
                    p.get('pmid', ''),
                    p.get('arxiv_id', ''),
                    p.get('source', ''),
                    p.get('abs_url', ''),
                    p.get('pdf_url', ''),
                    now,
                    json.dumps(p, ensure_ascii=False),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                count += 1
        except Exception as e:
            print(f"  DB insert error for {p.get('paper_id', '?')}: {e}", file=__import__('sys').stderr)
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Upsert by DOI (for CNSP and other DOI-based sources)
# ---------------------------------------------------------------------------

def upsert_search_results(conn: sqlite3.Connection, papers: list[dict]) -> int:
    """Insert or update by DOI. Fills blank fields in existing records. Returns count affected."""
    count = 0
    now = _now()
    for p in papers:
        doi = (p.get('doi', '') or '').strip()
        if not doi:
            # Fall back to INSERT OR IGNORE by paper_id
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO papers
                       (paper_id, title, authors, abstract, doi, pmid, arxiv_id,
                        source, source_url, pdf_url, status, search_date, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'searched', ?, ?)""",
                    (
                        p.get('paper_id', ''),
                        p.get('title', ''),
                        p.get('authors', ''),
                        p.get('abstract', ''),
                        doi,
                        p.get('pmid', ''),
                        p.get('arxiv_id', ''),
                        p.get('source', ''),
                        p.get('abs_url', ''),
                        p.get('pdf_url', ''),
                        now,
                        json.dumps(p, ensure_ascii=False),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    count += 1
            except Exception as e:
                print(f"  DB insert error for {p.get('paper_id', '?')}: {e}", file=__import__('sys').stderr)
            continue

        existing = conn.execute(
            "SELECT id, paper_id, title, authors, abstract, pdf_url, source_url FROM papers WHERE doi = ?",
            (doi,),
        ).fetchone()

        if existing:
            updates = {}
            for field in ['title', 'authors', 'abstract', 'pdf_url']:
                existing_val = (existing[field] or '').strip()
                new_val = (p.get(field, '') or '').strip()
                if new_val and not existing_val:
                    updates[field] = new_val
            if p.get('abs_url', '').strip():
                existing_source_url = (existing['source_url'] or '').strip()
                if not existing_source_url:
                    updates['source_url'] = p['abs_url']
            if updates:
                updates['updated_at'] = now
                set_clause = ', '.join(f"{k} = ?" for k in updates)
                values = list(updates.values()) + [doi]
                conn.execute(
                    f"UPDATE papers SET {set_clause} WHERE doi = ?", values,
                )
            count += 1
        else:
            try:
                conn.execute(
                    """INSERT INTO papers
                       (paper_id, title, authors, abstract, doi, pmid, arxiv_id,
                        source, source_url, pdf_url, status, search_date, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'searched', ?, ?)""",
                    (
                        doi,
                        p.get('title', ''),
                        p.get('authors', ''),
                        p.get('abstract', ''),
                        doi,
                        p.get('pmid', ''),
                        p.get('arxiv_id', ''),
                        p.get('source', ''),
                        p.get('abs_url', ''),
                        p.get('pdf_url', ''),
                        now,
                        json.dumps(p, ensure_ascii=False),
                    ),
                )
                count += 1
            except Exception as e:
                print(f"  DB insert error for {doi}: {e}", file=__import__('sys').stderr)
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Downloader skill operations
# ---------------------------------------------------------------------------

def get_papers_by_status(conn: sqlite3.Connection, status: str,
                         limit: int = None) -> list[dict]:
    """Return papers with the given status, ordered by search_date desc, paper_id."""
    sql = "SELECT * FROM papers WHERE status = ? ORDER BY search_date DESC, paper_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (status,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def is_downloaded(conn: sqlite3.Connection, paper_id: str) -> bool:
    """Return True if the paper has status 'downloaded'."""
    row = conn.execute(
        "SELECT 1 FROM papers WHERE paper_id = ? AND status = 'downloaded'",
        (paper_id,),
    ).fetchone()
    return row is not None


def mark_downloaded(conn: sqlite3.Connection, paper_id: str, dir_name: str,
                    metadata_updates: dict = None) -> None:
    """Mark a paper as downloaded: set status, dir_name, download_date, merge metadata."""
    now = _now()
    if metadata_updates:
        existing = conn.execute(
            "SELECT metadata_json FROM papers WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        if existing and existing[0]:
            meta = json.loads(existing[0])
            meta.update(metadata_updates)
        else:
            meta = metadata_updates
        conn.execute(
            "UPDATE papers SET status='downloaded', dir_name=?, download_date=?, metadata_json=?, error_message=NULL, updated_at=? WHERE paper_id=?",
            (dir_name, now, json.dumps(meta, ensure_ascii=False), now, paper_id),
        )
    else:
        conn.execute(
            "UPDATE papers SET status='downloaded', dir_name=?, download_date=?, error_message=NULL, updated_at=? WHERE paper_id=?",
            (dir_name, now, now, paper_id),
        )
    conn.commit()


def mark_download_failed(conn: sqlite3.Connection, paper_id: str, error: str,
                         dir_name: str = None) -> None:
    """Mark a paper download as failed."""
    now = _now()
    if dir_name:
        conn.execute(
            "UPDATE papers SET status='download_failed', error_message=?, dir_name=?, updated_at=? WHERE paper_id=?",
            (error, dir_name, now, paper_id),
        )
    else:
        conn.execute(
            "UPDATE papers SET status='download_failed', error_message=?, updated_at=? WHERE paper_id=?",
            (error, now, paper_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Interpreter skill operations
# ---------------------------------------------------------------------------

def mark_interpreted(conn: sqlite3.Connection, paper_id: str) -> None:
    """Mark a paper as interpreted."""
    now = _now()
    conn.execute(
        "UPDATE papers SET status='interpreted', interpret_date=?, updated_at=? WHERE paper_id=?",
        (now, now, paper_id),
    )
    conn.commit()


def mark_interpret_failed(conn: sqlite3.Connection, paper_id: str, error: str) -> None:
    """Mark a paper interpretation as failed."""
    now = _now()
    conn.execute(
        "UPDATE papers SET status='interpret_failed', error_message=?, updated_at=? WHERE paper_id=?",
        (error, now, paper_id),
    )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, paper_id: str) -> None:
    """Mark a paper as skipped (not bioinformatics-relevant)."""
    now = _now()
    conn.execute(
        "UPDATE papers SET status='skipped', updated_at=? WHERE paper_id=?",
        (now, paper_id),
    )
    conn.commit()


def update_relevance(conn: sqlite3.Connection, paper_id: str, data: dict) -> None:
    """Store relevance filter results as JSON."""
    now = _now()
    conn.execute(
        "UPDATE papers SET relevance=?, updated_at=? WHERE paper_id=?",
        (json.dumps(data, ensure_ascii=False), now, paper_id),
    )
    conn.commit()


def update_tags(conn: sqlite3.Connection, paper_id: str, data: dict) -> None:
    """Store matched tag results as JSON."""
    now = _now()
    conn.execute(
        "UPDATE papers SET matched_tags=?, updated_at=? WHERE paper_id=?",
        (json.dumps(data, ensure_ascii=False), now, paper_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# General queries
# ---------------------------------------------------------------------------

def get_paper_dir(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """Return dir_name for a paper, or None."""
    row = conn.execute(
        "SELECT dir_name FROM papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    return row[0] if row else None


def get_paper(conn: sqlite3.Connection, paper_id: str) -> dict | None:
    """Return full paper record with metadata_json parsed."""
    row = conn.execute(
        "SELECT * FROM papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return counts by status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM papers GROUP BY status"
    ).fetchall()
    return {r['status']: r['cnt'] for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str) -> str | None:
    """Extract a valid DOI (10.xxx/yyy pattern) from a possibly-malformed string.

    PubMed sometimes returns elocationid values like
    ``pii: S1234. 10.1016/j.cell.2024.01.001`` where the real DOI is embedded.
    Returns the extracted DOI or None if no DOI-like pattern is found.
    """
    if not doi:
        return None
    doi = doi.strip()
    if doi.startswith('10.'):
        return doi
    import re
    m = re.search(r'10\.\d{4,}/[^\s]+', doi)
    return m.group(0) if m else doi


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, parsing metadata_json."""
    d = dict(row)
    if d.get('metadata_json'):
        try:
            meta = json.loads(d['metadata_json'])
            # Only apply meta values that are non-null and don't clobber existing column values
            meta = {k: v for k, v in meta.items() if v is not None and not d.get(k)}
            d.update(meta)
        except json.JSONDecodeError:
            pass
    # Normalize DOI — fix malformed PII-prefixed values from PubMed search
    if d.get('doi'):
        d['doi'] = _normalize_doi(d['doi'])
    if d.get('relevance'):
        try:
            d['relevance'] = json.loads(d['relevance'])
        except json.JSONDecodeError:
            pass
    if d.get('matched_tags'):
        try:
            d['matched_tags'] = json.loads(d['matched_tags'])
        except json.JSONDecodeError:
            pass
    return d


# ---------------------------------------------------------------------------
# CNSP journal filter
# ---------------------------------------------------------------------------

def _load_journal_data(config: dict, config_path: str, keys: list) -> dict:
    """Load journal names, abbreviations, and ISSNs from journals_config/*.json.

    Returns a dict with 'names' (lowercase full names), 'abbrevs' (lowercase),
    and 'issns' (both hyphenated and un-hyphenated forms).
    """
    cnsp_cfg = config.get('cnsp', {})
    names: set = set()
    abbrevs: set = set()
    issns: set = set()
    config_dir = os.path.dirname(os.path.abspath(config_path))
    for key in keys:
        rel = cnsp_cfg.get(key, '')
        if not rel:
            continue
        jpath = rel if os.path.isabs(rel) else os.path.join(config_dir, rel)
        if not os.path.exists(jpath):
            continue
        for j in json.load(open(jpath)):
            name = (j.get('name', '') or '').replace(' (partner)', '')
            if name:
                names.add(name.lower())
            abbrev = (j.get('abbrev', '') or '').strip()
            if abbrev:
                abbrevs.add(abbrev.lower())
            for issn_key in ('issn_print', 'issn_electronic'):
                issn = (j.get(issn_key, '') or '').strip()
                if issn:
                    issns.add(issn)
                    issns.add(issn.replace('-', ''))
    return {'names': names, 'abbrevs': abbrevs, 'issns': issns}


def load_cnsp_journal_set(config: dict, config_path: str = 'config.yaml') -> dict:
    """Load all CNSP journal identifiers (names, abbrevs, ISSNs).

    Returns a dict with 'names', 'abbrevs', 'issns' sets.
    """
    return _load_journal_data(config, config_path,
                              ['nature_journals', 'science_journals',
                               'cell_journals', 'plos_journals'])


def load_cns_journal_set(config: dict, config_path: str = 'config.yaml') -> dict:
    """Load CNS journal identifiers (names, abbrevs, ISSNs), excluding PLOS.

    Returns a dict with 'names', 'abbrevs', 'issns' sets.
    """
    return _load_journal_data(config, config_path,
                              ['nature_journals', 'science_journals',
                               'cell_journals'])


def filter_cnsp_papers(papers: list, cnsp_data: dict) -> list:
    """Filter papers to only those matching CNSP journal identifiers.

    Matches by ISSN first (unambiguous), then full journal name, then
    abbreviation (to handle PubMed NLM abbreviations like 'Nat Commun').
    """
    names = cnsp_data.get('names', set())
    abbrevs = cnsp_data.get('abbrevs', set())
    issns = cnsp_data.get('issns', set())
    result = []
    for p in papers:
        meta = p.get('metadata_json')
        if not meta:
            continue
        try:
            data = json.loads(meta) if isinstance(meta, str) else meta
        except (json.JSONDecodeError, TypeError):
            continue
        # ISSN match (check both hyphenated and plain forms)
        issn = (data.get('issn', '') or '').strip()
        if issn and (issn in issns or issn.replace('-', '') in issns):
            result.append(p)
            continue
        # Journal name match
        journal = (data.get('journal', '') or '').lower()
        if journal and journal in names:
            result.append(p)
            continue
        # Abbreviation match (NLM style, e.g. 'Nat Commun')
        if journal and journal in abbrevs:
            result.append(p)
    return result