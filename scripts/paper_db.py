#!/usr/bin/env python3
"""
Shared SQLite database module for the PaperInt skills pipeline.

Used by: bio-paper-search, bio-paper-downloader, bio-paper-interpreter

Schema: a single `papers` table tracking each paper through the pipeline:
  searched -> downloaded (or download_failed) -> interpreted
"""

import html
import json
import os
import re
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
    title_zh        TEXT,
    authors         TEXT,
    abstract        TEXT,
    abstract_zh     TEXT,
    doi             TEXT,
    pmid            TEXT,
    arxiv_id        TEXT,
    source          TEXT,
    source_url      TEXT,
    pdf_url         TEXT,
    dir_name        TEXT,
    path_prefix     TEXT,
    status          TEXT NOT NULL DEFAULT 'searched',
    search_date     TEXT,
    download_date   TEXT,
    interpret_date  TEXT,
    metadata_json   TEXT,
    source_terms_text TEXT,
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

CREATE TABLE IF NOT EXISTS search_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    query             TEXT,
    query_translation TEXT,
    keywords_json     TEXT,
    start_date        TEXT,
    end_date          TEXT,
    requested_limit   INTEGER,
    total_results     INTEGER,
    result_count      INTEGER,
    saved_count       INTEGER,
    args_json         TEXT,
    searched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_search_hits (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                INTEGER NOT NULL,
    paper_id              TEXT NOT NULL,
    source                TEXT,
    rank                  INTEGER,
    matched_keywords_json TEXT,
    hit_metadata_json     TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_id, paper_id),
    FOREIGN KEY(run_id) REFERENCES search_runs(id),
    FOREIGN KEY(paper_id) REFERENCES papers(paper_id)
);

