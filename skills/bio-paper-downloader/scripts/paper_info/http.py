"""Small standard-library HTTP helpers."""

from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


USER_AGENT = "paper_info/0.1 (+https://example.invalid/paper_info)"


def _ssl_context() -> ssl.SSLContext:
    """SSL context capped at TLS 1.2 for older servers (bioRxiv, arXiv)."""
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class FetchError(RuntimeError):
    """Raised when a remote source cannot be fetched or parsed."""


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 4.0) -> Any:
    """GET a JSON endpoint using urllib."""

    data = get_text(url, params=params, timeout=timeout)
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid JSON from {url}") from exc


def get_text(url: str, params: dict[str, Any] | None = None, timeout: float = 4.0) -> str:
    """GET a text endpoint using urllib."""

    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < 2:
                retry_after = exc.headers.get('Retry-After', '')
                try:
                    wait = int(retry_after)
                except (ValueError, TypeError):
                    wait = 2 * (4 ** attempt) + random.uniform(0, 4)
                time.sleep(wait)
            elif 400 <= exc.code < 500:
                raise FetchError(str(exc))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
    raise FetchError(str(last_error))
