#!/usr/bin/env python3
"""
Unified Paper CLI — search and download bioinformatics papers.

Sources:  arxiv  |  biorxiv  |  medrxiv  |  pubmed

Usage:
  paper_cli.py search [-k keywords] [-s source] [-n N] [-l]
  paper_cli.py find   -t title    [-s source] [-l]
  paper_cli.py get    -u url      [-l]

All commands accept -l/--list to preview without downloading.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Config helpers
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
        return _simple_yaml(path)


def _simple_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    result = {}
    for line in content.split('\n'):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if ':' in s and not s.startswith('-'):
            k, _, v = s.partition(':')
            v = v.strip().strip('"').strip("'")
            if v in ('true', 'false'):
                result[k.strip()] = v == 'true'
            elif v.isdigit():
                result[k.strip()] = int(v)
            else:
                result[k.strip()] = v
    return result


def cfg(c, path, default=None):
    for p in path.split('.'):
        if isinstance(c, dict):
            c = c.get(p)
        else:
            return default
        if c is None:
            return default
    return c


def ua(config):
    return cfg(config, 'download.user_agent', 'PaperInt-Skills/1.0')


def tout(config):
    return cfg(config, 'download.timeout_seconds', 60)


def delay(config):
    return cfg(config, 'download.request_delay_seconds', 3)


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', '_', str(name))[:200]


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def load_state(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {'downloaded': []}


def save_state(path, state):
    state['last_updated'] = datetime.now().isoformat()
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Common paper normalizer
# ---------------------------------------------------------------------------

def make_paper(source, paper_id, title, authors, abstract, date, category,
               pdf_url, abs_url, doi=None, arxiv_id=None, pmid=None, extra=None):
    p = {
        'paper_id': paper_id,
        'source': source,
        'doi': doi,
        'arxiv_id': arxiv_id,
        'pmid': pmid,
        'title': title,
        'authors': authors,
        'abstract': abstract,
        'date': date,
        'category': category,
        'pdf_url': pdf_url,
        'abs_url': abs_url,
    }
    if extra:
        p.update(extra)
    return p


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def arxiv_api(query, config, max_results=50):
    base = cfg(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    url = f"{base}?search_query={urllib.parse.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with urllib.request.urlopen(req, timeout=tout(config)) as r:
            return r.read().decode('utf-8')
    except Exception as e:
        print(f"  arXiv error: {e}", file=sys.stderr)
        return None


def arxiv_parse(xml_data):
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return []

    papers = []
    for e in root.findall('a:entry', ns):
        i = e.find('a:id', ns)
        t = e.find('a:title', ns)
        if i is None or t is None:
            continue
        arxiv_url = (i.text or '').strip()
        aid = arxiv_url.split('/abs/')[-1].split('v')[0] if '/abs/' in arxiv_url else ''
        if not aid:
            continue
        title = (t.text or '').strip()
        pub = e.find('a:published', ns)
        date = (pub.text or '')[:10] if pub is not None else ''
        s = e.find('a:summary', ns)
        abstract = (s.text or '').strip() if s is not None else ''
        authors = []
        for a in e.findall('a:author', ns):
            n = a.find('a:name', ns)
            if n is not None and n.text:
                authors.append(n.text)
        au = ', '.join(authors[:5])
        if len(authors) > 5:
            au += ' et al.'

        papers.append(make_paper(
            'arxiv', aid, title, au, abstract, date, '',
            f"https://arxiv.org/pdf/{aid}.pdf",
            f"https://arxiv.org/abs/{aid}",
            arxiv_id=aid))
    return papers


def arxiv_search(keywords, config, max_results=50):
    q = ' AND '.join(f'all:"{kw}"' for kw in keywords)
    xml_data = arxiv_api(q, config, max_results)
    return arxiv_parse(xml_data) if xml_data else []


def arxiv_search_title(title, config, max_results=10):
    xml_data = arxiv_api(f'ti:"{title}"', config, max_results)
    return arxiv_parse(xml_data) if xml_data else []


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv (shared API)
# ---------------------------------------------------------------------------

def preprint_search(keywords, config, server='biorxiv', max_results=100, max_scan=500):
    """Search a single preprint server by keywords."""
    base = cfg(config, f'apis.{server}.base_url', f'https://api.{server}.org')
    date_range = cfg(config, 'search.date_range_days', 90)
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=date_range)).strftime('%Y-%m-%d')

    all_papers = []
    cursor = 0
    scanned = 0
    kw_lower = [k.lower() for k in keywords]

    while len(all_papers) < max_results and scanned < max_scan:
        url = f"{base}/details/{server}/{start}/{end}/{cursor}"
        req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
        try:
            with urllib.request.urlopen(req, timeout=tout(config)) as r:
                data = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            print(f"  {server} error: {e}", file=sys.stderr)
            break

        batch = data.get('collection', [])
        if not batch:
            break
        scanned += len(batch)

        for p in batch:
            title = (p.get('title', '')).lower()
            abstract = (p.get('abstract', '')).lower()
            combined = f"{title} {abstract}"
            if any(kw in combined for kw in kw_lower):
                doi = p.get('doi', '')
                all_papers.append(make_paper(
                    server, doi, p.get('title', ''), p.get('authors', ''),
                    p.get('abstract', ''), p.get('date', ''), p.get('category', ''),
                    f"https://www.{server}.org/content/{doi}.full.pdf",
                    f"https://www.{server}.org/content/{doi}",
                    doi=doi))

            if len(all_papers) >= max_results:
                break

        if len(all_papers) >= max_results:
            break
        cursor += len(batch)
        time.sleep(delay(config))

    return all_papers[:max_results], scanned


def preprint_search_title(title, config, server='biorxiv'):
    keywords = [w.lower() for w in title.split() if len(w) > 2]
    papers, scanned = preprint_search(keywords, config, server, max_results=500, max_scan=500)

    tw = set(title.lower().split())
    for p in papers:
        pw = set(p['title'].lower().split())
        p['_score'] = len(tw & pw) / max(len(tw), 1)
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers, scanned


# ---------------------------------------------------------------------------
# PubMed (NCBI E-utilities)
# ---------------------------------------------------------------------------

PUBMED_BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'


def pubmed_api(endpoint, params, config):
    """Call NCBI E-utilities API, return JSON."""
    params.setdefault('retmode', 'json')
    params.setdefault('tool', 'PaperInt')
    params.setdefault('email', cfg(config, 'download.user_agent', ''))
    url = f"{PUBMED_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with urllib.request.urlopen(req, timeout=tout(config)) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"  PubMed error: {e}", file=sys.stderr)
        return None


def pubmed_search(keywords, config, max_results=50):
    """Search PubMed by keywords, return paper list."""
    query = ' AND '.join(f'{kw}[All Fields]' for kw in keywords)
    # Step 1: search for IDs
    sr = pubmed_api('esearch.fcgi', {
        'db': 'pubmed', 'term': query, 'retmax': str(max_results),
        'sort': 'pub+date', 'datetype': 'pdat',
    }, config)
    if not sr:
        return [], 0

    idlist = sr.get('esearchresult', {}).get('idlist', [])
    total = int(sr.get('esearchresult', {}).get('count', 0))

    if not idlist:
        return [], total

    # Step 2: fetch summaries
    sm = pubmed_api('esummary.fcgi', {
        'db': 'pubmed', 'id': ','.join(idlist),
    }, config)
    if not sm:
        return [], total

    papers = []
    for pmid in idlist:
        info = sm.get('result', {}).get(pmid, {})
        if not info:
            continue
        title = info.get('title', '')
        authors = ', '.join(
            a.get('name', '') for a in info.get('authors', [])[:5])
        abstract = ''  # esummary doesn't include abstract; use efetch for full text

        # Check for PMC free full text
        pmc_id = None
        for aid in info.get('articleids', []):
            if aid.get('idtype') == 'pmc':
                pmc_id = aid.get('value')
                break

        pdf_url = ''
        if pmc_id:
            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf"
        abs_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        papers.append(make_paper(
            'pubmed', pmid, title, authors, abstract,
            info.get('pubdate', ''), '',
            pdf_url, abs_url, pmid=pmid,
            extra={'pmc_id': pmc_id, 'journal': info.get('source', ''),
                   'doi': info.get('elocationid', '').replace('doi: ', '') if info.get('elocationid') else None}))

    return papers, total


def pubmed_search_title(title, config, max_results=10):
    """Search PubMed by title."""
    papers, total = pubmed_search([f'{title}[Title]'], config, max_results)
    return papers, total


# ---------------------------------------------------------------------------
# Download functions
# ---------------------------------------------------------------------------

def download_arxiv(arxiv_id, config):
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with urllib.request.urlopen(req, timeout=tout(config)) as r:
            data = r.read()
            return data if len(data) >= cfg(config, 'download.min_pdf_size_bytes', 10000) else None
    except Exception as e:
        print(f"    download error: {e}", file=sys.stderr)
        return None


def download_preprint(doi, server, config):
    """Download PDF from biorxiv or medrxiv."""
    headers = {
        'User-Agent': ua(config),
        'Accept': 'application/pdf,*/*;q=0.9',
        'Referer': f'https://www.{server}.org/',
    }
    urls = [
        f"https://www.{server}.org/content/{doi}.full.pdf",
        f"https://www.{server}.org/content/{doi}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=tout(config)) as r:
                data = r.read()
                if data.startswith(b'%PDF') or (server == 'medrxiv' and len(data) > 10000):
                    return data
        except Exception:
            pass
        time.sleep(1)
    return None


def _pubmed_lookup_pmc(pmid, config):
    """Look up PMC ID for a PubMed article."""
    sr = pubmed_api('elink.fcgi', {
        'dbfrom': 'pubmed', 'db': 'pmc', 'id': pmid,
        'linkname': 'pubmed_pmc',
    }, config)
    if sr:
        links = sr.get('linksets', [{}])[0].get('linksetdbs', [{}])[0].get('links', [])
        if links:
            return links[0]
    return None


def _download_pmc_pdf(pmc_id, config):
    """Download PDF from PubMed Central."""
    url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with urllib.request.urlopen(req, timeout=tout(config)) as r:
            data = r.read()
            if len(data) > 10000 and not data[:4] == b'<!DO':
                return data
    except Exception:
        pass
    return None


def download_paper(paper, config, pdf_dir, metadata_dir, state):
    """Download a paper. Returns True on success, False if unavailable, None if skipped."""
    pid = paper.get('paper_id', '')
    src = paper.get('source', '')

    if pid in state.get('downloaded', []):
        print(f"  [skip] already downloaded")
        return None

    safe = sanitize(pid)
    os.makedirs(pdf_dir, exist_ok=True)
    os.makedirs(metadata_dir, exist_ok=True)

    # Save metadata
    with open(os.path.join(metadata_dir, f"{safe}.json"), 'w', encoding='utf-8') as f:
        json.dump(paper, f, ensure_ascii=False, indent=2)

    print(f"  Downloading: {paper.get('title', '?')[:80]}...")

    pdf_data = None
    if src == 'arxiv':
        pdf_data = download_arxiv(paper.get('arxiv_id', pid), config)
    elif src in ('biorxiv', 'medrxiv'):
        pdf_data = download_preprint(paper.get('doi', pid), src, config)
    elif src == 'pubmed':
        # Use pmc_id from paper metadata if already fetched, else look it up
        pmc_id = paper.get('pmc_id')
        if not pmc_id:
            pmc_id = _pubmed_lookup_pmc(paper.get('pmid', pid), config)
        if pmc_id:
            pdf_data = _download_pmc_pdf(pmc_id, config)

    if pdf_data:
        with open(os.path.join(pdf_dir, f"{safe}.pdf"), 'wb') as f:
            f.write(pdf_data)
        state.setdefault('downloaded', []).append(pid)
        print(f"  OK: {len(pdf_data)} bytes")
        return True
    else:
        print(f"  UNAVAILABLE (metadata saved)")
        return False


# ---------------------------------------------------------------------------
# URL detection for `get` command
# ---------------------------------------------------------------------------

def detect_url(url, config):
    """Parse a URL and return a paper dict, or None."""
    # arXiv
    m = re.search(r'arxiv\.org/(?:abs|pdf)/([0-9]+\.[0-9]+)', url)
    if m:
        aid = m.group(1).rstrip('.pdf')
        xml_data = arxiv_api(f'id:{aid}', config, 1)
        papers = arxiv_parse(xml_data) if xml_data else []
        if papers:
            return papers[0]
        return make_paper('arxiv', aid, f'arXiv:{aid}', '', '', '', '',
                          f"https://arxiv.org/pdf/{aid}.pdf",
                          f"https://arxiv.org/abs/{aid}", arxiv_id=aid)

    # bioRxiv / medRxiv
    m = re.search(r'(biorxiv|medrxiv)\.org/content/(10\.\d+/[\w.\-]+?)(?:\.full)?(?:\.pdf)?$', url)
    if m:
        server = m.group(1)
        doi = m.group(2)
        return make_paper(server, doi, f'{server}:{doi}', '', '', '', '',
                          f"https://www.{server}.org/content/{doi}.full.pdf",
                          f"https://www.{server}.org/content/{doi}", doi=doi)

    # PubMed / PMC
    pmid = None
    m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', url)
    if m:
        pmid = m.group(1)
    # Direct PMC URL
    m2 = re.search(r'ncbi\.nlm\.nih\.gov/pmc/articles/(PMC\d+)', url)

    if pmid:
        sm = pubmed_api('esummary.fcgi', {'db': 'pubmed', 'id': pmid}, config)
        if sm:
            info = sm.get('result', {}).get(pmid, {})
            title = info.get('title', '')
            authors = ', '.join(a.get('name', '') for a in info.get('authors', [])[:5])
            pmc_id = next((a.get('value') for a in info.get('articleids', [])
                           if a.get('idtype') == 'pmc'), None)
            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf" if pmc_id else ''
            return make_paper('pubmed', pmid, title, authors, '',
                              info.get('pubdate', ''), '',
                              pdf_url, url, pmid=pmid,
                              extra={'pmc_id': pmc_id,
                                     'journal': info.get('source', ''),
                                     'doi': (info.get('elocationid', '')
                                             .replace('doi: ', '')) if info.get('elocationid') else None})
        return make_paper('pubmed', pmid, f'PubMed:{pmid}', '', '', '', '',
                          '', url, pmid=pmid)

    if m2:
        pmc_id = m2.group(1)
        # Convert PMC ID to PMID via elink
        sr = pubmed_api('elink.fcgi', {
            'dbfrom': 'pmc', 'db': 'pubmed', 'id': pmc_id,
            'linkname': 'pmc_pubmed',
        }, config)
        if sr:
            links = sr.get('linksets', [{}])[0].get('linksetdbs', [{}])[0].get('links', [])
            if links:
                pmid = links[0]
        paper_id = pmid or pmc_id
        pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf"
        return make_paper('pubmed', paper_id, f'PMC:{pmc_id}', '', '', '', '',
                          pdf_url, url, pmid=pmid, extra={'pmc_id': pmc_id})

    # Generic PDF
    print(f"  Treating as generic PDF URL: {url}")
    name = url.split('/')[-1] or 'download'
    return make_paper('generic', sanitize(name), f'Download:{url}', '', '', '', '',
                      url, url)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

SOURCES = ['arxiv', 'biorxiv', 'medrxiv', 'pubmed']


def cmd_search(args, config):
    source = args.source or cfg(config, 'search.default_source', 'arxiv')
    num = args.num or cfg(config, 'search.default_num', 1)
    keywords = args.keywords or cfg(config, 'keywords.include', [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',') if k.strip()]

    print(f"Source: {source}  |  Keywords: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
    print(f"Target: {num} paper(s)\n")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search(keywords, config, max_results=max(num * 5, 20))
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search(keywords, config, source,
                                          max_results=num * 5)
    elif source == 'pubmed':
        papers, scanned = pubmed_search(keywords, config, max_results=num * 5)
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:num]

    return _show_or_download(papers, args, config)


def cmd_find(args, config):
    source = args.source or cfg(config, 'search.default_source', 'arxiv')

    print(f"Source: {source}  |  Title: {args.title}\n")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search_title(args.title, config, 10)
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search_title(args.title, config, source)
    elif source == 'pubmed':
        papers, scanned = pubmed_search_title(args.title, config, 10)
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:5]
    if not papers:
        print("No matching papers found.")
        return 1

    return _show_or_download(papers, args, config, single_best=True)


def cmd_get(args, config):
    paper = detect_url(args.url, config)
    if not paper:
        print("Could not parse URL.", file=sys.stderr)
        return 1

    print(f"Source: {paper['source']}  |  ID: {paper['paper_id']}\n")

    if args.list:
        print(f"Title: {paper['title'][:100]}")
        print(f"PDF:   {paper['pdf_url']}")
        print(f"Page:  {paper['abs_url']}")
        return 0

    state = load_state(args.state_file)
    ok = download_paper(paper, config, args.pdf_dir, args.metadata_dir, state)
    save_state(args.state_file, state)
    if ok is True:
        print("Downloaded")
    elif ok is False:
        print("PDF unavailable, metadata saved")
    else:
        print("Already downloaded")
    return 0


def _show_or_download(papers, args, config, single_best=False):
    if args.list:
        for i, p in enumerate(papers, 1):
            print(f"{i}. {p['title'][:100]}")
            print(f"   {p.get('pdf_url') or '(no PDF)'}")
            print(f"   {p.get('date', '?')}  |  {p['source']}\n")
        print(f"Total: {len(papers)} paper(s) (list only)")
        return 0

    if single_best:
        papers = papers[:1]
        print(f"Best match: {papers[0]['title'][:100]}")

    state = load_state(args.state_file)
    ok = 0
    unavail = 0
    for i, p in enumerate(papers, 1):
        if not single_best:
            print(f"[{i}/{len(papers)}] {p['paper_id']}")
        result = download_paper(p, config, args.pdf_dir, args.metadata_dir, state)
        if result is True:
            ok += 1
        elif result is False:
            unavail += 1
        if i < len(papers):
            time.sleep(delay(config))

    save_state(args.state_file, state)
    parts = [f"{ok} downloaded"]
    if unavail:
        parts.append(f"{unavail} metadata-only")
    print(f"\nDone: {', '.join(parts)}, {len(state.get('downloaded', []))} total in state")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  # Keyword mode — search latest N papers from arXiv (default)
  paper_cli.py search -k "methylation,single-cell" -n 3
  paper_cli.py search -k "CRISPR" -s pubmed -n 5 -l
  paper_cli.py search -f                     # use config keywords + filter

  # Title mode — find a specific paper by title
  paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"
  paper_cli.py find -t "CRISPR editing" -s pubmed -l

  # URL mode — download from a direct link
  paper_cli.py get -u "https://arxiv.org/abs/2301.00001"
  paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
  paper_cli.py get -u "https://pubmed.ncbi.nlm.nih.gov/12345678/"
  paper_cli.py get -u "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8371605/"

sources: arxiv, biorxiv, medrxiv, pubmed"""


