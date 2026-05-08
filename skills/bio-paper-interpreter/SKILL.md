---
name: bio-paper-interpreter
description: Interpret bioinformatics papers into structured Chinese technical reports. Filter for bioinformatics relevance, match topic tags, extract PDF text, and generate LLM-powered or Claude Code direct interpretations.
compatibility: Requires Python 3, bash, poppler-utils (pdftotext). External LLM path requires network access to an OpenAI-compatible API endpoint.
metadata:
  skit:
    version: 0.2.0
    requires:
      bins:
        - python3
        - bash
        - pdftotext
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

## Two Interpretation Paths

### Path A: Claude Code Direct (default when using Claude Code)

Claude Code reads the paper content and system prompts from `config.yaml`
directly, then interprets without any external API. This is the default
when the user is working inside Claude Code.

### Path B: External LLM Pipeline

Uses the configured LLM API endpoint (`config.yaml` → `llm.api_base_url`)
to interpret papers programmatically. Requires `LLM_API_KEY` set and the
LLM service running. Suitable for batch processing and automation.

## Core Protocol

The filesystem is the state machine. `data/interpreted/execution_log.md` is
the state log. Recover by reading files, not by relying on chat memory.

1. Ensure `data/interpreted/` exists.
2. Read `execution_log.md` to find the current phase for each paper.
3. Load the reference file for the current phase.
4. Read all completed prior phase outputs required by the current phase.

## Phase Map

| Phase | Output | Reference | Description |
|-------|--------|-----------|-------------|
| 1 Filter | `data/interpreted/{paper_id}_skipped.json` (if rejected) | `references/01_filter.md` | Relevance check + tag matching |
| 2 Interpret | `data/interpreted/{paper_id}.md` + `.json` | `references/02_interpret.md` | PDF extraction + interpretation |

State rules per paper:
- No log entry for this paper: start Phase 1.
- Last log is `Phase 1 - REJECTED`: paper is done (skipped).
- Last log is `Phase 1 - COMPLETED`: start Phase 2.
- Last log is `Phase 2 - COMPLETED`: paper is done (interpreted).
- Last log is `Phase N - FAILED`: diagnose and retry.

## Logging

Log meaningful actions in `data/interpreted/execution_log.md` with:

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
| `filter_relevance.py` | Apply bioinformatics relevance keyword filter |
| `match_tags.py` | Regex-based tag assignment from config definitions |
| `extract_pdf_text.sh` | Extract plain text from PDF via pdftotext |
| `build_prompt.py` | Template LLM system+user prompts (Path B only) |

## Output

- `data/interpreted/{paper_id}.md` — structured Markdown report (primary)
- `data/interpreted/{paper_id}.json` — metadata + full content (secondary)
- `data/interpreted/{paper_id}_skipped.json` — skip record for non-relevant papers
- `data/interpreted/execution_log.md` — phase state log

## Rules

- Run phases sequentially per paper; never skip Phase 1.
- Always filter relevance before interpreting; record skip reasons.
- Phase 2 must check the article landing page and supplementary material tab
  for preprints (medRxiv/bioRxiv); do not infer supplement absence from PDF
  text alone.
- Load prompts and keywords from `config.yaml`; never hardcode them.
- Never modify `config.yaml`; it is shared across all skills.
- Distinguish full-text vs abstract-only in output metadata (`mode` field).
- Path A (Claude Code) requires no API key; Path B requires `LLM_API_KEY`.
- PDF text truncated to `download.pdf_text_max_chars` (default 100000 chars).
- If extracted PDF text < 1000 chars, fall back to abstract-only mode.
- All interpretations saved under `data/interpreted/`.