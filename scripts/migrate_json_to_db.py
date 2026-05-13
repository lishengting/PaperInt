#!/usr/bin/env python3
"""
Migrate legacy data/downloaded.json + per-paper metadata.json into papers.db.

Infers status from file existence:
  - PDF on disk -> 'downloaded' (or 'interpreted' if interpret output exists)
  - interpret.md or interpret.json on disk -> 'interpreted'
  - skipped.json on disk -> 'skipped'
  - metadata.json exists but no PDF -> 'download_failed'
  - in downloaded.json but no metadata -> 'searched'
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from paper_db import get_conn, get_db_path


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.yaml')
    try:
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except ImportError:
        # Minimal YAML parser fallback
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        result = {}
        for line in content.split('\n'):
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            if ':' in s and not s.startswith('-'):
                k, _, v = s.partition(':')
                v = v.strip().strip('"').strip("'")
                result[k.strip()] = v
        return result


def migrate(config, downloaded_json_path, data_dir):
    """Main migration logic."""
    conn = get_conn(config)

    if not os.path.exists(downloaded_json_path):
        print(f"State file not found: {downloaded_json_path}")
        return 0

    with open(downloaded_json_path, 'r', encoding='utf-8') as f:
        state = json.load(f)

    paper_dirs = state.get('paper_dirs', {})
    downloaded = set(state.get('downloaded', []))
    last_updated = state.get('last_updated', datetime.now().isoformat())

    # Also scan the data directory for paper dirs not in state
    data_abs = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', data_dir)
    if os.path.isdir(data_abs):
        for entry in os.listdir(data_abs):
            entry_path = os.path.join(data_abs, entry)
            if not os.path.isdir(entry_path):
                continue
            # Check if this dir contains any metadata.json files
            for fname in os.listdir(entry_path):
                if fname.endswith('.metadata.json'):
                    pid = fname.replace('.metadata.json', '')
                    if pid not in paper_dirs:
                        paper_dirs[pid] = entry
                    break

    count = 0
    status_counts = {}

    for paper_id, dir_name in paper_dirs.items():
        paper_dir = os.path.join(data_abs, dir_name)
        metadata_path = os.path.join(paper_dir, f'{paper_id}.metadata.json')
        pdf_path = os.path.join(paper_dir, f'{paper_id}.pdf')
        interpret_md = os.path.join(paper_dir, f'{paper_id}.interpret.md')
        interpret_json = os.path.join(paper_dir, f'{paper_id}.interpret.json')
        skipped_path = os.path.join(paper_dir, f'{paper_id}.skipped.json')

        # Load metadata if available
        metadata = {}
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        # Determine status
        has_interpret = os.path.exists(interpret_md) or os.path.exists(interpret_json)
        has_skipped = os.path.exists(skipped_path)
        has_pdf = os.path.exists(pdf_path)

        if has_interpret:
            status = 'interpreted'
        elif has_skipped:
            status = 'skipped'
        elif has_pdf:
            status = 'downloaded'
        elif paper_id in downloaded:
            # Was in downloaded list but no PDF on disk
            status = 'download_failed'
        elif os.path.exists(metadata_path):
            status = 'searched'
        else:
            status = 'searched'

        # Get file modification times as proxy for dates
        search_date = last_updated
        download_date = None
        interpret_date = None

        if has_pdf:
            mtime = os.path.getmtime(pdf_path)
            download_date = datetime.fromtimestamp(mtime).isoformat()
        if has_interpret:
            target = interpret_md if os.path.exists(interpret_md) else interpret_json
            mtime = os.path.getmtime(target)
            interpret_date = datetime.fromtimestamp(mtime).isoformat()

        # Build paper dict from metadata
        title = metadata.get('title', '') or metadata.get('Title', '')
        authors = metadata.get('authors', '') or metadata.get('Authors', '')
        if isinstance(authors, list):
            authors = ', '.join(authors)

        # Check if already exists
        existing = conn.execute(
            "SELECT id, status FROM papers WHERE paper_id = ?", (paper_id,)
        ).fetchone()

        if existing:
            # Update existing record
            conn.execute(
                """UPDATE papers SET title=?, authors=?, abstract=?,
                   doi=?, pmid=?, arxiv_id=?, source=?, source_url=?,
                   pdf_url=?, dir_name=?, status=?, search_date=?,
                   download_date=?, interpret_date=?, metadata_json=?,
                   updated_at=?
                   WHERE paper_id=?""",
                (
                    title,
                    authors,
                    metadata.get('abstract', '') or metadata.get('Abstract', ''),
                    metadata.get('doi', '') or metadata.get('DOI', ''),
                    metadata.get('pmid', '') or metadata.get('PMID', ''),
                    metadata.get('arxiv_id', '') or metadata.get('arxiv_id', ''),
                    metadata.get('source', ''),
                    metadata.get('abs_url', '') or metadata.get('url', ''),
                    metadata.get('pdf_url', ''),
                    dir_name,
                    status,
                    search_date,
                    download_date,
                    interpret_date,
                    json.dumps(metadata, ensure_ascii=False),
                    datetime.now().isoformat(),
                    paper_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO papers
                   (paper_id, title, authors, abstract, doi, pmid, arxiv_id,
                    source, source_url, pdf_url, dir_name, status, search_date,
                    download_date, interpret_date, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    paper_id,
                    title,
                    authors,
                    metadata.get('abstract', '') or metadata.get('Abstract', ''),
                    metadata.get('doi', '') or metadata.get('DOI', ''),
                    metadata.get('pmid', '') or metadata.get('PMID', ''),
                    metadata.get('arxiv_id', '') or metadata.get('arxiv_id', ''),
                    metadata.get('source', ''),
                    metadata.get('abs_url', '') or metadata.get('url', ''),
                    metadata.get('pdf_url', ''),
                    dir_name,
                    status,
                    search_date,
                    download_date,
                    interpret_date,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

        count += 1
        status_counts[status] = status_counts.get(status, 0) + 1

    conn.commit()

    print(f"Migration complete: {count} papers imported")
    for status, n in sorted(status_counts.items()):
        print(f"  {status}: {n}")

    # Print full stats
    from paper_db import get_stats
    stats = get_stats(conn)
    print(f"\nDatabase stats: {stats}")

    return count


def main():
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    config = load_config()
    state_file = 'data/downloaded.json'
    data_dir = 'data'
    count = migrate(config, state_file, data_dir)
    if count == 0:
        print("No papers migrated. Is data/downloaded.json present and valid?")
        sys.exit(1)


if __name__ == '__main__':
    main()