#!/usr/bin/env python3
"""
Search bioRxiv API for papers matching configured keywords.

Query the bioRxiv content API by date range, filter results by keyword
matching in title/abstract/category, and output normalized paper JSON.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta


def load_config(path):
    """Load config from JSON or YAML. Tries PyYAML first, falls back to simple parser."""
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
    """Parse a minimal YAML subset: top-level mappings, lists, scalars."""
    import re

    result = {}
    current_key = None
    current_list = None
    current_mapping = None
    list_key = None
    in_mapping = None
    indent_level = 0

    lines = content.split('\n')
    i = 0
    stack = [(result, 0, None)]

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith('#'):
            i += 1
            continue

        indent = len(line) - len(line.lstrip())

        # Pop stack items with greater indent
        while len(stack) > 1 and indent <= stack[-1][1]:
            stack.pop()

        parent = stack[-1][0]

        # Top-level key-value
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
                # Check next line for block scalar
                next_i = i + 1
                if next_i < len(lines):
                    next_line = lines[next_i]
                    next_indent = len(next_line) - len(next_line.lstrip())
                    next_stripped = next_line.strip()
                    if next_indent > indent and next_stripped in ('|-', '|', '>'):
                        # Block scalar
                        content_lines = []
                        j = next_i + 1
                        while j < len(lines):
                            block_line = lines[j]
                            block_indent = len(block_line) - len(block_line.lstrip())
                            if block_indent <= next_indent and block_line.strip():
                                break
                            content_lines.append(block_line.rstrip())
                            j += 1
                        # Strip leading indent
                        min_indent = min(
                            (len(l) - len(l.lstrip()))
                            for l in content_lines
                            if l.strip()
                        ) if content_lines else 0
                        clean = [
                            l[min_indent:] if l.strip() else ''
                            for l in content_lines
                        ]
                        parent[key] = '\n'.join(clean)
                        i = j
                        continue
                    elif next_indent > indent and next_stripped.startswith('-'):
                        # List value
                        parent[key] = []
                        stack.append((parent[key], indent, key))
                        i = next_i
                        continue
                    else:
                        # Nested mapping
                        parent[key] = {}
                        indent_level = indent
                        i = next_i
                        continue
                else:
                    # Could be a nested mapping starting on next line
                    parent[key] = {}

            stack.pop()
            stack.append((parent, indent, None))

        elif stripped.startswith('- '):
            list_value = stripped[2:].strip().strip('"').strip("'")
            if isinstance(parent, list):
                parent.append(list_value)
            elif isinstance(parent, dict) and stack[-1][2]:
                parent.setdefault(stack[-1][2], []).append(list_value)

        i += 1

    return result


def _is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def get_config_value(cfg, path_str, default=None):
    """Resolve a dotted path like 'download.date_range_days' in a nested dict."""
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


def search_biorxiv(config, start_date, end_date, state_file=None):
    """Query bioRxiv API and return filtered paper list."""
    api_url = get_config_value(config, 'apis.biorxiv.details_url', '')
    url = api_url.format(start_date=start_date, end_date=end_date)

    request_delay = get_config_value(config, 'download.request_delay_seconds', 3)
    timeout = get_config_value(config, 'download.timeout_seconds', 30)
    user_agent = get_config_value(config, 'download.user_agent', 'PaperInt-Skills/1.0')

    req = urllib.request.Request(url, headers={'User-Agent': user_agent})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        print(f"Error querying bioRxiv API: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing bioRxiv response: {e}", file=sys.stderr)
        return []

    papers = data.get('collection', [])
    include_keywords = get_config_value(config, 'keywords.include', [])
    include_min = get_config_value(config, 'keywords.include_min_match', 2)
    exclude_keywords = get_config_value(config, 'keywords.exclude', [])

    # Load state for dedup
    downloaded_ids = set()
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                downloaded_ids = set(state.get('downloaded', []))
        except (json.JSONDecodeError, IOError):
            pass

    results = []
    for paper in papers:
        doi = paper.get('doi', '')
        if not doi:
            continue

        if doi in downloaded_ids:
            continue

        title = (paper.get('title', '')).lower()
        abstract = (paper.get('abstract', '')).lower()
        category = (paper.get('category', '')).lower()
        combined = f"{title} {abstract} {category}"

        # Exclude check first
        if any(kw in combined for kw in exclude_keywords):
            continue

        # Include check
        match_count = sum(1 for kw in include_keywords if kw in combined)
        if match_count < include_min:
            continue

        results.append({
            'paper_id': doi,
            'source': 'biorxiv',
            'doi': doi,
            'arxiv_id': None,
            'title': paper.get('title', ''),
            'authors': paper.get('authors', ''),
            'abstract': paper.get('abstract', ''),
            'date': paper.get('date', ''),
            'category': paper.get('category', ''),
            'pdf_url': f"https://www.biorxiv.org/content/{doi}.full.pdf",
            'abs_url': f"https://www.biorxiv.org/content/{doi}",
            'version': paper.get('version', ''),
        })

    time.sleep(request_delay)
    return results


def main():
    parser = argparse.ArgumentParser(description='Search bioRxiv for bioinformatics papers')
    parser.add_argument('--config', required=True, help='Path to config.yaml (or .json)')
    parser.add_argument('--start', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', help='End date (YYYY-MM-DD)')
    parser.add_argument('--days', type=int, help='Days to look back from today')
    parser.add_argument('--state-file', default='data/downloaded.json', help='Path to state file')
    parser.add_argument('--output', help='Output JSON file path')
    args = parser.parse_args()

    config = load_config(args.config)

    if args.start and args.end:
        start_date = args.start
        end_date = args.end
    elif args.days:
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')
    else:
        days = get_config_value(config, 'download.date_range_days', 7)
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    papers = search_biorxiv(config, start_date, end_date, args.state_file)

    output = json.dumps(papers, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"Found {len(papers)} papers, saved to {args.output}")
    else:
        print(output)

    print(f"bioRxiv: {len(papers)} papers found ({start_date} to {end_date})", file=sys.stderr)


if __name__ == '__main__':
    main()
