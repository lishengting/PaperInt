---
name: bio-paper-downloader
description: Search and download bioinformatics papers from arXiv and bioRxiv. Supports keyword search, title search, URL download, and list-only mode. One unified CLI for all operations.
compatibility: Requires Python 3 and network access to bioRxiv and arXiv APIs. No additional system dependencies needed.
metadata:
  skit:
    version: 0.1.0
    requires:
      bins:
        - python3
    keywords:
      - bioinformatics
      - preprint
      - paper-download
      - biorxiv
      - arxiv
---

# Bio Paper Downloader

## When To Use

Use this skill when the user asks to find and download bioinformatics papers
from arXiv or bioRxiv. Do not use it for generic web searches, paper
interpretation, or scheduled/cron-based periodic downloading (timing is
handled by an external orchestrator).

## Quick Start

All functionality is exposed through a single CLI:

```
python3 scripts/paper_cli.py {search|find|get} [options]
```

Global options: `--config` (config file path, default `config.yaml`).

All commands support `-l` / `--list` to preview results without downloading.

## Command Reference

### search — search by keywords, download latest N papers

```bash
# Download latest 3 methylation + single-cell papers from arXiv
python3 scripts/paper_cli.py search -k "methylation,single-cell" -s arxiv -n 3

# Use default keywords from config, download 1 paper from arXiv (defaults)
python3 scripts/paper_cli.py search

# Search bioRxiv, apply config relevance filter, list only
python3 scripts/paper_cli.py search -k "CRISPR" -s biorxiv -f -l

# Short form
python3 scripts/paper_cli.py search -k "tumor,immunotherapy" -n 2 -l
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-k, --keywords` | from config `keywords.include` | Comma-separated keywords |
| `-s, --source` | from config `search.default_source` (arxiv) | `arxiv` or `biorxiv` |
| `-n, --num` | from config `search.default_num` (1) | Number of papers to download |
| `-f, --filter` | off | Apply config keyword relevance filter |
| `-l, --list` | off | List only, don't download |

### find — search by paper title

```bash
# Search for a specific paper by title on arXiv
python3 scripts/paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"

# Search bioRxiv, list matches
python3 scripts/paper_cli.py find -t "CRISPR editing epigenetics" -s biorxiv -l
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-t, --title` | (required) | Paper title to search |
| `-s, --source` | from config `search.default_source` | `arxiv` or `biorxiv` |
| `-l, --list` | off | List top matches, don't download |

When not in list mode, only the best match is downloaded.

### get — download by URL

```bash
# Download from arXiv
python3 scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001"

# Download from bioRxiv
python3 scripts/paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"

# With full PDF URL
python3 scripts/paper_cli.py get -u "https://arxiv.org/pdf/2301.00001.pdf"
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-u, --url` | (required) | Paper URL (arXiv or bioRxiv) |
| `-l, --list` | off | Only show parsed URL, don't download |

The URL is auto-detected: arXiv abs/pdf URLs and bioRxiv content URLs are
all parsed to extract the paper ID.

## State Tracking

All commands use `--state-file` (default `data/downloaded.json`) to avoid
re-downloading papers. State is a JSON dict with a `downloaded` array of
paper IDs.

Override with `--pdf-dir` and `--metadata-dir` to change output locations.

## Output

- `data/pdf/*.pdf` — downloaded paper PDFs
- `data/metadata/*.json` — paper metadata (one JSON per paper)
- `data/downloaded.json` — state file tracking all downloaded IDs

## Other Scripts (internal helpers)

The following scripts are used internally by `paper_cli.py` but can also be
invoked directly for advanced workflows:

| Script | Purpose |
|--------|---------|
| `paper_cli.py` | **Primary** unified CLI (search, find, get) |
| `search_biorxiv.py` | Low-level: query bioRxiv API by date range |
| `search_arxiv.py` | Low-level: query arXiv API by categories |
| `merge_deduplicate.py` | Low-level: merge + deduplicate paper lists |
| `download_pdfs.py` | Low-level: batch download PDFs from JSON list |

## Configuration

All defaults come from `config.yaml`:

- `search.default_source` — default preprint source (`arxiv`)
- `search.default_num` — default paper count (1)
- `keywords.include` / `keywords.exclude` — default search keywords
- `apis.arxiv.*` / `apis.biorxiv.*` — API endpoints and URL patterns
- `download.*` — rate limits, timeouts, file size thresholds

## Rules

- Do not schedule periodic downloads; this skill runs on demand.
- Respect rate limits: arXiv and bioRxiv both enforce strict rate limiting.
  The script waits `download.request_delay_seconds` between API calls.
- Validate bioRxiv PDF downloads by checking `%PDF` magic bytes.
- Validate arXiv downloads by minimum file size (default 10000 bytes).
- Skip papers already present in the state file.
- Never modify `config.yaml`; it is the shared configuration.
