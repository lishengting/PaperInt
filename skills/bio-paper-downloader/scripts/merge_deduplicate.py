#!/usr/bin/env python3
"""
Merge and deduplicate paper lists from multiple sources.

Reads paper JSON files from bioRxiv and arXiv, normalizes to a common schema,
deduplicates by DOI or arXiv ID, applies keyword filtering from config,
and outputs a single merged paper list.
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


def get_config_value(cfg, path_str, default=None):
    parts = path_str.split('.')
    current = cfg
    for p in parts:
        if isinstance(current, dict):
            current = current.get(p)
        else:
            return default
        if current is None:
            return default
    return current


def normalize_doi(doi):
    """Normalize DOI for comparison."""
    if not doi:
        return None
    return doi.strip().lower()


def merge_and_deduplicate(input_files, config):
    """Merge paper lists from multiple input files."""
    include_keywords = get_config_value(config, 'keywords.include', [])
    include_min = get_config_value(config, 'keywords.include_min_match', 2)
    exclude_keywords = get_config_value(config, 'keywords.exclude', [])

    all_papers = []

    for filepath in input_files:
        if not os.path.exists(filepath):
            print(f"Warning: file not found: {filepath}", file=sys.stderr)
            continue

        with open(filepath, 'r', encoding='utf-8') as f:
            try:
                papers = json.load(f)
            except json.JSONDecodeError as e:
                print(f"Error reading {filepath}: {e}", file=sys.stderr)
                continue

        if isinstance(papers, dict):
            papers = papers.get('papers', papers.get('collection', []))

        if not isinstance(papers, list):
            print(f"Warning: {filepath} does not contain a paper list", file=sys.stderr)
            continue

        all_papers.extend(papers)

    # Deduplicate
    seen_dois = set()
    seen_arxiv_ids = set()
    merged = []

    for paper in all_papers:
        doi = normalize_doi(paper.get('doi'))
        arxiv_id = paper.get('arxiv_id', '')
        paper_id = paper.get('paper_id', doi or arxiv_id)

        if doi and doi in seen_dois:
            continue
        if arxiv_id and arxiv_id in seen_arxiv_ids:
            continue

        if doi:
            seen_dois.add(doi)
        if arxiv_id:
            seen_arxiv_ids.add(arxiv_id)

        # Re-apply keyword filtering on merged papers
        title = (paper.get('title', '')).lower()
        abstract = (paper.get('abstract', '')).lower()
        combined = f"{title} {abstract}"

        if any(kw in combined for kw in exclude_keywords):
            continue

        match_count = sum(1 for kw in include_keywords if kw in combined)
        if match_count < include_min:
            continue

        merged.append(paper)

    return merged


def main():
    parser = argparse.ArgumentParser(description='Merge and deduplicate paper lists')
    parser.add_argument('--config', required=True, help='Path to config file')
    parser.add_argument('--inputs', nargs='+', required=True, help='Input JSON files')
    parser.add_argument('--output', help='Output JSON file path')
    args = parser.parse_args()

    config = load_config(args.config)
    merged = merge_and_deduplicate(args.inputs, config)

    output = json.dumps(merged, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"Merged {len(merged)} unique papers, saved to {args.output}")
    else:
        print(output)

    # Summary stats
    sources = {}
    for p in merged:
        s = p.get('source', 'unknown')
        sources[s] = sources.get(s, 0) + 1
    print(f"Merged papers by source: {sources}", file=sys.stderr)
    print(f"Total unique papers: {len(merged)}", file=sys.stderr)


if __name__ == '__main__':
    main()
