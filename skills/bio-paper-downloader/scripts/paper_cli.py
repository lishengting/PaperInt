#!/usr/bin/env python3
"""
Unified Paper CLI — search and download bioinformatics papers from arXiv and bioRxiv.

Usage modes:
  search   Search by keywords, download latest N papers
  find     Search by paper title
  get      Download a specific paper by URL
  list     List mode (--list flag on any command, no download)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if ':' in stripped and not stripped.startswith('-'):
            key_end = stripped.index(':')
            key = stripped[:key_end].strip()
            value_part = stripped[key_end + 1:].strip()
            if value_part:
                if value_part in ('true', 'false'):
                    result[key] = value_part == 'true'
                elif value_part.isdigit():
                    result[key] = int(value_part)
                elif _is_float(value_part):
                    result[key] = float(value_part)
                else:
                    result[key] = value_part.strip('"').strip("'")
            else:
                result[key] = {}
        elif stripped.startswith('- '):
            pass
        i += 1
    return result


def _is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def cfg(c, path, default=None):
    parts = path.split('.')
    for p in parts:
        if isinstance(c, dict):
            c = c.get(p)
        else:
            return default
        if c is None:
            return default
    return c


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', '_', str(name))[:200]


# ---------------------------------------------------------------------------
# arXiv API
# ---------------------------------------------------------------------------

def arxiv_search(query, config, max_results=50):
    """Search arXiv API with a raw query string."""
    search_url = cfg(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    user_agent = cfg(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = cfg(config, 'download.timeout_seconds', 60)

    url = f"{search_url}?search_query={urllib.request.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"

    req = urllib.request.Request(url, headers={'User-Agent': user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_data = resp.read().decode('utf-8')
    except Exception as e:
        print(f"Error querying arXiv: {e}", file=sys.stderr)
        return []

    ns = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        print(f"Error parsing arXiv XML: {e}", file=sys.stderr)
        return []

    papers = []
    for entry in root.findall('atom:entry', ns):
        id_elem = entry.find('atom:id', ns)
        title_elem = entry.find('atom:title', ns)
        published_elem = entry.find('atom:published', ns)
        summary_elem = entry.find('atom:summary', ns)

        if id_elem is None or title_elem is None:
            continue

        arxiv_url = (id_elem.text or '').strip()
        arxiv_id = arxiv_url.split('/abs/')[-1].split('v')[0] if '/abs/' in arxiv_url else ''

        if not arxiv_id:
            continue

        title = (title_elem.text or '').strip()
        published = (published_elem.text or '')[:10] if published_elem is not None else ''
        abstract = (summary_elem.text or '').strip() if summary_elem is not None else ''

        authors = []
        for author in entry.findall('atom:author', ns):
            name = author.find('atom:name', ns)
            if name is not None and name.text:
                authors.append(name.text)
        author_str = ', '.join(authors[:5])
        if len(authors) > 5:
            author_str += ' et al.'

        papers.append({
            'paper_id': arxiv_id,
            'source': 'arxiv',
            'doi': None,
            'arxiv_id': arxiv_id,
            'title': title,
            'authors': author_str,
            'abstract': abstract,
            'date': published,
            'category': '',
            'pdf_url': f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            'abs_url': f"https://arxiv.org/abs/{arxiv_id}",
        })

    return papers


def arxiv_search_by_keywords(keywords, config, max_results=50):
    """Search arXiv for papers matching all keywords."""
    # Build query: all keywords ANDed together
    query = ' AND '.join(f'all:{kw}' for kw in keywords)
    return arxiv_search(query, config, max_results)


def arxiv_search_by_title(title, config, max_results=10):
    """Search arXiv for papers matching a title."""
    query = f'ti:{title}'
    return arxiv_search(query, config, max_results)


# ---------------------------------------------------------------------------
# bioRxiv API
# ---------------------------------------------------------------------------

def biorxiv_search(keywords, config, max_results=100):
    """Search bioRxiv by keywords within the configured date range."""
    api_url = cfg(config, 'apis.biorxiv.details_url', '')
    date_range = cfg(config, 'search.date_range_days', 90)
    user_agent = cfg(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = cfg(config, 'download.timeout_seconds', 60)
    delay = cfg(config, 'download.request_delay_seconds', 3)

    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=date_range)).strftime('%Y-%m-%d')

    all_papers = []
    cursor = 0

    while cursor < max_results:
        url = api_url.format(start_date=start_date, end_date=end_date) + f"/{cursor}"
        req = urllib.request.Request(url, headers={'User-Agent': user_agent})

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"Error querying bioRxiv: {e}", file=sys.stderr)
            break

        papers = data.get('collection', [])
        if not papers:
            break

        keyword_lower = [k.lower() for k in keywords]

        for paper in papers:
            title = (paper.get('title', '')).lower()
            abstract = (paper.get('abstract', '')).lower()
            category = (paper.get('category', '')).lower()
            combined = f"{title} {abstract} {category}"

            if any(kw in combined for kw in keyword_lower):
                doi = paper.get('doi', '')
                all_papers.append({
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
                })

            if len(all_papers) >= max_results:
                break

        if len(all_papers) >= max_results:
            break

        cursor += 100
        time.sleep(delay)

    return all_papers[:max_results]


def biorxiv_search_by_title(title, config, max_results=100):
    """Search bioRxiv for papers matching a title (approx via keyword matching)."""
    keywords = [w.lower() for w in title.split() if len(w) > 2]
    papers = biorxiv_search(keywords, config, max_results)

    # Score by title similarity (simple word overlap)
    title_words = set(title.lower().split())
    for p in papers:
        p_title_words = set(p['title'].lower().split())
        overlap = len(title_words & p_title_words)
        p['_score'] = overlap / max(len(title_words), 1)

    papers.sort(key=lambda p: p.get('_score', 0), reverse=True)
    for p in papers:
        del p['_score']
    return papers


# ---------------------------------------------------------------------------
# Keyword filtering (from config)
# ---------------------------------------------------------------------------

def apply_keyword_filter(papers, config):
    """Apply include/exclude keyword filter from config."""
    include_kw = cfg(config, 'keywords.include', [])
    include_min = cfg(config, 'keywords.include_min_match', 2)
    exclude_kw = cfg(config, 'keywords.exclude', [])

    filtered = []
    for paper in papers:
        title = (paper.get('title', '')).lower()
        abstract = (paper.get('abstract', '')).lower()
        combined = f"{title} {abstract}"

        if any(kw in combined for kw in exclude_kw):
            continue

        matches = sum(1 for kw in include_kw if kw in combined)
        if matches >= include_min:
            filtered.append(paper)

    return filtered


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_arxiv(arxiv_id, config):
    """Download a PDF from arXiv. Returns bytes or None."""
    user_agent = cfg(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = cfg(config, 'download.timeout_seconds', 60)
    min_size = cfg(config, 'download.min_pdf_size_bytes', 10000)

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    req = urllib.request.Request(url, headers={'User-Agent': user_agent})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            if len(content) < min_size:
                return None
            return content
    except Exception as e:
        print(f"  Download error: {e}", file=sys.stderr)
        return None


def download_biorxiv(doi, config):
    """Download a PDF from bioRxiv. Returns bytes or None."""
    user_agent = cfg(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = cfg(config, 'download.timeout_seconds', 60)

    urls = [
        f"https://www.biorxiv.org/content/{doi}.full.pdf",
        f"https://www.biorxiv.org/content/{doi.replace('10.1101/', '')}.full.pdf",
        f"https://www.biorxiv.org/content/{doi}",
    ]

    headers = {
        'User-Agent': user_agent,
        'Accept': 'application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.biorxiv.org/',
    }

    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                if content.startswith(b'%PDF'):
                    return content
            time.sleep(1)
        except Exception:
            time.sleep(1)

    return None


def download_paper(paper, config, pdf_dir, metadata_dir, state):
    """Download a single paper's PDF and save metadata. Returns True on success."""
    paper_id = paper.get('paper_id', '')
    source = paper.get('source', '')

    if paper_id in state.get('downloaded', []):
        print(f"  [skip] already downloaded")
        return False

    safe_name = sanitize(paper_id)
    os.makedirs(pdf_dir, exist_ok=True)
    os.makedirs(metadata_dir, exist_ok=True)

    # Save metadata
    meta_path = os.path.join(metadata_dir, f"{safe_name}.json")
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(paper, f, ensure_ascii=False, indent=2)

    print(f"  Downloading: {paper.get('title', 'No title')[:80]}...")

    if source == 'arxiv':
        pdf_data = download_arxiv(paper.get('arxiv_id', paper_id), config)
    elif source == 'biorxiv':
        pdf_data = download_biorxiv(paper.get('doi', paper_id), config)
    else:
        print(f"  Unknown source: {source}", file=sys.stderr)
        return False

    if pdf_data:
        pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")
        with open(pdf_path, 'wb') as f:
            f.write(pdf_data)
        state.setdefault('downloaded', []).append(paper_id)
        print(f"  OK: {len(pdf_data)} bytes -> {pdf_path}")
        return True
    else:
        print(f"  FAILED")
        return False


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def cmd_search(args, config):
    """Search by keywords and download latest N papers."""
    source = args.source or cfg(config, 'search.default_source', 'arxiv')
    num = args.num or cfg(config, 'search.default_num', 1)
    keywords = args.keywords or cfg(config, 'keywords.include', [])

    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',') if k.strip()]

    print(f"Searching {source} for: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
    print(f"Requesting latest {num} paper(s)\n")

    if source == 'arxiv':
        papers = arxiv_search_by_keywords(keywords, config, max_results=max(num * 5, 20))
    elif source == 'biorxiv':
        papers = biorxiv_search(keywords, config, max_results=max(num * 5, 100))
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    if args.filter:
        before = len(papers)
        papers = apply_keyword_filter(papers, config)
        print(f"Config filter: {before} -> {len(papers)} papers\n")

    papers = papers[:num]

    if args.list:
        for i, p in enumerate(papers, 1):
            print(f"{i}. {p.get('title', 'No title')[:100]}")
            print(f"   {p.get('pdf_url', '')}")
            print(f"   {p.get('date', '?')} | {p.get('source', '')}\n")
        print(f"Total: {len(papers)} paper(s) (list only)")
        return 0

    state = load_state(args.state_file)
    success = 0
    for i, p in enumerate(papers, 1):
        print(f"[{i}/{len(papers)}] {p.get('paper_id', '?')}")
        if download_paper(p, config, args.pdf_dir, args.metadata_dir, state):
            success += 1
        if i < len(papers):
            time.sleep(cfg(config, 'download.request_delay_seconds', 3))

    save_state(args.state_file, state)
    print(f"\nDone: {success}/{len(papers)} downloaded, {len(state.get('downloaded', []))} total in state")
    return 0


def cmd_find(args, config):
    """Search by title and download best match."""
    source = args.source or cfg(config, 'search.default_source', 'arxiv')

    print(f"Searching {source} for title: {args.title}\n")

    if source == 'arxiv':
        papers = arxiv_search_by_title(args.title, config, max_results=10)
    elif source == 'biorxiv':
        papers = biorxiv_search_by_title(args.title, config)
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    papers = papers[:5]  # top 5 matches

    if not papers:
        print("No matching papers found.")
        return 1

    if args.list:
        for i, p in enumerate(papers, 1):
            print(f"{i}. {p.get('title', 'No title')[:100]}")
            print(f"   {p.get('pdf_url', '')}")
            print(f"   {p.get('date', '?')} | {p.get('source', '')}\n")
        return 0

    # Download the best match
    paper = papers[0]
    state = load_state(args.state_file)
    print(f"Best match: {paper['title'][:100]}")
    ok = download_paper(paper, config, args.pdf_dir, args.metadata_dir, state)
    save_state(args.state_file, state)
    print(f"\n{'Downloaded' if ok else 'Failed'}")
    return 0 if ok else 1


def cmd_get(args, config):
    """Download a paper by URL."""
    url = args.url

    # Parse URL to determine source and ID
    paper = None

    # arXiv URL patterns
    arxiv_patterns = [
        r'arxiv\.org/abs/([0-9]+\.[0-9]+)',
        r'arxiv\.org/pdf/([0-9]+\.[0-9]+)',
    ]
    for pat in arxiv_patterns:
        m = re.search(pat, url)
        if m:
            arxiv_id = m.group(1).rstrip('.pdf')
            print(f"Detected arXiv ID: {arxiv_id}")

            # Fetch metadata
            papers = arxiv_search(f'id:{arxiv_id}', config, max_results=1)
            if papers:
                paper = papers[0]
            else:
                # Minimal paper entry
                paper = {
                    'paper_id': arxiv_id,
                    'source': 'arxiv',
                    'doi': None,
                    'arxiv_id': arxiv_id,
                    'title': f'arXiv:{arxiv_id}',
                    'authors': '',
                    'abstract': '',
                    'date': '',
                    'category': '',
                    'pdf_url': f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                    'abs_url': f"https://arxiv.org/abs/{arxiv_id}",
                }
            break

    # bioRxiv URL patterns
    if paper is None:
        biorxiv_patterns = [
            r'biorxiv\.org/content/(10\.\d+/[\w.\-]+?)(?:\.full)?\.pdf',
            r'biorxiv\.org/content/(10\.\d+/[\w.\-]+)',
        ]
        for pat in biorxiv_patterns:
            m = re.search(pat, url)
            if m:
                doi = m.group(1)
                print(f"Detected bioRxiv DOI: {doi}")
                paper = {
                    'paper_id': doi,
                    'source': 'biorxiv',
                    'doi': doi,
                    'arxiv_id': None,
                    'title': f'bioRxiv:{doi}',
                    'authors': '',
                    'abstract': '',
                    'date': '',
                    'category': '',
                    'pdf_url': f"https://www.biorxiv.org/content/{doi}.full.pdf",
                    'abs_url': f"https://www.biorxiv.org/content/{doi}",
                }
                break

    if paper is None:
        # Generic PDF URL
        print(f"Treating as generic PDF URL: {url}")
        paper = {
            'paper_id': sanitize(url.split('/')[-1] or 'download'),
            'source': 'generic',
            'doi': None,
            'arxiv_id': None,
            'title': f'Download:{url}',
            'authors': '',
            'abstract': '',
            'date': '',
            'category': '',
            'pdf_url': url,
            'abs_url': url,
        }

    if args.list:
        print(f"URL: {paper['pdf_url']}")
        return 0

    state = load_state(args.state_file)
    ok = download_paper(paper, config, args.pdf_dir, args.metadata_dir, state)
    save_state(args.state_file, state)
    print(f"\n{'Downloaded' if ok else 'Failed'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {'downloaded': []}


def save_state(path, state):
    state['last_updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Search and download bioinformatics papers from arXiv and bioRxiv')
    parser.add_argument('--config', default='config.yaml', help='Path to config file')

    sub = parser.add_subparsers(dest='command', required=True)

    # --- search ---
    p_search = sub.add_parser('search', help='Search by keywords, download latest N papers')
    p_search.add_argument('--keywords', '-k', help='Comma-separated keywords (default: from config)')
    p_search.add_argument('--source', '-s', choices=['arxiv', 'biorxiv'],
                          help='Source to search (default: from config)')
    p_search.add_argument('--num', '-n', type=int, help='Number of papers (default: from config)')
    p_search.add_argument('--filter', '-f', action='store_true',
                          help='Apply config keyword filter')
    p_search.add_argument('--list', '-l', action='store_true',
                          help='List only, do not download')
    p_search.add_argument('--pdf-dir', default='data/pdf')
    p_search.add_argument('--metadata-dir', default='data/metadata')
    p_search.add_argument('--state-file', default='data/downloaded.json')

    # --- find ---
    p_find = sub.add_parser('find', help='Search by paper title')
    p_find.add_argument('--title', '-t', required=True, help='Paper title to search')
    p_find.add_argument('--source', '-s', choices=['arxiv', 'biorxiv'],
                        help='Source to search (default: from config)')
    p_find.add_argument('--list', '-l', action='store_true',
                        help='List only, do not download')
    p_find.add_argument('--pdf-dir', default='data/pdf')
    p_find.add_argument('--metadata-dir', default='data/metadata')
    p_find.add_argument('--state-file', default='data/downloaded.json')

    # --- get ---
    p_get = sub.add_parser('get', help='Download a specific paper by URL')
    p_get.add_argument('--url', '-u', required=True, help='Paper URL (arXiv or bioRxiv)')
    p_get.add_argument('--list', '-l', action='store_true',
                       help='List only, do not download')
    p_get.add_argument('--pdf-dir', default='data/pdf')
    p_get.add_argument('--metadata-dir', default='data/metadata')
    p_get.add_argument('--state-file', default='data/downloaded.json')

    args = parser.parse_args()
    config = load_config(args.config)

    if args.command == 'search':
        return cmd_search(args, config)
    elif args.command == 'find':
        return cmd_find(args, config)
    elif args.command == 'get':
        return cmd_get(args, config)
    return 1


if __name__ == '__main__':
    sys.exit(main())
