---
name: bio-paper-search
description: Search for bioinformatics papers across arXiv, bioRxiv, medRxiv, PubMed, Google Scholar, Crossref, Europe PMC, and CNSP journals. Saves search results to the shared SQLite database for subsequent download by bio-paper-downloader.
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
python3 skills/bio-paper-search/scripts/paper_cli.py {search|find} [options]
```

Supported sources: `arxiv` | `biorxiv` | `medrxiv` | `pubmed` | `scholar` | `crossref` | `europepmc` | `cnsp` | `all`

## Command Reference

### search — keyword search

```bash
# Search arXiv for methylation + single-cell papers
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "methylation,single-cell" -n 3

# Search PubMed for CRISPR papers
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "CRISPR,gene editing" -s pubmed -n 5

# Search all sources
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "deep learning,single-cell" -s all -n 5

# Search CNSP journals (Cell/Nature/Science/PLOS) — browser auto-starts
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "CRISPR" -s cnsp -n 3 --start-date 2026-05-01 --end-date 2026-05-13
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "genomic" -s cnsp -n 5 --incremental
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "methylation" -s cnsp -n 2 --cnsp-journals "Nature" "Science"
```

Options:
| Flag | Default | Description |
|------|---------|-------------|
| `-k, --keywords` | config | Comma-separated keywords |
| `-s, --source` | config (`arxiv`) | `arxiv`, `biorxiv`, `medrxiv`, `pubmed`, `scholar`, `crossref`, `europepmc`, `cnsp`, `all` |
| `-n, --num` | config (1) | Number of papers |
| `-l, --list` | off | Preview only, do not save to database |
| `--start-date` | 7 days ago | Search from this date (YYYY-MM-DD). Supported by arxiv, biorxiv, medrxiv, pubmed, crossref, europepmc, cnsp. |
| `--end-date` | today | Search until this date (YYYY-MM-DD) |
| `--incremental` | off | Auto-compute start_date from last crawl for this source |
| `--cnsp-journals` | all enabled | Limit CNSP to specific journals (e.g., "Nature" "Science") |
| `--cns` | off | When using `-s cnsp`, search only CNS journals (Nature/Science/Cell, excluding PLOS) |

### find — search by title or DOI

```bash
python3 skills/bio-paper-search/scripts/paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"
python3 skills/bio-paper-search/scripts/paper_cli.py find -t "CRISPR editing methylation" -s pubmed
python3 skills/bio-paper-search/scripts/paper_cli.py find -d "10.1038/s41586-023-00000-0"
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
| `scholar` | Google Scholar (HTML scrape) | Disabled by default; use `-s scholar` explicitly |
| `crossref` | Crossref API | Published papers with DOIs |
| `europepmc` | Europe PMC API | Open-access life science literature |
| `cnsp` | Cell/Nature/Science/PLOS journal scraping | Scrapes articles by date range, then filters by keywords client-side. Browser auto-starts. |

## Rules

- This skill ONLY searches, never downloads.
- By default, saves found papers to the database with status `searched`; `-l/--list` previews only and does not write to the database.
- Never modify `config.yaml`; it is the shared configuration.
- Respect rate limits: the script waits `download.request_delay_seconds` between API calls.