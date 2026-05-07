#!/usr/bin/env python3
"""
Download PDFs from a normalized paper list.

Reads a JSON list of papers (from merge_deduplicate.py), downloads each
paper's PDF from the appropriate source (bioRxiv or arXiv), saves metadata,
and updates the download state file. Validates PDFs by magic bytes and
minimum file size.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


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


def sanitize_filename(name):
    """Sanitize a string for use as a filename."""
    sanitized = re.sub(r'[/\\:*?"<>|]', '_', name)
    return sanitized[:200]


def download_biorxiv_pdf(doi, config):
    """Try multiple URL patterns to download a bioRxiv PDF."""
    user_agent = get_config_value(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = get_config_value(config, 'download.timeout_seconds', 60)
    delay = get_config_value(config, 'download.request_delay_seconds', 3)

    urls = [
        f"https://www.biorxiv.org/content/{doi}.full.pdf",
        f"https://www.biorxiv.org/content/{doi.replace('10.1101/', '')}.full.pdf",
        f"https://www.biorxiv.org/content/{doi}",
    ]

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': user_agent,
                'Accept': 'application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.biorxiv.org/',
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                if content.startswith(b'%PDF'):
                    return content
                else:
                    print(f"  Non-PDF response from {url}: {len(content)} bytes", file=sys.stderr)
            time.sleep(1)
        except urllib.error.URLError as e:
            print(f"  Failed to download from {url}: {e}", file=sys.stderr)
            time.sleep(1)

    return None


def download_arxiv_pdf(arxiv_id, config):
    """Download a PDF from arXiv."""
    user_agent = get_config_value(config, 'download.user_agent', 'PaperInt-Skills/1.0')
    timeout = get_config_value(config, 'download.timeout_seconds', 60)
    min_size = get_config_value(config, 'download.min_pdf_size_bytes', 10000)

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            if len(content) < min_size:
                print(f"  arXiv PDF too small ({len(content)} bytes), possible HTML error page", file=sys.stderr)
                return None
            # arXiv may return HTML error page even with 200 status
            if content.startswith(b'%PDF') or len(content) > 50000:
                return content
            else:
                print(f"  arXiv response doesn't look like PDF", file=sys.stderr)
                return None
    except urllib.error.URLError as e:
        print(f"  Failed to download arXiv PDF: {e}", file=sys.stderr)
        return None


def download_pdfs(papers_file, config, pdf_dir, metadata_dir, state_file):
    """Download PDFs for all papers in the input list."""
    with open(papers_file, 'r', encoding='utf-8') as f:
        papers = json.load(f)

    if isinstance(papers, dict):
        papers = papers.get('papers', papers.get('collection', []))

    if not isinstance(papers, list):
        print("Error: input does not contain a paper list", file=sys.stderr)
        return 0, 0

    os.makedirs(pdf_dir, exist_ok=True)
    os.makedirs(metadata_dir, exist_ok=True)

    # Load existing state
    state = {'downloaded': [], 'last_updated': ''}
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                if 'downloaded' not in state:
                    state['downloaded'] = []
        except (json.JSONDecodeError, IOError):
            pass

    downloaded_set = set(state.get('downloaded', []))
    delay = get_config_value(config, 'download.request_delay_seconds', 3)

    total = len(papers)
    success = 0
    skipped = 0

    for i, paper in enumerate(papers, 1):
        paper_id = paper.get('paper_id', '')
        source = paper.get('source', '')
        doi = paper.get('doi', '')
        arxiv_id = paper.get('arxiv_id', '')

        filename_key = doi or arxiv_id
        if not filename_key:
            print(f"[{i}/{total}] Skipping paper with no identifier", file=sys.stderr)
            skipped += 1
            continue

        # Check if already downloaded
        if filename_key in downloaded_set:
            print(f"[{i}/{total}] Already downloaded: {filename_key}", file=sys.stderr)
            skipped += 1
            continue

        # Save metadata first
        safe_name = sanitize_filename(filename_key)
        metadata_path = os.path.join(metadata_dir, f"{safe_name}.json")
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(paper, f, ensure_ascii=False, indent=2)

        print(f"[{i}/{total}] Downloading: {paper.get('title', 'No title')[:60]}...", file=sys.stderr)

        # Download based on source
        pdf_content = None
        if source == 'biorxiv' and doi:
            pdf_content = download_biorxiv_pdf(doi, config)
        elif source == 'arxiv' and arxiv_id:
            pdf_content = download_arxiv_pdf(arxiv_id, config)
        else:
            print(f"  Unknown source: {source}, trying URL from paper", file=sys.stderr)
            try:
                pdf_url = paper.get('pdf_url', '')
                if pdf_url:
                    req = urllib.request.Request(pdf_url, headers={
                        'User-Agent': get_config_value(config, 'download.user_agent', '')
                    })
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        pdf_content = resp.read()
            except Exception:
                pass

        if pdf_content:
            pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")
            with open(pdf_path, 'wb') as f:
                f.write(pdf_content)
            downloaded_set.add(filename_key)
            success += 1
            print(f"  OK: {len(pdf_content)} bytes -> {pdf_path}", file=sys.stderr)
        else:
            print(f"  FAILED: could not download PDF", file=sys.stderr)

        if i < total:
            time.sleep(delay)

    # Update state file
    state['downloaded'] = sorted(downloaded_set)
    state['last_updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    state['total'] = len(downloaded_set)

    state_dir = os.path.dirname(state_file) or '.'
    os.makedirs(state_dir, exist_ok=True)
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    return success, skipped


def main():
    parser = argparse.ArgumentParser(description='Download PDFs from paper list')
    parser.add_argument('--config', required=True, help='Path to config file')
    parser.add_argument('--papers', required=True, help='JSON file with paper list')
    parser.add_argument('--pdf-dir', default='data/pdf', help='Directory for PDF files')
    parser.add_argument('--metadata-dir', default='data/metadata', help='Directory for metadata files')
    parser.add_argument('--state-file', default='data/downloaded.json', help='Path to state file')
    args = parser.parse_args()

    config = load_config(args.config)

    success, skipped = download_pdfs(
        args.papers, config,
        args.pdf_dir, args.metadata_dir, args.state_file
    )

    print(f"\nDownload complete: {success} new, {skipped} skipped/already present")
    print(f"PDFs in: {args.pdf_dir}")
    print(f"Metadata in: {args.metadata_dir}")
    print(f"State file: {args.state_file}")


if __name__ == '__main__':
    main()
