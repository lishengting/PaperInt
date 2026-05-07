---
name: bio-paper-downloader
description: Search and download bioinformatics preprint papers from bioRxiv and arXiv. Query APIs by date range or categories, filter by configurable keywords, download PDFs, and save metadata with deduplication.
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

Use this skill when the user asks to find and download bioinformatics-related
papers from bioRxiv or arXiv. Do not use it for generic web searches, PDF
reading, paper interpretation, or scheduled/cron-based periodic downloading
(timing is handled by an external orchestrator).

## Core Concepts

The downloader searches two preprint sources independently, merges results,
deduplicates by DOI, applies configurable keyword filtering, downloads PDFs,
and saves metadata. A state file (`data/downloaded.json`) tracks what has
already been downloaded to avoid re-fetching.

Workflow:

1. Search bioRxiv by date range and keywords
2. Search arXiv by configured categories
3. Merge and deduplicate results across sources
4. Download PDFs and save metadata for new papers

All configuration (API endpoints, keywords, download settings) comes from the
shared `config.yaml` at the project root.

## Workflow

### Read Configuration

First, locate and read the shared `config.yaml`. The skill directory is
`skills/bio-paper-downloader/`; the config file lives at `config.yaml`
in the parent of `skills/`.

### Prepare Output Directories

Create the data directories if they do not exist:

```bash
mkdir -p data/pdf data/metadata
```

### Step 1: Search bioRxiv

Use `scripts/search_biorxiv.py` to query the bioRxiv API:

```bash
python3 scripts/search_biorxiv.py \
  --config config.yaml \
  --days 7 \
  --output data/biorxiv_results.json
```

Options:
- `--start`, `--end`: explicit date range (YYYY-MM-DD)
- `--days`: lookback from today (default from config)
- `--state-file`: path to state file for skipping already-downloaded papers

### Step 2: Search arXiv

Use `scripts/search_arxiv.py` to query the arXiv API:

```bash
python3 scripts/search_arxiv.py \
  --config config.yaml \
  --max 50 \
  --output data/arxiv_results.json
```

### Step 3: Merge and Deduplicate

Use `scripts/merge_deduplicate.py` to combine results from both sources:

```bash
python3 scripts/merge_deduplicate.py \
  --config config.yaml \
  --inputs data/biorxiv_results.json data/arxiv_results.json \
  --output data/merged_papers.json
```

This step normalizes both sources into a common paper schema, deduplicates by
DOI, and re-applies keyword filtering from config.

### Step 4: Download PDFs

Use `scripts/download_pdfs.py` to fetch PDFs and save metadata:

```bash
python3 scripts/download_pdfs.py \
  --config config.yaml \
  --papers data/merged_papers.json \
  --pdf-dir data/pdf \
  --metadata-dir data/metadata \
  --state-file data/downloaded.json
```

### Summarize Results

After all steps, report the totals: papers found, newly downloaded, skipped
(already present or filtered out).

## Helper Scripts

Resolve scripts from this skill's `scripts/` directory.

| Script | Purpose |
|--------|---------|
| `search_biorxiv.py` | Query bioRxiv API by date range, filter by keywords |
| `search_arxiv.py` | Query arXiv API by categories, output JSON |
| `merge_deduplicate.py` | Merge bioRxiv+arXiv results, dedupe by DOI |
| `download_pdfs.py` | Download PDFs from normalized paper list |

## Output

- `data/pdf/*.pdf` — downloaded paper PDFs
- `data/metadata/*.json` — paper metadata (one JSON file per paper)
- `data/downloaded.json` — state file tracking all downloaded DOIs/IDs
- Intermediate JSON files from each step for inspection or piping

## Rules

- Do not schedule periodic downloads; this skill runs on demand.
- Respect rate limits: wait `download.request_delay_seconds` between API calls.
- Validate PDF downloads by checking the `%PDF` magic bytes at the start of
  each response body.
- Skip files smaller than `download.min_pdf_size_bytes` (default 10000 bytes).
- Skip papers already present in the state file (`data/downloaded.json`).
- Never modify `config.yaml`; it is the shared configuration read by all skills.
- If a paper has no DOI or arXiv ID, skip it with a warning.
