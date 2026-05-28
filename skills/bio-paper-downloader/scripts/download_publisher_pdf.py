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
                       'paper_cli_scholar_chrome' in cmdline or
                       'chrome_profile' in cmdline)
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

        # Remove stale SingletonLock left by a crashed Chrome process
        lock_file = os.path.join(self.profile_dir, 'SingletonLock')
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except OSError:
                pass

        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
        # Also kill any Chrome still holding this profile (e.g. from crashed run)
        subprocess.run(['pkill', '-f', f'user-data-dir={self.profile_dir}'],
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
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            preexec_fn=os.setsid, env=env)
        time.sleep(2)
        # Verify Chrome is alive — if it crashed, poll() will be non-None
        if self.process.poll() is not None:
            stderr = self.process.stderr.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Chrome exited immediately (code {self.process.returncode}): {stderr}')

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
    """Look up DOI for a PubMed ID via E-utilities.

    Tries three sources in order:
    1. ``doi`` field (present for most PMIDs)
    2. ``articleids`` list — look for ``idtype: doi`` (reliable)
    3. ``elocationid`` field — extract ``doi: 10.xxx/yyy`` (fallback
       for papers where the publisher hasn't registered the DOI in the
       articleids list yet)
    """
    url = ('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
           f'?db=pubmed&id={pmid}&retmode=json')
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'PaperInt/1.0 (mailto:example@example.com)'})
        import json, re
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            result = data.get('result', {}).get(str(pmid), {})
            # 1. Direct doi field
            dois = result.get('doi', '')
            if dois:
                m = re.search(r'10\.\d{4,}/[^\s]+', dois)
                if m:
                    return m.group(0)
            # 2. articleids list (most reliable)
            for aid in result.get('articleids', []) or []:
                if aid.get('idtype') == 'doi':
                    return aid['value']
            # 3. elocationid (may contain pii: + doi:)
            eloc = result.get('elocationid', '')
            if eloc:
                m = re.search(r'10\.\d{4,}/[^\s]+', eloc)
                if m:
                    return m.group(0)
            return None
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


_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.tif', '.tiff')


def _url_path(url):
    return urlparse(url).path.lower()


def _is_citation_export(url, text=''):
    haystack = f'{url} {text}'.lower()
    return any(x in haystack for x in (
        'citation-needed', 'format=refman', 'format=ris', 'format=bibtex',
        '/ris/', '/refman/', '/bibtex/', '/endnote/', 'downloadcitation')) or (
        'citation' in haystack and 'pdf' not in haystack)


def _is_obvious_non_pdf_asset(url, text=''):
    lower_url = url.lower()
    path = _url_path(url)
    lower_text = (text or '').lower()
    if path.endswith(_IMAGE_EXTENSIONS):
        return True
    if 'ars.els-cdn.com/content/image/' in lower_url:
        return True
    if re.search(r'-(?:ga|gr|fx|fig)\d+\.(?:jpe?g|png|gif|webp|svg)$', path):
        return True
    if any(x in lower_text for x in ('download image', 'download figure', 'graphical abstract')):
        return True
    return False


def _has_pdf_signal(url, text=''):
    lower_url = url.lower()
    path = _url_path(url)
    lower_text = (text or '').lower()
    return (
        path.endswith('.pdf') or
        '.pdf' in lower_url or
        '/pdf' in lower_url or
        'pdfft' in lower_url or
        'showpdf' in lower_url or
        'main.pdf' in lower_url or
        'application/pdf' in lower_text or
        'citation_pdf_url' in lower_text or
        'pdf' in lower_text)


def _score_pdf_candidate(candidate):
    url = candidate['url']
    text = candidate.get('text', '')
    lower_url = url.lower()
    lower_text = text.lower()
    if _is_citation_export(url, text) or _is_obvious_non_pdf_asset(url, text):
        return None

    has_pdf_signal = _has_pdf_signal(url, text)
    if not has_pdf_signal and 'download' not in lower_url and 'download' not in lower_text:
        return None

    score = 0
    if 'citation_pdf_url' in lower_text:
        score += 180
    if 'pdfft' in lower_url:
        score += 170
    if 'main.pdf' in lower_url:
        score += 150
    if 'showpdf' in lower_url:
        score += 140
    if _url_path(url).endswith('.pdf'):
        score += 130
    elif '.pdf' in lower_url:
        score += 100
    if 'download pdf' in lower_text or lower_text == 'pdf':
        score += 120
    if 'nature article pdf fallback' in lower_text:
        score += 70
    if 'download' in lower_text or 'download' in lower_url:
        score += 10
    if not has_pdf_signal:
        score -= 100
    if any(x in lower_text or x in lower_url for x in ('supplement', 'supplementary', 'esm', 'reporting summary')):
        score -= 100
    score -= min(len(url), 300) // 20
    return score


