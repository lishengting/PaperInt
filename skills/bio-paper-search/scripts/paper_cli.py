#!/usr/bin/env python3
"""
Bio Paper Search CLI — search bioinformatics papers across multiple sources.

Sources:  arxiv  |  biorxiv  |  medrxiv  |  pubmed  |  scholar  |  cnsp

Usage:
  paper_cli.py search [-k keywords] [-s source] [-n N] [-l] [--start-date DATE] [--end-date DATE]
  paper_cli.py find   -t title    [-s source] [-l]

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

def _ts():
    return datetime.now().strftime('[%H:%M:%S]')


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


# ---------------------------------------------------------------------------
# CNSP journal abbreviation lookup (loaded lazily)
# ---------------------------------------------------------------------------

_JOURNAL_ABBREV_MAP: dict[str, str] | None = None

def _cnsp_abbrev_map() -> dict[str, str]:
    """Return {journal_name_lower: nlm_abbrev} for all CNSP journals.

    Maps both full names (e.g., 'nature communications') and NLM abbreviations
    (e.g., 'nat commun') to the canonical NLM abbreviation.  Handles &/and
    variants.  Loaded once and cached.
    """
    global _JOURNAL_ABBREV_MAP
    if _JOURNAL_ABBREV_MAP is not None:
        return _JOURNAL_ABBREV_MAP

    import json as _json
    from pathlib import Path as _Path

    m: dict[str, str] = {}
    config_dir = _Path(__file__).resolve().parent / 'journals_config'
    for fname in ['nature_journals.json', 'science_journals.json',
                  'cell_journals.json', 'plos_journals.json']:
        fp = config_dir / fname
        if not fp.is_file():
            continue
        for j in _json.loads(fp.read_text()):
            name = j['name']
            abbrev = j.get('abbrev', '')
            if not abbrev:
                continue
            clean = name.replace(' (partner)', '')
            for variant in {clean, name, clean.replace('&', 'and'), clean.replace('&', '&amp;')}:
                m[variant.lower()] = abbrev
            m[abbrev.lower()] = abbrev

    _JOURNAL_ABBREV_MAP = m
    return m


def _add_abbrev_to_paper(paper: dict) -> dict:
    """If the paper's journal matches a CNSP journal, add its NLM abbrev."""
    journal = (paper.get('journal', '') or '').strip()
    category = (paper.get('category', '') or '').strip()
    candidate = journal or (category.split('/', 1)[1].strip() if '/' in category else '')
    if not candidate:
        return paper
    abbrev = _cnsp_abbrev_map().get(candidate.lower())
    if abbrev:
        paper['abbrev'] = abbrev
    return paper


def delay(config):
    return cfg(config, 'download.request_delay_seconds', 3)


# ---------------------------------------------------------------------------
# DOI → journal name resolver (lazily cached)
# ---------------------------------------------------------------------------

_JOURNAL_CACHE: dict[str, tuple[str, str]] = {}

def _extract_issn_from_crossref_message(msg: dict) -> str:
    """Extract ISSN from a Crossref API message dict, preferring print ISSN."""
    issn_types = msg.get('issn-type', [])
    for it in issn_types:
        if it.get('type') == 'print':
            return it.get('value', '')
    for it in issn_types:
        if it.get('type') == 'electronic':
            return it.get('value', '')
    issn_list = msg.get('ISSN', [])
    return issn_list[0] if issn_list else ''


