#!/usr/bin/env python3
"""
Build LLM system and user prompts for paper interpretation.

Reads a paper JSON object from stdin, selects the appropriate system prompt
from config based on interpretation mode, substitutes paper fields into
the user prompt template, and outputs a JSON object with `system_prompt`
and `user_prompt` fields.

Modes:
  - full_text: Deep interpretation from PDF full text (2000-2500 chars).
  - abstract_only: Interpretation from title+abstract only (1500-1800 chars).
"""
import argparse
import json
import os
import sys


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


def get_cfg(cfg, path_str, default=None):
    parts = path_str.split('.')
    current = cfg
    for p in parts:
        if isinstance(current, dict):
            current = current.get(p)
        else:
            return default
    return current


FULL_TEXT_USER_TEMPLATE = (
    "请深度解读以下生物信息学论文（基于PDF全文）：\n\n"
    "【论文元数据】\n"
    "标题: {title}\n"
    "作者: {authors}\n"
    "发表日期: {published}\n"
    "arXiv ID: {arxiv_id}\n"
    "原文链接: {abs_url}\n\n"
    "【论文摘要】\n"
    "{abstract}\n\n"
    "【PDF全文内容（前15000字符）】\n"
    "{pdf_text}\n\n"
    "请基于以上信息生成一篇深度解读文章，要求：\n"
    "1. 深入分析论文的核心方法和技术创新\n"
    "2. 解释实验设计和关键结果\n"
    "3. 评估研究的贡献和局限性\n"
    "4. 讨论对实际应用的指导意义\n"
    "5. 提供精炼的总结（3-5条bullet points）\n\n"
    "文章长度2000-2500字，要求专业、深入、有洞察力。"
)

ABSTRACT_ONLY_USER_TEMPLATE = (
    "请解读以下预印本论文（基于标题和摘要）：\n\n"
    "【论文标题】\n"
    "{title}\n\n"
    "【作者】\n"
    "{authors}\n\n"
    "【发表日期】\n"
    "{published}\n\n"
    "【DOI】\n"
    "{doi}\n\n"
    "【分类】\n"
    "{category}\n\n"
    "【摘要】\n"
    "{abstract}\n\n"
    "请生成一篇中文解读文章。要求：\n"
    "1. 标题突出创新点\n"
    "2. 解释研究背景和动机\n"
    "3. 用通俗语言解释核心方法（基于摘要推断）\n"
    "4. 总结主要发现和意义\n"
    "5. 精炼总结（3-5条bullet points）\n"
    "6. 明确标注哪些是摘要明确提到的，哪些是合理推断\n\n"
    "文章长度1200-1500字。"
)


def build_full_text_prompt(paper, config, pdf_text):
    """Build prompt for full-text PDF interpretation."""
    system_prompt = get_cfg(config, 'system_prompts.full_text', '')

    fields = {
        'title': paper.get('title', ''),
        'authors': paper.get('authors', ''),
        'published': paper.get('date', paper.get('published', '')),
        'arxiv_id': paper.get('arxiv_id', paper.get('paper_id', '')),
        'abs_url': paper.get('abs_url', ''),
        'abstract': paper.get('abstract', ''),
        'pdf_text': pdf_text or '',
    }

    user_prompt = FULL_TEXT_USER_TEMPLATE.format(**fields)

    return {
        'system_prompt': system_prompt,
        'user_prompt': user_prompt,
        'mode': 'full_text',
    }


def build_abstract_only_prompt(paper, config):
    """Build prompt for abstract-only interpretation."""
    system_prompt = get_cfg(config, 'system_prompts.abstract_only', '')

    fields = {
        'title': paper.get('title', ''),
        'authors': paper.get('authors', ''),
        'published': paper.get('date', paper.get('published', '')),
        'doi': paper.get('doi', ''),
        'category': paper.get('category', ''),
        'abstract': paper.get('abstract', ''),
    }

    user_prompt = ABSTRACT_ONLY_USER_TEMPLATE.format(**fields)

    return {
        'system_prompt': system_prompt,
        'user_prompt': user_prompt,
        'mode': 'abstract_only',
    }


def main():
    parser = argparse.ArgumentParser(description='Build LLM prompts for paper interpretation')
    parser.add_argument('--config', required=True, help='Path to config file')
    parser.add_argument('--mode', choices=['full_text', 'abstract_only'], required=True,
                        help='Interpretation mode')
    parser.add_argument('--pdf-text-file', help='File containing extracted PDF text')
    args = parser.parse_args()

    config = load_config(args.config)

    input_data = json.loads(sys.stdin.read())
    paper = input_data if isinstance(input_data, dict) else input_data[0]

    pdf_text = ''
    if args.pdf_text_file and os.path.exists(args.pdf_text_file):
        with open(args.pdf_text_file, 'r', encoding='utf-8') as f:
            pdf_text = f.read()

    if args.mode == 'full_text':
        result = build_full_text_prompt(paper, config, pdf_text)
    else:
        result = build_abstract_only_prompt(paper, config)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Built {args.mode} prompt: system={len(result['system_prompt'])}chars, user={len(result['user_prompt'])}chars",
          file=sys.stderr)


if __name__ == '__main__':
    main()
