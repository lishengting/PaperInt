# PaperInt

Bioinformatics paper intelligence pipeline — search, download, and interpret papers with AI.

```
bio-paper-search ──→ bio-paper-downloader ──→ bio-paper-interpreter
    (searched)          (downloaded)            (interpreted/skipped)
```

Each skill reads from and writes to a shared SQLite database, advancing papers through statuses. The final output is a styled Chinese interpretation report in HTML, with optional SVG/PNG posters.

## Quick Start

```bash
git clone https://github.com/lishengting/PaperInt.git
cd PaperInt
./install.sh

export LLM_API_KEY="your-api-key"
source venv/bin/activate

# Search for papers
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "DNA methylation" -n 5

# Download all searched papers
python3 skills/bio-paper-downloader/scripts/paper_cli.py
```

## Requirements

- **OS**: Linux (Ubuntu/Debian, RHEL/CentOS/Fedora, Arch)
- **System**: Python 3.8+, Google Chrome or Chromium, poppler-utils, Xvfb
- **API**: LLM endpoint (DashScope-compatible) — set `LLM_API_KEY`
- **Browser**: Optional, needed for bioRxiv/medRxiv downloads and Google Scholar

## Install

```bash
./install.sh
```

The script auto-detects your package manager (apt/dnf/yum/pacman) and installs everything. It creates a Python virtual environment at `venv/` so system packages are untouched.

For manual install:

```bash
# System packages
sudo apt install python3 python3-pip poppler-utils xvfb google-chrome

# Python
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

mkdir -p data data/tmp
```

## Usage

### Search

```bash
# Search by keyword on arXiv
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "single cell RNA-seq" -n 5

# Search across all sources (arXiv, bioRxiv, medRxiv, PubMed, Google Scholar)
python3 skills/bio-paper-search/scripts/paper_cli.py search -k "CRISPR" -s all -n 10 --browser

# Semantic search via topic
python3 skills/bio-paper-search/scripts/paper_cli.py find -t "Deep learning for protein structure prediction"
```

### Download

```bash
# Auto-download all papers with status 'searched'
python3 skills/bio-paper-downloader/scripts/paper_cli.py

# Download a specific paper by URL
python3 skills/bio-paper-downloader/scripts/paper_cli.py get -u "https://arxiv.org/abs/2301.00001"

# Download with browser (required for bioRxiv/medRxiv/publishers)
python3 skills/bio-paper-downloader/scripts/paper_cli.py --browser
```

### Interpret

The interpreter runs within Claude Code following reference docs in `skills/bio-paper-interpreter/references/`. It has a three-phase pipeline:

1. **Filter** — check relevance via keywords, assign topic tags
2. **Interpret** — extract PDF to Markdown, generate Chinese interpretation report via LLM
3. **Convert** — render styled HTML with dark/light mode
4. **Poster** — generate SVG/PNG posters (English + Chinese)

### Query the database

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from paper_db import get_conn, get_stats
import yaml
config = yaml.safe_load(open('config.yaml'))
conn = get_conn(config)
print(get_stats(conn))
"
```

## Architecture

### Status Pipeline

```
searched → downloaded → interpreted
                ↓               ↓
         download_failed     skipped
```

### Data Directory

```
data/{Title_Slug}/
  {paper_id}.pdf
  {paper_id}.metadata.json
  {paper_id}.info.md           # comprehensive dossier
  {paper_id}.interpret.md      # Chinese interpretation report
  {paper_id}.interpret.html    # styled HTML
  {paper_id}.interpret.json    # structured metadata
  {paper_id}.skipped.json      # skip record
  {paper_id}.poster.zh.svg     # Chinese poster
  {paper_id}.poster.en.svg     # English poster
  {paper_id}.poster.zh.png     # rendered PNG
  {paper_id}.poster.en.png     # rendered PNG
  images/                      # extracted PDF images
```

### Multi-Source Deduplication

Papers from arXiv, bioRxiv, medRxiv, PubMed, and Google Scholar are merged by DOI, arXiv ID, or title similarity (Jaccard ≥ 0.7). Source priority: `pubmed > scholar > arxiv > medrxiv > biorxiv`.

## Configuration

All settings live in `config.yaml` at the project root — APIs, keywords, tags, LLM models, prompts. Never hardcode values in scripts.

Key config points:

| Setting | Default |
|---|---|
| `llm.api_base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `llm.model` | `deepseek-v4-pro` |
| `llm.api_key_env` | `LLM_API_KEY` |
| `db.path` | `data/papers.db` |

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `LLM_API_KEY` | Yes | LLM interpretation and poster generation |
| `TWOCAPTCHA_API_KEY` | No | CAPTCHA solving for publisher sites |

## Database Module

`scripts/paper_db.py` provides the shared database layer. Key API:

- `insert_search_results(conn, papers)` — INSERT OR IGNORE with status `searched`
- `get_papers_by_status(conn, status)` — fetch papers at a given stage
- `mark_downloaded(conn, pid, dir_name, meta)` — record successful download
- `mark_interpreted(conn, pid)` / `mark_skipped(conn, pid)` — record interpretation result
- `get_stats(conn)` — overview of all papers

Uses SQLite with WAL mode. Connection is cached per path.

## License

MIT