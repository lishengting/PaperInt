#!/usr/bin/env python3
"""
AABots — Anti-Anti-Bot bypass chain for Cloudflare Turnstile and other protections.

Provides a unified cascading strategy: try methods from simple/fast (HTTP-level)
to complex/heavy (browser-based, captcha services), returning the first success.

Usage:
  from aabots import resolve_methods, run_aabots_sync

  methods = resolve_methods("Default,FlareSolverr")
  result = run_aabots_sync(url, methods, config, is_biorxiv=True)
  if result.success:
      pdf_bytes = result.content
"""

import asyncio
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

@dataclass
class BypassResult:
    success: bool = False
    mode: str = "none"  # "pdf", "session", "signal", "none"
    content: bytes | None = None
    html: str | None = None
    content_type: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    cookies: dict | None = None
    browser_cookies: list[dict] | None = None
    method: str = ""
    error: str | None = None
    elapsed_ms: float = 0.0
    # Signal fields for the caller — set when a method can't solve the challenge
    # itself but knows a browser-based approach is needed.
    needs_browser: bool = False
    stealth_recommended: bool = False
    captcha_recommended: bool = False


# ---------------------------------------------------------------------------
# Method registry
# ---------------------------------------------------------------------------

METHOD_MAP: dict[str, Callable] = {}


def register_method(name: str):
    """Decorator to register a bypass method."""
    def decorator(func):
        METHOD_MAP[name] = func
        return func
    return decorator


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, list[str]] = {
    "Default":       [],
    "CloudScraper":  ["cloudscraper"],
    "Stealth":       ["stealth"],
    "FlareSolverr":  ["flaresolverr"],
    "2Captcha":      ["2captcha"],
    "Full":          ["cloudscraper", "curl_cffi", "stealth", "flaresolverr", "2captcha"],
    "All":           ["cloudscraper", "curl_cffi", "stealth", "flaresolverr", "2captcha"],
    "Quick":         ["cloudscraper", "curl_cffi"],
    "Browser":       ["stealth", "flaresolverr"],
}


def resolve_methods(raw: str) -> list[str]:
    """Resolve a preset name or comma-separated method list into an ordered list of method names.

    Case-insensitive. Unknown names are passed through (the chain runner will skip them).
    """
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    # Check presets first (case-insensitive)
    for preset_name, methods in PRESETS.items():
        if raw.lower() == preset_name.lower():
            return list(methods)
    # Comma-separated: each part may be a preset or a method name
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Check if this part is a preset
        found = False
        for preset_name, methods in PRESETS.items():
            if part.lower() == preset_name.lower():
                result.extend(methods)
                found = True
                break
        if not found:
            result.append(part)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_pdf(data: bytes, min_size: int = 10000) -> bool:
    return len(data) >= min_size and data[:5] == b'%PDF-'


def _is_cf_challenge(data: bytes) -> bool:
    """Check if response body looks like an unresolved challenge page."""
    lower = data[:200000].lower()
    return (
        b'cf-browser-verify' in lower
        or b'cf-challenge' in lower
        or b'cf-please-wait' in lower
        or b'cf-spinner' in lower
        or b'just a moment' in lower
        or b'checking your browser' in lower
        or b'attention required' in lower
    )


def _decode_html(data: bytes) -> str:
    return data.decode('utf-8', errors='replace')


def _body_preview(data: bytes, limit: int = 100) -> str:
    text = _decode_html(data).replace('\r', '\\r').replace('\n', '\\n')
    return text[:limit]


def _header_value(headers, name: str) -> str:
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict):
                key = item.get('name') or item.get('key')
                if key and str(key).lower() == name.lower():
                    return str(item.get('value') or '')
    return ''


def _publisher_handoff_hint(source_url: str = '', final_url: str = '', html: bytes | str = b'') -> bool:
    source_host = (urllib.parse.urlparse(source_url or '').hostname or '').lower()
    final_host = (urllib.parse.urlparse(final_url or '').hostname or '').lower()
    publisher_hosts = {
        'doi.org', 'linkinghub.elsevier.com', 'cell.com', 'www.cell.com',
        'sciencedirect.com', 'www.sciencedirect.com',
    }
    if source_host == 'doi.org' or final_host in publisher_hosts:
        return True
    lower = (html.encode() if isinstance(html, str) else html or b'')[:200000].lower()
    markers = (
        b'location.href', b'meta refresh', b'linkinghub.elsevier', b'cell.com',
        b'sciencedirect', b'/pdf', b'showpdf'
    )
    return any(m in lower for m in markers)


