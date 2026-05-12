---
name: bio-paper-downloader
description: Search and download bioinformatics papers from arXiv, bioRxiv, medRxiv, PubMed, and Google Scholar. Supports keyword search, title search, URL download, and list-only mode via a single unified CLI.
compatibility: Requires Python 3 and google-chrome. Direct PDF downloads use stdlib only. Browser-based downloads (bioRxiv/medRxiv/PubMed) need Playwright (pip install playwright). On headless servers use xvfb-run.
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

Supported sources: `arxiv` | `biorxiv` | `medrxiv` | `pubmed` | `scholar`

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

# Search Google Scholar for deep learning + single-cell papers
python3 scripts/paper_cli.py search -k "deep learning,single-cell" -s scholar -n 3 --browser
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-k, --keywords` | config | Comma-separated keywords |
| `-s, --source` | config (`arxiv`) | `arxiv`, `biorxiv`, `medrxiv`, `pubmed`, `scholar` |
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
| `arxiv` | arXiv API | Direct HTTP | Most reliable for CS/bioinfo preprints |
| `biorxiv` | bioRxiv API | Browser | Biology preprints; Cloudflare requires headed Chrome |
| `medrxiv` | medRxiv API | Browser | Medical/clinical preprints; Cloudflare requires headed Chrome |
| `pubmed` | NCBI E-utilities | Browser via DOI | Follows DOI to publisher page, finds PDF link |
| `scholar` | Google Scholar (HTML scrape) | Browser | Broad search across all sources; requires `--browser` |

### Browser-Based PDF Download

bioRxiv and medRxiv use Cloudflare protection that blocks direct HTTP PDF
downloads. PubMed papers link to publisher websites (Nature, Springer, etc.)
that require a real browser. Pass `--browser` to enable headed-Chrome downloads:

```bash
# bioRxiv / medRxiv: bypass Cloudflare
python3 scripts/paper_cli.py search -k "methylation" -s biorxiv -n 1 --browser

# PubMed: follow DOI → publisher page → PDF link
python3 scripts/paper_cli.py search -k "deep learning" -s pubmed -n 1 --browser
```

Two browser scripts handle the download:
- `download_biorxiv_browser.py` — navigates article page → PDF page via same session
- `download_publisher_pdf.py` — follows DOI to publisher, locates PDF link, downloads

Both launch a real headed Chrome with a persistent profile, wait for anti-bot
challenges (Cloudflare/reCAPTCHA) to resolve, then fetch the PDF through the
authenticated browser session.

Requirements: `google-chrome`, `pip install playwright`.
On headless servers, prefix with `xvfb-run`.

## State Tracking

All commands use `--state-file` (default `data/downloaded.json`) to avoid
re-downloading papers. State is a JSON dict with a `downloaded` array of
paper IDs.

Override with `--data-dir` to change the output directory (default `data`).

## Output

- `data/{title_dir}/{paper_id}.pdf` — downloaded paper PDF
- `data/{title_dir}/{paper_id}.metadata.json` — paper metadata
- `data/{title_dir}/{paper_id}.info.md` — comprehensive paper dossier (identity, abstract, data availability, dataset accessions, code repos, supplementary materials, full text links)
- `data/downloaded.json` — state file tracking all downloaded IDs

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