def _resolve_journal_by_doi(doi: str, config) -> tuple[str, str]:
    """Look up a DOI via Crossref API and return (journal, issn) tuple."""
    if not doi:
        return ('', '')
    cached = _JOURNAL_CACHE.get(doi)
    if cached is not None:
        return cached

    url = f'https://api.crossref.org/works/{urllib.parse.quote(doi, safe="")}'
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config, attempts=2) as r:
            data = json.loads(r.read().decode('utf-8'))
        msg = data.get('message', {})
        journal = (msg.get('container-title') or [''])[0]
        issn = _extract_issn_from_crossref_message(msg)
    except Exception:
        journal = ''
        issn = ''

    _JOURNAL_CACHE[doi] = (journal, issn)
    return (journal, issn)


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
                    print(f"{_ts()}   HTTP 429 rate-limited, waiting {round(wait)}s...", file=sys.stderr)
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
    # All attempts failed — raise the last error
    raise last_err


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def arxiv_api(query, config, max_results=50):
    max_results = min(max_results, cfg(config, 'apis.arxiv.max_results', 2000))
    base = cfg(config, 'apis.arxiv.search_url', 'https://export.arxiv.org/api/query')
    url = f"{base}?search_query={urllib.parse.quote(query)}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    print(f"{_ts()}   arXiv API: {url}")
    time.sleep(delay(config))
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config) as r:
            return r.read().decode('utf-8')
    except Exception as e:
        print(f"{_ts()}   arXiv error: {e}", file=sys.stderr)
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
            doi=f"10.48550/arXiv.{aid}",
            arxiv_id=aid))
    return papers


def _parse_arxiv_search_html(html, max_results=50):
    """Extract paper metadata from arXiv web search results page."""
    import html as _html
    papers = []
    # Each result is in <li class="arxiv-result">
    parts = html.split('<li class="arxiv-result">')[1:]  # skip first (header)
    for part in parts[:max_results]:
        # arXiv ID from the abs link
        id_m = re.search(r'/abs/(\d+\.\d+)', part)
        if not id_m:
            continue
        aid = id_m.group(1)
        # Title
        title_m = re.search(r'<p class="title is-5 mathjax">\s*(.*?)\s*</p>', part, re.DOTALL)
        title = _html.unescape(re.sub(r'<[^>]+>', '', title_m.group(1)).strip()) if title_m else ''
        # Authors
        authors = ''
        auth_m = re.search(r'<p class="authors">(.*?)</p>', part, re.DOTALL)
        if auth_m:
            auth_text = re.sub(r'<[^>]+>', '', auth_m.group(1))
            auth_text = auth_text.replace('Authors:', '').strip()
            authors_list = [a.strip() for a in auth_text.split(',')]
            authors = ', '.join(authors_list[:5])
            if len(authors_list) > 5:
                authors += ' et al.'
        # Abstract
        abstract = ''
        abs_m = re.search(r'<p class="abstract mathjax">(.*?)</p>', part, re.DOTALL)
        if abs_m:
            abstract = re.sub(r'<[^>]+>', '', abs_m.group(1)).replace('Abstract:', '').strip()
            abstract = abstract.replace('&hellip;', '...').replace('…', '...')
            # Remove "△ Less" suffix
            abstract = re.sub(r'\s*△\s*Less\s*$', '', abstract)
        # Date
        date = ''
        date_m = re.search(r'(?:submitted|originally announced)\s+(\d{1,2}\s+\w+,\s+\d{4})', part)
        if date_m:
            try:
                date = datetime.strptime(date_m.group(1), '%d %B, %Y').strftime('%Y-%m-%d')
            except ValueError:
                pass
        # Category (first tag)
        category = ''
        cat_m = re.search(r'<span class="tag is-small is-link[^"]*"[^>]*>([^<]+)</span>', part)
        if cat_m:
            category = cat_m.group(1).strip()

        papers.append(make_paper(
            'arxiv', aid, title, authors, abstract, date, category,
            f"https://arxiv.org/pdf/{aid}.pdf",
            f"https://arxiv.org/abs/{aid}",
            doi=f"10.48550/arXiv.{aid}", arxiv_id=aid))
    return papers