def _html_access_message(pdf_url, body, final_url='', content_type=''):
    preview = body[:200].decode('latin-1', errors='replace')
    preview = ' '.join(preview.split())
    haystack = f'{pdf_url} {final_url} {content_type} {preview}'.lower()
    if 'cookies_not_supported' in haystack or 'idp.nature.com/authorize' in haystack:
        return f'PDF endpoint returned HTML; publisher needs browser cookies/session ({len(body)} bytes, starts with: {preview})'
    if 'text/html' in content_type.lower() or preview.lower().startswith('<!doctype html') or preview.lower().startswith('<html'):
        return f'PDF endpoint returned HTML instead of PDF ({len(body)} bytes, starts with: {preview})'
    return f'Not a valid PDF ({len(body)} bytes, starts with: {preview})'


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
                const addCandidate = (href, text) => {
                    if (!href || href.startsWith('#')) return;
                    found.push({href: href, text: (text || '').toLowerCase().trim()});
                };
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.innerText || '').toLowerCase().trim();
                    // Direct PDF, showPdf (Cell/Elsevier), /pdf/ path, or text hint
                    const isPdf = href.includes('.pdf') ||
                        href.endsWith('.pdf') ||
                        href.includes('showPdf') ||
                        href.includes('/pdf') ||
                        href.includes('download') ||
                        text.includes('pdf') || text.includes('download');
                    // Exclude citation/reference export links
                    const isCitation = href.includes('citation-needed') ||
                        href.includes('format=refman') ||
                        href.includes('format=ris') ||
                        href.includes('format=bibtex') ||
                        href.includes('/ris/') ||
                        href.includes('/refman/') ||
                        href.includes('/bibtex/') ||
                        href.includes('/endnote/') ||
                        (text.includes('citation') && !text.includes('pdf'));
                    if (isPdf && !isCitation) {
                        addCandidate(href, text);
                    }
                });
                document.querySelectorAll('meta, link').forEach(el => {
                    const href = el.getAttribute('href') || el.getAttribute('content') || '';
                    const name = el.getAttribute('name') || el.getAttribute('property') || el.getAttribute('rel') || '';
                    if (href.includes('.pdf') || name.toLowerCase().includes('pdf')) {
                        addCandidate(href, name);
                    }
                });
                if (location.hostname.endsWith('nature.com') &&
                    location.pathname.startsWith('/articles/') &&
                    !location.pathname.endsWith('.pdf')) {
                    addCandidate(location.origin + location.pathname + '.pdf', 'nature article pdf fallback');
                }
                return found;
            }''')
            except Exception:
                result['message'] = 'Page navigation destroyed execution context'
                return result

            if not pdf_links:
                result['message'] = 'No PDF links found on publisher page'
                return result

            article_url = page.url
            candidates = {}
            for link in pdf_links:
                href = link.get('href') or ''
                if not href or href.startswith('#'):
                    continue
                url = urljoin(article_url, href)
                existing = candidates.get(url)
                text = (link.get('text') or '').lower().strip()
                if existing:
                    if text and text not in existing['text']:
                        existing['text'] = f"{existing['text']} {text}".strip()
                else:
                    candidates[url] = {'url': url, 'text': text}

            scored = []
            for candidate in candidates.values():
                score = _score_pdf_candidate(candidate)
                if score is None:
                    continue
                candidate['score'] = score
                scored.append(candidate)
            scored.sort(key=lambda item: item['score'], reverse=True)

            print(f"  [publisher:{mode}] PDF links found: {len(candidates)}", file=sys.stderr)
            if not scored:
                result['message'] = 'No usable PDF candidates found on publisher page'
                return result

            fetch_js = """
                async ([url]) => {
                    try {
                        const r = await fetch(url, {
                            credentials: 'include',
                            headers: {Accept: 'application/pdf,*/*'}
                        });
                        if (!r.ok) {
                            return {error: 'HTTP ' + r.status, status: r.status, url: r.url,
                                    contentType: r.headers.get('content-type') || ''};
                        }
                        const blob = await r.blob();
                        return new Promise(resolve => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve({
                                data: reader.result.split(',')[1],
                                size: blob.size,
                                url: r.url,
                                contentType: r.headers.get('content-type') || ''
                            });
                            reader.onerror = () => resolve({error: 'FileReader failed'});
                            reader.readAsDataURL(blob);
                        });
                    } catch (e) {
                        return {error: e.message || 'fetch failed'};
                    }
                }
            """

            last_non_pdf = None
            last_pdf_url = ''
            last_final_url = ''
            last_content_type = ''
            last_error = ''

            for candidate in scored[:5]:
                pdf_url = candidate['url']
                pdf_bytes = None
                how = ''
                print(f"  [publisher:{mode}] PDF URL: {pdf_url}", file=sys.stderr)
                print(f"  [publisher:{mode}] Downloading PDF...", file=sys.stderr)

                def _accept_pdf(body, method, final_url='', content_type=''):
                    nonlocal pdf_bytes, how, last_non_pdf, last_pdf_url, last_final_url, last_content_type
                    if not body:
                        return False
                    if len(body) >= 10000 and body[:5] == b'%PDF-':
                        pdf_bytes = body
                        how = method
                        return True
                    last_non_pdf = body
                    last_pdf_url = pdf_url
                    last_final_url = final_url
                    last_content_type = content_type
                    return False

                try:
                    if page.url != article_url:
                        await page.goto(article_url, wait_until='domcontentloaded',
                                        timeout=timeout * 1000)
                except Exception:
                    pass

                try:
                    js_result = await page.evaluate(fetch_js, [pdf_url])
                    if isinstance(js_result, dict) and 'data' in js_result:
                        _accept_pdf(base64.b64decode(js_result['data']), 'fetch',
                                    js_result.get('url', ''), js_result.get('contentType', ''))
                    elif isinstance(js_result, dict) and 'error' in js_result:
                        last_error = js_result['error']
                except Exception as e:
                    last_error = str(e)

                if pdf_bytes is None:
                    goto_response = None
                    try:
                        goto_response = await page.goto(pdf_url, wait_until='domcontentloaded',
                                                        timeout=timeout * 1000)
                        await asyncio.sleep(wait)
                    except Exception as e:
                        last_error = str(e)

                    if goto_response is not None:
                        try:
                            body = await goto_response.body()
                            _accept_pdf(body, 'goto', goto_response.url,
                                        goto_response.headers.get('content-type', ''))
                        except Exception as e:
                            last_error = str(e)

                    if pdf_bytes is None:
                        try:
                            js_result = await page.evaluate(fetch_js, [pdf_url])
                            if isinstance(js_result, dict) and 'data' in js_result:
                                _accept_pdf(base64.b64decode(js_result['data']), 'fetch (after goto)',
                                            js_result.get('url', ''), js_result.get('contentType', ''))
                            elif isinstance(js_result, dict) and 'error' in js_result:
                                last_error = js_result['error']
                        except Exception as e:
                            last_error = str(e)

                if pdf_bytes is None:
                    try:
                        js_click = """([url]) => {
                            const a = document.createElement('a');
                            a.href = url; a.download = ''; a.target = '_blank';
                            document.body.appendChild(a);
                            a.click();
                            document.body.removeChild(a);
                        }"""
                        async with page.expect_download(timeout=timeout * 1000) as dl_info:
                            await page.evaluate(js_click, [pdf_url])
                        download = await dl_info.value
                        tmp_path = str(output_path) + '.tmpdl'
                        await download.save_as(tmp_path)
                        with open(tmp_path, 'rb') as f:
                            body = f.read()
                        os.unlink(tmp_path)
                        _accept_pdf(body, 'download')
                    except Exception as e:
                        last_error = str(e)

                if pdf_bytes is not None:
                    with open(output_path, 'wb') as f:
                        f.write(pdf_bytes)
                    result['success'] = True
                    result['file_path'] = str(output_path)
                    result['file_size'] = len(pdf_bytes)
                    result['message'] = f'OK ({how}): {len(pdf_bytes)} bytes'
                    return result

            if last_non_pdf:
                result['message'] = _html_access_message(last_pdf_url, last_non_pdf,
                                                         last_final_url, last_content_type)
                result['file_size'] = len(last_non_pdf)
            else:
                result['message'] = f'PDF download failed: {last_error}' if last_error else 'PDF download failed'
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

    # Try headless first (up to 2 attempts), retrying with stealth when available
    for attempt in range(2):
        attempt_stealth = stealth_enabled or (attempt > 0 and _STEALTH_AVAILABLE)
        if attempt > 0:
            suffix = ' with stealth' if attempt_stealth and not stealth_enabled else ''
            print(f"  [publisher] headless retry 2/2{suffix} after 5s...", file=sys.stderr)
            time.sleep(5)

        result = await _do_download_via_publisher(doi_url, output_path,
                                                    chrome_bin, timeout,
                                                    headless=True,
                                                    profile_dir=profile_dir,
                                                    wait=wait,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=attempt_stealth)
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
    headed_stealth = stealth_enabled or _STEALTH_AVAILABLE
    suffix = ', stealth' if headed_stealth and not stealth_enabled else ''
    print(f"  [publisher] falling back to headed Chrome (Xvfb{suffix})...", file=sys.stderr)
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
                                                    stealth_enabled=headed_stealth)
        if result['success']:
            # ...
            return result
        msg = result.get('message', '')
        print(f"  [publisher] headed (xvfb) failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
            return result

    if fallback_level < 3:
        return result

    # Restore original DISPLAY before system display fallback.
    # _xvfb_start() polluted os.environ with its virtual display (:99).
    if _orig_display:
        os.environ['DISPLAY'] = _orig_display
    else:
        os.environ.pop('DISPLAY', None)

    # Fallback to system display headed (3 attempts)
    print(f"  [publisher] falling back to headed Chrome (system display{suffix})...", file=sys.stderr)
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
                                                    stealth_enabled=headed_stealth)
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
