---
name: bio-paper-db-viewer
description: View papers and database statistics from the shared SQLite database. Browse papers by status, source, or keyword; display full paper details; see pipeline statistics.
compatibility: Requires Python 3. Read-only — never modifies the database or config.
metadata:
  skit:
    version: 0.1.0
    requires:
      bins:
        - python3
    keywords:
      - bioinformatics
      - paper-database
      - sqlite
      - viewer
      - statistics
---

# Bio Paper DB Viewer

## When To Use

Use this skill to inspect the shared SQLite database (`data/papers.db`). Browse
papers by status, source, or keyword; see per-paper details; check pipeline
statistics. This skill is read-only — it never modifies the database or config.

## Quick Start

```
python3 scripts/paper_cli.py {stats|list|show} [options]
```

## Command Reference

### stats — database summary

```bash
python3 scripts/paper_cli.py stats
```

Displays total papers, counts by status, counts by source, date range, and
recent activity (last 7 days / last 30 days).

Options: `--config` (default: config.yaml), `--db` (override database path).

### list — browse papers

```bash
# All papers (most recent 20)
python3 scripts/paper_cli.py list

# Filter by status
python3 scripts/paper_cli.py list -s downloaded -n 10

# Filter by source
python3 scripts/paper_cli.py list --source nature

# Filter by keyword in title
python3 scripts/paper_cli.py list -k "microbiome"

# Combined filters with pagination
python3 scripts/paper_cli.py list -s downloaded --source arxiv -k "CRISPR" -n 10 --offset 20
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-s, --status` | (all) | Filter by status: `searched`, `downloaded`, `download_failed`, `interpreted`, `skipped` |
| `--source` | (all) | Filter by source: `arxiv`, `biorxiv`, `medrxiv`, `pubmed`, `scholar`, `nature`, `science`, `cell`, `plos` |
| `-k, --keyword` | (none) | Filter papers whose title contains the keyword |
| `-n, --limit` | 20 | Maximum results |
| `--offset` | 0 | Pagination offset |

### show — paper details

```bash
python3 scripts/paper_cli.py show -p s41467-026-70776-7
```

Displays all non-empty fields for a single paper: title, authors, abstract, DOI,
source, status, dates, directory, and any parsed JSON fields (relevance, tags).

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-p, --paper-id` | (required) | Paper ID from the database (DOI, arXiv ID, PMID) |

## Output

- `stats` — text summary of database contents
- `list` — tabular listing with paper_id, title, source, status, date
- `show` — full field listing for one paper

## Rules

- Read-only — never modifies the database or config.yaml.
- Uses the shared `scripts/paper_db.py` module for connections and lookups.
- Database path from `config.yaml` `db.path`, overridable via `--db`.