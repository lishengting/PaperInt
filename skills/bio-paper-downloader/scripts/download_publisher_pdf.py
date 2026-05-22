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
import atexit
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

import log_utils; log_utils.install()

try:
    from playwright_stealth import Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

try:
    from captcha_solver import try_solve_captcha
    _CAPTCHA_AVAILABLE = True
except ImportError:
    _CAPTCHA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Virtual display (Xvfb) — for headed Chrome on headless servers
# ---------------------------------------------------------------------------

_XVFB_PROC = None
_XVFB_DISPLAY = None


def _xvfb_start():
    """Start an Xvfb virtual display, reusing the one found for this process.

    Scans for a free display number on first call, then reuses it.
    Registers atexit cleanup so Xvfb is killed when the process exits.
    """
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None and _XVFB_PROC.poll() is None:
        return True

    if _XVFB_DISPLAY is None:
        import socket
        for d in range(99, 110):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(f'/tmp/.X11-unix/X{d}')
                sock.close()
            except (socket.error, FileNotFoundError):
                _XVFB_DISPLAY = f':{d}'
                break
        if _XVFB_DISPLAY is None:
            _XVFB_DISPLAY = ':99'

    try:
        _XVFB_PROC = subprocess.Popen(
            ['Xvfb', _XVFB_DISPLAY, '-screen', '0', '1920x1080x24', '-ac'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid)
        os.environ['DISPLAY'] = _XVFB_DISPLAY
        atexit.register(_xvfb_stop)
        time.sleep(0.5)
        return True
    except FileNotFoundError:
        return False


def _xvfb_stop():
    """Stop the Xvfb virtual display started by this process."""
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None:
        try:
            os.killpg(os.getpgid(_XVFB_PROC.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            _XVFB_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_XVFB_PROC.pid), signal.SIGKILL)
            except Exception:
                pass
        _XVFB_PROC = None
        _XVFB_DISPLAY = None


def _cleanup_orphan_chrome_and_xvfb():
    """Kill orphan Chrome/Xvfb processes left by dead parent processes.

    Checks all Chrome processes with our temp-profile pattern and Xvfb
    processes on display 99-110. If the parent process no longer exists,
    the browser/display is an orphan and we kill it to free resources.
    """
    try:
        for pid_str in os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='replace')
            except (FileNotFoundError, PermissionError):
                continue

            # Check if this is a Chrome with our temp profile or an Xvfb
            is_ours = ('paper_cli_chrome_' in cmdline or
                       'paper_cli_pub_chrome_' in cmdline or
                       'paper_cli_scholar_chrome' in cmdline)
            is_xvfb = cmdline.startswith('Xvfb') and any(
                f':{d}' in cmdline for d in range(99, 111))

            if not is_ours and not is_xvfb:
                continue

            # Check if parent still exists
            try:
                with open(f'/proc/{pid}/stat', 'rb') as f:
                    stat = f.read().decode('utf-8', errors='replace')
                ppid = int(stat.split(') ')[1].split()[0])
                if ppid == 1:
                    # Reparented to init — definitely orphaned
                    pass
                elif ppid > 1:
                    os.kill(ppid, 0)
                    continue  # Parent exists, skip
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                pass  # Parent gone, kill the orphan

            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except (FileNotFoundError, PermissionError):
        pass  # /proc not available, skip cleanup


# ---------------------------------------------------------------------------
# Chrome launcher
# ---------------------------------------------------------------------------