def _arxiv_search_browser(keywords, config, max_results=50, start_date=None, end_date=None):
    """Search arXiv via advanced search URL in headless browser (fallback when API is rate-limited)."""
    chrome_port = _get_or_start_chrome()
    term = ' AND '.join(f'all:"{kw}"' for kw in keywords) if len(keywords) > 1 else keywords[0]
    params = (
        f'advanced=&terms-0-operator=AND&terms-0-term={urllib.parse.quote(term)}'
        f'&terms-0-field=all'
        f'&classification-physics_archives=all&classification-include_cross_list=include'
        f'&date-year=&date-filter_by=date_range'
        f'&date-from_date={start_date}&date-to_date={end_date or datetime.now().strftime("%Y-%m-%d")}'
        f'&date-date_type=submitted_date'
        f'&abstracts=show&size=50&order=-announced_date_first'
    ) if start_date else (
        f'advanced=&terms-0-operator=AND&terms-0-term={urllib.parse.quote(term)}'
        f'&terms-0-field=all'
        f'&classification-physics_archives=all&classification-include_cross_list=include'
        f'&date-year=&date-filter_by=all_dates'
        f'&date-date_type=submitted_date'
        f'&abstracts=show&size=50&order=-announced_date_first'
    )
    url = f'https://arxiv.org/search/advanced?{params}'

    async def _scrape():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
            last_err = None
            for attempt in range(3):
                page = await browser.contexts[0].new_page()
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    html = await page.content()
                    if 'Rate exceeded' in html or 'rate exceeded' in html.lower():
                        if attempt < 2:
                            wait = 10 * (attempt + 1)
                            print(f"  arXiv browser rate-limited, waiting {wait}s...", file=sys.stderr)
                            time.sleep(wait)
                            continue
                    return html
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        wait = 5 * (attempt + 1)
                        print(f"  arXiv browser fallback retry {attempt + 2}/3, waiting {wait}s...", file=sys.stderr)
                        time.sleep(wait)
                finally:
                    await page.close()
            raise last_err

    import asyncio as _asyncio
    try:
        html = _asyncio.run(_scrape())
        papers = _parse_arxiv_search_html(html, max_results)
        if not papers:
            # Debug: show what page we got
            title_m = re.search(r'<title>([^<]*)</title>', html)
            print(f"  arXiv browser: url={url}", file=sys.stderr)
            print(f"  arXiv browser: page title='{title_m.group(1) if title_m else '?'}', "
                  f"html len={len(html)}, arxiv-result count={html.count('arxiv-result')}", file=sys.stderr)
            print(f"  arXiv browser: html preview={html[:500]}", file=sys.stderr)
        return papers
    except Exception as e:
        print(f"{_ts()}   arXiv browser fallback failed: {e}", file=sys.stderr)
        return []


def arxiv_search(keywords, config, max_results=50, start_date=None, end_date=None):
    q = ' AND '.join(f'all:"{kw}"' for kw in keywords)
    if start_date:
        s = start_date.replace('-', '') + '000000'
        e = (end_date or datetime.now().strftime('%Y-%m-%d')).replace('-', '') + '235959'
        q += f' AND submittedDate:[{s} TO {e}]'
    xml_data = arxiv_api(q, config, max_results)
    if xml_data:
        return arxiv_parse(xml_data)
    # API rate-limited — fall back to browser-based web search
    print("  arXiv API failed, trying browser fallback...", file=sys.stderr)
    return _arxiv_search_browser(keywords, config, max_results, start_date, end_date)


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

    print(f"{_ts()}   Searching {server}...")
    print(f"{_ts()}   URL: {base}/details/{server}/{start}/{end}/0")

    while len(all_papers) < max_results and scanned < max_scan:
        url = f"{base}/details/{server}/{start}/{end}/{cursor}"
        req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
        try:
            with _urlopen_with_retry(req, config, attempts=3) as r:
                data = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            print(f"{_ts()}   {server} error: {e}", file=sys.stderr)
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
                published_doi = p.get('published', '')
                journal = ''
                issn = ''
                if published_doi:
                    journal, issn = _resolve_journal_by_doi(published_doi.strip(), config)
                if not journal:
                    journal = p.get('category', '') or server
                extras = {}
                if issn:
                    extras['issn'] = issn
                all_papers.append(make_paper(
                    server, doi, p.get('title', ''), p.get('authors', ''),
                    p.get('abstract', ''), p.get('date', ''), journal,
                    f"https://www.{server}.org/content/{doi}.full.pdf",
                    f"https://www.{server}.org/content/{doi}",
                    doi=doi,
                    extra=extras if extras else None))

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

    print(f"{_ts()}   Searching {server}...")
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