def _looks_like_html_session(data: bytes, content_type: str = '', final_url: str = '', source_url: str = '') -> bool:
    """Return True when HTML looks useful enough to hand to browser fallback."""
    lower = data[:200000].lower()
    if not data or _is_cf_challenge(data):
        return False
    is_html = 'text/html' in (content_type or '').lower() or b'<html' in lower or b'<!doctype html' in lower
    if not is_html:
        return False
    if len(data) < 20000 and not _publisher_handoff_hint(source_url, final_url, data):
        return False
    markers = (
        b'citation_', b'citation_pdf_url', b'article', b'fulltext', b'.pdf', b'showpdf',
        b'/pdf', b'download', b'publisher', b'journal'
    )
    url_markers = ('cell.com', 'sciencedirect', 'nature.com', 'springer')
    return (
        any(m in lower for m in markers)
        or any(m in (final_url or '').lower() for m in url_markers)
        or _publisher_handoff_hint(source_url, final_url, data)
    )


def _extract_cookies(cookie_list: list) -> dict:
    """Convert cookie list to a simple name/value dict."""
    cookies = {}
    if not cookie_list:
        return cookies
    for c in cookie_list:
        name = c.get('name') if isinstance(c, dict) else None
        value = c.get('value') if isinstance(c, dict) else None
        if name and value is not None:
            cookies[name] = value
    return cookies


def _domain_from_url(url: str) -> str:
    host = urllib.parse.urlparse(url or '').hostname or ''
    return host


def _valid_same_site(value):
    if not value:
        return None
    value = str(value)
    mapping = {'lax': 'Lax', 'strict': 'Strict', 'none': 'None'}
    return mapping.get(value.lower())


def _flaresolverr_cookies_to_playwright(cookie_list: list, final_url: str = '') -> list[dict]:
    """Convert FlareSolverr cookies to Playwright add_cookies() shape."""
    host = _domain_from_url(final_url)
    result = []
    for c in cookie_list or []:
        if not isinstance(c, dict) or not c.get('name') or c.get('value') is None:
            continue
        cookie = {
            'name': c['name'],
            'value': str(c['value']),
            'domain': c.get('domain') or host,
            'path': c.get('path') or '/',
        }
        if 'secure' in c:
            cookie['secure'] = bool(c.get('secure'))
        if 'httpOnly' in c:
            cookie['httpOnly'] = bool(c.get('httpOnly'))
        same_site = _valid_same_site(c.get('sameSite') or c.get('same_site'))
        if same_site:
            cookie['sameSite'] = same_site
        expires = c.get('expires') or c.get('expiry')
        if isinstance(expires, (int, float)) and expires > 0:
            cookie['expires'] = int(expires)
        result.append(cookie)
    return result


def _cookiejar_to_playwright(jar, final_url: str = '') -> list[dict]:
    """Convert requests-like or dict-like cookie jars to Playwright cookies."""
    host = _domain_from_url(final_url)
    result = []
    if not jar:
        return result
    if hasattr(jar, 'items'):
        for name, value in jar.items():
            if name and value is not None:
                result.append({'name': str(name), 'value': str(value), 'domain': host, 'path': '/'})
        return result
    for c in list(jar or []):
        if isinstance(c, str):
            value = jar.get(c) if hasattr(jar, 'get') else None
            if c and value is not None:
                result.append({'name': c, 'value': str(value), 'domain': host, 'path': '/'})
            continue
        name = getattr(c, 'name', None)
        value = getattr(c, 'value', None)
        if not name or value is None:
            continue
        domain = getattr(c, 'domain', '') or host
        path = getattr(c, 'path', '') or '/'
        cookie = {'name': name, 'value': str(value), 'domain': domain, 'path': path}
        secure = getattr(c, 'secure', None)
        if secure is not None:
            cookie['secure'] = bool(secure)
        expires = getattr(c, 'expires', None)
        if isinstance(expires, (int, float)) and expires > 0:
            cookie['expires'] = int(expires)
        result.append(cookie)
    return result


def _cfg(config: dict, path: str, default=None):
    """Minimal config path lookup (avoids importing paper_cli)."""
    for p in path.split('.'):
        if isinstance(config, dict):
            config = config.get(p)
        else:
            return default
        if config is None:
            return default
    return config


