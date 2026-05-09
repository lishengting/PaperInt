#!/usr/bin/env python3
"""One-shot migration: restructure data/ from flat-by-type to per-paper directories.

Before:
  data/pdf/{paper_id}.pdf
  data/metadata/{paper_id}.json
  data/interpreted/{paper_id}.md, .json, .html, _skipped.json

After:
  data/{title_dir}/{paper_id}.pdf
  data/{title_dir}/{paper_id}.metadata.json
  data/{title_dir}/{paper_id}.interpret.md
  data/{title_dir}/{paper_id}.interpret.json
  data/{title_dir}/{paper_id}.interpret.html
  data/{title_dir}/{paper_id}.skipped.json
  data/execution_log.md
"""
import json, os, re, shutil, sys

DATA = os.path.join(os.path.dirname(__file__), '..', 'data')
if not os.path.isdir(DATA):
    print(f"data/ directory not found at {DATA}", file=sys.stderr)
    sys.exit(1)


def title_to_dirname(title):
    if not title:
        return 'unknown'
    safe = re.sub(r'[/\\:*?"<>|\s]+', '_', str(title)).strip('_')
    return safe[:256]


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', '_', str(name))[:200]


def main():
    state_file = os.path.join(DATA, 'downloaded.json')
    if not os.path.exists(state_file):
        print("data/downloaded.json not found", file=sys.stderr)
        sys.exit(1)

    with open(state_file) as f:
        state = json.load(f)

    paper_ids = state.get('downloaded', [])
    print(f"Migrating {len(paper_ids)} papers...")

    for pid in paper_ids:
        safe_pid = sanitize(pid)
        # Find metadata JSON
        old_meta = os.path.join(DATA, 'metadata', f'{safe_pid}.json')
        if not os.path.exists(old_meta):
            print(f"  [skip] {pid}: no metadata found at {old_meta}")
            continue

        # Read title
        with open(old_meta) as f:
            meta = json.load(f)
        title = meta.get('title', '')
        dirname = title_to_dirname(title)

        paper_dir = os.path.join(DATA, dirname)
        os.makedirs(paper_dir, exist_ok=True)

        moves = []

        # Move metadata: metadata/{pid}.json → {dir}/{pid}.metadata.json
        dest = os.path.join(paper_dir, f'{safe_pid}.metadata.json')
        moves.append((old_meta, dest))

        # Move PDF: pdf/{pid}.pdf → {dir}/{pid}.pdf
        old_pdf = os.path.join(DATA, 'pdf', f'{safe_pid}.pdf')
        dest = os.path.join(paper_dir, f'{safe_pid}.pdf')
        if os.path.exists(old_pdf):
            moves.append((old_pdf, dest))

        # Move interpreted files
        old_int_dir = os.path.join(DATA, 'interpreted')
        # Interpret .md → .interpret.md
        old = os.path.join(old_int_dir, f'{safe_pid}.md')
        dest = os.path.join(paper_dir, f'{safe_pid}.interpret.md')
        if os.path.exists(old):
            moves.append((old, dest))

        # Interpret .json → .interpret.json
        old = os.path.join(old_int_dir, f'{safe_pid}.json')
        dest = os.path.join(paper_dir, f'{safe_pid}.interpret.json')
        if os.path.exists(old):
            moves.append((old, dest))

        # Interpret .html → .interpret.html
        old = os.path.join(old_int_dir, f'{safe_pid}.html')
        dest = os.path.join(paper_dir, f'{safe_pid}.interpret.html')
        if os.path.exists(old):
            moves.append((old, dest))

        # Skipped .json
        old = os.path.join(old_int_dir, f'{safe_pid}_skipped.json')
        dest = os.path.join(paper_dir, f'{safe_pid}.skipped.json')
        if os.path.exists(old):
            moves.append((old, dest))

        # Execute moves
        for src, dst in moves:
            shutil.move(src, dst)
            print(f"  {src} -> {dst}")

    # Move execution_log.md to data/ level
    old_log = os.path.join(DATA, 'interpreted', 'execution_log.md')
    new_log = os.path.join(DATA, 'execution_log.md')
    if os.path.exists(old_log):
        shutil.move(old_log, new_log)
        print(f"  {old_log} -> {new_log}")

    # Remove empty old directories
    for sub in ['pdf', 'metadata', 'interpreted']:
        d = os.path.join(DATA, sub)
        if os.path.isdir(d):
            try:
                os.rmdir(d)
                print(f"  Removed empty dir: {d}")
            except OSError:
                print(f"  Dir not empty (left as-is): {d}")

    print("Migration complete.")


if __name__ == '__main__':
    main()