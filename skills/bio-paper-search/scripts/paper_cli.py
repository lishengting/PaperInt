#!/usr/bin/env python3
"""
Bio Paper Search CLI — search bioinformatics papers across multiple sources.

Sources:  arxiv  |  biorxiv  |  medrxiv  |  pubmed  |  scholar  |  cnsp

Usage:
  paper_cli.py search [-k keywords] [-s source] [-n N] [-l] [--browser] [--start-date DATE] [--end-date DATE]
  paper_cli.py find   -t title    [-s source] [-l] [--browser]

Results are saved to the shared SQLite database (data/papers.db).
Use bio-paper-downloader to download found papers.
"""
import argparse
import json
import os
import re
import random
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'scripts'))
from paper_db import get_conn, get_db_path, insert_search_results, upsert_search_results

# ---------------------------------------------------------------------------
# SSL workaround for older servers (bioRxiv, etc.)
# ---------------------------------------------------------------------------

def _ssl_context():
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


def _urlopen_with_retry(req, config, attempts=4, backoff=2):
    last_err = None
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=tout(config), context=_ssl_context())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                if i < attempts - 1:
                    retry_after = e.headers.get('Retry-After', '')
                    try:
                        wait = int(retry_after)
                    except (ValueError, TypeError):
                        wait = backoff * (4 ** i) + random.uniform(0, backoff * 2)
                    print(f"  HTTP 429 rate-limited, waiting {round(wait)}s...", file=sys.stderr)
                    time.sleep(wait)
            elif 400 <= e.code < 500:
                raise
            elif i < attempts - 1:
                wait = backoff * (2 ** i) + random.uniform(0, backoff)
                time.sleep(wait)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                wait = backoff * (2 ** i) + random.uniform(0, backoff)
                time.sleep(wait)
    # All attempts failed — if it was 429, do one final long-wait retry
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 429:
        wait = 120 + random.uniform(0, 30)
        print(f"  HTTP 429 persisted, final long wait {round(wait)}s...", file=sys.stderr)
        time.sleep(wait)
        return urllib.request.urlopen(req, timeout=tout(config), context=_ssl_context())
    raise last_err


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def arxiv_api(query, config, max_results=50):
    max_results = min(max_results, cfg(config, 'apis.arxiv.max_results', 2000))
    base = cfg(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    url = f"{base}?search_query={urllib.parse.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    time.sleep(delay(config))
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


def arxiv_search(keywords, config, max_results=50, start_date=None, end_date=None):
    q = ' AND '.join(f'all:"{kw}"' for kw in keywords)
    if start_date:
        s = start_date.replace('-', '') + '000000'
        e = (end_date or datetime.now().strftime('%Y-%m-%d')).replace('-', '') + '235959'
        q += f' AND submittedDate:[{s} TO {e}]'
    xml_data = arxiv_api(q, config, max_results)
    return arxiv_parse(xml_data) if xml_data else []


def arxiv_search_title(title, config, max_results=10):
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

def preprint_search(keywords, config, server='biorxiv', max_results=100, max_scan=500,
                     start_date=None, end_date=None):
    base = cfg(config, f'apis.{server}.base_url', f'https://api.{server}.org')
    if start_date:
        end = end_date or datetime.now().strftime('%Y-%m-%d')
        start = start_date
    else:
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
    import asyncio as _asyncio
    from playwright.async_api import async_playwright

    query = urllib.parse.quote(title)
    url = f'https://www.{server}.org/search/{query}'

    print(f"  Searching {server}...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
        ctx = browser.contexts[0]

        page = await ctx.new_page()
        homepage = f'https://www.{server}.org/'
        await page.goto(homepage, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

        await page.goto(url, wait_until='domcontentloaded', timeout=120000)

        for _ in range(30):
            await _asyncio.sleep(4)
            page_title = await page.title()
            if 'Search Results' in page_title or 'search' in page_title.lower():
                break

        raw = await page.evaluate(f'''
            () => {{
                const results = [];
                const items = document.querySelectorAll('.highwire-article-citation, .search-result, article.search-result, li.search-result');
                if (items.length === 0) {{
                    const links = document.querySelectorAll('a[href*="/content/10."]');
                    links.forEach((a) => {{
                        if (results.length >= {max_results}) return;
                        const href = a.getAttribute('href');
                        const title = a.innerText.trim();
                        if (title && href) {{
                            results.push({{title: title, link: href, authors: '', snippet: '', doiText: href, date: ''}});
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

                        results.push({{title: title, link: link, authors: authors, snippet: snippet, doiText: doiText, date: dateText}});
                    }});
                }}
                return results;
            }}
        ''')

        await page.close()

    papers = []
    seen_dois = set()
    seen_titles = set()
    for r in raw:
        if not r['title']:
            continue

        doi = ''
        for src in [r['link'], r['doiText']]:
            m = re.search(r'(10\.\d{4,}/[^\s&]+)', src)
            if m:
                doi = m.group(1).rstrip('.')
                break

        if doi and doi in seen_dois:
            continue
        norm_title = ' '.join(r['title'].lower().split())
        if norm_title in seen_titles:
            continue

        if doi:
            seen_dois.add(doi)
        seen_titles.add(norm_title)

        paper_id = doi or r['link'].split('/')[-1] or str(hash(r['title']) % 100000000)

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


def _get_or_start_chrome(headless=True, fallback_to_headed=False):
    if headless:
        profile = os.path.join(tempfile.gettempdir(), 'paper_cli_cnsp_chrome')
    else:
        profile = os.path.join(tempfile.gettempdir(), 'paper_cli_scholar_chrome')
    os.makedirs(profile, exist_ok=True)

    r = subprocess.run(['pgrep', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        r2 = subprocess.run(['pgrep', '-a', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
        m = re.search(r'--remote-debugging-port=(\d+)', r2.stdout)
        if m:
            port = int(m.group(1))
            if _verify_chrome_port(port):
                return port

    port = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        port = s.getsockname()[1]

    subprocess.run(['pkill', '-f', f'remote-debugging-port={port}'], capture_output=True)
    time.sleep(1)

    cmd = [
        'google-chrome',
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', '--no-sandbox',
    ]
    if headless:
        cmd.append('--headless=new')
    cmd.append('about:blank')

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)

    if _verify_chrome_port(port):
        return port

    if headless and fallback_to_headed:
        print("  Headless Chrome unavailable, falling back to headed mode...", file=sys.stderr)
        return _get_or_start_chrome(headless=False, fallback_to_headed=False)

    raise RuntimeError(f"Chrome failed to start on port {port} (headless={headless})")


def _verify_chrome_port(port, timeout=5):
    """Poll Chrome's /json/version endpoint to confirm it is listening."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False

_shared_chrome_port = None

def _kill_shared_chrome():
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


BATCH_SIZE = 1000


def pubmed_fetch_abstracts(pmids, config):
    if not pmids:
        return {}
    abstracts = {}
    url = f"{PUBMED_BASE}/efetch.fcgi"
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i:i + BATCH_SIZE]
        params = urllib.parse.urlencode({
            'db': 'pubmed', 'id': ','.join(batch),
            'rettype': 'abstract', 'retmode': 'xml',
            'tool': 'PaperInt',
            'email': cfg(config, 'download.user_agent', ''),
        })
        req = urllib.request.Request(f"{url}?{params}", headers={'User-Agent': ua(config)})
        try:
            with _urlopen_with_retry(req, config, attempts=3) as r:
                xml_text = r.read().decode('utf-8')
        except Exception as e:
            print(f"  PubMed efetch error: {e}", file=sys.stderr)
            if not abstracts:
                return {}
            continue

        for m in re.finditer(
            r'<PubmedArticle>.*?<PMID[^>]*>(\d+)</PMID>.*?<Abstract>(.*?)</Abstract>',
            xml_text, re.DOTALL
        ):
            pmid = m.group(1)
            abs_text = re.sub(r'<[^>]+>', ' ', m.group(2)).strip()
            abs_text = re.sub(r'\s+', ' ', abs_text)
            abstracts[pmid] = abs_text
    return abstracts


def pubmed_search(keywords, config, max_results=50, start_date=None, end_date=None):
    query = ' AND '.join(f'{kw}[All Fields]' for kw in keywords)
    params = {
        'db': 'pubmed', 'term': query, 'retmax': str(max_results),
        'sort': 'pub+date', 'datetype': 'pdat',
    }
    if start_date:
        params['mindate'] = start_date
        params['maxdate'] = end_date or datetime.now().strftime('%Y-%m-%d')
    sr = pubmed_api('esearch.fcgi', params, config)
    if not sr:
        return [], 0

    idlist = sr.get('esearchresult', {}).get('idlist', [])
    total = int(sr.get('esearchresult', {}).get('count', 0))

    if not idlist:
        return [], total

    sm = _pubmed_esummary_batched(idlist, config)
    if not sm:
        return [], total

    papers = []
    abstracts_map = pubmed_fetch_abstracts(idlist, config)
    for pmid in idlist:
        info = sm.get('result', {}).get(pmid, {})
        if not info:
            continue
        title = info.get('title', '')
        authors = ', '.join(
            a.get('name', '') for a in info.get('authors', [])[:5])
        abstract = abstracts_map.get(pmid, '')

        pmc_id = None
        doi = None
        for aid in info.get('articleids', []):
            if aid.get('idtype') == 'pmc':
                pmc_id = aid.get('value')
            elif aid.get('idtype') == 'doi':
                doi = aid.get('value')

        pdf_url = ''
        if pmc_id:
            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf"
        abs_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        papers.append(make_paper(
            'pubmed', pmid, title, authors, abstract,
            info.get('pubdate', ''), '',
            pdf_url, abs_url, pmid=pmid,
            extra={'pmc_id': pmc_id, 'journal': info.get('source', ''),
                   'doi': info.get('elocationid', '').replace('doi: ', '') if info.get('elocationid') else doi}))

    return papers, total


def _pubmed_esummary_batched(idlist, config):
    """Call esummary.fcgi in batches, merging results."""
    merged = None
    for i in range(0, len(idlist), BATCH_SIZE):
        batch = idlist[i:i + BATCH_SIZE]
        sm = pubmed_api('esummary.fcgi', {'db': 'pubmed', 'id': ','.join(batch)}, config)
        if not sm:
            return None
        if merged is None:
            merged = sm
        else:
            merged['result'].update(sm.get('result', {}))
    return merged


def pubmed_search_title(title, config, max_results=10):
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
# Google Scholar
# ---------------------------------------------------------------------------

def scholar_search(keywords, config, max_results=10, chrome_port=None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Google Scholar search requires: pip install playwright", file=sys.stderr)
        return [], 0

    import asyncio as _asyncio
    return _asyncio.run(_scholar_search_async(keywords, config, max_results, chrome_port=chrome_port))


async def _scholar_search_async(keywords, config, max_results, chrome_port=None):
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
    import asyncio as _asyncio
    keywords = [title]
    return _asyncio.run(_scholar_search_async(keywords, config, max_results, chrome_port=chrome_port))


# ---------------------------------------------------------------------------
# All-sources search
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = {'pubmed': 0, 'scholar': 1, 'arxiv': 2, 'medrxiv': 3, 'biorxiv': 4}


def _paper_score(paper, kw_lower):
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
    w1 = set(re.sub(r'[^a-z0-9]', ' ', t1.lower()).split())
    w2 = set(re.sub(r'[^a-z0-9]', ' ', t2.lower()).split())
    if not w1 or not w2:
        return False
    return len(w1 & w2) / len(w1 | w2) >= threshold


def _merge_dedup(papers, source_priority=None):
    if source_priority is None:
        source_priority = SOURCE_PRIORITY
    merged = []
    for p in papers:
        merged_into = None
        for existing in merged:
            if p.get('doi') and existing.get('doi') and p['doi'].lower() == existing['doi'].lower():
                merged_into = existing
                break
            if p.get('arxiv_id') and existing.get('arxiv_id') and p['arxiv_id'] == existing['arxiv_id']:
                merged_into = existing
                break
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


def search_all(keywords, config, max_results=10, use_browser=False, sort_by='date', chrome_port=None,
               start_date=None, end_date=None):
    all_papers = []

    try:
        papers = arxiv_search(keywords, config, max_results=max_results * 3,
                             start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"  arxiv: {len(papers)} results")
    except Exception as e:
        print(f"  arxiv: error - {e}", file=sys.stderr)

    try:
        papers, _ = preprint_search(keywords, config, 'biorxiv', max_results=max_results * 3,
                                     start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"  biorxiv: {len(papers)} results")
    except Exception as e:
        print(f"  biorxiv: error - {e}", file=sys.stderr)

    try:
        papers, _ = preprint_search(keywords, config, 'medrxiv', max_results=max_results * 3,
                                     start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"  medrxiv: {len(papers)} results")
    except Exception as e:
        print(f"  medrxiv: error - {e}", file=sys.stderr)

    try:
        papers, total = pubmed_search(keywords, config, max_results=max_results * 3,
                                       start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"  pubmed: {len(papers)} results (total: {total})")
    except Exception as e:
        print(f"  pubmed: error - {e}", file=sys.stderr)

    try:
        papers, _ = scholar_search(keywords, config, max_results=max_results, chrome_port=chrome_port)
        all_papers.extend(papers)
        print(f"  scholar: {len(papers)} results")
    except Exception as e:
        print(f"  scholar: error - {e}", file=sys.stderr)

    try:
        from cnsp import cnsp_search
        papers = cnsp_search(keywords, config, max_results=max_results * 3,
                             start_date=start_date, end_date=end_date,
                             chrome_port=chrome_port)
        all_papers.extend(papers)
        print(f"  cnsp: {len(papers)} results")
    except Exception as e:
        print(f"  cnsp: error - {e}", file=sys.stderr)

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

SOURCES = ['arxiv', 'biorxiv', 'medrxiv', 'pubmed', 'scholar', 'cnsp', 'all']


def _show_results(papers):
    """Display search results as a list."""
    for i, p in enumerate(papers, 1):
        print(f"{i}. {p['title'][:100]}")
        print(f"   ID: {p['paper_id']}  |  {p.get('date', '?')}  |  {p['source']}")
        print(f"   PDF: {p.get('pdf_url') or '(no PDF)'}")
        print(f"   URL: {p.get('abs_url', '')}\n")


def _save_to_db(papers, config):
    """Save search results to the shared database."""
    conn = get_conn(config)
    n = insert_search_results(conn, papers)
    print(f"Saved: {n} new paper(s) to database ({len(papers) - n} already existed)")
    return n


def _save_to_db_upsert(papers, config):
    """Save search results with upsert-by-DOI (for CNSP source)."""
    conn = get_conn(config)
    n = upsert_search_results(conn, papers)
    print(f"Saved/updated: {n} paper(s) to database")
    return n


def _resolve_start_date(args, config, source):
    """Resolve start date for date-range-aware sources (--start-date or --incremental).

    Returns (start_date, end_date) as YYYY-MM-DD strings, or (None, None) if no
    date range is configured.
    """
    incremental = getattr(args, 'incremental', False)
    explicit_start = getattr(args, 'start_date', None)
    end_date = getattr(args, 'end_date', None) or datetime.now().strftime('%Y-%m-%d')

    if incremental:
        conn = get_conn(config)
        if source == 'cnsp':
            row = conn.execute(
                "SELECT MAX(search_date) FROM papers WHERE source IN ('nature','science','cell','plos')"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(search_date) FROM papers WHERE source = ?", (source,)
            ).fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0]).strftime('%Y-%m-%d'), end_date
        days_back = cfg(config, 'search.incremental_days_back', 90)
        return (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d'), end_date

    if explicit_start:
        return explicit_start, end_date

    if source == 'cnsp':
        days_back = cfg(config, 'cnsp.default_days_back', 7)
        return (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d'), end_date

    return None, None


def cmd_search(args, config):
    global _shared_chrome_port
    source = args.source or cfg(config, 'search.default_source', 'arxiv')
    keywords = args.keywords or cfg(config, 'keywords.include', [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',') if k.strip()]

    has_date_range = bool(getattr(args, 'start_date', None) or getattr(args, 'incremental', False))
    unlimited = args.num is None and has_date_range
    if unlimited:
        num = 999999
    else:
        num = args.num or cfg(config, 'search.default_num', 1)

    print(f"Source: {source}  |  Keywords: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
    print(f"Target: {'all (no limit)' if unlimited else f'{num} paper(s)'}\n")

    # Resolve date range for sources that support it
    start_date, end_date = None, None
    if source in ('arxiv', 'biorxiv', 'medrxiv', 'pubmed', 'cnsp', 'all'):
        start_date, end_date = _resolve_start_date(args, config, source)
        if start_date:
            print(f"Date range: {start_date} — {end_date}")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search(keywords, config, max_results=max(num * 5, 20),
                             start_date=start_date, end_date=end_date)
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search(keywords, config, source,
                                          max_results=num * 5,
                                          start_date=start_date, end_date=end_date)
    elif source == 'pubmed':
        papers, scanned = pubmed_search(keywords, config, max_results=num * 5,
                                         start_date=start_date, end_date=end_date)
    elif source == 'scholar':
        papers, scanned = scholar_search(keywords, config, max_results=num)
    elif source == 'all':
        chrome_port = None
        try:
            chrome_port = _get_or_start_chrome(headless=True, fallback_to_headed=args.browser)
            _shared_chrome_port = chrome_port
        except Exception as e:
            print(f"  Chrome: {e}", file=sys.stderr)
        papers = search_all(keywords, config, max_results=num, use_browser=args.browser,
                           sort_by='date', chrome_port=chrome_port,
                           start_date=start_date, end_date=end_date)
    elif source == 'cnsp':
        _shared_chrome_port = _get_or_start_chrome(headless=True, fallback_to_headed=args.browser)
        from cnsp import cnsp_search
        cnsp_journals = getattr(args, 'cnsp_journals', None) or None
        papers = cnsp_search(keywords, config, max_results=num * 3,
                             start_date=start_date, end_date=end_date,
                             cnsp_journals=cnsp_journals,
                             chrome_port=_shared_chrome_port)
    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        return 1

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:num]

    if not papers:
        print("No papers found.")
        return 1

    _show_results(papers)
    if source == 'cnsp':
        _save_to_db_upsert(papers, config)
    else:
        _save_to_db(papers, config)
    _kill_shared_chrome()
    return 0


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
        papers, scanned = scholar_search_title(args.title, config, 5)
    elif source == 'all':
        global _shared_chrome_port
        _shared_chrome_port = _get_or_start_chrome(fallback_to_headed=args.browser)
        try:
            all_papers = []
            try:
                papers = arxiv_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"  arxiv: {len(papers)} results")
            except Exception as e:
                print(f"  arxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = preprint_search_title(args.title, config, 'biorxiv', use_browser=args.browser)
                all_papers.extend(papers)
                print(f"  biorxiv: {len(papers)} results")
            except Exception as e:
                print(f"  biorxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = preprint_search_title(args.title, config, 'medrxiv', use_browser=args.browser)
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
            if args.browser:
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
            pass

    if scanned:
        print(f"Scanned {scanned}, found {len(papers)}")
    papers = papers[:5]
    if not papers:
        print("No matching papers found.")
        return 1

    _show_results(papers)
    _save_to_db(papers, config)
    _kill_shared_chrome()
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  # Keyword search
  paper_cli.py search -k "methylation,single-cell" -n 3
  paper_cli.py search -k "CRISPR" -s pubmed -n 5

  # Title search
  paper_cli.py find -t "Deep learning for single cell RNA-seq analysis"
  paper_cli.py find -t "CRISPR editing" -s pubmed

  # Google Scholar (requires --browser)
  paper_cli.py search -k "deep learning single cell" -s scholar -n 3 --browser

  # CNSP journal scraping (requires --browser)
  paper_cli.py search -k "CRISPR" -s cnsp -n 3 --browser
  paper_cli.py search -k "CRISPR" -s cnsp -n 3 --start-date 2026-05-01 --end-date 2026-05-13 --browser
  paper_cli.py search -k "genomic" -s cnsp -n 5 --incremental --browser
  paper_cli.py search -k "methylation" -s cnsp -n 2 --browser --cnsp-journals Nature Science

sources: arxiv, biorxiv, medrxiv, pubmed, scholar, cnsp, all"""


def _add_browser_arg(p):
    p.add_argument('--browser', action='store_true',
                   help='Use Chrome browser for searches. Required for Google Scholar and CNSP.')


def main():
    sys.stdout.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Bio Paper Search — search bioinformatics papers across multiple sources.\n'
                    'Run `paper_cli.py search -h` or `paper_cli.py find -h` for detailed options.',
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--config', default='config.yaml',
                   help='Path to shared YAML config file (default: config.yaml)')
    p.add_argument('--db', default=None,
                   help='Path to SQLite database (default: from config.yaml)')
    sub = p.add_subparsers(dest='cmd', required=True,
                           title='commands',
                           description='"search" by keywords or "find" by title')

    # ---- search ----
    sp = sub.add_parser(
        'search',
        help='Search papers by keyword',
        description='Keyword search: query a source for papers matching keywords.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument('-k', '--keywords',
                    help='Comma-separated search keywords (default: keywords.include from config)')
    sp.add_argument('-s', '--source', choices=SOURCES,
                    help='Paper source to search (default: search.default_source from config)')
    sp.add_argument('-n', '--num', type=int,
                    help='Max number of papers to return. '
                         'When --start-date or --incremental is set and -n is omitted, '
                         'returns all papers in range. '
                         '(default: search.default_num from config)')
    sp.add_argument('-l', '--list', action='store_true',
                    help='Preview only (results are always saved to database)')
    _add_browser_arg(sp)
    sp.add_argument('--start-date', help='Search from this date (YYYY-MM-DD). '
                    'Supported by arxiv, biorxiv, medrxiv, pubmed, cnsp.')
    sp.add_argument('--end-date', help='Search until this date (YYYY-MM-DD). Default: today.')
    sp.add_argument('--incremental', action='store_true',
                    help='Auto-compute start_date from last crawl date for this source.')
    sp.add_argument('--cnsp-journals', nargs='+',
                    help='Limit CNSP search to specific journals (e.g., "Nature" "Nature Biotechnology").')

    # ---- find ----
    fp = sub.add_parser(
        'find',
        help='Search for a specific paper by title',
        description='Title search: look up a paper by its title.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fp.add_argument('-t', '--title', required=True,
                    help='Paper title to search for (required)')
    fp.add_argument('-s', '--source', choices=SOURCES,
                    help='Paper source to search (default: search.default_source from config)')
    fp.add_argument('-l', '--list', action='store_true',
                    help='Preview only (results are always saved to database)')
    _add_browser_arg(fp)

    args = p.parse_args()
    config = load_config(args.config)

    # Override db path if specified
    if args.db:
        config.setdefault('db', {})['path'] = args.db

    if args.cmd == 'search':
        return cmd_search(args, config)
    elif args.cmd == 'find':
        return cmd_find(args, config)
    return 1


if __name__ == '__main__':
    sys.exit(main())