# ---------------------------------------------------------------------------
# Method implementations
# ---------------------------------------------------------------------------

@register_method("cloudscraper")
async def _method_cloudscraper(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """Pure Python: reverses Cloudflare's free-tier JS challenge. No browser needed."""
    try:
        import cloudscraper
    except ImportError:
        return BypassResult(success=False, method="cloudscraper",
                            error="cloudscraper not installed (pip install cloudscraper)")

    print("  [aabots:cloudscraper] Creating scraper...", file=sys.stderr)
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                'custom': _cfg(config, 'download.user_agent',
                               'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            }
        )
        resp = await asyncio.to_thread(scraper.get, url, timeout=30)
        content = resp.content
        content_type = resp.headers.get('content-type', '')
        final_url = getattr(resp, 'url', url)
        cookies = _extract_cookies([{'name': c.name, 'value': c.value} for c in scraper.cookies])
        browser_cookies = _cookiejar_to_playwright(scraper.cookies, final_url)
        if resp.status_code == 200 and _is_pdf(content):
            return BypassResult(success=True, mode="pdf", content=content,
                                method="cloudscraper", cookies=cookies,
                                browser_cookies=browser_cookies, final_url=final_url,
                                status_code=resp.status_code, content_type=content_type)
        if _looks_like_html_session(content, content_type, final_url):
            return BypassResult(success=True, mode="session", content=content,
                                html=_decode_html(content), method="cloudscraper",
                                cookies=cookies, browser_cookies=browser_cookies,
                                final_url=final_url, status_code=resp.status_code,
                                content_type=content_type, needs_browser=True)
        if _is_cf_challenge(content):
            return BypassResult(success=False, method="cloudscraper",
                                error="Cloudflare interactive challenge detected (needs browser)")
        return BypassResult(success=False, method="cloudscraper",
                            error=f"HTTP {resp.status_code}, got {len(content)} bytes, not PDF/HTML session, preview={_body_preview(content)!r}")
    except Exception as e:
        return BypassResult(success=False, method="cloudscraper", error=str(e))


@register_method("curl_cffi")
async def _method_curl_cffi(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """TLS fingerprint impersonation at the HTTP level (JA3/JA4 as Chrome)."""
    try:
        from curl_cffi import requests
    except ImportError:
        return BypassResult(success=False, method="curl_cffi",
                            error="curl_cffi not installed (pip install curl_cffi)")

    print("  [aabots:curl_cffi] Sending request with Chrome TLS fingerprint...", file=sys.stderr)
    try:
        session = requests.Session()
        resp = await asyncio.to_thread(
            session.get, url, impersonate="chrome", timeout=30
        )
        content = resp.content
        content_type = resp.headers.get('content-type', '')
        final_url = getattr(resp, 'url', url)
        cookies = session.cookies.get_dict() if hasattr(session.cookies, 'get_dict') else dict(session.cookies.items())
        browser_cookies = _cookiejar_to_playwright(session.cookies, final_url)
        if resp.status_code == 200 and _is_pdf(content):
            return BypassResult(success=True, mode="pdf", content=content,
                                method="curl_cffi", cookies=cookies,
                                browser_cookies=browser_cookies, final_url=final_url,
                                status_code=resp.status_code, content_type=content_type)
        if _looks_like_html_session(content, content_type, final_url):
            return BypassResult(success=True, mode="session", content=content,
                                html=_decode_html(content), method="curl_cffi",
                                cookies=cookies, browser_cookies=browser_cookies,
                                final_url=final_url, status_code=resp.status_code,
                                content_type=content_type, needs_browser=True)
        if _is_cf_challenge(content):
            return BypassResult(success=False, method="curl_cffi",
                                error="Cloudflare challenge detected despite TLS impersonation")
        return BypassResult(success=False, method="curl_cffi",
                            error=f"HTTP {resp.status_code}, got {len(content)} bytes, not PDF/HTML session, preview={_body_preview(content)!r}")
    except Exception as e:
        return BypassResult(success=False, method="curl_cffi", error=str(e))


@register_method("stealth")
async def _method_stealth(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """Signal method: tells the caller to use browser with enhanced stealth.

    This method does not start a browser itself. The actual browser work happens
    in the existing download_biorxiv_browser.py / download_publisher_pdf.py scripts
    with the --aabots-stealth flag.
    """
    print("  [aabots:stealth] Signaling caller to use enhanced-stealth browser...", file=sys.stderr)
    return BypassResult(success=False, mode="signal", method="stealth",
                        needs_browser=True, stealth_recommended=True)


@register_method("flaresolverr")
async def _method_flaresolverr(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """FlareSolverr Docker service at localhost:8191. Handles interactive CF challenges."""
    fs_url = "http://localhost:8191/v1"
    session_id = f"aabots_{int(time.time() * 1000)}_{os.getpid()}"
    payload_obj = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 180000,
        "session": session_id,
        "session_ttl_minutes": 5,
    }
    payload_text = json.dumps(payload_obj, ensure_ascii=False)
    payload = payload_text.encode()
    curl_command = (
        f"curl -X POST {shlex.quote(fs_url)} "
        f"-H {shlex.quote('Content-Type: application/json')} "
        f"-d {shlex.quote(payload_text)}"
    )

    print("  [aabots:flaresolverr] Sending request to FlareSolverr...", file=sys.stderr)
    print(f"  [aabots:flaresolverr] Request: {curl_command}", file=sys.stderr)
    try:
        req = urllib.request.Request(
            fs_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=210)
        data = json.loads(resp.read())
        solution = data.get("solution", {})
        status = solution.get("status", 0)
        response = solution.get("response", "")
        content = response.encode() if isinstance(response, str) else response or b''
        final_url = solution.get("url") or url
        cookie_list = solution.get("cookies", [])
        cookies = _extract_cookies(cookie_list)
        browser_cookies = _flaresolverr_cookies_to_playwright(cookie_list, final_url)
        headers = solution.get("headers") or {}
        content_type = _header_value(headers, "content-type")
        if not content_type and isinstance(response, str):
            content_type = "text/html"
        print(
            f"  [aabots:flaresolverr] Response: top_status={data.get('status')} "
            f"top_message={data.get('message')!r} solution_status={status} "
            f"final_url={final_url} content_type={content_type or '-'} "
            f"cookies={len(cookie_list or [])}/{len(browser_cookies or [])} "
            f"response_len={len(content)} preview={_body_preview(content, 200)!r}",
            file=sys.stderr,
        )

        if status == 200:
            if _is_pdf(content):
                return BypassResult(success=True, mode="pdf", content=content,
                                    method="flaresolverr", cookies=cookies,
                                    browser_cookies=browser_cookies, final_url=final_url,
                                    status_code=status, content_type=content_type)
            if _looks_like_html_session(content, content_type, final_url, url):
                return BypassResult(success=True, mode="session", content=content,
                                    html=_decode_html(content), method="flaresolverr",
                                    cookies=cookies, browser_cookies=browser_cookies,
                                    final_url=final_url, status_code=status,
                                    content_type=content_type, needs_browser=True)
            return BypassResult(success=False, method="flaresolverr",
                                final_url=final_url, status_code=status,
                                content_type=content_type, cookies=cookies,
                                browser_cookies=browser_cookies,
                                error=f"Response is not PDF or useful HTML session (got {len(content)} bytes), preview={_body_preview(content)!r}")
        return BypassResult(success=False, method="flaresolverr",
                            final_url=final_url, status_code=status,
                            content_type=content_type, cookies=cookies,
                            browser_cookies=browser_cookies,
                            error=solution.get("message", f"FlareSolverr returned status {status}"))
    except urllib.error.HTTPError as e:
        body = e.read()
        return BypassResult(success=False, method="flaresolverr",
                            error=f"FlareSolverr API HTTP {e.code}, preview={_body_preview(body)!r}")
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', None) or e
        reason_text = str(reason)
        if isinstance(reason, ConnectionRefusedError) or 'Connection refused' in reason_text:
            return BypassResult(success=False, method="flaresolverr",
                                error=f"FlareSolverr connection refused at {fs_url} (is the service reachable from this process?)")
        return BypassResult(success=False, method="flaresolverr",
                            error=f"FlareSolverr request failed: {reason_text}")
    except ConnectionRefusedError:
        return BypassResult(success=False, method="flaresolverr",
                            error=f"FlareSolverr connection refused at {fs_url} (is the service reachable from this process?)")
    except TimeoutError as e:
        return BypassResult(success=False, method="flaresolverr",
                            error=f"FlareSolverr request timed out: {e}")
    except Exception as e:
        return BypassResult(success=False, method="flaresolverr", error=str(e))


@register_method("2captcha")
async def _method_2captcha(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """Signal method: tells the caller to use browser with 2Captcha solving.

    The actual captcha solving happens in the existing captcha_solver.py module.
    """
    api_key_env = _cfg(config, 'download.twocaptcha_api_key_env', 'TWOCAPTCHA_API_KEY')
    api_key = os.environ.get(api_key_env, "")
    if not api_key and len(api_key_env) > 20:
        api_key = api_key_env  # inline key in config

    if not api_key:
        return BypassResult(success=False, method="2captcha",
                            error="No 2Captcha API key configured (set TWOCAPTCHA_API_KEY env var or twocaptcha_api_key_env in config)")

    print("  [aabots:2captcha] Signaling caller to use browser with 2Captcha...", file=sys.stderr)
    return BypassResult(success=False, mode="signal", method="2captcha",
                        needs_browser=True, captcha_recommended=True)


# ---------------------------------------------------------------------------
# BypassChain
# ---------------------------------------------------------------------------

class BypassChain:
    """Runs a sequence of bypass methods in order, returning the first success."""

    def __init__(self, methods: list[str], config: dict, timeout_per_method: int = 210):
        self._methods = methods
        self._config = config
        self._timeout = timeout_per_method
        self._results: list[BypassResult] = []

    @property
    def results(self) -> list[BypassResult]:
        return self._results

    async def run(self, url: str, is_biorxiv: bool = False) -> BypassResult:
        """Try each method in order. Returns first success, or the last signal/error result."""
        if not self._methods:
            print("  [aabots] No methods configured (Default preset), delegating to existing pipeline", file=sys.stderr)
            return BypassResult(success=False, method="chain", needs_browser=True)

        print(f"  [aabots] Chain starting: {', '.join(self._methods)}", file=sys.stderr)
        chain_start = time.monotonic()

        last_signal = None
        for method_name in self._methods:
            func = METHOD_MAP.get(method_name)
            if func is None:
                print(f"  [aabots] Unknown method '{method_name}', skipping", file=sys.stderr)
                continue
            result = await self._try_one(func, method_name, url, is_biorxiv)
            self._results.append(result)

            if result.success:
                elapsed = (time.monotonic() - chain_start) * 1000
                print(f"  [aabots] Chain succeeded: {method_name} ({result.mode}, {elapsed:.0f}ms total)",
                      file=sys.stderr)
                return result

            if result.needs_browser:
                last_signal = result

        elapsed = (time.monotonic() - chain_start) * 1000
        if last_signal:
            print(f"  [aabots] Chain complete: {len(self._results)} methods tried, "
                  f"delegating to browser ({elapsed:.0f}ms)", file=sys.stderr)
            return last_signal

        errors = "; ".join(r.error for r in self._results if r.error)
        print(f"  [aabots] Chain exhausted: all {len(self._results)} methods failed ({elapsed:.0f}ms)",
              file=sys.stderr)
        return BypassResult(success=False, method="chain",
                            error=errors or "All methods failed")

    async def _try_one(self, func: Callable, name: str, url: str, is_biorxiv: bool) -> BypassResult:
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                func(url, self._config, is_biorxiv),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            result = BypassResult(success=False, method=name, error="Timeout")
        except Exception as e:
            result = BypassResult(success=False, method=name, error=str(e))
        result.elapsed_ms = (time.monotonic() - start) * 1000

        if result.success and result.mode == "pdf":
            status = "OK-PDF"
        elif result.success and result.mode == "session":
            status = "OK-SESSION"
        elif result.needs_browser:
            status = "BROWSER"
        else:
            status = "FAIL"
        detail = f" : {result.error}" if result.error else ""
        if result.success and result.mode == "session":
            detail = f" : html={len(result.html or '')} final_url={result.final_url or url}"
        print(f"  [aabots:{name}] {status} ({result.elapsed_ms:.0f}ms){detail}",
              file=sys.stderr)
        return result


# ---------------------------------------------------------------------------
# Synchronous wrapper
# ---------------------------------------------------------------------------

def run_aabots_sync(url: str, methods: list[str], config: dict,
                    is_biorxiv: bool = False, timeout_per_method: int = 210) -> BypassResult:
    """Synchronous wrapper for callers in paper_cli.py (which are sync functions)."""
    chain = BypassChain(methods, config, timeout_per_method=timeout_per_method)
    return asyncio.run(chain.run(url, is_biorxiv))