def _get_or_start_chrome(force_headed=False):
    """Start Chrome with CDP. Delegates to cnsp.browser_utils.start_chrome()."""
    from cnsp.browser_utils import start_chrome
    return start_chrome(force_headed=force_headed)

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
        print(f"{_ts()}   PubMed error: {e}", file=sys.stderr)
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
            print(f"{_ts()}   PubMed efetch error: {e}", file=sys.stderr)
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
    esearch_url = f"{PUBMED_BASE}/esearch.fcgi?{urllib.parse.urlencode(params)}"
    print(f"{_ts()}   PubMed API: {esearch_url}")
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

        issns = info.get('issn', '') or info.get('essn', '') or ''

        papers.append(make_paper(
            'pubmed', pmid, title, authors, abstract,
            info.get('pubdate', ''), '',
            pdf_url, abs_url, pmid=pmid,
            extra={'pmc_id': pmc_id, 'journal': info.get('source', ''),
                   'issn': issns,
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
# Crossref
# ---------------------------------------------------------------------------

def crossref_search(keywords, config, max_results=50, start_date=None, end_date=None):
    """Search Crossref API for papers matching keywords."""
    max_results = min(max_results, 1000)  # Crossref API limit
    query = ' '.join(keywords)
    params = {
        'query': query,
        'rows': str(max_results),
        'sort': 'published',
        'order': 'desc',
    }
    if start_date:
        end = end_date or datetime.now().strftime('%Y-%m-%d')
        params['filter'] = f'from-pub-date:{start_date},until-pub-date:{end}'

    encoded = urllib.parse.urlencode(params, doseq=True)
    url = f"https://api.crossref.org/works?{encoded}"
    print(f"{_ts()}   Crossref API: {url}")

    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config, attempts=3) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"{_ts()}   Crossref error: {e}", file=sys.stderr)
        return [], 0

    items = data.get('message', {}).get('items', [])
    total = data.get('message', {}).get('total-results', 0)

    papers = []
    for item in items:
        doi = (item.get('DOI') or '').strip().lower()
        if not doi:
            continue
        title = (item.get('title') or [''])[0]
        if not title:
            continue

        authors_list = item.get('author', [])
        authors = ', '.join(
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in authors_list[:5]
        )
        if len(authors_list) > 5:
            authors += ' et al.'

        abstract = item.get('abstract', '') or ''
        abstract = re.sub(r'<[^>]+>', ' ', abstract).strip()
        abstract = re.sub(r'\s+', ' ', abstract)

        date = ''
        for date_field in ('published-print', 'published-online', 'created'):
            date_parts = item.get(date_field, {}).get('date-parts', [[None]])[0]
            if date_parts and date_parts[0]:
                date = '-'.join(str(d) for d in date_parts if d)
                break

        journal = (item.get('container-title') or [''])[0]
        issn = _extract_issn_from_crossref_message(item)

        pdf_url = ''
        for link in item.get('link', []):
            if link.get('content-type') == 'application/pdf':
                pdf_url = link.get('URL', '')
                break

        abs_url = item.get('URL', '') or f"https://doi.org/{doi}"

        extra = {}
        if journal:
            extra['journal'] = journal
        if issn:
            extra['issn'] = issn
        papers.append(make_paper(
            'crossref', doi, title, authors, abstract, date, journal,
            pdf_url, abs_url, doi=doi,
            extra=extra if extra else None,
        ))
        _add_abbrev_to_paper(papers[-1])

    return papers, total


def crossref_search_title(title, config, max_results=10):
    """Search Crossref by title."""
    papers, total = crossref_search([title], config, max_results)
    tw = set(title.lower().split())
    for p in papers:
        pw = set(p['title'].lower().split())
        p['_score'] = len(tw & pw) / max(len(tw), 1)
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers[:max_results], total


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------

def europepmc_search(keywords, config, max_results=50, start_date=None, end_date=None):
    """Search Europe PMC API for papers matching keywords."""
    max_results = min(max_results, 1000)  # Europe PMC API limit
    query_parts = []
    for kw in keywords:
        if ' ' in kw:
            query_parts.append(f'"{kw}"')
        else:
            query_parts.append(kw)
    query = ' AND '.join(query_parts)

    if start_date:
        end = end_date or datetime.now().strftime('%Y-%m-%d')
        query += f' AND FIRST_PDATE:[{start_date} TO {end}]'

    params = {
        'query': query,
        'format': 'json',
        'pageSize': str(max_results),
        'resultType': 'lite',
    }

    encoded = urllib.parse.urlencode(params, doseq=True)
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{encoded}"
    print(f"{_ts()}   Europe PMC API: {url}")

    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    try:
        with _urlopen_with_retry(req, config, attempts=3) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"{_ts()}   Europe PMC error: {e}", file=sys.stderr)
        return [], 0

    results = data.get('resultList', {}).get('result', [])
    total = data.get('hitCount', 0)

    papers = []
    for r in results:
        doi = (r.get('doi') or '').strip().lower()
        if not doi:
            continue
        title = (r.get('title') or '').strip()
        if not title:
            continue

        authors = r.get('authorString', '') or ''
        abstract = r.get('abstractText', '') or ''

        date = ''
        for key in ('firstPublicationDate', 'electronicPublicationDate'):
            val = r.get(key)
            if val:
                try:
                    date = datetime.strptime(val[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
                    break
                except ValueError:
                    pass
        if not date and r.get('pubYear'):
            date = f"{r['pubYear']}-01-01"

        journal = r.get('journalTitle', '') or ''
        issn_raw = r.get('journalIssn', '') or ''
        issn = issn_raw.split(';')[0].strip()
        pmid = r.get('pmid')
        pmcid = r.get('pmcid')

        pdf_url = ''
        if pmcid:
            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/main.pdf"

        abs_url = ''
        if pmid:
            abs_url = f"https://europepmc.org/article/med/{pmid}"
        elif doi:
            abs_url = f"https://doi.org/{doi}"

        extra = {}
        if pmcid:
            extra['pmc_id'] = pmcid
        if journal:
            extra['journal'] = journal
        if issn:
            extra['issn'] = issn
        papers.append(make_paper(
            'europepmc', doi, title, authors, abstract, date, journal,
            pdf_url, abs_url, doi=doi, pmid=pmid,
            extra=extra if extra else None,
        ))
        _add_abbrev_to_paper(papers[-1])

    return papers, total


def europepmc_search_title(title, config, max_results=10):
    """Search Europe PMC by title."""
    papers, total = europepmc_search([title], config, max_results)
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

    print(f"{_ts()}   Searching Google Scholar...")
    print(f"{_ts()}   URL: {url}")

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
                    print(f"{_ts()}   Google Scholar blocked us (captcha/IP rate limit)", file=sys.stderr)
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

SOURCE_PRIORITY = {'pubmed': 0, 'crossref': 1, 'europepmc': 2, 'scholar': 3, 'arxiv': 4, 'medrxiv': 5, 'biorxiv': 6}


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


def search_all(keywords, config, max_results=10, sort_by='date', chrome_port=None,
               start_date=None, end_date=None):
    all_papers = []

    try:
        papers = arxiv_search(keywords, config, max_results=max_results * 3,
                             start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"{_ts()}   arxiv: {len(papers)} results")
    except Exception as e:
        print(f"{_ts()}   arxiv: error - {e}", file=sys.stderr)

    try:
        papers, _ = preprint_search(keywords, config, 'biorxiv', max_results=max_results * 3,
                                     start_date=start_date, end_date=end_date)
        for p in papers:
            _add_abbrev_to_paper(p)
        all_papers.extend(papers)
        print(f"{_ts()}   biorxiv: {len(papers)} results")
    except Exception as e:
        print(f"{_ts()}   biorxiv: error - {e}", file=sys.stderr)

    try:
        papers, _ = preprint_search(keywords, config, 'medrxiv', max_results=max_results * 3,
                                     start_date=start_date, end_date=end_date)
        for p in papers:
            _add_abbrev_to_paper(p)
        all_papers.extend(papers)
        print(f"{_ts()}   medrxiv: {len(papers)} results")
    except Exception as e:
        print(f"{_ts()}   medrxiv: error - {e}", file=sys.stderr)

    try:
        papers, total = pubmed_search(keywords, config, max_results=max_results * 3,
                                       start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"{_ts()}   pubmed: {len(papers)} results (total: {total})")
    except Exception as e:
        print(f"{_ts()}   pubmed: error - {e}", file=sys.stderr)

    try:
        papers, total = crossref_search(keywords, config, max_results=max_results * 3,
                                          start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"{_ts()}   crossref: {len(papers)} results (total: {total})")
    except Exception as e:
        print(f"{_ts()}   crossref: error - {e}", file=sys.stderr)

    try:
        papers, total = europepmc_search(keywords, config, max_results=max_results * 3,
                                           start_date=start_date, end_date=end_date)
        all_papers.extend(papers)
        print(f"{_ts()}   europepmc: {len(papers)} results (total: {total})")
    except Exception as e:
        print(f"{_ts()}   europepmc: error - {e}", file=sys.stderr)

    # Scholar disabled by default due to aggressive anti-bot protection.
    # To use scholar, run explicitly with -s scholar.
    # try:
    #     papers, _ = scholar_search(keywords, config, max_results=max_results, chrome_port=chrome_port)
    #     all_papers.extend(papers)
    #     print(f"{_ts()}   scholar: {len(papers)} results")
    # except Exception as e:
    #     print(f"{_ts()}   scholar: error - {e}", file=sys.stderr)

    try:
        from cnsp import cnsp_search
        papers = cnsp_search(keywords, config, max_results=max_results * 3,
                             start_date=start_date, end_date=end_date,
                             chrome_port=chrome_port)
        all_papers.extend(papers)
        print(f"{_ts()}   cnsp: {len(papers)} results")
    except Exception as e:
        print(f"{_ts()}   cnsp: error - {e}", file=sys.stderr)

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

    print(f"{_ts()}   Merged: {len(merged)} unique papers (from {len(all_papers)} total)")
    return merged[:max_results]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

SOURCES = ['arxiv', 'biorxiv', 'medrxiv', 'pubmed', 'scholar', 'crossref', 'europepmc', 'cnsp', 'all']


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
    print(f"{_ts()} Saved: {n} new paper(s) to database ({len(papers) - n} already existed)")
    return n


def _save_to_db_upsert(papers, config):
    """Save search results with upsert-by-DOI (for CNSP source)."""
    conn = get_conn(config)
    n = upsert_search_results(conn, papers)
    print(f"{_ts()} Saved/updated: {n} paper(s) to database")
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
            if source == 'all':
                row = conn.execute(
                    "SELECT MAX(search_date) FROM papers"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(search_date) FROM papers WHERE source = ?", (source,)
                ).fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0]).strftime('%Y-%m-%d'), end_date
        days_back = cfg(config, 'search.incremental_days_back', 7)
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

    print(f"{_ts()} Source: {source}  |  Keywords: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
    print(f"{_ts()} Target: {'all (no limit)' if unlimited else f'{num} paper(s)'}\n")

    # Resolve date range for sources that support it
    start_date, end_date = None, None
    if source in ('arxiv', 'biorxiv', 'medrxiv', 'pubmed', 'crossref', 'europepmc', 'cnsp', 'all'):
        start_date, end_date = _resolve_start_date(args, config, source)
        if start_date:
            print(f"{_ts()} Date range: {start_date} — {end_date}")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search(keywords, config, max_results=max(num * 5, 20),
                             start_date=start_date, end_date=end_date)
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search(keywords, config, source,
                                          max_results=num * 5,
                                          start_date=start_date, end_date=end_date)
        for p in papers:
            _add_abbrev_to_paper(p)
    elif source == 'pubmed':
        papers, scanned = pubmed_search(keywords, config, max_results=num * 5,
                                         start_date=start_date, end_date=end_date)
    elif source == 'scholar':
        papers, scanned = scholar_search(keywords, config, max_results=num)
    elif source == 'crossref':
        papers, scanned = crossref_search(keywords, config, max_results=num * 5,
                                           start_date=start_date, end_date=end_date)
    elif source == 'europepmc':
        papers, scanned = europepmc_search(keywords, config, max_results=num * 5,
                                            start_date=start_date, end_date=end_date)
    elif source == 'all':
        _shared_chrome_port = _get_or_start_chrome()
        papers = search_all(keywords, config, max_results=num,
                           sort_by='date', chrome_port=_shared_chrome_port,
                           start_date=start_date, end_date=end_date)
    elif source == 'cnsp':
        from cnsp import cnsp_search
        cnsp_journals = getattr(args, 'cnsp_journals', None) or None
        papers = cnsp_search(keywords, config, max_results=num * 3,
                             start_date=start_date, end_date=end_date,
                             cnsp_journals=cnsp_journals)
    else:
        print(f"{_ts()} Unknown source: {source}", file=sys.stderr)
        return 1

    if scanned:
        print(f"{_ts()} Scanned {scanned}, found {len(papers)}")
    papers = papers[:num]

    if not papers:
        print(f"{_ts()}No papers found.")
        return 1

    _show_results(papers)
    if not args.list:
        if source == 'cnsp':
            _save_to_db_upsert(papers, config)
        else:
            _save_to_db(papers, config)
    _kill_shared_chrome()
    return 0


def _crossref_lookup(doi, config):
    """Resolve a DOI via Crossref API and return a paper list."""
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    req = urllib.request.Request(url, headers={'User-Agent': ua(config)})
    with _urlopen_with_retry(req, config, attempts=3) as r:
        data = json.loads(r.read())
    msg = data.get('message', {})
    title = (msg.get('title') or [''])[0]
    authors_list = msg.get('author', [])
    authors = ', '.join(
        (a.get('given', '') + ' ' + a.get('family', '')).strip()
        for a in authors_list[:5]
    )
    if len(authors_list) > 5:
        authors += ' et al.'
    abstract = msg.get('abstract', '') or ''
    date_parts = msg.get('published-print', {}).get('date-parts', [[None]])[0]
    date = '-'.join(str(d) for d in date_parts if d) if date_parts and date_parts[0] else ''
    journal = (msg.get('container-title') or [''])[0]
    issn = _extract_issn_from_crossref_message(msg)
    extra = {}
    if journal:
        extra['journal'] = journal
    if issn:
        extra['issn'] = issn
    return [make_paper(
        'crossref', doi, title, authors, abstract, date, journal,
        '', f"https://doi.org/{doi}", doi=doi,
        extra=extra if extra else None,
    )]


def cmd_find(args, config):
    if not args.title and not args.doi:
        print("Error: either --title or --doi is required", file=sys.stderr)
        return 1

    # DOI lookup path
    if args.doi:
        doi = args.doi.strip()
        for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
                break
        try:
            papers = _crossref_lookup(doi, config)
        except Exception as e:
            print(f"{_ts()}   DOI lookup failed: {e}", file=sys.stderr)
            return 1
        _show_results(papers)
        if not args.list:
            _save_to_db(papers, config)
        return 0

    source = args.source or cfg(config, 'search.default_source', 'arxiv')

    print(f"{_ts()} Source: {source}  |  Title: {args.title}\n")

    scanned = 0
    if source == 'arxiv':
        papers = arxiv_search_title(args.title, config, 10)
    elif source in ('biorxiv', 'medrxiv'):
        papers, scanned = preprint_search_title(args.title, config, source,
                                                  use_browser=True)
    elif source == 'pubmed':
        papers, scanned = pubmed_search_title(args.title, config, 10)
    elif source == 'scholar':
        papers, scanned = scholar_search_title(args.title, config, 5)
    elif source == 'crossref':
        papers, scanned = crossref_search_title(args.title, config, 10)
    elif source == 'europepmc':
        papers, scanned = europepmc_search_title(args.title, config, 10)
    elif source == 'all':
        global _shared_chrome_port
        _shared_chrome_port = _get_or_start_chrome()
        try:
            all_papers = []
            try:
                papers = arxiv_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"{_ts()}   arxiv: {len(papers)} results")
            except Exception as e:
                print(f"{_ts()}   arxiv: error - {e}", file=sys.stderr)
            try:
                papers, _ = pubmed_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"{_ts()}   pubmed: {len(papers)} results")
            except Exception as e:
                print(f"{_ts()}   pubmed: error - {e}", file=sys.stderr)
            try:
                papers, _ = crossref_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"{_ts()}   crossref: {len(papers)} results")
            except Exception as e:
                print(f"{_ts()}   crossref: error - {e}", file=sys.stderr)
            try:
                papers, _ = europepmc_search_title(args.title, config, 10)
                all_papers.extend(papers)
                print(f"{_ts()}   europepmc: {len(papers)} results")
            except Exception as e:
                print(f"{_ts()}   europepmc: error - {e}", file=sys.stderr)
            # Scholar disabled by default due to aggressive anti-bot protection.
            # try:
            #     papers, _ = scholar_search_title(args.title, config, 10, chrome_port=_shared_chrome_port)
            #     all_papers.extend(papers)
            #     print(f"{_ts()}   scholar: {len(papers)} results")
            # except Exception as e:
            #     print(f"{_ts()}   scholar: error - {e}", file=sys.stderr)

            if all_papers:
                merged = _merge_dedup(all_papers)
                tw = set(args.title.lower().split())
                for p in merged:
                    pw = set(p['title'].lower().split())
                    p['_score'] = len(tw & pw) / max(len(tw), 1)
                merged.sort(key=lambda x: x.get('_score', 0), reverse=True)
                for p in merged:
                    p.pop('_score', None)
                print(f"{_ts()}   Merged: {len(merged)} unique papers (from {len(all_papers)} total)")
                papers = merged
            else:
                papers = []
        finally:
            pass

    if scanned:
        print(f"{_ts()} Scanned {scanned}, found {len(papers)}")
    papers = papers[:5]
    if not papers:
        print(f"{_ts()}No matching papers found.")
        return 1

    _show_results(papers)
    if not args.list:
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

  # Google Scholar (disabled by default, use -s scholar explicitly)
  paper_cli.py search -k "deep learning single cell" -s scholar -n 3

  # CNSP journal scraping (browser auto-starts)
  paper_cli.py search -k "CRISPR" -s cnsp -n 3
  paper_cli.py search -k "CRISPR" -s cnsp -n 3 --start-date 2026-05-01 --end-date 2026-05-13
  paper_cli.py search -k "genomic" -s cnsp -n 5 --incremental
  paper_cli.py search -k "methylation" -s cnsp -n 2 --cnsp-journals Nature Science