def _add_output_args(p):
    """Add shared output-directory options to a sub-parser."""
    p.add_argument('--pdf-dir', default='data/pdf',
                   help='Directory for downloaded PDFs (default: data/pdf)')
    p.add_argument('--metadata-dir', default='data/metadata',
                   help='Directory for paper metadata JSON (default: data/metadata)')
    p.add_argument('--state-file', default='data/downloaded.json',
                   help='Download-tracking state file (default: data/downloaded.json)')


def main():
    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Unified paper search & download CLI — arXiv, bioRxiv, medRxiv, PubMed.',
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--config', default='config.yaml',
                   help='Path to shared YAML config file (default: config.yaml)')
    sub = p.add_subparsers(dest='cmd', required=True,
                           title='commands',
                           description='"search" by keywords, "find" by title, or "get" by URL')

    # ---- search ----
    sp = sub.add_parser(
        'search',
        help='Search papers by keyword, download latest N results',
        description='Keyword search: query a source for papers matching keywords, '
                    'return the most recent N matches (optionally downloading PDFs).',
        epilog='See "paper_cli.py --help" for full examples.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument('-k', '--keywords',
                    help='Comma-separated search keywords (default: keywords.include from config)')
    sp.add_argument('-s', '--source', choices=SOURCES,
                    help='Paper source to search (default: search.default_source from config)')
    sp.add_argument('-n', '--num', type=int,
                    help='Max number of papers to return (default: search.default_num from config)')
    sp.add_argument('-f', '--filter', action='store_true',
                    help='Apply config keywords.include/exclude relevance filter before download')
    sp.add_argument('-l', '--list', action='store_true',
                    help='Preview only — list matching papers without downloading')
    _add_output_args(sp)

    # ---- find ----
    fp = sub.add_parser(
        'find',
        help='Search for a specific paper by title',
        description='Title search: look up a paper by its title and download '
                    'the best match (first result).',
        epilog='See "paper_cli.py --help" for full examples.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fp.add_argument('-t', '--title', required=True,
                    help='Paper title to search for (required)')
    fp.add_argument('-s', '--source', choices=SOURCES,
                    help='Paper source to search (default: search.default_source from config)')
    fp.add_argument('-l', '--list', action='store_true',
                    help='Preview only — show the best match without downloading')
    _add_output_args(fp)

    # ---- get ----
    gp = sub.add_parser(
        'get',
        help='Download a paper directly from a URL',
        description='URL download: auto-detect the source from the URL pattern '
                    'and download the paper (and/or its metadata). '
                    'Supports arXiv abs/pdf links, bioRxiv/medRxiv content links, '
                    'PubMed abstract links, PMC article links, and generic .pdf URLs.',
        epilog='See "paper_cli.py --help" for full examples.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gp.add_argument('-u', '--url', required=True,
                    help='Paper URL (arXiv, bioRxiv, medRxiv, PubMed, PMC, or direct PDF)')
    gp.add_argument('-l', '--list', action='store_true',
                    help='Parse and show paper info from the URL without downloading')
    _add_output_args(gp)

    args = p.parse_args()
    config = load_config(args.config)

    if args.cmd == 'search':
        return cmd_search(args, config)
    elif args.cmd == 'find':
        return cmd_find(args, config)
    elif args.cmd == 'get':
        return cmd_get(args, config)
    return 1


if __name__ == '__main__':
    sys.exit(main())
