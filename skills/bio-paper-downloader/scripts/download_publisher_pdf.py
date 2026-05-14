#!/usr/bin/env python3
"""
Download PDFs from publisher websites via DOI resolution.

Follows a DOI to the publisher's article page, locates the PDF download link,
and fetches the PDF through a real Chrome browser. Works with Nature, Springer,
and other publishers that include PDF links on their article pages.

Usage:
    python3 download_publisher_pdf.py "10.1038/s41586-024-07903-1" -o ./papers
    python3 download_publisher_pdf.py --pmid 34265844 -o ./papers

Requirements: google-chrome, pip install playwright
"""

import argparse
import asyncio
import base64
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse


# ---------------------------------------------------------------------------
# Chrome launcher
# ---------------------------------------------------------------------------

class ChromeInstance:
    """Manage a Chrome browser process with CDP enabled."""

    def __init__(self, chrome_bin='google-chrome', profile_dir=None, port=None,
                 headless=True):
        self.chrome_bin = chrome_bin
        self.profile_dir = profile_dir or os.path.join(
            tempfile.gettempdir(), 'paper_cli_publisher_chrome')
        self.port = port or _find_free_port()
        self.process = None
        self.headless = headless

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)
        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
        time.sleep(1)
        args = [
            self.chrome_bin,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run', '--no-default-browser-check',
            '--no-sandbox', '--disable-gpu',
        ]
        if self.headless:
            args.insert(1, '--headless=new')
        args.append('about:blank')
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid)
        time.sleep(2)

    @property
    def cdp_url(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except Exception:
                    pass


def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _pick_chrome():
    for name in ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']:
        try:
            subprocess.run([name, '--version'], capture_output=True, check=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


# ---------------------------------------------------------------------------
# DOI helpers
# ---------------------------------------------------------------------------

def doi_from_pmid(pmid):
    """Look up DOI for a PubMed ID via E-utilities."""
    url = ('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
           f'?db=pubmed&id={pmid}&retmode=json')
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'PaperInt/1.0 (mailto:example@example.com)'})
        import json
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            result = data.get('result', {}).get(str(pmid), {})
            dois = result.get('doi', '')
            return dois if dois else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------

async def _wait_for_page(page, timeout=30):
    """Wait for anti-bot challenges (Cloudflare/reCAPTCHA) to resolve."""
    for _ in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            t = (await page.title()).lower()
            if 'moment' not in t and 'recaptcha' not in t and 'checking' not in t:
                return True
        except Exception:
            if 'cloudflare' not in page.url.lower():
                return True
    return False


