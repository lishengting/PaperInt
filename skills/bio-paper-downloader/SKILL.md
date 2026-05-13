---
name: bio-paper-downloader
description: Download bioinformatics papers from search results or URLs. Supports arXiv, bioRxiv, medRxiv, PubMed, and generic PDFs. Reads search results from the shared SQLite database; when run without arguments, downloads all un-downloaded papers.
compatibility: Requires Python 3 and google-chrome. Direct PDF downloads use stdlib only. Browser-based downloads (bioRxiv/medRxiv/PubMed) need Playwright (pip install playwright). On headless servers use xvfb-run.
metadata:
  skit:
    version: 0.2.0
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
---

# Bio Paper Downloader

## When To Use

Use this skill when the user asks to download papers. Use `bio-paper-search` to
find papers first (results are saved to the database), then use this skill to
download them. Or download directly by URL.

## Quick Start

```
python3 scripts/paper_cli.py {get|auto} [options]
```

Supported sources: `arxiv` | `biorxiv` | `medrxiv` | `pubmed` | `scholar` | `generic`

### Auto-mode (no arguments)

When run without a subcommand, downloads all papers with status `searched` from
the shared database:

```bash
python3 scripts/paper_cli.py
python3 scripts/paper_cli.py --browser   # enable browser for bioRxiv/PubMed
```

## Command Reference

### get — download by URL

```bash
python3 scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001"
python3 scripts/paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
python3 scripts/paper_cli.py get -u "https://pubmed.ncbi.nlm.nih.gov/12345678/"
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-u, --url` | (required) | Paper URL |
| `-l, --list` | off | Parse and show info without downloading |

### Auto-mode options

| Flag | Default | Description |
|------|---------|-------------|
| `--browser` | off | Use Chrome for PDF downloads |
| `--data-dir` | `data` | Output directory |
| `--db` | from config | SQLite database path |

## Sources

| Source | PDF Download | Notes |
|--------|-------------|-------|
| `arxiv` | Direct HTTP | Most reliable for CS/bioinfo preprints |
| `biorxiv` | Browser | Cloudflare requires headed Chrome |
| `medrxiv` | Browser | Cloudflare requires headed Chrome |
| `pubmed` | Browser via DOI or PMC | Follows DOI to publisher, falls back to PMC OA |
| `scholar` | Browser | Follows detected links |

## State Tracking

The shared SQLite database (`data/papers.db`) replaces the old `downloaded.json`.
Papers progress through statuses: `searched` → `downloaded` (or `download_failed`).

## Output

- `data/{title_dir}/{paper_id}.pdf` — downloaded paper PDF
- `data/{title_dir}/{paper_id}.metadata.json` — paper metadata
- `data/{title_dir}/{paper_id}.info.md` — comprehensive paper dossier

## Configuration

All defaults from `config.yaml`:
- `db.path` — database location (default: `data/papers.db`)
- `download.*` — rate limits, timeouts, file size thresholds

## Rules

- Do not schedule periodic downloads; this skill runs on demand.
- Respect rate limits. The script waits `download.request_delay_seconds` between API calls.
- Validate bioRxiv PDF downloads by checking `%PDF` magic bytes.
- Validate arXiv downloads by minimum file size (default 10000 bytes).
- Skip papers already marked `downloaded` in the database.
- Never modify `config.yaml`; it is the shared configuration.