CREATE INDEX IF NOT EXISTS idx_search_runs_source_date ON search_runs(source, searched_at);
CREATE INDEX IF NOT EXISTS idx_paper_search_hits_paper_id ON paper_search_hits(paper_id);
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

    # Auto-migration: add path_prefix column for existing databases
    try:
        _conn.execute("ALTER TABLE papers ADD COLUMN path_prefix TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass
    # Auto-migration: add translation columns for existing databases
    try:
        _conn.execute("ALTER TABLE papers ADD COLUMN title_zh TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        _conn.execute("ALTER TABLE papers ADD COLUMN abstract_zh TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass
    # Auto-migration: add source_terms_text column for existing databases
    try:
        _conn.execute("ALTER TABLE papers ADD COLUMN source_terms_text TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_source_terms_text ON papers(source_terms_text)")
    _conn.commit()
    _backfill_path_prefix(_conn)
    _backfill_source_terms_text(_conn)

    _db_path = path
    return _conn


def _now() -> str:
    return datetime.now().isoformat()


def _is_empty(value) -> bool:
    return value is None or value == '' or value == [] or value == {}


def _clean_markup_text(value):
    if not isinstance(value, str):
        return value
    text = html.unescape(value)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = ' '.join(text.split())
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    text = re.sub(r'\s+([,.;:?!])', r'\1', text)
    return text


def _clean_nested_markup(value):
    if isinstance(value, str):
        return _clean_markup_text(value)
    if isinstance(value, list):
        return [_clean_nested_markup(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_nested_markup(item) for key, item in value.items()}
    return value


def _clean_paper_text(paper: dict) -> dict:
    cleaned = dict(paper)
    if cleaned.get('title'):
        cleaned['title'] = _clean_markup_text(cleaned['title'])
    return cleaned


def _load_metadata(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        result = {k: v for k, v in value.items() if k != 'metadata_json'}
        nested = _load_metadata(value.get('metadata_json'))
        for key, nested_value in nested.items():
            if _is_empty(result.get(key)) and not _is_empty(nested_value):
                result[key] = nested_value
        return result
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return _load_metadata(data)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _merge_list_values(existing, new) -> list:
    merged = []
    seen = set()
    for item in _as_list(existing) + _as_list(new):
        if _is_empty(item):
            continue
        try:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        except TypeError:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _merge_metadata(existing_json, new_paper: dict) -> dict:
    existing = _load_metadata(existing_json)
    new_data = _load_metadata(new_paper)
    for key, value in new_data.items():
        if key == '_score' or _is_empty(value):
            continue
        if key == 'title':
            value = _clean_markup_text(value)
        if key in existing and not _is_empty(existing[key]):
            if isinstance(existing[key], list) or isinstance(value, list):
                existing[key] = _merge_list_values(existing[key], value)
            elif isinstance(existing[key], dict) and isinstance(value, dict):
                nested = dict(existing[key])
                for nk, nv in value.items():
                    if not _is_empty(nv) and _is_empty(nested.get(nk)):
                        nested[nk] = nv
                existing[key] = nested
            continue
        existing[key] = value
    return existing


def _add_source_term(terms: list[str], value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return
    value = ' '.join(value.split())
    if value:
        terms.append(value)


def _mesh_labels(metadata: dict) -> list[str]:
    labels = []
    for mesh in _as_list(metadata.get('mesh_headings') or metadata.get('mesh_terms')):
        if isinstance(mesh, dict):
            descriptor = mesh.get('descriptor') or mesh.get('name') or mesh.get('term')
            _add_source_term(labels, descriptor)
            _add_source_term(labels, mesh.get('ui'))
            for qualifier in _as_list(mesh.get('qualifiers')):
                if isinstance(qualifier, dict):
                    _add_source_term(labels, qualifier.get('name') or qualifier.get('term'))
                    _add_source_term(labels, qualifier.get('ui'))
                else:
                    _add_source_term(labels, qualifier)
        else:
            _add_source_term(labels, mesh)
    return labels


def _keyword_labels(metadata: dict) -> list[str]:
    labels = []
    for key in ('pubmed_keywords', 'keywords'):
        for keyword in _as_list(metadata.get(key)):
            if isinstance(keyword, dict):
                _add_source_term(labels, keyword.get('term') or keyword.get('name'))
                _add_source_term(labels, keyword.get('owner'))
            else:
                _add_source_term(labels, keyword)
    return labels


def _publication_type_labels(metadata: dict) -> list[str]:
    labels = []
    for pub_type in _as_list(metadata.get('publication_types')):
        if isinstance(pub_type, dict):
            _add_source_term(labels, pub_type.get('term') or pub_type.get('name'))
            _add_source_term(labels, pub_type.get('ui'))
        else:
            _add_source_term(labels, pub_type)
    return labels


def _chemical_labels(metadata: dict) -> list[str]:
    labels = []
    for chemical in _as_list(metadata.get('chemicals')):
        if isinstance(chemical, dict):
            _add_source_term(labels, chemical.get('name') or chemical.get('term'))
            _add_source_term(labels, chemical.get('registry_number'))
            _add_source_term(labels, chemical.get('ui'))
        else:
            _add_source_term(labels, chemical)
    return labels


def _source_terms_text(paper_or_metadata: dict) -> str:
    metadata = _load_metadata(paper_or_metadata)
    terms = []
    for key in ('journal', 'issn', 'category', 'abbrev'):
        _add_source_term(terms, metadata.get(key))
    terms.extend(_mesh_labels(metadata))
    terms.extend(_keyword_labels(metadata))
    terms.extend(_publication_type_labels(metadata))
    terms.extend(_chemical_labels(metadata))

    unique = []
    seen = set()
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return ' | '.join(unique)


def _match_search_keywords(paper_or_metadata: dict, keywords: list[str]) -> dict:
    metadata = _load_metadata(paper_or_metadata)
    fields = {
        'title': [metadata.get('title', '')],
        'abstract': [metadata.get('abstract', '')],
        'journal': [metadata.get('journal', '')],
        'source': [metadata.get('source', '')],
        'mesh_headings': _mesh_labels(metadata),
        'pubmed_keywords': _keyword_labels(metadata),
        'publication_types': _publication_type_labels(metadata),
        'chemicals': _chemical_labels(metadata),
    }
    source_terms = metadata.get('source_terms_text') or _source_terms_text(metadata)
    if source_terms:
        fields['source_terms_text'] = [source_terms]

    matches = {}
    for keyword in keywords or []:
        keyword = str(keyword).strip()
        if not keyword:
            continue
        needle = keyword.lower()
        field_matches = {}
        for field, values in fields.items():
            hits = []
            for value in values:
                value = str(value or '')
                if needle in value.lower():
                    hits.append(value[:300])
            if hits:
                field_matches[field] = hits[:5]
        if field_matches:
            matches[keyword] = field_matches
    return matches


def _paper_source_url(paper: dict) -> str:
    return paper.get('source_url') or paper.get('abs_url') or ''


def _paper_metadata_json(paper: dict) -> str:
    return json.dumps(_merge_metadata(None, paper), ensure_ascii=False)


def _merge_existing_paper(conn: sqlite3.Connection, existing: sqlite3.Row,
                          paper: dict, now: str) -> str:
    updates = {}
    field_map = {
        'title': paper.get('title', ''),
        'authors': paper.get('authors', ''),
        'abstract': paper.get('abstract', ''),
        'doi': _sanitize_doi(paper.get('doi', '') or ''),
        'pmid': paper.get('pmid', ''),
        'arxiv_id': paper.get('arxiv_id', ''),
        'source': paper.get('source', ''),
        'source_url': _paper_source_url(paper),
        'pdf_url': paper.get('pdf_url', ''),
    }
    for field, new_value in field_map.items():
        if new_value and not (existing[field] or '').strip():
            updates[field] = new_value

    merged_metadata = _merge_metadata(existing['metadata_json'], paper)
    updates['metadata_json'] = json.dumps(merged_metadata, ensure_ascii=False)
    updates['source_terms_text'] = _source_terms_text(merged_metadata)
    updates['updated_at'] = now

    set_clause = ', '.join(f"{field} = ?" for field in updates)
    values = list(updates.values()) + [existing['paper_id']]
    conn.execute(f"UPDATE papers SET {set_clause} WHERE paper_id = ?", values)
    return existing['paper_id']


def _insert_or_merge_paper(conn: sqlite3.Connection, paper: dict, now: str,
                           by_doi: bool = False) -> tuple[str, bool]:
    doi = _sanitize_doi(paper.get('doi', '') or '')
    if by_doi and doi:
        existing = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        if existing:
            return _merge_existing_paper(conn, existing, paper, now), False

    paper_id = paper.get('paper_id') or doi
    if not paper_id:
        return '', False

    metadata = _merge_metadata(None, paper)
    source_terms = _source_terms_text(metadata)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO papers
               (paper_id, title, authors, abstract, doi, pmid, arxiv_id,
                source, source_url, pdf_url, status, search_date, metadata_json,
                source_terms_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'searched', ?, ?, ?)""",
            (
                paper_id,
                paper.get('title', ''),
                paper.get('authors', ''),
                paper.get('abstract', ''),
                doi,
                paper.get('pmid', ''),
                paper.get('arxiv_id', ''),
                paper.get('source', ''),
                _paper_source_url(paper),
                paper.get('pdf_url', ''),
                now,
                json.dumps(metadata, ensure_ascii=False),
                source_terms,
            ),
        )
    except Exception:
        raise

    inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
    if inserted:
        return paper_id, True

    existing = conn.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    if existing:
        return _merge_existing_paper(conn, existing, paper, now), False
    return paper_id, False


def _json_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _create_search_run(conn: sqlite3.Connection, search_context: dict | None) -> int | None:
    if not search_context:
        return None
    conn.execute(
        """INSERT INTO search_runs
           (source, query, query_translation, keywords_json, start_date, end_date,
            requested_limit, total_results, result_count, saved_count, args_json,
            searched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            search_context.get('source') or '',
            search_context.get('query'),
            search_context.get('query_translation'),
            _json_or_none(search_context.get('keywords')),
            search_context.get('start_date'),
            search_context.get('end_date'),
            search_context.get('requested_limit'),
            search_context.get('total_results'),
            search_context.get('result_count'),
            search_context.get('saved_count'),
            _json_or_none(search_context.get('args')),
            search_context.get('searched_at') or _now(),
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _update_search_run_saved_count(conn: sqlite3.Connection, run_id: int | None,
                                   saved_count: int) -> None:
    if run_id is None:
        return
    conn.execute("UPDATE search_runs SET saved_count = ? WHERE id = ?", (saved_count, run_id))


def _record_search_hit(conn: sqlite3.Connection, run_id: int | None, paper_id: str,
                       paper: dict, rank: int, keywords: list[str]) -> None:
    if run_id is None or not paper_id:
        return
    metadata = _merge_metadata(None, paper)
    if not metadata.get('source_terms_text'):
        metadata['source_terms_text'] = _source_terms_text(metadata)
    matched = _match_search_keywords(metadata, keywords)
    hit_metadata = {
        key: paper.get(key)
        for key in ('title', 'doi', 'pmid', 'arxiv_id', 'source', 'date', 'abs_url', 'pdf_url')
        if paper.get(key)
    }
    conn.execute(
        """INSERT OR IGNORE INTO paper_search_hits
           (run_id, paper_id, source, rank, matched_keywords_json, hit_metadata_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            paper_id,
            paper.get('source', ''),
            rank,
            json.dumps(matched, ensure_ascii=False),
            json.dumps(hit_metadata, ensure_ascii=False),
        ),
    )


# ---------------------------------------------------------------------------
# Search skill operations
# ---------------------------------------------------------------------------

def insert_search_results(conn: sqlite3.Connection, papers: list[dict],
                          search_context: dict | None = None) -> int:
    """Insert search results with status='searched'. Returns count inserted."""
    count = 0
    now = _now()
    run_id = _create_search_run(conn, search_context)
    keywords = (search_context or {}).get('keywords') or []
    saved_count = 0
    for rank, p in enumerate(papers, 1):
        p = _clean_paper_text(p)
        try:
            paper_id, inserted = _insert_or_merge_paper(conn, p, now)
            if inserted:
                count += 1
            if paper_id:
                _record_search_hit(conn, run_id, paper_id, p, rank, keywords)
                saved_count += 1
        except Exception as e:
            print(f"  DB insert error for {p.get('paper_id', '?')}: {e}", file=__import__('sys').stderr)
    _update_search_run_saved_count(conn, run_id, saved_count)
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Upsert by DOI (for CNSP and other DOI-based sources)
# ---------------------------------------------------------------------------

def upsert_search_results(conn: sqlite3.Connection, papers: list[dict],
                          search_context: dict | None = None) -> int:
    """Insert or update by DOI. Fills blank fields in existing records. Returns count affected."""
    count = 0
    now = _now()
    run_id = _create_search_run(conn, search_context)
    keywords = (search_context or {}).get('keywords') or []
    saved_count = 0
    for rank, p in enumerate(papers, 1):
        p = _clean_paper_text(p)
        try:
            doi = _sanitize_doi(p.get('doi', '') or '')
            paper_id, inserted = _insert_or_merge_paper(conn, p, now, by_doi=bool(doi))
            if paper_id:
                count += 1
                _record_search_hit(conn, run_id, paper_id, p, rank, keywords)
                saved_count += 1
        except Exception as e:
            ident = p.get('doi') or p.get('paper_id') or '?'
            print(f"  DB insert error for {ident}: {e}", file=__import__('sys').stderr)
    _update_search_run_saved_count(conn, run_id, saved_count)
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
    pp = f"{dir_name}/{_sanitize_path(paper_id)}"
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
            "UPDATE papers SET status='downloaded', dir_name=?, path_prefix=?, download_date=?, metadata_json=?, error_message=NULL, updated_at=? WHERE paper_id=?",
            (dir_name, pp, now, json.dumps(meta, ensure_ascii=False), now, paper_id),
        )
    else:
        conn.execute(
            "UPDATE papers SET status='downloaded', dir_name=?, path_prefix=?, download_date=?, error_message=NULL, updated_at=? WHERE paper_id=?",
            (dir_name, pp, now, now, paper_id),
        )
    conn.commit()


def mark_download_failed(conn: sqlite3.Connection, paper_id: str, error: str,
                         dir_name: str = None) -> None:
    """Mark a paper download as failed."""
    now = _now()
    if dir_name:
        pp = f"{dir_name}/{_sanitize_path(paper_id)}"
        conn.execute(
            "UPDATE papers SET status='download_failed', error_message=?, dir_name=?, path_prefix=?, updated_at=? WHERE paper_id=?",
            (error, dir_name, pp, now, paper_id),
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


def update_translations(conn: sqlite3.Connection, paper_id: str,
                        title_zh: str = None, abstract_zh: str = None) -> None:
    """Store Chinese title/abstract translations for a paper."""
    updates = []
    params = []
    for column, value in (('title_zh', title_zh), ('abstract_zh', abstract_zh)):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        updates.append(f"{column}=?")
        params.append(text)
    if not updates:
        return
    now = _now()
    updates.append("updated_at=?")
    params.extend([now, paper_id])
    conn.execute(
        f"UPDATE papers SET {', '.join(updates)} WHERE paper_id=?",
        params,
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


def get_search_history(conn: sqlite3.Connection, paper_id: str, limit: int = 10) -> list[dict]:
    """Return recent search runs that produced this paper."""
    rows = conn.execute(
        """SELECT h.rank, h.source as hit_source, h.matched_keywords_json,
                  h.hit_metadata_json, h.created_at,
                  r.source, r.query, r.query_translation, r.keywords_json,
                  r.start_date, r.end_date, r.requested_limit,
                  r.total_results, r.result_count, r.saved_count, r.searched_at
           FROM paper_search_hits h
           JOIN search_runs r ON r.id = h.run_id
           WHERE h.paper_id = ?
           ORDER BY r.searched_at DESC, h.id DESC
           LIMIT ?""",
        (paper_id, int(limit)),
    ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        for key in ('matched_keywords_json', 'hit_metadata_json', 'keywords_json'):
            if item.get(key):
                try:
                    parsed = json.loads(item[key])
                    if key in ('matched_keywords_json', 'hit_metadata_json'):
                        parsed = _clean_nested_markup(parsed)
                    item[key[:-5] if key.endswith('_json') else key] = parsed
                except json.JSONDecodeError:
                    pass
        history.append(item)
    return history


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


def _sanitize_doi(doi: str) -> str:
    """Validate and clean a DOI for writing to the database.

    Returns the DOI if it starts with '10.', empty string otherwise.
    Logs a warning when rejecting an invalid DOI.
    """
    if not doi:
        return ''
    doi = doi.strip()
    if doi.startswith('10.'):
        return doi
    print(f"  DB: rejecting invalid DOI (no 10. prefix): {doi[:80]}", file=__import__('sys').stderr)
    return ''


def _sanitize_path(name: str) -> str:
    """Replace filesystem-unsafe characters with underscores, truncate to 200."""
    import re
    return re.sub(r"[/\\:*?\"<>|']", '_', str(name))[:200]


def _backfill_path_prefix(conn: sqlite3.Connection) -> None:
    """Backfill path_prefix for rows that have dir_name but no path_prefix."""
    rows = conn.execute(
        "SELECT paper_id, dir_name FROM papers WHERE dir_name IS NOT NULL AND path_prefix IS NULL"
    ).fetchall()
    for row in rows:
        pp = f"{row['dir_name']}/{_sanitize_path(row['paper_id'])}"
        conn.execute("UPDATE papers SET path_prefix = ? WHERE paper_id = ?",
                     (pp, row['paper_id']))
    if rows:
        conn.commit()


def _backfill_source_terms_text(conn: sqlite3.Connection) -> None:
    """Backfill source_terms_text from metadata_json for rows that do not have it."""
    rows = conn.execute(
        "SELECT paper_id, metadata_json FROM papers WHERE source_terms_text IS NULL OR source_terms_text = ''"
    ).fetchall()
    for row in rows:
        source_terms = _source_terms_text(_load_metadata(row['metadata_json']))
        if source_terms:
            conn.execute("UPDATE papers SET source_terms_text = ? WHERE paper_id = ?",
                         (source_terms, row['paper_id']))
    if rows:
        conn.commit()


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
    if d.get('title'):
        d['title'] = _clean_markup_text(d['title'])
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
# Journal filters
# ---------------------------------------------------------------------------

def normalize_journal_text(value: str) -> str:
    if not isinstance(value, str):
        return ''
    text = html.unescape(value).replace(' (partner)', '')
    return ' '.join(text.lower().split())


def _journal_text_variants(value: str) -> set[str]:
    normalized = normalize_journal_text(value)
    if not normalized:
        return set()
    variants = {normalized}
    if '&' in normalized:
        variants.add(normalized.replace('&', 'and'))
    if ' and ' in normalized:
        variants.add(normalized.replace(' and ', ' & '))
    return variants


def normalize_issn_values(value: str) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        variants = set()
        for item in value:
            variants.update(normalize_issn_values(item))
        return variants
    text = str(value).upper()
    matches = re.findall(r'[0-9X]{4}-?[0-9X]{4}', text)
    variants = set()
    for issn in matches:
        compact = issn.replace('-', '')
        variants.add(issn)
        variants.add(compact)
        if len(compact) == 8:
            variants.add(f'{compact[:4]}-{compact[4:]}')
    return variants


def _add_journal_text(target: set, value) -> None:
    for variant in _journal_text_variants(value):
        target.add(variant)


def _merge_journal_data(target: dict, source: dict) -> None:
    for key in ('names', 'abbrevs', 'issns'):
        target.setdefault(key, set()).update(source.get(key, set()))


def build_custom_journal_set(journals: str, known_journal_data: dict | None = None) -> dict:
    names: set = set()
    abbrevs: set = set()
    issns: set = set()
    result = {'names': names, 'abbrevs': abbrevs, 'issns': issns}
    aliases = (known_journal_data or {}).get('aliases', {})
    for token in str(journals or '').split(','):
        token = token.strip()
        if not token:
            continue
        token_names = _journal_text_variants(token)
        token_issns = normalize_issn_values(token)
        names.update(token_names)
        abbrevs.update(token_names)
        issns.update(token_issns)
        for alias in token_names | token_issns:
            if alias in aliases:
                _merge_journal_data(result, aliases[alias])
    return result


def _metadata_from_paper(paper: dict) -> dict:
    meta = paper.get('metadata_json')
    if not meta:
        return {}
    if isinstance(meta, dict):
        return meta
    try:
        return json.loads(meta)
    except (json.JSONDecodeError, TypeError):
        return {}


def paper_matches_journal_set(paper: dict, journal_data: dict) -> bool:
    names = journal_data.get('names', set())
    abbrevs = journal_data.get('abbrevs', set())
    issns = journal_data.get('issns', set())
    metadata = _metadata_from_paper(paper)

    for key in ('issn', 'issn_print', 'issn_electronic'):
        for variant in normalize_issn_values(paper.get(key) or metadata.get(key)):
            if variant in issns:
                return True

    for key in ('journal', 'container-title', 'category'):
        for variant in _journal_text_variants(paper.get(key) or metadata.get(key)):
            if variant in names or variant in abbrevs:
                return True

    for variant in _journal_text_variants(paper.get('abbrev') or metadata.get('abbrev')):
        if variant in abbrevs or variant in names:
            return True
    return False


def filter_papers_by_journals(papers: list, journal_data: dict) -> list:
    return [p for p in papers if paper_matches_journal_set(p, journal_data)]


def _load_journal_data(config: dict, config_path: str, keys: list) -> dict:
    """Load journal names, abbreviations, and ISSNs from journals_config/*.json.

    Returns a dict with 'names' (lowercase full names), 'abbrevs' (lowercase),
    and 'issns' (both hyphenated and un-hyphenated forms).
    """
    cnsp_cfg = config.get('cnsp', {})
    names: set = set()
    abbrevs: set = set()
    issns: set = set()
    aliases = {}
    config_dir = os.path.dirname(os.path.abspath(config_path))
    for key in keys:
        rel = cnsp_cfg.get(key, '')
        if not rel:
            continue
        jpath = rel if os.path.isabs(rel) else os.path.join(config_dir, rel)
        if not os.path.exists(jpath):
            continue
        for j in json.load(open(jpath)):
            entry = {'names': set(), 'abbrevs': set(), 'issns': set()}
            _add_journal_text(entry['names'], j.get('name', ''))
            _add_journal_text(entry['abbrevs'], j.get('abbrev', ''))
            for issn_key in ('issn_print', 'issn_electronic'):
                entry['issns'].update(normalize_issn_values(j.get(issn_key, '')))
            _merge_journal_data({'names': names, 'abbrevs': abbrevs, 'issns': issns}, entry)
            for alias in entry['names'] | entry['abbrevs'] | entry['issns']:
                aliases.setdefault(alias, {'names': set(), 'abbrevs': set(), 'issns': set()})
                _merge_journal_data(aliases[alias], entry)
    return {'names': names, 'abbrevs': abbrevs, 'issns': issns, 'aliases': aliases}


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
    """Filter papers to only those matching CNSP journal identifiers."""
    return filter_papers_by_journals(papers, cnsp_data)
