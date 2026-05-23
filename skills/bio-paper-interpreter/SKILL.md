---
name: bio-paper-interpreter
description: Interpret bioinformatics papers into structured Chinese technical reports. Match topic tags, extract PDF text, and generate LLM-powered or Claude Code direct interpretations with HTML conversion and poster generation.
compatibility: Requires Python 3, pymupdf4llm (PyMuPDF), bash. pdftotext (poppler-utils) is optional for fallback. External LLM path requires network access to an OpenAI-compatible API endpoint.
metadata:
  skit:
    version: 0.4.0
    requires:
      bins:
        - python3
        - bash
        - pdftotext  # optional, fallback only
      packages:
        - pymupdf4llm
        - PyMuPDF
    keywords:
      - bioinformatics
      - paper-interpretation
      - chinese-summary
      - llm
---

# Bio-Paper-Interpreter

## When To Use

Use this skill when the user asks to interpret downloaded paper PDFs or
metadata into Chinese-language structured technical reports. Do not use it for
scheduled/cron-based periodic interpretation, Flarum publishing, or generic
translation tasks.

## Quick Start

### Path A — Claude Code Direct

Claude Code reads the paper content, reference docs, and `config.yaml` directly,
then interprets without any external API. This is the default when working inside
Claude Code.

### Path B — External LLM Pipeline (CLI)

```bash
# Auto-mode — interpret all downloaded papers
python3 scripts/paper_cli.py

# Interpret a single paper
python3 scripts/paper_cli.py run s41467-026-70776-7

# Run specific phases only
python3 scripts/paper_cli.py run s41467-026-70776-7 --phase 1,2

# Force re-interpret
python3 scripts/paper_cli.py run s41467-026-70776-7 --force

# Preview what would be processed
python3 scripts/paper_cli.py --dry-run

# Limit papers, retry failed, or filter by journal
python3 scripts/paper_cli.py --limit 5 --retry-failed
python3 scripts/paper_cli.py --cnsp          # C/N/S/P journals only
python3 scripts/paper_cli.py --cns           # C/N/S journals only (excludes PLOS)
python3 scripts/paper_cli.py --en            # also generate English posters
```

Path B uses the configured LLM API endpoint (`config.yaml` → `llm.api_base_url`).
Requires `LLM_API_KEY` set and the LLM service running. Suitable for batch
processing and automation.

### `run` subcommand options

| Flag | Default | Description |
|------|---------|-------------|
| `paper_id` | (required) | Paper ID to interpret |
| `--phase` | `1,2,3,4` | Comma-separated phases to run (e.g., `1,2`) |
| `-f, --force` | off | Force re-interpret even if already interpreted |
| `--en` | off | Also generate English posters (default: Chinese only) |

### Auto-mode options

| Flag | Default | Description |
|------|---------|-------------|
| `--retry-failed` | off | Retry papers with `interpret_failed` status |
| `--limit, -n` | (no limit) | Max number of papers to process |
| `--cnsp` | off | Only process papers in C/N/S/P journals |
| `--cns` | off | Only process papers in C/N/S journals |
| `--en` | off | Also generate English posters |
| `--dry-run` | off | List papers that would be processed, then exit |

## Two Interpretation Paths

### Path A: Claude Code Direct (default when using Claude Code)

Claude Code reads the paper content and system prompts from `config.yaml`
directly, then interprets without any external API. This is the default
when the user is working inside Claude Code.

### Path B: External LLM Pipeline

Uses the configured LLM API endpoint (`config.yaml` → `llm.api_base_url`)
to interpret papers programmatically via `paper_cli.py`. Requires
`LLM_API_KEY` set and the LLM service running. Suitable for batch
processing and automation.

## Core Protocol

The filesystem is the state machine. The SQLite database (`data/papers.db`) is the
shared source of truth for paper locations and status. `data/execution_log.md` is
the state log. Recover by reading files, not by relying on chat memory.

1. Ensure `data/` exists.
2. Find the paper directory from the database using `scripts/paper_db.py`:
   ```
   PAPER_DIR="data/$(python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   from paper_db import get_conn, get_db_path, get_paper_dir
   import yaml
   config = yaml.safe_load(open('config.yaml'))
   conn = get_conn(config)
   print(get_paper_dir(conn, '{paper_id}') or '')
   ")"
   ```
3. Read `execution_log.md` to find the current phase for each paper.
4. Load the reference file for the current phase.
5. Read all completed prior phase outputs required by the current phase.

### Auto-mode (no target)

When invoked without a specific paper_id target, find all papers with status
`downloaded` in the database and interpret them in sequence:

```
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from paper_db import get_conn, get_db_path, get_papers_by_status
import yaml
config = yaml.safe_load(open('config.yaml'))
conn = get_conn(config)
papers = get_papers_by_status(conn, 'downloaded')
for p in papers:
    print(p['paper_id'], p.get('dir_name', ''))
"
```

## Phase Map

| Phase | Output | Reference | Description |
|-------|--------|-----------|-------------|
| 1 Filter | `{paper_dir}/{paper_id}.skipped.json` (if rejected) | `references/01_filter.md` | Tag matching |
| 2 Interpret | `{paper_dir}/{paper_id}.interpret.md` + `.json` + `.brief.md` | `references/02_interpret.md` | PDF extraction + LLM interpretation + brief article |
| 3 Convert | `{paper_dir}/{paper_id}.interpret.html` + `.brief.html` | `references/03_convert.md` | Markdown → standalone HTML |
| 4 Poster | `{paper_dir}/{paper_id}.poster.*.svg` + `.png` | `references/04_poster.md` | SVG/PNG poster generation |

