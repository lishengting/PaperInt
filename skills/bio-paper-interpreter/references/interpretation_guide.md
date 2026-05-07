# Paper Interpretation Guide

## Mode Selection

The interpreter supports two modes. Mode is selected automatically based on
PDF text availability:

### Full-Text Mode

**Trigger**: Extracted PDF text is >= 1000 characters.

**System prompt**: `system_prompts.full_text` from `config.yaml`

**Output**: 2000-2500 word Chinese article with:
- Title: 【论文解读】xxx
- Research background (300-400 words)
- Core methods (400-500 words)
- Main findings (300-400 words)
- Discussion & significance (200-300 words)
- Practical implications (150-200 words)
- Concise summary: 3-5 bullet points, each <= 30 chars

**Sources**: PDF full text (first 15000 chars) + metadata (title, authors, abstract)

### Abstract-Only Mode

**Trigger**: No PDF available, or extracted PDF text is < 1000 characters.

**System prompt**: `system_prompts.abstract_only` from `config.yaml`

**Output**: 1200-1500 word Chinese article with:
- Title: 【论文解读】xxx (highlighting innovation)
- Background (200-300 words)
- Core methods (300-400 words, inferred from abstract)
- Main findings (200-300 words)
- Significance & outlook (150-200 words)
- Concise summary: 3-5 bullet points, each <= 30 chars

**Requirement**: Must label which claims are from the abstract and which are
reasonable inferences.

## Relevance Filtering

Before interpreting, every paper passes through `filter_relevance.py`:

1. **Exclude check**: Any exclude keyword match in title+abstract -> immediate reject.
2. **Include check**: Count include keyword matches in title+abstract.
3. **Threshold**: Must match >= `keywords.include_min_match` keywords (default 2).
4. **Empty content**: Papers with no title and no abstract are rejected.

## Tag Matching

After relevance filtering, `match_tags.py` assigns topic tags:

1. Always included: base tags `[2, 9]` (生物信息学, AI论文解读).
2. Domain tags matched by regex patterns from `config.yaml` `tags.definitions`.
3. AI parent tag `[1]` added if any ML/DL/LLM/AF tags matched.
4. Tags are numeric IDs for downstream systems (e.g., Flarum).

## Output Format

Each interpreted paper produces a JSON file at `data/interpreted/{doi}.json`:

```json
{
  "paper_id": "DOI or arXiv ID",
  "source": "biorxiv | arxiv",
  "title": "Chinese title extracted from first Markdown heading",
  "content": "Full Markdown body including citation block",
  "original_title": "Original English title",
  "tags": [2, 9, 17, 29],
  "matched_labels": ["RNA-seq", "深度学习"],
  "mode": "full_text | abstract_only",
  "interpreted_at": "2025-01-01T12:00:00"
}
```

Skipped papers produce `data/interpreted/{doi}_skipped.json`:

```json
{
  "paper_id": "DOI or arXiv ID",
  "skipped_at": "2025-01-01T12:00:00",
  "reason": "not_relevant | no_matching_tags | excluded"
}
```

## Citation Block

The LLM output content is appended with a standard citation block:

```
---

**文献信息**

- **标题**: {original_title}
- **作者**: {authors}
- **预印本平台**: {source_label}
- **发表日期**: {date}
- **{id_type}**: [{id}]({link})
- **本地下载**: {local_url}

> 本文由AI基于{interpretation_type}深度解读生成。
```

## LLM Configuration

From `config.yaml` `llm`:
- `api_base_url`: OpenAI-compatible endpoint
- `api_key_env`: Environment variable name for the API key
- `model`: Model name to use
- `temperature`: Sampling temperature (0.3)
- `max_tokens`: Max tokens in response (4000)
- `timeout_seconds`: Request timeout (120)
