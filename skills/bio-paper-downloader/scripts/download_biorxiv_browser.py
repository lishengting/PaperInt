#!/usr/bin/env python3
"""
Download PDFs from bioRxiv / medRxiv using a real Chrome browser.

Bypasses Cloudflare by launching a real (headed) Chrome instance with a
persistent user-data directory, then connecting to it via CDP. Cloudflare's
JavaScript challenge runs in the real browser and passes normally.

Usage:
    python3 download_biorxiv_browser.py "10.1101/2025.01.01.123456" -o ./papers
    python3 download_biorxiv_browser.py "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"

Requirements: google-chrome (or chromium), pip install playwright
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
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_chrome():
    """Find an available Chrome/Chromium binary."""
    for name in ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']:
        try:
            subprocess.run([name, '--version'], capture_output=True, check=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def extract_doi(url_or_doi):
    """Extract DOI and determine server (biorxiv / medrxiv)."""
    if '/' in url_or_doi and '://' not in url_or_doi:
        return url_or_doi.strip(), 'biorxiv'

    parsed = urlparse(url_or_doi)
    path = parsed.path
    host = parsed.netloc or ''

    server = 'medrxiv' if 'medrxiv' in host else 'biorxiv'

    for pat in [r'/content/(10\.\d+/[\w.\-]+?)\.full\.pdf$',
                r'/content/(10\.\d+/[\w.\-]+)$']:
        m = re.search(pat, path)
        if m:
            return m.group(1), server

    return None, server


# ---------------------------------------------------------------------------
# Browser launcher
# ---------------------------------------------------------------------------

class ChromeInstance:
    """Manage a Chrome browser process with CDP enabled."""

    def __init__(self, chrome_bin='google-chrome', profile_dir=None, port=None):
        self.chrome_bin = chrome_bin
        self.profile_dir = profile_dir or os.path.join(
            tempfile.gettempdir(), 'paper_cli_chrome_profile')
        self.port = port or _find_free_port()
        self.process = None

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)

        # Kill any process already on our port
        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
        time.sleep(1)

        self.process = subprocess.Popen([
            self.chrome_bin,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-sandbox',
            '--disable-gpu',
            'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           preexec_fn=os.setsid)

        time.sleep(2)
        print(f"  [browser] Chrome PID {self.process.pid} on port {self.port}",
              file=sys.stderr)

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
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

async def _wait_cloudflare(page, timeout=60):
    """Wait for a Cloudflare JS challenge to resolve."""
    for _ in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            title = await page.title()
            if 'moment' not in title.lower():
                return True
        except Exception:
            # Page may have redirected/navigated — check URL
            if 'cloudflare' not in page.url.lower():
                return True
    return False


async def download_via_browser(url_or_doi, output_dir, chrome_bin=None, timeout=60):
    """
    Download a PDF from bioRxiv/medRxiv via a real Chrome browser.

    Returns dict: {success, file_path, file_size, message}
    """
    from playwright.async_api import async_playwright

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    doi, server = extract_doi(url_or_doi)
    if not doi:
        result['message'] = f'Could not extract DOI from: {url_or_doi}'
        return result

    article_url = f"https://www.{server}.org/content/{doi}"
    pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
    safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
    output_path = Path(output_dir) / safe_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [browser] DOI: {doi}  server: {server}", file=sys.stderr)
    print(f"  [browser] PDF URL: {pdf_url}", file=sys.stderr)

    chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome())

    try:
        chrome.start()

        async with async_playwright() as p:
            print(f"  [browser] Connecting to {chrome.cdp_url}", file=sys.stderr)
            browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
            ctx = browser.contexts[0]

            # Step 1 — navigate to homepage, pass Cloudflare challenge
            print(f"  [browser] Passing Cloudflare...", file=sys.stderr)
            page = await ctx.new_page()
            await page.goto(f'https://www.{server}.org/', wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            if not await _wait_cloudflare(page, 30):
                result['message'] = 'Cloudflare challenge did not resolve'
                return result
            print(f"  [browser] Cloudflare passed", file=sys.stderr)

            # Step 2 — load article page
            print(f"  [browser] Loading article page...", file=sys.stderr)
            await page.goto(article_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            await asyncio.sleep(3)

            # Step 3 — load PDF page
            print(f"  [browser] Loading PDF page...", file=sys.stderr)
            pdf_page = await ctx.new_page()
            await pdf_page.goto(pdf_url, wait_until='domcontentloaded',
                                timeout=timeout * 1000)
            await asyncio.sleep(3)

            ct = await pdf_page.evaluate('() => document.contentType')
            if ct != 'application/pdf':
                result['message'] = f'Expected PDF but got Content-Type: {ct}'
                return result

            # Step 4 — fetch PDF bytes via in-page JS
            print(f"  [browser] Fetching PDF data...", file=sys.stderr)
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

            if isinstance(js_result, dict) and 'error' in js_result:
                result['message'] = f"JS fetch failed: {js_result['error']}"
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
            result['message'] = f'OK: {len(pdf_bytes)} bytes'
            return result

    except ImportError:
        result['message'] = (
            'Playwright not installed. Run: pip install playwright && playwright install chromium')
        return result
    except Exception as e:
        result['message'] = f'Browser download error: {e}'
        return result
    finally:
        chrome.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Download bioRxiv / medRxiv PDF via real Chrome browser')
    p.add_argument('url_or_doi', help='bioRxiv/medRxiv URL or DOI')
    p.add_argument('-o', '--output-dir', default='.',
                   help='Output directory for PDF (default: .)')
    p.add_argument('--chrome-bin', default=None,
                   help='Path to Chrome binary (auto-detect if omitted)')
    p.add_argument('--timeout', type=int, default=60,
                   help='Page load timeout in seconds (default: 60)')
    args = p.parse_args()

    result = asyncio.run(download_via_browser(
        args.url_or_doi, args.output_dir, args.chrome_bin, args.timeout))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
