#!/usr/bin/env python3
"""
Build LLM system and user prompts for paper interpretation.

Reads prompt templates from references/prompts/{name}.yaml, substitutes paper
fields into the user prompt template, and outputs a JSON object with
`system_prompt` and `user_prompt` fields.

Modes:
  - interpret_en: Structured English technical report
  - brief_en:     Narrative English article
  - interpret:    Structured Chinese technical report
  - brief:        Narrative Chinese article
"""
import argparse
import json
import os
import sys

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(SKILL_DIR, '..', 'references', 'prompts')


def load_config(path):
    if path.endswith('.json'):
        with open(path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        try:
            import yaml
            with open(path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
        except ImportError:
            config = _parse_simple_yaml(open(path, 'r', encoding='utf-8').read())
    return _resolve_env_vars(config)


def _resolve_env_vars(obj):
    """Recursively resolve leaf string values that match an env var name."""
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    if isinstance(obj, str) and obj in os.environ:
        return os.environ[obj]
    return obj


def _parse_simple_yaml(content):
    result = {}
    lines = content.split('\n')
    stack = [(result, 0)]

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue

        indent = len(line) - len(line.lstrip())
        while len(stack) > 1 and indent <= stack[-1][1]:
            stack.pop()
        parent = stack[-1][0]

        if ':' in stripped and not stripped.startswith('-'):
            key_end = stripped.index(':')
            key = stripped[:key_end].strip()
            value_part = stripped[key_end + 1:].strip()

            if value_part:
                if value_part in ('true', 'false'):
                    parent[key] = value_part == 'true'
                elif value_part.isdigit():
                    parent[key] = int(value_part)
                elif _is_float(value_part):
                    parent[key] = float(value_part)
                else:
                    parent[key] = value_part.strip('"').strip("'")
            else:
                next_i = i + 1
                if next_i < len(lines):
                    next_line = lines[next_i]
                    next_indent = len(next_line) - len(next_line.lstrip())
                    next_stripped = next_line.strip()
                    if next_indent > indent and next_stripped in ('|-', '|', '>'):
                        content_lines = []
                        j = next_i + 1
                        while j < len(lines):
                            block_line = lines[j]
                            block_indent = len(block_line) - len(block_line.lstrip())
                            if block_indent <= next_indent and block_line.strip():
                                break
                            content_lines.append(block_line.rstrip())
                            j += 1
                        min_ind = min(
                            (len(l) - len(l.lstrip())) for l in content_lines if l.strip()
                        ) if content_lines else 0
                        clean = [l[min_ind:] if l.strip() else '' for l in content_lines]
                        parent[key] = '\n'.join(clean)
                        i = j
                        continue
                    elif next_indent > indent and next_stripped.startswith('-'):
                        parent[key] = []
                        i = next_i
                        continue
                parent[key] = {}

        elif stripped.startswith('- '):
            list_value = stripped[2:].strip().strip('"').strip("'")
            if isinstance(parent, list):
                parent.append(list_value)
            elif isinstance(parent, dict):
                for k, v in parent.items():
                    if isinstance(v, list):
                        v.append(list_value)
                        break

        i += 1

    return result


def _is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_prompt(name):
    """Load a prompt template from references/prompts/{name}.yaml.

    Returns dict with keys: system, user_template
    """
    prompt_path = os.path.join(PROMPT_DIR, f'{name}.yaml')
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    return load_config(prompt_path)


def build_paper_context(paper, pdf_text='', extract_meta=None):
    """Build a stable paper context block shared by Phase 2 prompts."""
    image_count = 0
    representative_image = ''
    if extract_meta:
        image_count = extract_meta.get('image_count', 0) or 0
        representative_image = extract_meta.get('representative_image') or ''

    return (
        "=== PAPER CONTEXT ===\n"
        f"Title: {paper.get('title', '')}\n"
        f"Authors: {paper.get('authors', '')}\n"
        f"Published: {paper.get('date', paper.get('published', ''))}\n"
        f"Paper ID: {paper.get('arxiv_id', paper.get('paper_id', ''))}\n"
        f"Original URL: {paper.get('abs_url', '')}\n\n"
        "Abstract:\n"
        f"{paper.get('abstract', '')}\n\n"
        "Extracted figure metadata:\n"
        f"Representative figure extracted: {'true' if representative_image else 'false'}\n"
        f"Representative figure path: {representative_image}\n"
        f"Extracted image count: {image_count}\n\n"
        "PDF full text:\n"
        f"{pdf_text or ''}\n"
        "=== END PAPER CONTEXT ==="
    )


def _build_fields(paper, pdf_text='', extract_meta=None, language='en'):
    fields = {
        'title': paper.get('title', ''),
        'authors': paper.get('authors', ''),
        'published': paper.get('date', paper.get('published', '')),
        'arxiv_id': paper.get('arxiv_id', paper.get('paper_id', '')),
        'abs_url': paper.get('abs_url', ''),
        'abstract': paper.get('abstract', ''),
        'pdf_text': pdf_text or '',
        'paper_context': build_paper_context(paper, pdf_text, extract_meta),
    }

    if extract_meta and extract_meta.get('representative_image'):
        fields['has_representative_figure'] = 'true'
        if language == 'zh':
            fields['representative_figure_instruction'] = (
                '本文包含代表性图表。请在报告的 Key Findings 或 Method Overview '
                '部分描述该图表的内容、关键数据和意义。如果正文中提到了 Figure 编号，'
                '请在描述中引用该编号。'
            )
        else:
            fields['representative_figure_instruction'] = (
                'A representative figure was extracted. Describe its content, key data, '
                'and significance in the Key Findings or Method Overview section. If the '
                'paper text mentions a figure number, cite that figure number.'
            )
    else:
        fields['has_representative_figure'] = 'false'
        fields['representative_figure_instruction'] = (
            '本文未包含图表。' if language == 'zh' else 'No representative figure was extracted.'
        )

    return fields


def build_template_prompt(name, fields):
    prompt = load_prompt(name)
    return {
        'system_prompt': prompt['system'],
        'user_prompt': prompt['user_template'].format(**fields),
        'mode': name,
    }


def build_prompt(paper, name, pdf_text='', extract_meta=None, language='en'):
    """Build prompt from a named template."""
    fields = _build_fields(paper, pdf_text, extract_meta, language)
    return build_template_prompt(name, fields)


# ---------------------------------------------------------------------------
# Backward-compatible API (used by paper_cli.py and CLI)
# ---------------------------------------------------------------------------

def build_full_text_prompt(paper, config, pdf_text, extract_meta=None,
                           prompt_name='interpret_en', language='en'):
    """Build structured interpretation prompt."""
    return build_prompt(paper, prompt_name, pdf_text, extract_meta, language)


def build_brief_prompt(paper, config, pdf_text, extract_meta=None,
                       prompt_name='brief_en', language='en'):
    """Build brief/article-style prompt."""
    return build_prompt(paper, prompt_name, pdf_text, extract_meta, language)


def build_abstract_only_prompt(paper, config, prompt_name='interpret_en', language='en'):
    """Build abstract-only prompt without PDF full text."""
    return build_prompt(paper, prompt_name, '', None, language)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_cfg(cfg, path_str, default=None):
    parts = path_str.split('.')
    current = cfg
    for p in parts:
        if isinstance(current, dict):
            current = current.get(p)
        else:
            return default
    return current


# ---------------------------------------------------------------------------
# CLI (kept for backward compatibility)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Build LLM prompts for paper interpretation')
    parser.add_argument('--config', help='Path to config file (ignored for prompts, kept for compat)')
    parser.add_argument('--mode', choices=['full_text', 'abstract_only', 'brief'], default='full_text',
                        help='Interpretation mode')
    parser.add_argument('--lang', choices=['en', 'zh'], default='en',
                        help='Prompt language (default: en)')
    parser.add_argument('--pdf-text-file', help='File containing extracted PDF text')
    args = parser.parse_args()

    input_data = json.loads(sys.stdin.read())
    paper = input_data if isinstance(input_data, dict) else input_data[0]

    pdf_text = ''
    if args.pdf_text_file and os.path.exists(args.pdf_text_file):
        with open(args.pdf_text_file, 'r', encoding='utf-8') as f:
            pdf_text = f.read()

    full_prompt_name = 'interpret' if args.lang == 'zh' else 'interpret_en'
    brief_prompt_name = 'brief' if args.lang == 'zh' else 'brief_en'

    if args.mode == 'brief':
        result = build_brief_prompt(paper, {}, pdf_text,
                                    prompt_name=brief_prompt_name, language=args.lang)
    elif args.mode == 'full_text':
        result = build_full_text_prompt(paper, {}, pdf_text,
                                        prompt_name=full_prompt_name, language=args.lang)
    else:
        result = build_abstract_only_prompt(paper, {},
                                            prompt_name=full_prompt_name, language=args.lang)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Built {result['mode']} prompt: system={len(result['system_prompt'])}chars, user={len(result['user_prompt'])}chars",
          file=sys.stderr)


if __name__ == '__main__':
    main()