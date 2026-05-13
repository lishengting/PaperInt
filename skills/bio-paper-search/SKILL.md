---
name: bio-paper-search
description: Search for bioinformatics papers across arXiv, bioRxiv, medRxiv, PubMed, and Google Scholar. Saves search results to the shared SQLite database for subsequent download by bio-paper-downloader.
compatibility: Requires Python 3. Google Scholar searches require google-chrome and Playwright (pip install playwright). On headless servers use xvfb-run.
metadata:
  skit:
    version: 0.1.0
    requires:
      bins:
        - python3
    keywords:
      - bioinformatics
      - preprint
      - paper-search
      - biorxiv
      - arxiv
      - pubmed
---

# Bio Paper Search

## When To Use

Use this skill when the user asks to search for bioinformatics papers. This skill
ONLY searches — it never downloads. Use `bio-paper-downloader` to download found
papers, and `bio-paper-interpreter` to interpret them.

## Quick Start

```
python3 scripts/paper_cli.py {search|find} [options]
```

Supported sources: `arxiv` | `biorxiv` | `medrxiv` | `pubmed` | `scholar`

## Command Reference

### search — keyword search

```bash
# Search arXiv for methylation + single-cell papers
python3 scripts/paper_cli.py search -k "methylation,single-cell" -n 3

# Search PubMed for CRISPR papers
python3 scripts/paper_cli.py search -k "CRISPR,gene editing" -s pubmed -n 5

# Search all sources
python3 scripts/paper_cli.py search -k "deep learning,single-cell" -s all -n 5 --browser
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-k, --keywords` | config | Comma-separated keywords |
| `-s, --source` | config (`arxiv`) | `arxiv`, `biorxiv`, `medrxiv`, `pubmed`, `scholar`, `all` |
| `-n, --num` | config (1) | Number of papers |
| `-l, --list` | off | Preview only (results always saved to DB) |
| `--browser` | off | Use Chrome (required for Google Scholar) |

### find — search by title

```bash
python3 scripts/paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"
python3 scripts/paper_cli.py find -t "CRISPR editing methylation" -s pubmed
```

## State

Search results are saved to the shared SQLite database (`data/papers.db`).
The downloader and interpreter skills read from this same database.

## Sources

| Source | Search API | Notes |
|--------|-----------|-------|
| `arxiv` | arXiv API | Most reliable for CS/bioinfo preprints |
| `biorxiv` | bioRxiv API | Biology preprints |
| `medrxiv` | medRxiv API | Medical/clinical preprints |
| `pubmed` | NCBI E-utilities | Published biomedical papers |
| `scholar` | Google Scholar (HTML scrape) | Broad search across all sources; requires `--browser` |

## Rules

- This skill ONLY searches, never downloads.
- Saves all found papers to the database with status `searched`.
- Never modify `config.yaml`; it is the shared configuration.
- Respect rate limits: the script waits `download.request_delay_seconds` between API calls.