#!/usr/bin/env python3
"""
Unified Paper CLI — search and download bioinformatics papers.

Sources:  arxiv  |  biorxiv  |  medrxiv  |  pubmed  |  scholar

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
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

os.environ.setdefault('NODE_NO_WARNINGS', '1')

# ---------------------------------------------------------------------------
# SSL workaround for older servers (bioRxiv, etc.)
# ---------------------------------------------------------------------------

def _ssl_context():
    """Return an SSL context compatible with older OpenSSL servers.

    Some preprint APIs (arXiv, bioRxiv, medRxiv) run older OpenSSL that
    can't handle TLS 1.3 ClientHello. We cap at TLS 1.2 to avoid handshake
    EOF errors.
    """
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx

STOP_WORDS = frozenset({
    'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by',
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'and', 'or', 'not',
    'but', 'if', 'then', 'else', 'when', 'up', 'so', 'as', 'its', 'it',
    'that', 'this', 'these', 'those', 'from', 'has', 'have', 'had', 'do',
    'does', 'did', 'will', 'would', 'can', 'could', 'may', 'might', 'we',
    'no', 'than', 'also', 'all', 'into', 'about', 'after', 'some', 'such',
    'only', 'other', 'more', 'their', 'them', 'our', 'us', 'he', 'she',
})

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


def _urlopen_with_retry(req, config, attempts=3, backoff=2):
    """urlopen with retry for transient SSL/network errors."""
    last_err = None
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=tout(config), context=_ssl_context())
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
    raise last_err


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def arxiv_api(query, config, max_results=50):
    base = cfg(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    url = f"{base}?search_query={urllib.parse.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config) as r:
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
    """Search arXiv by title — exact title field search, scored by relevance."""
    xml_data = arxiv_api(f'ti:"{title}"', config, max_results)
    papers = arxiv_parse(xml_data) if xml_data else []
    tw = set(title.lower().split())
    for p in papers:
        pw = set(p['title'].lower().split())
        p['_score'] = len(tw & pw) / max(len(tw), 1)
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers[:max_results]


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
            with _urlopen_with_retry(req, config, attempts=3) as r:
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


async def _preprint_search_title_browser(title, server, config, max_results, chrome_port):
    """Search biorxiv/medrxiv by title using Chrome browser — connect to existing CDP."""
    import asyncio as _asyncio
    from playwright.async_api import async_playwright

    query = urllib.parse.quote(title)
    url = f'https://www.{server}.org/search/{query}'

    print(f"  Searching {server}...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
        ctx = browser.contexts[0]
        page = await ctx.new_page()

        await page.goto(url, wait_until='domcontentloaded', timeout=120000)

        for _ in range(30):
            await _asyncio.sleep(4)
            page_title = await page.title()
            if 'Search Results' in page_title or 'search' in page_title.lower():
                break

        # Extract search result cards
        raw = await page.evaluate(f'''
            () => {{
                const results = [];
                const items = document.querySelectorAll('.highwire-article-citation, .search-result, article.search-result, li.search-result');
                if (items.length === 0) {{
                    // Fallback: try any link to /content/10.
                    const links = document.querySelectorAll('a[href*="/content/10."]');
                    links.forEach((a) => {{
                        if (results.length >= {max_results}) return;
                        const href = a.getAttribute('href');
                        const title = a.innerText.trim();
                        if (title && href) {{
                            results.push({{
                                title: title,
                                link: href,
                                authors: '',
                                snippet: '',
                                doiText: href,
                                date: '',
                            }});
                        }}
                    }});
                }} else {{
                    items.forEach((el) => {{
                        if (results.length >= {max_results}) return;
                        const titleEl = el.querySelector('.highwire-cite-title, .title, h4 a, .highwire-cite-title a, a[href*="/content/10."]');
                        const title = titleEl?.innerText?.trim() || '';
                        const link = titleEl?.getAttribute('href') || '';
                        const authorsEl = el.querySelector('.highwire-citation-authors, .authors, .contributors');
                        const authors = authorsEl?.innerText?.trim() || '';
                        const snippetEl = el.querySelector('.highwire-cite-snippet, .snippet, .abstract');
                        const snippet = snippetEl?.innerText?.trim().substring(0, 500) || '';
                        const doiEl = el.querySelector('.highwire-cite-metadata-doi, .doi');
                        const doiText = doiEl?.innerText?.trim() || link;
                        const dateEl = el.querySelector('.highwire-cite-metadata-pub-date, .pub-date, .date');
                        const dateText = dateEl?.innerText?.trim() || '';

                        results.push({{
                            title: title,
                            link: link,
                            authors: authors,
                            snippet: snippet,
                            doiText: doiText,
                            date: dateText,
                        }});
                    }});
                }}
                return results;
            }}
        ''')

        await page.close()

    papers = []
    for r in raw:
        if not r['title']:
            continue

        # Extract DOI from various fields
        doi = ''
        for src in [r['link'], r['doiText']]:
            m = re.search(r'(10\.\d{{4,}}/[^\s&]+)', src)
            if m:
                doi = m.group(1).rstrip('.')
                break

        paper_id = doi or r['link'].split('/')[-1] or str(hash(r['title']) % 100000000)

        # Build full abs_url from relative link
        abs_url = r['link']
        if abs_url and not abs_url.startswith('http'):
            abs_url = f'https://www.{server}.org' + abs_url

        papers.append(make_paper(
            server, paper_id, r['title'], r['authors'],
            r['snippet'], r['date'], '',
            f"https://www.{server}.org/content/{doi}.full.pdf" if doi else '',
            abs_url, doi=doi or None,
        ))

    return papers


def preprint_search_title(title, config, server='biorxiv', use_browser=False):
    """Search a preprint server by title.

    When use_browser=True, uses Chrome to search the site's search page
    (much more accurate for title matching).
    """
    if use_browser:
        import asyncio as _asyncio
        return _asyncio.run(_preprint_search_title_browser(
            title, server, config, max_results=20,
            chrome_port=_get_or_start_chrome())), 0

    keywords = [w.lower() for w in title.split() if w.lower() not in STOP_WORDS]
    papers, scanned = preprint_search(keywords, config, server, max_results=500, max_scan=500)

    tw = set(title.lower().split())
    for p in papers:
        pw = set(p['title'].lower().split())
        p['_score'] = len(tw & pw) / max(len(tw), 1)
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers, scanned


def _get_or_start_chrome():
    """Get or create a reusable Chrome debugging port."""
    profile = os.path.join(tempfile.gettempdir(), 'paper_cli_scholar_chrome')
    os.makedirs(profile, exist_ok=True)

    # Check for existing Chrome on our profile
    r = subprocess.run(['pgrep', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        # Find the debugging port from the existing Chrome process
        r2 = subprocess.run(['pgrep', '-a', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
        import re as _re
        m = _re.search(r'--remote-debugging-port=(\d+)', r2.stdout)
        if m:
            return int(m.group(1))

    # Start new Chrome
    port = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        port = s.getsockname()[1]

    subprocess.run(['pkill', '-f', f'remote-debugging-port={port}'], capture_output=True)
    time.sleep(1)

    subprocess.Popen([
        'google-chrome',
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', '--no-sandbox',
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    time.sleep(3)
    return port

_shared_chrome_port = None

def _kill_shared_chrome():
    """Kill the shared Chrome browser if running."""
    global _shared_chrome_port
    if _shared_chrome_port:
        try:
            subprocess.run(['pkill', '-f', f'remote-debugging-port={_shared_chrome_port}'],
                         capture_output=True)
        except Exception:
            pass
        _shared_chrome_port = None


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
        with _urlopen_with_retry(req, config, attempts=3) as r:
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
    """Search PubMed by title — exact title field search, scored by relevance."""
    papers, total = pubmed_search([f'{title}[Title]'], config, max_results)
    tw = set(title.lower().split())
    for p in papers:
        pw = set(p['title'].lower().split())
        p['_score'] = len(tw & pw) / max(len(tw), 1)
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers[:max_results], total


# ---------------------------------------------------------------------------
# Download functions
# ---------------------------------------------------------------------------

def download_arxiv(arxiv_id, config):
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config, attempts=2) as r:
            data = r.read()
            return data if len(data) >= cfg(config, 'download.min_pdf_size_bytes', 10000) else None
    except Exception as e:
        print(f"    download error: {e}", file=sys.stderr)
        return None


def download_preprint(doi, server, config, use_browser=False):
    """Download PDF from biorxiv or medrxiv. Falls back to Playwright browser if enabled."""
    if not doi:
        return None
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
            with _urlopen_with_retry(req, config, attempts=2) as r:
                data = r.read()
                if data.startswith(b'%PDF') or (server == 'medrxiv' and len(data) > 10000):
                    return data
        except Exception:
            pass
        time.sleep(1)

    if use_browser:
        print(f"  Direct download failed, trying Playwright browser...", file=sys.stderr)
        try:
            data = _browser_download(doi, server, config)
            if data:
                return data
        except Exception as e:
            print(f"  Browser fallback error: {e}", file=sys.stderr)

    return None


def _browser_download(doi, server, config):
    """Download biorxiv/medRxiv PDF via CDP (reuses shared Chrome if available)."""
    if not doi:
        return None

    # Reuse shared Chrome if available
    if _shared_chrome_port:
        import asyncio as _asyncio
        return _asyncio.run(_browser_download_cdp(doi, server, config, _shared_chrome_port))

    # Fallback: call download_biorxiv_browser.py subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'download_biorxiv_browser.py')
    with tempfile.TemporaryDirectory() as tmpdir:
        r = subprocess.run(
            [sys.executable, script, doi, '-o', tmpdir,
             '--timeout', '180'],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
            pdf_path = os.path.join(tmpdir, safe_name)
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    return f.read()
    return None


async def _browser_download_cdp(doi, server, config, chrome_port):
    """Download biorxiv/medRxiv PDF by connecting to existing headed Chrome via CDP."""
    import asyncio as _asyncio
    from playwright.async_api import async_playwright

    pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
    article_url = f"https://www.{server}.org/content/{doi}"

    print(f"  Browser: connecting to Chrome on port {chrome_port}...", file=sys.stderr)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
        ctx = browser.contexts[0]

        # First ensure Cloudflare is passed
        page = await ctx.new_page()
        homepage = f'https://www.{server}.org/'
        await page.goto(homepage, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

        # Go to article page
        await page.goto(article_url, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

        # Go to PDF page and fetch via JS
        pdf_page = await ctx.new_page()
        await pdf_page.goto(pdf_url, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

        js_result = await pdf_page.evaluate("""
            async () => {
                const r = await fetch(window.location.href, {credentials: 'include'});
                if (!r.ok) return {error: 'HTTP ' + r.status};
                const blob = await r.blob();
                return new Promise(resolve => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve({data: reader.result.split(',')[1], size: blob.size});
                    reader.onerror = () => resolve({error: 'FileReader failed'});
                    reader.readAsDataURL(blob);
                });
            }
        """)

        await pdf_page.close()
        await page.close()

    if isinstance(js_result, dict) and 'data' in js_result:
        import base64
        return base64.b64decode(js_result['data'])
    return None


def _publisher_download(doi, pmid, config):
    """Call the publisher-based PDF downloader as a subprocess."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'download_publisher_pdf.py')
    cmd = [sys.executable, script, '--doi', doi, '-o', '/dev/stdout',
           '--timeout', '180']
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd[cmd.index('/dev/stdout')] = tmpdir
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300)
        if r.returncode == 0:
            safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
            pdf_path = os.path.join(tmpdir, safe_name)
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    return f.read()
    return None


