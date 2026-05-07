#!/usr/bin/env python3
"""
Apply bioinformatics relevance filtering to a paper.

Reads a paper JSON object from stdin, checks title+abstract against
configured include/exclude keywords, and outputs the paper JSON with
a `relevance` field added.

Logic:
  - Exclude keywords checked first: any match -> rejected immediately.
  - Include keywords then counted. Must meet include_min_match threshold.
  - Papers with no title and no abstract are rejected.
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


def check_relevance(paper, config):
    """Check if a paper is bioinformatics-relevant."""
    title = (paper.get('title', '')).lower()
    abstract = (paper.get('abstract', '')).lower()
    combined = f"{title} {abstract}"

    include_keywords = get_cfg(config, 'keywords.include', [])
    include_min = get_cfg(config, 'keywords.include_min_match', 2)
    exclude_keywords = get_cfg(config, 'keywords.exclude', [])

    # Empty content cannot be filtered -> reject
    if not title.strip() and not abstract.strip():
        return {
            'passed': False,
            'reason': 'no_content',
            'include_matches': [],
            'exclude_matches': [],
        }

    # Check exclusions first
    exclude_matches = [kw for kw in exclude_keywords if kw in combined]
    if exclude_matches:
        return {
            'passed': False,
            'reason': 'excluded',
            'include_matches': [],
            'exclude_matches': exclude_matches,
        }

    # Count includes
    include_matches = [kw for kw in include_keywords if kw in combined]
    passed = len(include_matches) >= include_min

    return {
        'passed': passed,
        'reason': 'ok' if passed else f'insufficient_matches_{len(include_matches)}_lt_{include_min}',
        'include_matches': include_matches,
        'exclude_matches': [],
    }


def main():
    parser = argparse.ArgumentParser(description='Filter papers by bioinformatics relevance')
    parser.add_argument('--config', required=True, help='Path to config file')
    args = parser.parse_args()

    config = load_config(args.config)

    input_data = json.loads(sys.stdin.read())

    # Handle both single paper and paper list
    if isinstance(input_data, list):
        for paper in input_data:
            paper['relevance'] = check_relevance(paper, config)
        print(json.dumps(input_data, ensure_ascii=False, indent=2))
    else:
        input_data['relevance'] = check_relevance(input_data, config)
        print(json.dumps(input_data, ensure_ascii=False, indent=2))

    # Count summary to stderr
    papers = input_data if isinstance(input_data, list) else [input_data]
    passed = sum(1 for p in papers if p.get('relevance', {}).get('passed'))
    print(f"Relevance filter: {passed}/{len(papers)} passed", file=sys.stderr)


if __name__ == '__main__':
    main()