async def _do_download_via_publisher(doi_url, output_path, chrome_bin, timeout,
                                       headless):
    """Core download logic. Returns dict result."""
    from playwright.async_api import async_playwright

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}
    mode = 'headless' if headless else 'headed'

    chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                            headless=headless)

    try:
        chrome.start()

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
            ctx = browser.contexts[0]
            page = await ctx.new_page()

            # Step 1: follow DOI to publisher page
            print(f"  [publisher:{mode}] Following DOI...", file=sys.stderr)
            await page.goto(doi_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            if not await _wait_for_page(page, 30):
                result['message'] = 'Anti-bot challenge did not resolve'
                return result
            print(f"  [publisher:{mode}] Landed on: {page.url}", file=sys.stderr)

            # Step 2: find PDF links on the article page
            pdf_links = await page.evaluate('''() => {
                const found = [];
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href');
                    const text = (a.innerText || '').toLowerCase().trim();
                    if (href && href.endsWith('.pdf')) {
                        found.push({href: href, text: text});
                    }
                });
                return found;
            }''')

            if not pdf_links:
                result['message'] = 'No PDF links found on publisher page'
                return result

            # De-duplicate and pick the best PDF link
            seen = set()
            unique = []
            for l in pdf_links:
                h = l['href']
                if h not in seen:
                    seen.add(h)
                    unique.append(l)

            print(f"  [publisher:{mode}] PDF links found: {len(unique)}", file=sys.stderr)

            def _score(link):
                s = 0
                t = link['text']
                h = link['href']
                if 'download pdf' in t:
                    s += 100
                if 'supplement' in t or 'esm' in h.lower():
                    s -= 50
                if 'reporting summary' in t:
                    s -= 50
                s -= len(h)
                return s

            best = max(unique, key=_score)
            pdf_href = best['href']

            if pdf_href.startswith('/'):
                parsed = urlparse(page.url)
                pdf_url = f'{parsed.scheme}://{parsed.netloc}{pdf_href}'
            elif not pdf_href.startswith('http'):
                pdf_url = urljoin(page.url, pdf_href)
            else:
                pdf_url = pdf_href

            print(f"  [publisher:{mode}] PDF URL: {pdf_url}", file=sys.stderr)

            # Step 3: navigate to PDF
            print(f"  [publisher:{mode}] Downloading PDF...", file=sys.stderr)
            await page.goto(pdf_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            await asyncio.sleep(3)

            ct = await page.evaluate('() => document.contentType')
            if ct != 'application/pdf':
                result['message'] = f'Expected PDF but got Content-Type: {ct}'
                return result

            # Step 4: fetch PDF bytes
            js_result = await page.evaluate("""
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

            if isinstance(js_result, dict) and 'error' in js_result:
                result['message'] = f"PDF fetch failed: {js_result['error']}"
                return result

            pdf_bytes = base64.b64decode(js_result['data'])

            if len(pdf_bytes) < 10000:
                result['message'] = f'PDF too small ({len(pdf_bytes)} bytes)'
                result['file_size'] = len(pdf_bytes)
                return result

            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)

            result['success'] = True
            result['file_path'] = str(output_path)
            result['file_size'] = len(pdf_bytes)
            result['message'] = f'OK ({mode}): {len(pdf_bytes)} bytes'
            return result

    except ImportError:
        result['message'] = 'Playwright not installed'
        return result
    except Exception as e:
        result['message'] = f'Publisher download error ({mode}): {e}'
        return result
    finally:
        chrome.stop()


async def download_via_publisher(doi=None, pmid=None, output_dir='.',
                                  chrome_bin=None, timeout=60):
    """
    Download a paper PDF via DOI → publisher page → PDF link.

    Tries headless Chrome first (3 attempts), then falls back to headed (1 attempt).

    Returns dict: {success, file_path, file_size, message}
    """
    # Resolve PMID to DOI if needed
    if pmid and not doi:
        doi = doi_from_pmid(pmid)
        if not doi:
            return {'success': False, 'message': f'Could not find DOI for PMID {pmid}'}

    if not doi:
        return {'success': False, 'message': 'No DOI provided'}

    doi_url = f'https://doi.org/{doi}'
    safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
    output_path = Path(output_dir) / safe_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [publisher] DOI: {doi}", file=sys.stderr)
    print(f"  [publisher] URL: {doi_url}", file=sys.stderr)

    # Try headless first (3 attempts, but skip retries on anti-bot)
    for attempt in range(3):
        if attempt > 0:
            delay = 5 * attempt
            print(f"  [publisher] headless retry {attempt+1}/3 after {delay}s...", file=sys.stderr)
            time.sleep(delay)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=True)
        if result['success']:
            return result
        print(f"  [publisher] headless failed: {result['message']}", file=sys.stderr)
        if 'Anti-bot' in result.get('message', ''):
            break

    # Fallback to headed (3 attempts)
    print(f"  [publisher] falling back to headed Chrome...", file=sys.stderr)
    for attempt in range(3):
        if attempt > 0:
            delay = 5 * attempt
            print(f"  [publisher] headed retry {attempt+1}/3 after {delay}s...", file=sys.stderr)
            time.sleep(delay)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=False)
        if result['success']:
            result['message'] = result['message'].replace('(headed)', '(headed fallback)')
            return result
        print(f"  [publisher] headed failed: {result['message']}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Download paper PDF via DOI → publisher page')
    p.add_argument('--doi', default=None, help='Paper DOI')
    p.add_argument('--pmid', default=None, help='PubMed ID (resolves DOI automatically)')
    p.add_argument('-o', '--output-dir', default='.',
                   help='Output directory for PDF (default: .)')
    p.add_argument('--chrome-bin', default=None,
                   help='Path to Chrome binary (auto-detect if omitted)')
    p.add_argument('--timeout', type=int, default=60,
                   help='Page load timeout in seconds (default: 60)')
    args = p.parse_args()

    if not args.doi and not args.pmid:
        p.error('Either --doi or --pmid is required')

    result = asyncio.run(download_via_publisher(
        doi=args.doi, pmid=args.pmid, output_dir=args.output_dir,
        chrome_bin=args.chrome_bin, timeout=args.timeout))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
