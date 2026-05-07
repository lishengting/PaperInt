---
name: bio-paper-interpreter
description: Interpret bioinformatics papers into structured Chinese technical articles. Extract text from PDFs, filter for bioinformatics relevance, match topic tags, and generate LLM-powered interpretations in full-text or abstract-only mode.
compatibility: Requires Python 3, bash, poppler-utils (pdftotext), and network access to an OpenAI-compatible LLM API endpoint.
metadata:
  skit:
    version: 0.1.0
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

# Bio Paper Interpreter

## When To Use

Use this skill when the user asks to interpret downloaded paper PDFs or
metadata into Chinese-language technical summaries. Do not use it for
scheduled/cron-based periodic interpretation, Flarum publishing, or generic
translation tasks.

## Core Concepts

The interpreter has two modes:

- **Full-text mode**: Extracts text from a PDF via `pdftotext`, feeds the
  truncated content to an LLM with the full-text system prompt from config.
- **Abstract-only mode**: Uses paper metadata (title, abstract, authors) when
  PDF text cannot be extracted or is too short (< 1000 characters).

Relevance filtering runs first — non-bioinformatics papers are skipped.
Tag matching assigns topic categories based on configured regex patterns.

All configuration (keywords for filtering, tag definitions, LLM settings,
system prompts) comes from the shared `config.yaml` at the project root.

## Workflow

### Read Configuration

Locate and read the shared `config.yaml`. The skill directory is
`skills/bio-paper-interpreter/`; the config file lives at `config.yaml`
in the parent of `skills/`.

### Prepare Output Directories

```bash
mkdir -p data/interpreted
```

### For Each Candidate Paper

For each paper to interpret (identified by the agent or user):

#### Step 1: Filter by Relevance

```bash
cat data/metadata/{paper_doi}.json | python3 scripts/filter_relevance.py \
  --config config.yaml
```

The script adds a `relevance` field to the JSON. Skip the paper if
`relevance.passed` is `false`. Record the skip reason in
`data/interpreted/{doi}_skipped.json`.

#### Step 2: Extract PDF Text (if PDF available)

```bash
bash scripts/extract_pdf_text.sh data/pdf/{paper_doi}.pdf \
  --max-chars 15000
```

If a PDF exists and the extracted text is > 1000 characters, use full-text
mode. Otherwise, fall back to abstract-only mode.

#### Step 3: Match Topic Tags

```bash
cat data/metadata/{paper_doi}.json | python3 scripts/match_tags.py \
  --config config.yaml
```

The script adds a `matched_tags` field. If only base tags (2 tags) match,
consider skipping the paper (insufficient topic specificity).

#### Step 4: Build LLM Prompts

```bash
cat data/metadata/{paper_doi}.json | python3 scripts/build_prompt.py \
  --config config.yaml \
  --mode full_text \
  --pdf-text-file /tmp/extracted_text.txt
```

Replace `--mode full_text` with `--mode abstract_only` as appropriate.
The script outputs a JSON object with `system_prompt` and `user_prompt` fields.

#### Step 5: Call LLM API

Use the prompts from Step 4 to call the configured LLM API. The LLM
configuration (endpoint, model, temperature, max_tokens) is in `config.yaml`
under the `llm` key. Read `llm.api_key_env` to find which environment
variable holds the API key.

Example curl call:

```bash
curl -s "$LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<from config>",
    "temperature": <from config>,
    "max_tokens": <from config>,
    "messages": [
      {"role": "system", "content": "<system_prompt from build_prompt.py>"},
      {"role": "user", "content": "<user_prompt from build_prompt.py>"}
    ]
  }'
```

#### Step 6: Save Interpretation

Save the full interpretation result as JSON:

```bash
cat > data/interpreted/{paper_doi}.json << 'EOF'
{
  "paper_id": "...",
  "title": "<extracted Chinese title>",
  "content": "<full markdown content from LLM>",
  "tags": [<matched tag ids>],
  "mode": "full_text | abstract_only",
  "interpreted_at": "<ISO timestamp>"
}
EOF
```

## Helper Scripts

Resolve scripts from this skill's `scripts/` directory.

| Script | Purpose |
|--------|---------|
| `extract_pdf_text.sh` | Extract plain text from PDF via pdftotext |
| `filter_relevance.py` | Apply bioinformatics relevance keyword filter |
| `match_tags.py` | Regex-based tag assignment from config definitions |
| `build_prompt.py` | Template LLM system+user prompts from paper + config |

## Output

- `data/interpreted/{doi}.json` — interpretation results (title, content, tags, mode)
- `data/interpreted/{doi}_skipped.json` — skip records for non-relevant papers

## Rules

- Do not schedule periodic interpretation; this skill runs on demand.
- Always filter relevance before interpreting; skip non-bioinformatics papers
  and record the reason.
- Truncate PDF text to `download.pdf_text_max_chars` (default 15000 chars).
- If extracted PDF text is < 1000 characters, fall back to abstract-only mode.
- Distinguish full-text vs abstract-only in output metadata (`mode` field).
- Always load prompts and keywords from `config.yaml`; never hardcode them.
- Never modify `config.yaml`; it is the shared configuration read by all skills.
- Make LLM calls with timeouts matching `llm.timeout_seconds` from config.
