#!/usr/bin/env python3
"""
Search arXiv API for papers in configured categories.

Query the arXiv API for recent papers in bioinformatics-related categories,
filter results by configured keywords, and output normalized paper JSON.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime


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
    """Parse a minimal YAML subset."""
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


def search_arxiv(config, max_results=50, state_file=None):
    """Query arXiv API and return filtered paper list."""
    categories = get_config_value(config, 'apis.arxiv.categories', ['q-bio.BM', 'q-bio.GN'])
    search_url = get_config_value(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    request_delay = get_config_value(config, 'download.request_delay_seconds', 3)
    timeout = get_config_value(config, 'download.timeout_seconds', 60)
    user_agent = get_config_value(config, 'download.user_agent', 'PaperInt-Skills/1.0')

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

    all_papers = []
    seen_ids = set()

    for category in categories:
        # Build query: category + sort by submitted date descending
        query = f"cat:{category}"
        query_url = f"{search_url}?search_query={urllib.request.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"

        req = urllib.request.Request(query_url, headers={'User-Agent': user_agent})

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read().decode('utf-8')
        except urllib.error.URLError as e:
            print(f"Error querying arXiv API for {category}: {e}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            print(f"Error parsing arXiv XML for {category}: {e}", file=sys.stderr)
            continue

        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'arxiv': 'http://arxiv.org/schemas/atom',
        }

        for entry in root.findall('atom:entry', ns):
            id_elem = entry.find('atom:id', ns)
            title_elem = entry.find('atom:title', ns)
            published_elem = entry.find('atom:published', ns)
            summary_elem = entry.find('atom:summary', ns)

            if id_elem is None or title_elem is None:
                continue

            arxiv_url = id_elem.text.strip() if id_elem.text else ''
            arxiv_id = arxiv_url.split('/abs/')[-1].split('v')[0] if '/abs/' in arxiv_url else ''

            if not arxiv_id:
                continue

            if arxiv_id in seen_ids:
                continue
            seen_ids.add(arxiv_id)

            if arxiv_id in downloaded_ids:
                continue

            title = title_elem.text.strip() if title_elem.text else ''
            published = published_elem.text[:10] if published_elem is not None and published_elem.text else ''
            abstract = summary_elem.text.strip() if summary_elem is not None and summary_elem.text else ''

            # Get authors
            authors = []
            for author in entry.findall('atom:author', ns):
                name = author.find('atom:name', ns)
                if name is not None and name.text:
                    authors.append(name.text)

            author_str = ', '.join(authors[:5])
            if len(authors) > 5:
                author_str += ' et al.'

            # Get primary category
            primary_cat = ''
            cat_elem = entry.find('arxiv:primary_category', ns)
            if cat_elem is not None:
                primary_cat = cat_elem.get('term', '')

            # Keyword filtering
            text_for_matching = f"{title.lower()} {abstract.lower()}"

            if any(kw in text_for_matching for kw in exclude_keywords):
                continue

            match_count = sum(1 for kw in include_keywords if kw in text_for_matching)
            if match_count < include_min:
                continue

            all_papers.append({
                'paper_id': arxiv_id,
                'source': 'arxiv',
                'doi': None,
                'arxiv_id': arxiv_id,
                'title': title,
                'authors': author_str,
                'abstract': abstract,
                'date': published,
                'category': primary_cat or category,
                'pdf_url': f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                'abs_url': f"https://arxiv.org/abs/{arxiv_id}",
            })

        time.sleep(request_delay)

    return all_papers


def main():
    parser = argparse.ArgumentParser(description='Search arXiv for bioinformatics papers')
    parser.add_argument('--config', required=True, help='Path to config.yaml (or .json)')
    parser.add_argument('--max', type=int, default=50, help='Max results per category')
    parser.add_argument('--state-file', default='data/downloaded.json', help='Path to state file')
    parser.add_argument('--output', help='Output JSON file path')
    args = parser.parse_args()

    config = load_config(args.config)

    max_results = args.max
    papers = search_arxiv(config, max_results, args.state_file)

    output = json.dumps(papers, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"Found {len(papers)} papers, saved to {args.output}")
    else:
        print(output)

    print(f"arXiv: {len(papers)} papers found", file=sys.stderr)


if __name__ == '__main__':
    main()