class ChromeInstance:
    """Manage a Chrome browser process with CDP enabled.

    When headless=False, launches a virtual X display (Xvfb) so headed
    Chrome can render without a physical screen.
    """

    def __init__(self, chrome_bin='google-chrome', profile_dir=None, port=None,
                 headless=True, xvfb=True):
        self.chrome_bin = chrome_bin
        self.profile_dir = profile_dir or tempfile.mkdtemp(prefix='paper_cli_pub_chrome_')
        self.port = port or _find_free_port()
        self.process = None
        self.headless = headless
        self.xvfb = xvfb

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)

        # Clean up orphan Chrome/Xvfb from previous crashed runs
        _cleanup_orphan_chrome_and_xvfb()

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
        env = os.environ.copy()

        if self.headless:
            args.insert(1, '--headless=new')
            # Hide automation flags from detection
            args.insert(2, '--disable-blink-features=AutomationControlled')
        else:
            if self.xvfb:
                if not _xvfb_start():
                    raise RuntimeError('Xvfb is required for headed Chrome on headless servers')
                env['DISPLAY'] = os.environ.get('DISPLAY', ':99')
            else:
                if 'DISPLAY' not in env:
                    raise RuntimeError('No DISPLAY available for headed Chrome (level 3 requires a real display)')
            print(f"  [publisher] Virtual display: {env.get('DISPLAY', 'system')}", file=sys.stderr)

        args.append('about:blank')
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid, env=env)
        time.sleep(2)
        # Verify Chrome is alive — if it crashed, poll() will be non-None
        if self.process.poll() is not None:
            raise RuntimeError(f'Chrome exited immediately (code {self.process.returncode})')

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

async def _wait_for_page(page, timeout=30, captcha_enabled=False,
                         captcha_api_key='', log_prefix=''):
    """Wait for anti-bot challenges to resolve, watching for captcha widgets.

    If the page stays blocked, periodically check if a Turnstile/reCAPTCHA
    widget has appeared in the DOM and solve it via 2Captcha.
    """
    if captcha_enabled and captcha_api_key and _CAPTCHA_AVAILABLE:
        print(f"{log_prefix} 2Captcha: watching for captcha widget (max {timeout}s)",
              file=sys.stderr)
    for i in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            t = (await page.title()).lower()
            u = page.url.lower()
            blocked = ('moment' in t or 'recaptcha' in t or 'checking' in t or
                      'challenge' in t or 'captcha' in t or 'attention required' in t or
                      'cloudflare' in u or 'challenge' in u or 'access denied' in t)
            if not blocked:
                return True
        except Exception:
            if 'cloudflare' not in page.url.lower():
                return True
        # Still blocked — check if a captcha widget has appeared
        if captcha_enabled and captcha_api_key and _CAPTCHA_AVAILABLE:
            solved = await try_solve_captcha(page, captcha_enabled,
                                              api_key=captcha_api_key,
                                              log_prefix=log_prefix,
                                              quiet=True)
            if solved:
                print(f"{log_prefix} 2Captcha: captcha solved, waiting for redirect...",
                      file=sys.stderr)
    return False


async def _wait_for_url_stable(page, max_wait=20, stable_secs=3):
    """Wait for URL to stop changing for `stable_secs` consecutive seconds."""
    prev_url = page.url
    stable_count = 0
    for _ in range(max_wait):
        await asyncio.sleep(1)
        try:
            cur_url = page.url
        except Exception:
            stable_count = 0
            continue
        if cur_url == prev_url:
            stable_count += 1
            if stable_count >= stable_secs:
                return cur_url
        else:
            stable_count = 0
            prev_url = cur_url
    return prev_url


async def _safe_eval(page, js, retries=3):
    """Evaluate JS on a page, retrying on navigation errors."""
    for attempt in range(retries):
        try:
            return await page.evaluate(js)
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(2)
    raise Exception('Page navigation destroyed execution context')


