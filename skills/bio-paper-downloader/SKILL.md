---
name: bio-paper-downloader
description: Search and download bioinformatics papers from arXiv, bioRxiv, medRxiv, and PubMed. Supports keyword search, title search, URL download, and list-only mode via a single unified CLI.
compatibility: Requires Python 3 and network access to public APIs. No additional system dependencies needed.
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
      - pubmed
      - arxiv
---

# Bio Paper Downloader

## When To Use

Use this skill when the user asks to find and download bioinformatics papers
from arXiv or bioRxiv. Do not use it for generic web searches, paper
interpretation, or scheduled/cron-based periodic downloading (timing is
handled by an external orchestrator).

## Quick Start

All functionality via a single CLI:

```
python3 scripts/paper_cli.py {search|find|get} [options]
```

Supported sources: `arxiv` | `biorxiv` | `medrxiv` | `pubmed`

Global options: `--config CONFIG` (default `config.yaml`).
All commands support `-l` / `--list` to preview without downloading.

## Command Reference

### search — keyword search, download latest N papers

```bash
# Download latest 3 methylation + single-cell papers from arXiv (default)
python3 scripts/paper_cli.py search -k "methylation,single-cell" -n 3

# Search PubMed for CRISPR papers, list only
python3 scripts/paper_cli.py search -k "CRISPR,gene editing" -s pubmed -n 5 -l

# Search medRxiv for COVID papers, download 2
python3 scripts/paper_cli.py search -k "vaccine,immunity" -s medrxiv -n 2
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-k, --keywords` | config | Comma-separated keywords |
| `-s, --source` | config (`arxiv`) | `arxiv`, `biorxiv`, `medrxiv`, `pubmed` |
| `-n, --num` | config (1) | Number of papers |
| `-f, --filter` | off | Config keyword relevance filter |
| `-l, --list` | off | List only, no download |

### find — search by title

```bash
python3 scripts/paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"
python3 scripts/paper_cli.py find -t "CRISPR editing methylation" -s pubmed -l
```

Downloads only the best match (unless `-l`).

### get — download by URL

```bash
python3 scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001"
python3 scripts/paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
python3 scripts/paper_cli.py get -u "https://pubmed.ncbi.nlm.nih.gov/12345678/"
```

URLs are auto-detected by domain pattern. Generic PDF URLs also work.

## Sources

| Source | Search API | PDF Download | Notes |
|--------|-----------|-------------|-------|
| `arxiv` | arXiv API | Direct PDF | Most reliable for CS/bioinfo preprints |
| `biorxiv` | bioRxiv API | Direct PDF | Biology preprints |
| `medrxiv` | medRxiv API | Direct PDF | Medical/clinical preprints |
| `pubmed` | NCBI E-utilities | PMC (if available) | Metadata always saved; PDF via PMC free full text |

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