def _download_direct_pdf(pdf_url, config):
    """Download a PDF from a direct URL. Tries urllib first, falls back to browser."""
    # Quick try with urllib first
    try:
        req = urllib.request.Request(pdf_url, headers={'User-Agent': ua(config)})
        with _urlopen_with_retry(req, config, attempts=2) as r:
            data = r.read()
            if data.startswith(b'%PDF') or len(data) > 50000:
                return data
    except Exception:
        pass

    # Fall back to browser for anti-bot bypass
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'download_biorxiv_browser.py')
    with tempfile.TemporaryDirectory() as tmpdir:
        r = subprocess.run(
            [sys.executable, script, pdf_url, '-o', tmpdir,
             '--timeout', '180'],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            for f in os.listdir(tmpdir):
                if f.endswith('.pdf'):
                    pdf_path = os.path.join(tmpdir, f)
                    with open(pdf_path, 'rb') as fh:
                        return fh.read()
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
        with _urlopen_with_retry(req, config, attempts=2) as r:
            data = r.read()
            if len(data) > 10000 and not data[:4] == b'<!DO':
                return data
    except Exception:
        pass
    return None


def download_paper(paper, config, pdf_dir, metadata_dir, state, use_browser=False):
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
        pdf_data = download_preprint(paper.get('doi') or pid, src, config,
                                     use_browser=use_browser)
    elif src == 'scholar':
        # Scholar papers may have PDF link, DOI, or known underlying source
        pdf_url = paper.get('pdf_url', '')
        doi = paper.get('doi', '')
        # 1) Direct PDF link from Scholar (retry up to 2 extra times)
        if pdf_url and use_browser:
            for attempt in range(3):
                print(f"  Trying direct PDF from Scholar (attempt {attempt+1}/3)...", file=sys.stderr)
                pdf_data = _download_direct_pdf(pdf_url, config)
                if pdf_data:
                    break
                if attempt < 2:
                    time.sleep(5)
        # 2) DOI → publisher download (retry up to 2 extra times)
        if not pdf_data and doi and use_browser:
            for attempt in range(3):
                print(f"  Trying publisher via DOI {doi} (attempt {attempt+1}/3)...", file=sys.stderr)
                pdf_data = _publisher_download(doi, paper.get('pmid'), config)
                if pdf_data:
                    break
                if attempt < 2:
                    time.sleep(5)
        # 3) If source is actually biorxiv/medrxiv/arxiv (detected from link)
        if not pdf_data and paper.get('arxiv_id'):
            pdf_data = download_arxiv(paper.get('arxiv_id'), config)
    elif src == 'pubmed':
        # 1) Try DOI → publisher PDF (if --browser enabled and DOI available)
        if use_browser and paper.get('doi'):
            pdf_data = _publisher_download(paper.get('doi'), paper.get('pmid'), config)
        # 2) Fall back to PMC
        if not pdf_data:
            pmc_id = paper.get('pmc_id')
            if not pmc_id:
                pmc_id = _pubmed_lookup_pmc(paper.get('pmid', pid), config)
            if pmc_id:
                pdf_data = _download_pmc_pdf(pmc_id, config)

    # Fallback: try alternative sources in priority order
    if not pdf_data and paper.get('_alt_sources'):
        alts = sorted(paper['_alt_sources'],
                      key=lambda a: SOURCE_PRIORITY.get(a.get('source'), 99))
        # Also try _primary_alt if we swapped sources during dedup
        if paper.get('_primary_alt'):
            alts.insert(0, paper['_primary_alt'])
        for alt in alts:
            alt_src = alt.get('source', '')
            print(f"  Fallback: trying {alt_src}...", file=sys.stderr)
            if alt_src == 'arxiv' or alt.get('arxiv_id'):
                aid = alt.get('arxiv_id') or paper.get('arxiv_id')
                if aid:
                    pdf_data = download_arxiv(aid, config)
            elif alt_src in ('biorxiv', 'medrxiv'):
                adoi = alt.get('doi') or paper.get('doi') or pid
                pdf_data = download_preprint(adoi, alt_src, config, use_browser=use_browser)
            elif alt_src == 'scholar':
                if alt.get('pdf_url') and use_browser:
                    pdf_data = _download_direct_pdf(alt['pdf_url'], config)
                if not pdf_data and alt.get('doi') and use_browser:
                    pdf_data = _publisher_download(alt['doi'], paper.get('pmid'), config)
            elif alt_src == 'pubmed':
                if use_browser and alt.get('doi'):
                    pdf_data = _publisher_download(alt['doi'], alt.get('pmid'), config)
                if not pdf_data and alt.get('pmid'):
                    pmc_id = _pubmed_lookup_pmc(alt['pmid'], config)
                    if pmc_id:
                        pdf_data = _download_pmc_pdf(pmc_id, config)
            if pdf_data:
                print(f"  Fallback OK from {alt_src}", file=sys.stderr)
                break
            time.sleep(2)

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

SOURCES = ['arxiv', 'biorxiv', 'medrxiv', 'pubmed', 'scholar', 'all']


def scholar_search(keywords, config, max_results=10, chrome_port=None):
    """Search Google Scholar and return normalized paper list."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Google Scholar search requires: pip install playwright", file=sys.stderr)
        return [], 0

    import asyncio as _asyncio
    return _asyncio.run(_scholar_search_async(keywords, config, max_results, chrome_port=chrome_port))


async def _scholar_search_async(keywords, config, max_results, chrome_port=None):
    """Async implementation of scholar search.

    If chrome_port is provided, connects to existing Chrome instead of
    launching a new instance. Caller manages Chrome lifecycle in that case.
    """
    import asyncio as _asyncio
    from playwright.async_api import async_playwright

    query = '+'.join(k.strip().replace(' ', '+') for k in keywords)
    url = f'https://scholar.google.com/scholar?q={query}&num={min(max_results, 20)}&as_sdt=0,5'

    print(f"  Searching Google Scholar...")

    own_chrome = chrome_port is None
    if own_chrome:
        chrome_port = _get_or_start_chrome()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
            ctx = browser.contexts[0]
            page = await ctx.new_page()

            await page.goto(url, wait_until='domcontentloaded', timeout=120000)

            for _ in range(30):
                await _asyncio.sleep(4)
                title = await page.title()
                if 'sorry' in title.lower():
                    print(f"  Google Scholar blocked us (captcha/IP rate limit)", file=sys.stderr)
                    return [], 0
                if 'scholar' in title.lower():
                    break

            raw = await page.evaluate(f'''
                () => {{
                    const results = [];
                    const containers = document.querySelectorAll('.gs_r.gs_or.gs_scl, .gs_r.gs_or');
                    containers.forEach((r) => {{
                        if (results.length >= {min(max_results, 20)}) return;
                        const titleEl = r.querySelector('h3.gs_rt');
                        const titleLink = r.querySelector('h3.gs_rt a');
                        const metaEl = r.querySelector('.gs_a');
                        const snippetEl = r.querySelector('.gs_rs');

                        const pdfLinks = [];
                        r.querySelectorAll('a').forEach(a => {{
                            const href = a.getAttribute('href');
                            const text = (a.innerText || '').trim();
                            if (href && !href.startsWith('#') && !href.startsWith('javascript')) {{
                                if (text === '[PDF]' || href.endsWith('.pdf')) {{
                                    pdfLinks.push({{href: href, text: text}});
                                }}
                            }}
                        }});

                        results.push({{
                            title: (titleEl?.innerText || '').replace(/^\\[[A-Z]+\\]\\s*/, ''),
                            link: titleLink?.getAttribute('href') || '',
                            meta: metaEl?.innerText || '',
                            snippet: snippetEl?.innerText?.substring(0, 300) || '',
                            pdf_links: pdfLinks,
                        }});
                    }});
                    return results;
                }}
            ''')

            await browser.close()
    finally:
        if own_chrome:
            try:
                subprocess.run(['pkill', '-f', f'remote-debugging-port={chrome_port}'], capture_output=True)
            except Exception:
                pass

    # Normalize results
    import asyncio  # re-import for sleep in normalize loop
    normalized = []
    for r in raw:
        title = r['title'].strip()
        if not title:
            continue

        main_link = r['link']
        meta = r['meta']

        doi = ''
        for src in [main_link, meta]:
            m = re.search(r'(10\.\d{4,}/[^\s&]+)', src)
            if m:
                doi = m.group(1).rstrip('.')
                break

        source = 'scholar'
        if 'biorxiv.org' in main_link:
            source = 'biorxiv'
        elif 'medrxiv.org' in main_link:
            source = 'medrxiv'
        elif 'arxiv.org' in main_link:
            source = 'arxiv'
        elif doi:
            source = 'pubmed'

        pdf_url = ''
        if r['pdf_links']:
            pdf_url = r['pdf_links'][0]['href']

        paper_id = doi or main_link.split('/')[-1] or str(hash(title) % 100000000)

        authors = ''
        journal = ''
        if ' - ' in meta:
            parts = meta.split(' - ')
            authors = parts[0].strip()
            if len(parts) >= 2:
                journal = parts[-1].strip().rstrip(',')

        p = make_paper(
            source, paper_id, title, authors, '',
            '', journal, pdf_url, main_link,
            doi=doi or None,
        )
        p['scholar_meta'] = meta
        normalized.append(p)

    return normalized, len(raw)


def scholar_search_title(title, config, max_results=5, chrome_port=None):
    """Search Google Scholar by title — returns best matches."""
    import asyncio as _asyncio
    keywords = [title]
    return _asyncio.run(_scholar_search_async(keywords, config, max_results, chrome_port=chrome_port))


# ---------------------------------------------------------------------------
# All-sources search
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = {'pubmed': 0, 'scholar': 1, 'arxiv': 2, 'medrxiv': 3, 'biorxiv': 4}


def _paper_score(paper, kw_lower):
    """Score a paper by keyword match: title ×3, abstract ×1."""
    title = (paper.get('title', '') or '').lower()
    abstract = (paper.get('abstract', '') or '').lower()
    score = 0
    for kw in kw_lower:
        if kw in title:
            score += 3
        if kw in abstract:
            score += 1
    return score


def _titles_similar(t1, t2, threshold=0.7):
    """Check if two titles are likely the same paper using Jaccard on word sets."""
    import re as _re
    w1 = set(_re.sub(r'[^a-z0-9]', ' ', t1.lower()).split())
    w2 = set(_re.sub(r'[^a-z0-9]', ' ', t2.lower()).split())
    if not w1 or not w2:
        return False
    return len(w1 & w2) / len(w1 | w2) >= threshold


def _merge_dedup(papers, source_priority=None):
    """Deduplicate a list of papers from multiple sources.

    Two papers are the same if they share a DOI, arxiv_id, or have title
    overlap >= 70%. When merging, the paper from the higher-priority source
    is kept and the other is stored in _alt_sources.
    """
    if source_priority is None:
        source_priority = SOURCE_PRIORITY
    merged = []
    for p in papers:
        merged_into = None
        for existing in merged:
            # Same DOI
            if p.get('doi') and existing.get('doi') and p['doi'].lower() == existing['doi'].lower():
                merged_into = existing
                break
            # Same arxiv_id
            if p.get('arxiv_id') and existing.get('arxiv_id') and p['arxiv_id'] == existing['arxiv_id']:
                merged_into = existing
                break
            # Title similarity
            if _titles_similar(p.get('title', ''), existing.get('title', '')):
                merged_into = existing
                break

        if merged_into:
            alt = {
                'source': p.get('source'),
                'doi': p.get('doi'),
                'arxiv_id': p.get('arxiv_id'),
                'pmid': p.get('pmid'),
                'pdf_url': p.get('pdf_url', ''),
                'abs_url': p.get('abs_url', ''),
            }
            merged_into.setdefault('_alt_sources', []).append(alt)
            # If the new paper is from a higher-priority source, swap
            p_prio = source_priority.get(p.get('source'), 99)
            e_prio = source_priority.get(merged_into.get('source'), 99)
            if p_prio < e_prio:
                merged_into['_primary_alt'] = {
                    'source': merged_into.get('source'),
                    'doi': merged_into.get('doi'),
                    'arxiv_id': merged_into.get('arxiv_id'),
                    'pmid': merged_into.get('pmid'),
                    'pdf_url': merged_into.get('pdf_url', ''),
                    'abs_url': merged_into.get('abs_url', ''),
                }
                merged_into['source'] = p.get('source')
                merged_into['doi'] = p.get('doi') or merged_into.get('doi')
                merged_into['arxiv_id'] = p.get('arxiv_id') or merged_into.get('arxiv_id')
                merged_into['pmid'] = p.get('pmid') or merged_into.get('pmid')
                merged_into['pdf_url'] = p.get('pdf_url') or merged_into.get('pdf_url')
                merged_into['abs_url'] = p.get('abs_url') or merged_into.get('abs_url')
        else:
            merged.append(p)
    return merged


def search_all(keywords, config, max_results=10, use_browser=False, sort_by='date', chrome_port=None):
    """Search all sources, merge, deduplicate, and sort.

    sort_by: 'date' (newest first, for keyword search) or 'relevance' (best match, for title search).
    chrome_port: shared Chrome CDP port to reuse (avoids launching new Chrome).
    """
    all_papers = []

    # arXiv
    try:
        papers = arxiv_search(keywords, config, max_results=max_results * 3)
        all_papers.extend(papers)
        print(f"  arxiv: {len(papers)} results")
    except Exception as e:
        print(f"  arxiv: error - {e}", file=sys.stderr)

    # bioRxiv
    try:
        papers, _ = preprint_search(keywords, config, 'biorxiv', max_results=max_results * 3)
        all_papers.extend(papers)
        print(f"  biorxiv: {len(papers)} results")
    except Exception as e:
        print(f"  biorxiv: error - {e}", file=sys.stderr)

    # medRxiv
    try:
        papers, _ = preprint_search(keywords, config, 'medrxiv', max_results=max_results * 3)
        all_papers.extend(papers)
        print(f"  medrxiv: {len(papers)} results")
    except Exception as e:
        print(f"  medrxiv: error - {e}", file=sys.stderr)

    # PubMed
    try:
        papers, total = pubmed_search(keywords, config, max_results=max_results * 3)
        all_papers.extend(papers)
        print(f"  pubmed: {len(papers)} results (total: {total})")
    except Exception as e:
        print(f"  pubmed: error - {e}", file=sys.stderr)

    # Google Scholar
    if use_browser:
        try:
            papers, _ = scholar_search(keywords, config, max_results=max_results, chrome_port=chrome_port)
            all_papers.extend(papers)
            print(f"  scholar: {len(papers)} results")
        except Exception as e:
            print(f"  scholar: error - {e}", file=sys.stderr)

    if not all_papers:
        return []

    merged = _merge_dedup(all_papers)

    if sort_by == 'date':
        def _date_key(p):
            d = p.get('date', '')
            return d if d else '0000-00-00'
        merged.sort(key=_date_key, reverse=True)
    else:
        kw_lower = [k.lower() for k in keywords]
        for p in merged:
            p['_score'] = _paper_score(p, kw_lower)
        merged.sort(key=lambda x: x.get('_score', 0), reverse=True)
        for p in merged:
            p.pop('_score', None)

    print(f"  Merged: {len(merged)} unique papers (from {len(all_papers)} total)")
    return merged[:max_results]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


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
    elif source == 'scholar':
        if not args.browser:
            print(" --browser is required for Google Scholar searches", file=sys.stderr)
            return 1
        papers, scanned = scholar_search(keywords, config, max_results=num)
    elif source == 'all':
        if not args.browser:
            print(" --browser is required for 'all' source (includes Google Scholar)", file=sys.stderr)
            return 1
        global _shared_chrome_port
        _shared_chrome_port = _get_or_start_chrome()
        papers = search_all(keywords, config, max_results=num, use_browser=True,
                           sort_by='date', chrome_port=_shared_chrome_port)
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:num]

    result = _show_or_download(papers, args, config)
    _kill_shared_chrome()
    return result


def cmd_find(args, config):
    source = args.source or cfg(config, 'search.default_source', 'arxiv')

    print(f"Source: {source}  |  Title: {args.title}\n")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search_title(args.title, config, 10)
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search_title(args.title, config, source,
                                                  use_browser=args.browser)
    elif source == 'pubmed':
        papers, scanned = pubmed_search_title(args.title, config, 10)
    elif source == 'scholar':
        if not args.browser:
            print(" --browser is required for Google Scholar searches", file=sys.stderr)
            return 1
        papers, scanned = scholar_search_title(args.title, config, 5)
    elif source == 'all':
        if not args.browser:
            print(" --browser is required for 'all' source (includes Google Scholar)", file=sys.stderr)
            return 1
        global _shared_chrome_port
        _shared_chrome_port = _get_or_start_chrome()
        try:
            # Search each source with the complete title, merge by relevance
            all_papers = []
            try:
                papers = arxiv_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"  arxiv: {len(papers)} results")
            except Exception as e:
                print(f"  arxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = preprint_search_title(args.title, config, 'biorxiv', use_browser=True)
                all_papers.extend(papers)
                print(f"  biorxiv: {len(papers)} results")
            except Exception as e:
                print(f"  biorxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = preprint_search_title(args.title, config, 'medrxiv', use_browser=True)
                all_papers.extend(papers)
                print(f"  medrxiv: {len(papers)} results")
            except Exception as e:
                print(f"  medrxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = pubmed_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"  pubmed: {len(papers)} results")
            except Exception as e:
                print(f"  pubmed: error - {e}", file=sys.stderr)
            try:
                papers, _ = scholar_search_title(args.title, config, 10, chrome_port=_shared_chrome_port)
                all_papers.extend(papers)
                print(f"  scholar: {len(papers)} results")
            except Exception as e:
                print(f"  scholar: error - {e}", file=sys.stderr)

            if all_papers:
                merged = _merge_dedup(all_papers)
                tw = set(args.title.lower().split())
                for p in merged:
                    pw = set(p['title'].lower().split())
                    p['_score'] = len(tw & pw) / max(len(tw), 1)
                merged.sort(key=lambda x: x.get('_score', 0), reverse=True)
                for p in merged:
                    p.pop('_score', None)
                print(f"  Merged: {len(merged)} unique papers (from {len(all_papers)} total)")
                papers = merged
            else:
                papers = []
        finally:
            pass  # Chrome cleanup is at end of function

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:5]
    if not papers:
        print("No matching papers found.")
        return 1

    result = _show_or_download(papers, args, config, single_best=True)
    _kill_shared_chrome()
    return result


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
    ok = download_paper(paper, config, args.pdf_dir, args.metadata_dir, state,
                        use_browser=args.browser)
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
        result = download_paper(p, config, args.pdf_dir, args.metadata_dir, state,
                                use_browser=args.browser)
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

  # Google Scholar — requires --browser
  paper_cli.py search -k "deep learning single cell" -s scholar -n 3 --browser
  paper_cli.py find -t "CRISPR editing methylation" -s scholar --browser

  # Browser-assisted downloads (Cloudflare bypass / publisher redirects)
  paper_cli.py search -k "methylation" -s biorxiv -n 2 --browser
  paper_cli.py search -k "deep learning" -s pubmed -n 1 --browser

sources: arxiv, biorxiv, medrxiv, pubmed, scholar"""


def _add_output_args(p):
    """Add shared output-directory options to a sub-parser."""
    p.add_argument('--pdf-dir', default='data/pdf',
                   help='Directory for downloaded PDFs (default: data/pdf)')
    p.add_argument('--metadata-dir', default='data/metadata',
                   help='Directory for paper metadata JSON (default: data/metadata)')
    p.add_argument('--state-file', default='data/downloaded.json',
                   help='Download-tracking state file (default: data/downloaded.json)')


def _add_browser_arg(p):
    """Add --browser flag to a sub-parser."""
    p.add_argument('--browser', action='store_true',
                   help='Use Chrome browser for PDF downloads. ' +
                        'bioRxiv/medRxiv: bypasses Cloudflare. ' +
                        'PubMed: follows DOI to publisher page for PDF. ' +
                        'Requires: google-chrome, pip install playwright')


def main():
    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Unified paper search & download CLI — arXiv, bioRxiv, medRxiv, PubMed, Google Scholar.',
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
    _add_browser_arg(sp)

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
    _add_browser_arg(fp)

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
    _add_browser_arg(gp)

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