State rules per paper:
- No log entry for this paper: start Phase 1.
- Last log is `Phase 1 - REJECTED`: paper is done (skipped).
- Last log is `Phase 1 - COMPLETED`: start Phase 2.
- Last log is `Phase 2 - COMPLETED`: start Phase 3.
- Last log is `Phase 3 - COMPLETED`: start Phase 4.
- Last log is `Phase 4 - COMPLETED`: paper is done (interpreted + rendered + poster).
- Last log is `Phase N - FAILED`: diagnose and retry. Paper status set to `interpret_failed`.

## Logging

Log meaningful actions in `data/execution_log.md` with:

```
Phase N - STATUS: {paper_id} — message
```

Use these status values:

| Status | Use |
|--------|-----|
| `START` | A phase began for a paper. |
| `COMPLETED` | A phase completed and its required output exists. |
| `REJECTED` | Phase 1 determined the paper is not relevant. |
| `FAILED` | A phase or operation failed. |
| `INFO` | Important context that affects future work. |

Log phase starts/completions, rejections, and failures. Do not log pure
reads, directory creation, or simple checks unless the result changes
how the paper should be handled.

## Helper Scripts

Resolve scripts from this skill's `scripts/` directory.

| Script | Purpose |
|--------|---------|
| `paper_cli.py` | Main entry point — orchestrates all four phases via external LLM (Path B) |
| `filter_relevance.py` | Apply bioinformatics relevance keyword filter |
| `match_tags.py` | Regex-based tag assignment from config definitions |
| `extract_pdf.py` | Extract PDF → Markdown via pymupdf4llm (primary) or pdftotext (fallback); extract embedded images, select representative figure |
| `build_prompt.py` | Template LLM system+user prompts (Path B only) |
| `md_to_html.py` | Convert Markdown report to styled standalone HTML |
| `generate_poster.py` | Generate poster SVGs and PNGs via LLM pipeline (Phase 4) |
| `merge_split_images.py` | Merge split figure panels into composite images |

## Output

- `{paper_dir}/{paper_id}.interpret.md` — structured Markdown report (Phase 2)
- `{paper_dir}/{paper_id}.interpret.json` — metadata + full content (Phase 2)
- `{paper_dir}/{paper_id}.brief.md` — article-style brief (Phase 2)
- `{paper_dir}/{paper_id}.interpret.html` — standalone styled HTML (Phase 3)
- `{paper_dir}/{paper_id}.brief.html` — brief article HTML (Phase 3)
- `{paper_dir}/images/` — extracted embedded images (if pymupdf4llm used)
- `{paper_dir}/{paper_id}.poster.zh.svg` — Chinese SVG poster (Phase 4)
- `{paper_dir}/{paper_id}.poster.en.svg` — English SVG poster (Phase 4, with `--en`)
- `{paper_dir}/{paper_id}.poster.zh.png` — Chinese PNG poster (Phase 4)
- `{paper_dir}/{paper_id}.poster.en.png` — English PNG poster (Phase 4, with `--en`)
- `{paper_dir}/{paper_id}.poster.direct.zh.png` — Chinese direct text-to-image poster (Phase 4)
- `{paper_dir}/{paper_id}.poster.direct.en.png` — English direct text-to-image poster (Phase 4, with `--en`)
- `{paper_dir}/{paper_id}.skipped.json` — skip record for non-relevant papers
- `{paper_dir}/{paper_id}.metadata.json` — paper metadata (from downloader)
- `{paper_dir}/{paper_id}.pdf` — downloaded paper PDF
- `data/execution_log.md` — phase state log
- `data/papers.db` — shared database (paper status updated on phase completion)

## Rules

- Run phases sequentially per paper; never skip Phase 1.
- Phase 1 assigns topic tags via `match_tags.py`. No relevance rejection — all papers pass.
- Phase 2 must check the article landing page and supplementary material tab
  for preprints (medRxiv/bioRxiv); do not infer supplement absence from PDF
  text alone.
- Phase 2 validates PDF content via LLM to reject corrections, supplements, and non-articles.
- Load prompts and keywords from `config.yaml`; never hardcode them.
- Never modify `config.yaml`; it is shared across all skills.
- Distinguish full-text vs abstract-only in output metadata (`mode` field).
- Path A (Claude Code) requires no API key; Path B requires `LLM_API_KEY`.
- PDF text truncated to `download.pdf_text_max_chars` (default 100000 chars).
- If extracted PDF text < 1000 chars, mark as `interpret_failed`; do not fall back to abstract-only.
- Phase 3 runs `md_to_html.py` to produce standalone HTML for both `.interpret.md` and `.brief.md`;
  the HTML supports light/dark mode and requires no external resources.
- Phase 4 generates 3-6 poster files per paper. Skipped if `LLM_API_KEY` is not set.
- `--en` flag enables bilingual poster generation (default: Chinese only).
- All interpretations saved under the paper's directory in `data/`.
- After Phase 1 completion, call `update_tags()` to store matched tags.
- After Phase 2 completion, call `mark_interpreted()` to update the database status.
- On failure, call `mark_interpret_failed()` to set status to `interpret_failed`.