#!/usr/bin/env python3
"""
Match topic tags against paper content using configured regex patterns.

Reads a paper JSON object from stdin, matches title+abstract against the
tag regex patterns defined in config.yaml, and outputs the paper JSON
with a `matched_tags` field added. Base tags are always included.
"""
import argparse
import json
import os
import re
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


def match_tags(paper, config):
    """Match tags against paper title+abstract."""
    title = (paper.get('title', '')).lower()
    abstract = (paper.get('abstract', '')).lower()
    combined = f"{title} {abstract}"

    tag_defs = get_cfg(config, 'tags.definitions', [])
    base_tag_ids = get_cfg(config, 'tags.base_tag_ids', [2, 9])
    ai_parent_id = get_cfg(config, 'tags.ai_parent_tag_id', 1)

    matched = []
    matched_labels = []

    for tag_def in tag_defs:
        tag_id = tag_def.get('id')
        label = tag_def.get('label', '')
        patterns = tag_def.get('patterns', [])

        for pattern in patterns:
            try:
                if re.search(pattern, combined):
                    if tag_id not in matched:
                        matched.append(tag_id)
                        matched_labels.append(label)
                    break
            except re.error:
                print(f"Warning: invalid regex pattern for tag {tag_id}: {pattern}", file=sys.stderr)

    # Add AI parent tag if any ML/DL/LLM/AF tags matched
    ai_tags = {11, 12, 28, 29}
    if any(t in ai_tags for t in matched):
        if ai_parent_id not in matched:
            matched.append(ai_parent_id)

    # Always prepend base tags
    result_tags = base_tag_ids.copy()
    for t in matched:
        if t not in result_tags:
            result_tags.append(t)

    return {
        'tag_ids': result_tags,
        'matched_labels': matched_labels,
    }


def main():
    parser = argparse.ArgumentParser(description='Match topic tags to paper content')
    parser.add_argument('--config', required=True, help='Path to config file')
    args = parser.parse_args()

    config = load_config(args.config)

    input_data = json.loads(sys.stdin.read())

    if isinstance(input_data, list):
        for paper in input_data:
            paper['matched_tags'] = match_tags(paper, config)
        print(json.dumps(input_data, ensure_ascii=False, indent=2))
    else:
        input_data['matched_tags'] = match_tags(input_data, config)
        print(json.dumps(input_data, ensure_ascii=False, indent=2))

    # Summary to stderr
    papers = input_data if isinstance(input_data, list) else [input_data]
    for p in papers:
        tags = p.get('matched_tags', {})
        n_tags = len(tags.get('tag_ids', []))
        labels = tags.get('matched_labels', [])
        pid = p.get('paper_id', '?')
        print(f"Tags for {pid}: {n_tags} tags, labels: {labels}", file=sys.stderr)


if __name__ == '__main__':
    main()
