#!/usr/bin/env python3
"""
Bio Paper Downloader CLI — download papers from search results.

Sources:  arxiv  |  biorxiv  |  medrxiv  |  pubmed  |  scholar  |  generic

Usage:
  paper_cli.py get -u URL    # download by URL
  paper_cli.py               # auto-mode: download all 'searched' papers from DB
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'scripts'))
from paper_db import (get_conn, get_db_path, get_papers_by_status,
                      is_downloaded, mark_downloaded, mark_download_failed,
                      get_paper, get_stats)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from paper_info import resolver as _pi_resolver
    from paper_info import info_md as _pi_info_md
    _PI_AVAILABLE = True
except ImportError:
    _PI_AVAILABLE = False

# ---------------------------------------------------------------------------
# SSL workaround for older servers (bioRxiv, etc.)
# ---------------------------------------------------------------------------

def _ssl_context():
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx

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


def title_to_dirname(title):
    if not title:
        return 'unknown'
    safe = re.sub(r'[()]+', '-', str(title))
    safe = re.sub(r'[/\\:*?"<>|\s;]+', '_', safe).strip('_')
    return safe[:256]


def _urlopen_with_retry(req, config, attempts=3, backoff=2):
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
# PubMed API helpers (needed for detect_url and PMC lookup)
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


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------

_shared_chrome_port = None


def _get_or_start_chrome():
    profile = os.path.join(tempfile.gettempdir(), 'paper_cli_scholar_chrome')
    os.makedirs(profile, exist_ok=True)
    r = subprocess.run(['pgrep', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        r2 = subprocess.run(['pgrep', '-a', '-f', f'user-data-dir={profile}'], capture_output=True, text=True)
        m = re.search(r'--remote-debugging-port=(\d+)', r2.stdout)
        if m:
            return int(m.group(1))
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
    if not doi:
        return None
    if _shared_chrome_port:
        import asyncio as _asyncio
        return _asyncio.run(_browser_download_cdp(doi, server, config, _shared_chrome_port))
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
    import asyncio as _asyncio
    from playwright.async_api import async_playwright

    pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
    article_url = f"https://www.{server}.org/content/{doi}"

    print(f"  Browser: connecting to Chrome on port {chrome_port}...", file=sys.stderr)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
        ctx = browser.contexts[0]

        page = await ctx.new_page()
        homepage = f'https://www.{server}.org/'
        await page.goto(homepage, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

        await page.goto(article_url, wait_until='domcontentloaded', timeout=60000)
        await _asyncio.sleep(3)

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
    try:
        req = urllib.request.Request(pdf_url, headers={'User-Agent': ua(config)})
        with _urlopen_with_retry(req, config, attempts=2) as r:
            data = r.read()
            if data.startswith(b'%PDF') or len(data) > 50000:
                return data
    except Exception:
        pass

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


# ---------------------------------------------------------------------------
# Paper info markdown generation
# ---------------------------------------------------------------------------

def _make_paper_info_identifier(paper: dict) -> str:
    doi = paper.get('doi')
    if doi:
        return doi
    arxiv_id = paper.get('arxiv_id')
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    pmid = paper.get('pmid')
    if pmid:
        return f"pmid:{pmid}"
    title = paper.get('title', '')
    if title:
        return title
    return paper.get('paper_id', '')


def _generate_info_md(paper, paper_dir, safe_pid) -> dict | None:
    if not _PI_AVAILABLE:
        print(f"  [info] paper_info not available, skipping info.md", file=sys.stderr)
        return None
    try:
        identifier = _make_paper_info_identifier(paper)
        record = _pi_resolver.get_paper(identifier, depth="full", timeout=8.0)
        md_content = _pi_info_md.generate(record)
        info_path = os.path.join(paper_dir, f"{safe_pid}.info.md")
        with open(info_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"  [info] info.md generated", file=sys.stderr)
        return record.identity.raw.get("pmc_oa")
    except Exception as e:
        print(f"  [info] info.md unavailable: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = {'pubmed': 0, 'scholar': 1, 'arxiv': 2, 'medrxiv': 3, 'biorxiv': 4}


def download_paper(paper, config, data_dir, conn, use_browser=False):
    """Download a paper. Returns True on success, False if unavailable, None if skipped."""
    pid = paper.get('paper_id', '')
    src = paper.get('source', '')

    if is_downloaded(conn, pid):
        print(f"  [skip] already downloaded")
        return None

    safe_pid = sanitize(pid)

    dirname = title_to_dirname(paper.get('title', ''))
    paper_dir = os.path.join(data_dir, dirname)
    if os.path.isdir(paper_dir):
        meta_file = os.path.join(paper_dir, f"{safe_pid}.metadata.json")
        if not os.path.exists(meta_file):
            dirname = f"{dirname}_{safe_pid}"[:256]
            paper_dir = os.path.join(data_dir, dirname)
    os.makedirs(paper_dir, exist_ok=True)

    # Save metadata
    with open(os.path.join(paper_dir, f"{safe_pid}.metadata.json"), 'w', encoding='utf-8') as f:
        json.dump(paper, f, ensure_ascii=False, indent=2)

    # Generate comprehensive info markdown
    oa_info = _generate_info_md(paper, paper_dir, safe_pid)

    print(f"  Downloading: {paper.get('title', '?')[:80]}...")

    pdf_data = None
    if src == 'arxiv':
        pdf_data = download_arxiv(paper.get('arxiv_id', pid), config)
    elif src in ('biorxiv', 'medrxiv'):
        pdf_data = download_preprint(paper.get('doi') or pid, src, config,
                                     use_browser=use_browser)
    elif src == 'scholar':
        pdf_url = paper.get('pdf_url', '')
        doi = paper.get('doi', '')
        if pdf_url and use_browser:
            for attempt in range(3):
                print(f"  Trying direct PDF from Scholar (attempt {attempt+1}/3)...", file=sys.stderr)
                pdf_data = _download_direct_pdf(pdf_url, config)
                if pdf_data:
                    break
                if attempt < 2:
                    time.sleep(5)
        if not pdf_data and doi and use_browser:
            for attempt in range(3):
                print(f"  Trying publisher via DOI {doi} (attempt {attempt+1}/3)...", file=sys.stderr)
                pdf_data = _publisher_download(doi, paper.get('pmid'), config)
                if pdf_data:
                    break
                if attempt < 2:
                    time.sleep(5)
        if not pdf_data and paper.get('arxiv_id'):
            pdf_data = download_arxiv(paper.get('arxiv_id'), config)
    elif src == 'pubmed':
        if use_browser and paper.get('doi'):
            pdf_data = _publisher_download(paper.get('doi'), paper.get('pmid'), config)
        if not pdf_data:
            pmc_has_pdf = oa_info.get('has_pdf') if oa_info else False
            if pmc_has_pdf:
                pmc_id = paper.get('pmc_id')
                if not pmc_id:
                    pmc_id = _pubmed_lookup_pmc(paper.get('pmid', pid), config)
                if pmc_id:
                    pdf_data = _download_pmc_pdf(pmc_id, config)
            else:
                print(f"  [info] not OA via PMC (has_pdf=False), skipping PMC download", file=sys.stderr)

    # Fallback: try alternative sources
    if not pdf_data and paper.get('_alt_sources'):
        alts = sorted(paper['_alt_sources'],
                      key=lambda a: SOURCE_PRIORITY.get(a.get('source'), 99))
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
                if not pdf_data and alt.get('pmid') and oa_info and oa_info.get('has_pdf'):
                    pmc_id = _pubmed_lookup_pmc(alt['pmid'], config)
                    if pmc_id:
                        pdf_data = _download_pmc_pdf(pmc_id, config)
            if pdf_data:
                print(f"  Fallback OK from {alt_src}", file=sys.stderr)
                break
            time.sleep(2)

    if pdf_data:
        with open(os.path.join(paper_dir, f"{safe_pid}.pdf"), 'wb') as f:
            f.write(pdf_data)
        mark_downloaded(conn, pid, dirname, paper)
        print(f"  OK: {len(pdf_data)} bytes")
        return True
    else:
        mark_download_failed(conn, pid, "PDF unavailable")
        print(f"  UNAVAILABLE (metadata saved)")
        return False


# ---------------------------------------------------------------------------
# URL detection for `get` command
# ---------------------------------------------------------------------------

def detect_url(url, config):
    """Parse a URL and return a paper dict, or None."""
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

    m = re.search(r'(biorxiv|medrxiv)\.org/content/(10\.\d+/[\w.\-]+?)(?:\.full)?(?:\.pdf)?$', url)
    if m:
        server = m.group(1)
        doi = m.group(2)
        return make_paper(server, doi, f'{server}:{doi}', '', '', '', '',
                          f"https://www.{server}.org/content/{doi}.full.pdf",
                          f"https://www.{server}.org/content/{doi}", doi=doi)

    pmid = None
    m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', url)
    if m:
        pmid = m.group(1)
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

    print(f"  Treating as generic PDF URL: {url}")
    name = url.split('/')[-1] or 'download'
    return make_paper('generic', sanitize(name), f'Download:{url}', '', '', '', '',
                      url, url)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_get(args, config):
    """Download a paper by URL."""
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

    conn = get_conn(config)
    ok = download_paper(paper, config, args.data_dir, conn,
                        use_browser=args.browser)
    if ok is True:
        print("Downloaded")
    elif ok is False:
        print("PDF unavailable, metadata saved")
    else:
        print("Already downloaded")
    return 0


def cmd_auto(config, data_dir, use_browser=False):
    """Auto-mode: download all papers with status='searched' from the database."""
    conn = get_conn(config)
    papers = get_papers_by_status(conn, 'searched')

    if not papers:
        stats = get_stats(conn)
        print(f"No un-downloaded papers found in database.")
        print(f"DB stats: {stats}")
        return 0

    print(f"Auto-mode: {len(papers)} paper(s) to download\n")

    ok = 0
    unavail = 0
    skipped = 0
    for i, p in enumerate(papers, 1):
        print(f"[{i}/{len(papers)}] {p['paper_id']} — {p.get('title', '?')[:80]}")
        result = download_paper(p, config, data_dir, conn, use_browser=use_browser)
        if result is True:
            ok += 1
        elif result is False:
            unavail += 1
        else:
            skipped += 1
        if i < len(papers):
            time.sleep(delay(config))

    parts = [f"{ok} downloaded"]
    if unavail:
        parts.append(f"{unavail} unavailable")
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"\nDone: {', '.join(parts)}")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

EXAMPLES = """\
examples:
  # Download by URL
  paper_cli.py get -u "https://arxiv.org/abs/2301.00001"
  paper_cli.py get -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
  paper_cli.py get -u "https://pubmed.ncbi.nlm.nih.gov/12345678/"

  # Auto-mode — download all searched papers from database
  paper_cli.py

  # Auto-mode with browser (for bioRxiv/medRxiv/PubMed publisher PDFs)
  paper_cli.py --browser

  # Preview URL without downloading
  paper_cli.py get -u "https://arxiv.org/abs/2301.00001" -l"""


def main():
    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Bio Paper Downloader — download papers from search results or URLs.',
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--config', default='config.yaml',
                   help='Path to shared YAML config file (default: config.yaml)')
    p.add_argument('--db', default=None,
                   help='Path to SQLite database (default: from config.yaml)')
    p.add_argument('--data-dir', default='data',
                   help='Directory for paper data (default: data)')
    p.add_argument('--browser', action='store_true',
                   help='Use Chrome browser for PDF downloads (bioRxiv/medRxiv/PubMed)')

    sub = p.add_subparsers(dest='cmd', required=False,
                           title='commands',
                           description='"get" by URL, or no command for auto-mode')

    # ---- get ----
    gp = sub.add_parser(
        'get',
        help='Download a paper directly from a URL',
        description='URL download: auto-detect the source from the URL pattern.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gp.add_argument('-u', '--url', required=True,
                    help='Paper URL (arXiv, bioRxiv, medRxiv, PubMed, PMC, or direct PDF)')
    gp.add_argument('-l', '--list', action='store_true',
                    help='Parse and show paper info from the URL without downloading')

    args = p.parse_args()
    config = load_config(args.config)

    if args.db:
        config.setdefault('db', {})['path'] = args.db

    if args.cmd == 'get':
        return cmd_get(args, config)
    elif args.cmd is None:
        # Auto-mode: download all searched papers
        return cmd_auto(config, args.data_dir, use_browser=args.browser)
    return 1


if __name__ == '__main__':
    sys.exit(main())