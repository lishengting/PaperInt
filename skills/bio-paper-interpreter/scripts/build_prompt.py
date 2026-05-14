#!/usr/bin/env python3
"""
Build LLM system and user prompts for paper interpretation.

Reads prompt templates from references/prompts/{name}.yaml, substitutes paper
fields into the user prompt template, and outputs a JSON object with
`system_prompt` and `user_prompt` fields.

Modes:
  - interpret: Structured technical report (Paper Understanding, Claims tables, etc.)
  - brief:     Narrative Chinese article (2000-2500 chars, reader-friendly)
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
            return json.load(f)
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except ImportError:
        with open(path, 'r', encoding='utf-8') as f:
            return _parse_simple_yaml(f.read())


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


def _build_fields(paper, pdf_text='', extract_meta=None):
    fields = {
        'title': paper.get('title', ''),
        'authors': paper.get('authors', ''),
        'published': paper.get('date', paper.get('published', '')),
        'arxiv_id': paper.get('arxiv_id', paper.get('paper_id', '')),
        'abs_url': paper.get('abs_url', ''),
        'abstract': paper.get('abstract', ''),
        'pdf_text': pdf_text or '',
    }

    if extract_meta and extract_meta.get('representative_image'):
        fields['has_representative_figure'] = 'true'
        fields['representative_figure_instruction'] = (
            '本文包含代表性图表。请在报告的 Key Findings 或 Method Overview '
            '部分描述该图表的内容、关键数据和意义。如果正文中提到了 Figure 编号，'
            '请在描述中引用该编号。'
        )
    else:
        fields['has_representative_figure'] = 'false'
        fields['representative_figure_instruction'] = '本文未包含图表。'

    return fields


def build_prompt(paper, name, pdf_text='', extract_meta=None):
    """Build prompt from a named template.

    Args:
        paper: dict with title, authors, abstract, etc.
        name: prompt name ('interpret' or 'brief')
        pdf_text: extracted PDF full text
        extract_meta: optional dict with image extraction metadata
            (representative_image, image_count, images_dir)

    Returns dict with keys: system_prompt, user_prompt, mode
    """
    prompt = load_prompt(name)
    fields = _build_fields(paper, pdf_text, extract_meta)
    return {
        'system_prompt': prompt['system'],
        'user_prompt': prompt['user_template'].format(**fields),
        'mode': name,
    }


# ---------------------------------------------------------------------------
# Backward-compatible API (used by paper_cli.py and CLI)
# ---------------------------------------------------------------------------

def build_full_text_prompt(paper, config, pdf_text, extract_meta=None):
    """Build structured interpretation prompt (uses interpret.yaml)."""
    return build_prompt(paper, 'interpret', pdf_text, extract_meta)


def build_brief_prompt(paper, config, pdf_text, extract_meta=None):
    """Build brief/article-style prompt (uses brief.yaml)."""
    return build_prompt(paper, 'brief', pdf_text, extract_meta)


def build_abstract_only_prompt(paper, config):
    """Build abstract-only prompt (falls back to interpret template without PDF)."""
    return build_prompt(paper, 'interpret', '')


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
    parser.add_argument('--pdf-text-file', help='File containing extracted PDF text')
    args = parser.parse_args()

    input_data = json.loads(sys.stdin.read())
    paper = input_data if isinstance(input_data, dict) else input_data[0]

    pdf_text = ''
    if args.pdf_text_file and os.path.exists(args.pdf_text_file):
        with open(args.pdf_text_file, 'r', encoding='utf-8') as f:
            pdf_text = f.read()

    if args.mode == 'brief':
        # Brief mode doesn't need config for prompts, but we still accept --config
        result = build_brief_prompt(paper, {}, pdf_text)
    elif args.mode == 'full_text':
        result = build_full_text_prompt(paper, {}, pdf_text)
    else:
        result = build_abstract_only_prompt(paper, {})

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Built {result['mode']} prompt: system={len(result['system_prompt'])}chars, user={len(result['user_prompt'])}chars",
          file=sys.stderr)


if __name__ == '__main__':
    main()