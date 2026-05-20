"""CNSP base parser — requests-first with Playwright CDP fallback for blocked servers."""

from __future__ import annotations

import random
import ssl
import sys
import time
from datetime import date, datetime

import asyncio
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Suppress InsecureRequestWarning from verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class PermissiveSSLAdapter(HTTPAdapter):
    """Adapter with permissive SSL context (no verify, TLS 1.2 max)."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def clean_error(err: Exception, max_len: int = 120) -> str:
    """Sanitize exception message — strip non-printable chars and truncate."""
    msg = str(err)
    msg = ''.join(c if c.isprintable() or c in '\t\n\r' else '?' for c in msg)
    if len(msg) > max_len:
        msg = msg[:max_len] + '...'
    return msg


class CNSP_Parser:
    """Base class for CNSP journal parsers. No Selenium, no DB, no AI agent."""

    def __init__(self, journal_type: str, use_browser: bool = False):
        self.journal_type = journal_type
        self.use_browser = use_browser
        self.session = requests.Session()

        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/120.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
        ]

        self.session.headers.update({
            'User-Agent': random.choice(self.user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'DNT': '1',
        })

        retry_strategy = Retry(
            total=2,
            backoff_factor=5,
            status_forcelist=[403, 429, 500, 502, 503, 504],
            respect_retry_after_header=True,
        )
        adapter = PermissiveSSLAdapter(max_retries=retry_strategy)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    # -- HTTP helpers ----------------------------------------------------------

    def _rotate_ua(self) -> None:
        self.session.headers['User-Agent'] = random.choice(self.user_agents)

    def _get_text(self, url: str, timeout: int = 30) -> str | None:
        """GET url with requests, return text or None."""
        self._rotate_ua()
        try:
            resp = self.session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return None

    def _get_page_with_retry(self, url: str, timeout: int = 60) -> str | None:
        """Try requests up to 3 times with backoff. Returns HTML text or None."""
        for attempt in range(3):
            self._rotate_ua()
            if attempt > 0:
                delay = random.uniform(5, 15) * (attempt + 1)
                time.sleep(delay)
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 403:
                    if attempt < 2:
                        time.sleep(random.uniform(10, 20))
                        continue
            except Exception:
                if attempt < 2:
                    time.sleep(random.uniform(5, 12))
        return None

    async def _cdp_fallback(self, url: str, browser_context,
                            wait_selector: str | None = None,
                            timeout: int = 60) -> str | None:
        """Playwright CDP fallback when requests is blocked."""
        if not browser_context:
            print(f"  CDP fallback skipped (no browser): {url[:100]}", file=sys.stderr)
            return None
        try:
            page = await browser_context.new_page()
            resp = await page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
            status = resp.status if resp else 0
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=15000)
            # Wait for JS to render (PLOS and other JS-heavy pages)
            await asyncio.sleep(3)
            html = await page.content()
            await page.close()
            if status in (403, 429, 503) or status == 0:
                print(f"  CDP got HTTP {status}: {url[:100]}", file=sys.stderr)
                return None
            if len(html) < 500:
                print(f"  CDP got short page (len={len(html)}): {url[:100]}", file=sys.stderr)
                return None
            return html
        except Exception as e:
            print(f"  CDP error ({e}): {url[:100]}", file=sys.stderr)
        return None

    @staticmethod
    def _is_template_html(html: str) -> bool:
        """Detect JS-template placeholders (EJS, Underscore) in HTML — page is client-rendered."""
        return '<%=' in html or '<%' in html

    async def _get_page(self, url: str, browser_context=None,
                        wait_selector: str | None = None,
                        timeout: int = 60) -> str | None:
        """Requests-first, CDP fallback if browser_context is available."""
        html = self._get_page_with_retry(url, timeout=timeout)
        if html and not self._is_template_html(html):
            return html
        if html:
            # JS-rendered page — fall through to CDP
            print(f"  JS-rendered page, falling back to CDP: {url[:100]}", file=sys.stderr)
        if browser_context and self.use_browser:
            return await self._cdp_fallback(url, browser_context, wait_selector, timeout)
        if not browser_context:
            print(f"  No browser available for CDP fallback: {url[:100]}", file=sys.stderr)
        return None

    # -- Date helpers ----------------------------------------------------------

    @staticmethod
    def _is_date_in_range(article_dt, start_date, end_date) -> bool:
        try:
            if isinstance(article_dt, str):
                article_dt = datetime.strptime(article_dt, '%Y-%m-%d').date()
            elif isinstance(article_dt, datetime):
                article_dt = article_dt.date()
            if isinstance(start_date, datetime):
                start_date = start_date.date()
            if isinstance(end_date, datetime):
                end_date = end_date.date()
            return start_date <= article_dt <= end_date
        except Exception:
            return False

    @staticmethod
    def _human_like_delay(min_delay: float = 0.1, max_delay: float = 0.8) -> None:
        time.sleep(random.uniform(min_delay, max_delay))

    # -- Cleanup ---------------------------------------------------------------

    def cleanup(self) -> None:
        self.session.close()