sources: arxiv, biorxiv, medrxiv, pubmed, scholar, crossref, europepmc, cnsp, all"""


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
                    help='Preview only, do not save to database')
    sp.add_argument('--start-date', help='Search from this date (YYYY-MM-DD). '
                    'Supported by arxiv, biorxiv, medrxiv, pubmed, crossref, europepmc, cnsp.')
    sp.add_argument('--end-date', help='Search until this date (YYYY-MM-DD). Default: today.')
    sp.add_argument('--incremental', action='store_true',
                    help='Auto-compute start_date from last crawl date for this source.')
    sp.add_argument('--cnsp-journals', nargs='+',
                    help='Limit CNSP search to specific journals (e.g., "Nature" "Nature Biotechnology").')

    # ---- find ----
    fp = sub.add_parser(
        'find',
        help='Search for a paper by title or DOI',
        description='Look up a paper by title or DOI.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fp.add_argument('-t', '--title',
                    help='Paper title to search for')
    fp.add_argument('-d', '--doi',
                    help='Look up paper by DOI (e.g., 10.1038/s41586-023-00000-0)')
    fp.add_argument('-s', '--source', choices=SOURCES,
                    help='Paper source to search (default: search.default_source from config)')
    fp.add_argument('-l', '--list', action='store_true',
                    help='Preview only, do not save to database')

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