#!/usr/bin/env python3
"""
Download PDFs from bioRxiv / medRxiv using a real Chrome browser.

Bypasses Cloudflare by launching a real (headed) Chrome instance with a
persistent user-data directory, then connecting to it via CDP. Cloudflare's
JavaScript challenge runs in the real browser and passes normally.

Also handles generic PDF URLs by letting Chrome display the PDF, then
clicking the PDF viewer's built-in download button to save it.

Usage:
    python3 download_biorxiv_browser.py "10.1101/2025.01.01.123456" -o ./papers
    python3 download_biorxiv_browser.py "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
    python3 download_biorxiv_browser.py "https://example.com/paper.pdf" -o ./papers

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

    def __init__(self, chrome_bin='google-chrome', profile_dir=None, port=None,
                 headless=True):
        self.chrome_bin = chrome_bin
        self.profile_dir = profile_dir or tempfile.mkdtemp(prefix='paper_cli_chrome_')
        self.port = port or _find_free_port()
        self.process = None
        self.headless = headless

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)

        # Kill any process already on our port
        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
        time.sleep(1)

        args = [
            self.chrome_bin,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-sandbox',
            '--disable-gpu',
        ]
        if self.headless:
            args.insert(1, '--headless=new')
        args.append('about:blank')

        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            if 'cloudflare' not in page.url.lower():
                return True
    return False


async def _download_generic_pdf(page, url_or_doi, output_path, timeout):
    """Download a PDF that Chrome displays with its built-in viewer.

    Navigates to the PDF URL, lets Chrome render it, then clicks the
    viewer's download button to trigger a download we can capture.

    Also tries JS fetch from the page context as a fallback for sites
    without anti-bot protection.
    """
    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    print(f"  [browser] Navigating to PDF...", file=sys.stderr)
    await page.goto(url_or_doi, wait_until='domcontentloaded',
                    timeout=timeout * 1000)
    await asyncio.sleep(3)

    final_url = page.url
    ct = await page.evaluate('() => document.contentType')
    print(f"  [browser] Content-Type: {ct}", file=sys.stderr)

    # Method 1: Click the PDF viewer's built-in download button.
    # Chrome's PDF viewer (chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/)
    # has a download button we can trigger.
    try:
        print(f"  [browser] Trying viewer download button...", file=sys.stderr)
        async with page.expect_download(timeout=min(timeout, 30) * 1000) as dl_info:
            # The download button is #download in the PDF viewer toolbar.
            # It may be in shadow DOM; try multiple approaches.
            clicked = await page.evaluate('''() => {
                // Try direct ID first
                let btn = document.querySelector('#download');
                if (btn) { btn.click(); return true; }
                // Try shadow DOM in pdf-viewer element
                const viewer = document.querySelector('pdf-viewer');
                if (viewer && viewer.shadowRoot) {
                    btn = viewer.shadowRoot.querySelector('#download');
                    if (btn) { btn.click(); return true; }
                }
                // Try the embed element
                const embed = document.querySelector('embed');
                if (embed) {
                    // The embed might have its own download mechanism
                    embed.click();
                    return true;
                }
                return false;
            }''')
            if not clicked:
                print(f"  [browser] Download button not found", file=sys.stderr)
                raise Exception("Download button not found")

        download = await dl_info.value
        tmp_path = await download.path()
        if tmp_path and os.path.exists(tmp_path):
            pdf_bytes = open(tmp_path, 'rb').read()
            if pdf_bytes.startswith(b'%PDF'):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(pdf_bytes)
                result['success'] = True
                result['file_path'] = str(output_path)
                result['file_size'] = len(pdf_bytes)
                result['message'] = f'OK: {len(pdf_bytes)} bytes'
            else:
                result['message'] = 'Downloaded file is not a valid PDF'
                result['file_size'] = len(pdf_bytes)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return result
    except Exception as e:
        msg = str(e)
        if 'Timeout' in msg:
            print(f"  [browser] Download button timed out", file=sys.stderr)
        elif 'not found' in msg:
            print(f"  [browser] Download button not available", file=sys.stderr)
        else:
            print(f"  [browser] Download button approach failed: {e}", file=sys.stderr)

    # Method 2: JS fetch from page context.
    # This works for sites without anti-bot (e.g., university pages).
    try:
        print(f"  [browser] Trying JS fetch from page context...", file=sys.stderr)
        js_result = await page.evaluate("""
            async () => {
                const r = await fetch(window.location.href,
                    {credentials: 'include'});
                if (!r.ok) return {error: 'HTTP ' + r.status};
                const blob = await r.blob();
                return new Promise(resolve => {
                    const reader = new FileReader();
                    reader.onloadend = () =>
                        resolve({data: reader.result.split(',')[1],
                                 size: blob.size});
                    reader.onerror = () =>
                        resolve({error: 'FileReader failed'});
                    reader.readAsDataURL(blob);
                });
            }
        """)
        if isinstance(js_result, dict) and 'error' in js_result:
            print(f"  [browser] JS fetch failed: {js_result['error']}",
                  file=sys.stderr)
        else:
            pdf_bytes = base64.b64decode(js_result['data'])
            if pdf_bytes.startswith(b'%PDF'):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(pdf_bytes)
                result['success'] = True
                result['file_path'] = str(output_path)
                result['file_size'] = len(pdf_bytes)
                result['message'] = f'OK: {len(pdf_bytes)} bytes'
                return result
            print(f"  [browser] JS fetch got non-PDF data ({len(pdf_bytes)} bytes)",
                  file=sys.stderr)
    except Exception as e:
        print(f"  [browser] JS fetch error: {e}", file=sys.stderr)

    result['message'] = 'Unable to capture PDF data from browser'
    return result


async def _do_download_via_browser(url_or_doi, output_dir, chrome_bin, timeout,
                                     headless):
    """
    Core download logic. Returns dict result.
    """
    from playwright.async_api import async_playwright

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}
    mode = 'headless' if headless else 'headed'

    doi, server = extract_doi(url_or_doi)

    # --- Generic URL path (not a biorxiv/medrxiv DOI) ---
    if doi is None:
        parsed = urlparse(url_or_doi)
        if not parsed.scheme:
            result['message'] = f'Not a valid URL or DOI: {url_or_doi}'
            return result

        safe_name = url_or_doi.split('/')[-1].split('?')[0] or 'download'
        if not safe_name.endswith('.pdf'):
            safe_name += '.pdf'
        output_path = Path(output_dir) / safe_name

        print(f"  [browser:{mode}] Generic URL: {url_or_doi}", file=sys.stderr)
        chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                                headless=headless)

        try:
            chrome.start()

            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
                ctx = browser.contexts[0]

                # Set download path so captures go to output dir
                about_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                cdp_tmp = await ctx.new_cdp_session(about_page)
                await cdp_tmp.send('Page.setDownloadBehavior', {
                    'behavior': 'allow',
                    'downloadPath': str(output_path.parent.absolute()),
                    'eventsEnabled': True,
                })
                await cdp_tmp.detach()

                page = await ctx.new_page()
                return await _download_generic_pdf(page, url_or_doi,
                                                   output_path, timeout)

        except ImportError:
            result['message'] = 'Playwright not installed'
            return result
        except Exception as e:
            result['message'] = f'Browser download error ({mode}): {e}'
            return result
        finally:
            chrome.stop()

    # --- bioRxiv / medRxiv path ---
    article_url = f"https://www.{server}.org/content/{doi}"
    pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
    safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
    output_path = Path(output_dir) / safe_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [browser:{mode}] DOI: {doi}  server: {server}", file=sys.stderr)
    print(f"  [browser:{mode}] PDF URL: {pdf_url}", file=sys.stderr)

    chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                            headless=headless)

    try:
        chrome.start()

        async with async_playwright() as p:
            print(f"  [browser:{mode}] Connecting to {chrome.cdp_url}", file=sys.stderr)
            browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
            ctx = browser.contexts[0]

            # Step 1 — navigate to homepage, pass Cloudflare challenge
            print(f"  [browser:{mode}] Passing Cloudflare...", file=sys.stderr)
            page = await ctx.new_page()
            await page.goto(f'https://www.{server}.org/', wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            if not await _wait_cloudflare(page, 120):
                result['message'] = 'Cloudflare challenge did not resolve'
                return result
            print(f"  [browser:{mode}] Cloudflare passed", file=sys.stderr)

            # Step 2 — load article page
            print(f"  [browser:{mode}] Loading article page...", file=sys.stderr)
            await page.goto(article_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            await asyncio.sleep(3)

            # Step 3 — load PDF page (may display inline or trigger download)
            print(f"  [browser:{mode}] Loading PDF page...", file=sys.stderr)

            # Set download capture path in case Chrome downloads instead of displaying
            about_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            cdp_tmp = await ctx.new_cdp_session(about_page)
            await cdp_tmp.send('Page.setDownloadBehavior', {
                'behavior': 'allow',
                'downloadPath': str(output_path.parent.absolute()),
                'eventsEnabled': True,
            })
            await cdp_tmp.detach()

            pdf_page = await ctx.new_page()

            # Try download capture first (in case server sends attachment)
            pdf_bytes = None
            try:
                async with pdf_page.expect_download(timeout=min(timeout, 30) * 1000) as dl:
                    await pdf_page.goto(pdf_url, wait_until='commit',
                                        timeout=timeout * 1000)
                download = await dl.value
                tmp_path = await download.path()
                if tmp_path and os.path.exists(tmp_path):
                    pdf_bytes = open(tmp_path, 'rb').read()
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            except Exception:
                # Page displayed inline (or other failure), navigate normally
                try:
                    await pdf_page.goto(pdf_url, wait_until='domcontentloaded',
                                        timeout=timeout * 1000)
                except Exception:
                    pass
                await asyncio.sleep(3)

            if not pdf_bytes:
                ct = await pdf_page.evaluate('() => document.contentType')
                if ct != 'application/pdf':
                    result['message'] = f'Expected PDF but got Content-Type: {ct}'
                    return result

                # Step 4 — fetch PDF bytes via in-page JS (for inline display)
                print(f"  [browser:{mode}] Fetching PDF data via JS...", file=sys.stderr)
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
            result['message'] = f'OK ({mode}): {len(pdf_bytes)} bytes'
            return result

    except ImportError:
        result['message'] = (
            'Playwright not installed. Run: pip install playwright && playwright install chromium')
        return result
    except Exception as e:
        result['message'] = f'Browser download error ({mode}): {e}'
        return result
    finally:
        chrome.stop()


async def download_via_browser(url_or_doi, output_dir, chrome_bin=None, timeout=60,
                               headed_fallback=False):
    """
    Download a PDF from bioRxiv/medRxiv via a real Chrome browser, or any URL directly.

    Tries headless Chrome first (3 attempts), then falls back to headed if headed_fallback=True.

    Returns dict: {success, file_path, file_size, message}
    """
    # Try headless first (3 attempts, but skip retries on Cloudflare failure)
    for attempt in range(3):
        if attempt > 0:
            delay = 5 * attempt
            print(f"  [browser] headless retry {attempt+1}/3 after {delay}s...", file=sys.stderr)
            time.sleep(delay)

        result = await _do_download_via_browser(url_or_doi, output_dir,
                                                chrome_bin, timeout,
                                                headless=True)
        if result['success']:
            return result
        print(f"  [browser] headless failed: {result['message']}", file=sys.stderr)
        if 'Cloudflare' in result.get('message', ''):
            break

    if not headed_fallback:
        return result

    # Fallback to headed (3 attempts)
    print(f"  [browser] falling back to headed Chrome...", file=sys.stderr)
    for attempt in range(3):
        if attempt > 0:
            delay = 5 * attempt
            print(f"  [browser] headed retry {attempt+1}/3 after {delay}s...", file=sys.stderr)
            time.sleep(delay)

        result = await _do_download_via_browser(url_or_doi, output_dir,
                                                chrome_bin, timeout,
                                                headless=False)
        if result['success']:
            result['message'] = result['message'].replace('(headed)', '(headed fallback)')
            return result
        print(f"  [browser] headed failed: {result['message']}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Download bioRxiv / medRxiv PDF via real Chrome browser')
    p.add_argument('url_or_doi', help='bioRxiv/medRxiv URL or DOI, or any PDF URL')
    p.add_argument('-o', '--output-dir', default='.',
                   help='Output directory for PDF (default: .)')
    p.add_argument('--chrome-bin', default=None,
                   help='Path to Chrome binary (auto-detect if omitted)')
    p.add_argument('--timeout', type=int, default=60,
                   help='Page load timeout in seconds (default: 60)')
    p.add_argument('--headed-fallback', action='store_true',
                   help='Allow falling back to headed Chrome if headless fails')
    args = p.parse_args()

    result = asyncio.run(download_via_browser(
        args.url_or_doi, args.output_dir, args.chrome_bin, args.timeout,
        headed_fallback=args.headed_fallback))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