async def _do_download_via_publisher(doi_url, output_path, chrome_bin, timeout,
                                       headless, profile_dir=None, wait=10, xvfb=True,
                                       captcha_enabled=False, captcha_api_key='',
                                       stealth_enabled=False):
    """Core download logic. Returns dict result."""
    from playwright.async_api import async_playwright

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}
    mode = 'headless' if headless else 'headed'

    chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                            headless=headless,
                            profile_dir=profile_dir,
                            xvfb=xvfb)

    try:
        chrome.start()

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
            ctx = browser.contexts[0]
            page = await ctx.new_page()
            if _STEALTH_AVAILABLE and stealth_enabled:
                await Stealth().apply_stealth_async(page)

            # Step 1: follow DOI to publisher page
            print(f"  [publisher:{mode}] Following DOI...", file=sys.stderr)
            await page.goto(doi_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            # Elsevier redirect chain (doi.org → linkinghub → cell.com):
            # wait for JS-driven navigations to settle before touching the page
            final_url = await _wait_for_url_stable(page)
            print(f"  [publisher:{mode}] Landed on: {final_url}", file=sys.stderr)

            if not await _wait_for_page(page, 30,
                                         captcha_enabled=captcha_enabled,
                                         captcha_api_key=captcha_api_key,
                                         log_prefix=f'  [publisher:{mode}]'):
                result['message'] = 'Anti-bot challenge did not resolve'
                return result

            # Verify page has real content (bot pages have very few links)
            try:
                link_count = await _safe_eval(page, '() => document.querySelectorAll("a").length')
            except Exception:
                result['message'] = 'Page navigation destroyed execution context'
                return result
            if link_count < 5:
                result['message'] = f'Page has no content ({link_count} links), likely blocked'
                return result

            # Step 2: find PDF links on the article page
            try:
                pdf_links = await _safe_eval(page, '''() => {
                const found = [];
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.innerText || '').toLowerCase().trim();
                    // Direct PDF, showPdf (Cell/Elsevier), /pdf/ path, or text hint
                    const isPdf = href.endsWith('.pdf') ||
                        href.includes('showPdf') ||
                        href.includes('/pdf/') ||
                        href.includes('download') ||
                        text.includes('pdf') || text.includes('download');
                    // Exclude citation/reference export links
                    const isCitation = href.includes('citation-needed') ||
                        href.includes('format=refman') ||
                        href.includes('format=ris') ||
                        href.includes('format=bibtex') ||
                        (text.includes('citation') && !text.includes('pdf'));
                    if (isPdf && !isCitation && href && !href.startsWith('#')) {
                        found.push({href: href, text: text});
                    }
                });
                return found;
            }''')
            except Exception:
                result['message'] = 'Page navigation destroyed execution context'
                return result

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
                if 'showpdf' in h.lower():
                    s += 80
                if 'supplement' in t or 'esm' in h.lower():
                    s -= 50
                if 'reporting summary' in t:
                    s -= 50
                if 'citation' in t or 'citation-needed' in h:
                    s -= 200
                if any(x in h for x in ('format=refman', 'format=ris', 'format=bibtex')):
                    s -= 200
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

            # Step 3: fetch PDF (JS fetch from page context — works for both
            # inline display and Content-Disposition:attachment responses)
            print(f"  [publisher:{mode}] Downloading PDF...", file=sys.stderr)

            # Navigate to the PDF URL first, so the page session has access
            try:
                await page.goto(pdf_url, wait_until='domcontentloaded',
                                timeout=timeout * 1000)
                await asyncio.sleep(wait)
            except Exception:
                # "Download is starting" or timeout — page may not have loaded,
                # but the session is still valid for fetching the PDF URL
                pass

            # Fetch PDF bytes via JS, using the explicit PDF URL (not
            # window.location.href, which may be wrong after navigation failure)
            js_result = await page.evaluate("""
                async ([url]) => {
                    const r = await fetch(url, {credentials: 'include'});
                    if (!r.ok) return {error: 'HTTP ' + r.status};
                    const blob = await r.blob();
                    return new Promise(resolve => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve({data: reader.result.split(',')[1], size: blob.size});
                        reader.onerror = () => resolve({error: 'FileReader failed'});
                        reader.readAsDataURL(blob);
                    });
                }
            """, [pdf_url])

            if isinstance(js_result, dict) and 'error' in js_result:
                result['message'] = f"PDF fetch failed: {js_result['error']}"
                return result

            pdf_bytes = base64.b64decode(js_result['data'])

            if len(pdf_bytes) < 10000 or not pdf_bytes[:5] == b'%PDF-':
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
                                  chrome_bin=None, timeout=60,
                                  fallback_level=2, wait=10,
                                  captcha_enabled=False, captcha_api_key='',
                                  stealth_enabled=False):
    """
    Download a paper PDF via DOI → publisher page → PDF link.

    fallback_level:
      0 — not applicable (should not be called without browser)
      1 — headless Chrome only (2 attempts)
      2 — headless → Xvfb headed Chrome (default)
      3 — headless → Xvfb headed → system display headed

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

    # Shared profile dir so retries share cookies
    profile_dir = os.path.join(output_dir, 'chrome_profile')
    os.makedirs(profile_dir, exist_ok=True)

    # Save original DISPLAY — _xvfb_start() overwrites it globally
    _orig_display = os.environ.get('DISPLAY')

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    def _is_fatal(msg):
        """Chrome startup failures — retrying won't help."""
        return 'ECONNREFUSED' in msg or 'Xvfb is required' in msg or 'No DISPLAY' in msg

    # Try headless first (up to 2 attempts), break immediately on anti-bot
    for attempt in range(2):
        if attempt > 0:
            print(f"  [publisher] headless retry 2/2 after 5s...", file=sys.stderr)
            time.sleep(5)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=True,
                                                    profile_dir=profile_dir,
                                                    wait=wait,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=stealth_enabled)
        if result['success']:
            # ...
            return result
        print(f"  [publisher] headless failed: {result['message']}", file=sys.stderr)
        if _is_fatal(result.get('message', '')):
            return result
        if 'Anti-bot' in result.get('message', ''):
            break

    if fallback_level < 2:
        return result

    # Fallback to Xvfb headed (3 attempts)
    print(f"  [publisher] falling back to headed Chrome (Xvfb)...", file=sys.stderr)
    for attempt in range(3):
        if attempt > 0:
            print(f"  [publisher] headed retry {attempt+1}/3 after 5s...", file=sys.stderr)
            time.sleep(5)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=False,
                                                    profile_dir=profile_dir,
                                                    wait=wait, xvfb=True,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=stealth_enabled)
        if result['success']:
            # ...
            return result
        msg = result.get('message', '')
        print(f"  [publisher] headed (xvfb) failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
            return result
        if 'Anti-bot' in msg or 'No PDF links' in msg:
            break

    if fallback_level < 3:
        return result

    # Restore original DISPLAY before system display fallback.
    # _xvfb_start() polluted os.environ with its virtual display (:99).
    if _orig_display:
        os.environ['DISPLAY'] = _orig_display
    else:
        os.environ.pop('DISPLAY', None)

    # Fallback to system display headed (3 attempts)
    print(f"  [publisher] falling back to headed Chrome (system display)...", file=sys.stderr)
    for attempt in range(3):
        if attempt > 0:
            print(f"  [publisher] headed (system) retry {attempt+1}/3 after 5s...", file=sys.stderr)
            time.sleep(5)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=False,
                                                    profile_dir=profile_dir,
                                                    wait=wait, xvfb=False,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=stealth_enabled)
        if result['success']:
            # ...
            return result
        msg = result.get('message', '')
        print(f"  [publisher] headed (system) failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
            return result
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
    p.add_argument('--wait', type=int, default=10,
                   help='Post-navigation wait in seconds (default: 10)')
    p.add_argument('--fallback-level', type=int, default=2, choices=[0, 1, 2, 3],
                   help='Browser fallback level (0=no-browser, 1=headless, 2=+xvfb, 3=+system-display)')
    p.add_argument('--captcha', action='store_true', default=False,
                   help='Enable 2Captcha solving for anti-bot challenges (default: off)')
    p.add_argument('--twocap-api', default='',
                   help='2Captcha API key (resolved from config.yaml download.twocaptcha_api_key_env)')
    p.add_argument('--stealth', action='store_true', default=False,
                   help='Enable playwright-stealth (default: off)')
    args = p.parse_args()

    if not args.doi and not args.pmid:
        p.error('Either --doi or --pmid is required')

    result = asyncio.run(download_via_publisher(
        doi=args.doi, pmid=args.pmid, output_dir=args.output_dir,
        chrome_bin=args.chrome_bin, timeout=args.timeout,
        fallback_level=args.fallback_level, wait=args.wait,
        captcha_enabled=args.captcha,
        captcha_api_key=args.twocap_api,
        stealth_enabled=args.stealth))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
