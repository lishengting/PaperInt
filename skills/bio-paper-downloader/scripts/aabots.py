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
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

@dataclass
class BypassResult:
    success: bool = False
    content: bytes | None = None
    cookies: dict | None = None
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
    """Check if response body looks like a Cloudflare challenge page."""
    return (
        b'cf-browser-verify' in data
        or b'challenges.cloudflare.com' in data
        or b'Just a moment' in data
        or b'checking your browser' in data
    )


def _extract_cookies(cookie_list: list) -> dict:
    """Convert FlareSolverr cookie list to a simple dict."""
    return {c["name"]: c["value"] for c in cookie_list} if cookie_list else {}


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
        if resp.status_code == 200 and _is_pdf(content):
            return BypassResult(success=True, content=content, method="cloudscraper")
        if _is_cf_challenge(content):
            return BypassResult(success=False, method="cloudscraper",
                                error="Cloudflare interactive challenge detected (needs browser)")
        return BypassResult(success=False, method="cloudscraper",
                            error=f"HTTP {resp.status_code}, got {len(content)} bytes, not PDF")
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
        resp = await asyncio.to_thread(
            requests.get, url, impersonate="chrome", timeout=30
        )
        content = resp.content
        if resp.status_code == 200 and _is_pdf(content):
            return BypassResult(success=True, content=content, method="curl_cffi")
        if _is_cf_challenge(content):
            return BypassResult(success=False, method="curl_cffi",
                                error="Cloudflare challenge detected despite TLS impersonation")
        return BypassResult(success=False, method="curl_cffi",
                            error=f"HTTP {resp.status_code}, got {len(content)} bytes, not PDF")
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
    return BypassResult(success=False, method="stealth",
                        needs_browser=True, stealth_recommended=True)


@register_method("flaresolverr")
async def _method_flaresolverr(url: str, config: dict, is_biorxiv: bool) -> BypassResult:
    """FlareSolverr Docker service at localhost:8191. Handles interactive CF challenges."""
    fs_url = "http://localhost:8191/v1"
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }).encode()

    print("  [aabots:flaresolverr] Sending request to FlareSolverr...", file=sys.stderr)
    try:
        req = urllib.request.Request(
            fs_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=90)
        data = json.loads(resp.read())
        solution = data.get("solution", {})
        status = solution.get("status", 0)

        if status == 200:
            content = solution.get("response", "")
            if isinstance(content, str):
                content = content.encode()
            if _is_pdf(content):
                cookies = _extract_cookies(solution.get("cookies", []))
                return BypassResult(success=True, content=content, method="flaresolverr",
                                    cookies=cookies)
            return BypassResult(success=False, method="flaresolverr",
                                error=f"Response is not PDF (got {len(content)} bytes HTML, needs browser to follow links)")
        return BypassResult(success=False, method="flaresolverr",
                            error=solution.get("message", f"FlareSolverr returned status {status}"))
    except (urllib.error.URLError, ConnectionRefusedError):
        return BypassResult(success=False, method="flaresolverr",
                            error="FlareSolverr not running (docker not started? Run: docker run -d --name=flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest)")
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
    return BypassResult(success=False, method="2captcha",
                        needs_browser=True, captcha_recommended=True)


# ---------------------------------------------------------------------------
# BypassChain
# ---------------------------------------------------------------------------

class BypassChain:
    """Runs a sequence of bypass methods in order, returning the first success."""

    def __init__(self, methods: list[str], config: dict, timeout_per_method: int = 45):
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
                print(f"  [aabots] Chain succeeded: {method_name} ({elapsed:.0f}ms total)",
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

        if result.success:
            status = "OK"
        elif result.needs_browser:
            status = "BROWSER"
        else:
            status = "FAIL"
        detail = f" : {result.error}" if result.error else ""
        print(f"  [aabots:{name}] {status} ({result.elapsed_ms:.0f}ms){detail}",
              file=sys.stderr)
        return result


# ---------------------------------------------------------------------------
# Synchronous wrapper
# ---------------------------------------------------------------------------

def run_aabots_sync(url: str, methods: list[str], config: dict,
                    is_biorxiv: bool = False, timeout_per_method: int = 45) -> BypassResult:
    """Synchronous wrapper for callers in paper_cli.py (which are sync functions)."""
    chain = BypassChain(methods, config, timeout_per_method=timeout_per_method)
    return asyncio.run(chain.run(url, is_biorxiv))