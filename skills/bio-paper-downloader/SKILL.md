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
python3 skills/bio-paper-downloader/scripts/paper_cli.py {get|pdf} [options]
# or no subcommand for auto-mode
```

Download mechanisms: arXiv direct HTTP, bioRxiv/medRxiv browser, PubMed/PMC/publisher browser fallback, and generic direct PDF URLs.

### Auto-mode (no arguments)

When run without a subcommand, downloads all papers with status `searched` from
the shared database:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py                         # auto-mode (browser auto-starts when needed)
```

## Command Reference

### get — download by URL or paper ID

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001"
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://pubmed.ncbi.nlm.nih.gov/12345678/"
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -p "2301.00001"              # resolve URL from database
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001" -l   # preview only
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001" -f   # force re-download
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-u, --url` | (one of `--url`/`--paper-id`) | Paper URL |
| `-p, --paper-id` | (none) | Resolve URL from database by paper ID |
| `-l, --list` | off | Parse and show info without downloading |
| `-f, --force` | off | Force re-download even if already downloaded |
| `--browser-only` | off | Bypass fallback logic: headed Chrome with real display (biorxiv/medrxiv only) |
| `--fallback-level` | 2 | Browser fallback: 0=direct-HTTP, 1=headless, 2=+xvfb, 3=+system-display |
| `--captcha` | off | Enable 2Captcha solving (costs money) |
| `--stealth` | off | Enable playwright-stealth for browser downloads |

### pdf — download PDF directly

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py pdf -u "https://example.com/paper.pdf"
python3 skills/bio-paper-downloader/scripts/paper_cli.py pdf -u "https://example.com/paper.pdf" -o my-paper.pdf
```

Raw PDF download — no database tracking, no metadata, like curl.

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-u, --url` | (required) | PDF URL |
| `-o, --output` | derived from URL | Output file path |

### Auto-mode options

When run without a subcommand, downloads all papers with status `searched` from the database.

| Flag | Default | Description |
|------|---------|-------------|
| `--limit, -n` | (no limit) | Max number of papers to download |
| `--retry-failed` | off | Retry papers with `download_failed` status |
| `--cnsp` | off | Only download papers published in C/N/S/P journals |
| `--cns` | off | Only download papers published in C/N/S journals (excludes PLOS) |
| `--fallback-level` | 2 | Browser fallback: 0=direct-HTTP, 1=headless, 2=+xvfb, 3=+system-display |
| `--captcha` | off | Enable 2Captcha solving (costs money) |
| `--stealth` | off | Enable playwright-stealth for browser downloads |
| `--data-dir` | `data` | Output directory |
| `--db` | from config | SQLite database path |

## Sources

| Source | PDF Download | Notes |
|--------|-------------|-------|
| `arxiv` | Direct HTTP | Most reliable for CS/bioinfo preprints |
| `biorxiv` | Browser | Cloudflare requires headed Chrome |
| `medrxiv` | Browser | Cloudflare requires headed Chrome |
| `pubmed` | Browser via DOI or PMC | Follows DOI to publisher, falls back to PMC OA |
| `generic` | Direct HTTP | Any direct PDF URL |

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