"""CNSP — Cell/Nature/Science/PLOS journal scraping for bio-paper-search.

Usage:
    from cnsp import cnsp_search
    papers = cnsp_search(keywords, config, max_results=10,
                         start_date='2026-05-01', end_date='2026-05-13',
                         cnsp_journals=['Nature'], chrome_port=9222)
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import date, datetime, timedelta

from .config_manager import filter_journals_by_keywords, get_enabled_journals, get_request_delay
from .base import clean_error
from .nature import NatureParser
from .science import ScienceParser
from .cell import CellParser
from .plos import PLOSParser
from .utils import filter_by_keywords, normalize_article, score_and_rank


async def _scrape_journal_type(journal_type: str, config: dict,
                                start_date: date, end_date: date,
                                cnsp_journals_filter: list[str] | None,
                                keywords: list[str] | None,
                                chrome_port: int | None) -> list[dict]:
    """Scrape all enabled journals of one type. Returns normalized paper dicts."""
    journals = get_enabled_journals(config, journal_type, cnsp_journals_filter)
    if not journals:
        print(f"  CNSP {journal_type}: no journals selected")
        return []

    cnsp_cfg = config.get('cnsp', {})
    per_parser = cnsp_cfg.get(journal_type, {})
    use_browser = per_parser.get('use_browser', True)

    if journal_type == 'nature':
        parser = NatureParser(use_browser=False)
    elif journal_type == 'science':
        parser = ScienceParser(use_browser=use_browser)
    elif journal_type == 'cell':
        parser = CellParser(use_browser=use_browser)
    elif journal_type == 'plos':
        parser = PLOSParser(use_browser=use_browser)
    else:
        return []

    delay = get_request_delay(config, journal_type)
    all_articles: list[dict] = []

    # Set up browser context if needed
    browser = None
    ctx = None
    if use_browser and chrome_port:
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{chrome_port}')
            ctx = browser.contexts[0]
        except Exception as e:
            print(f"  CNSP {journal_type}: CDP connect failed ({e}), requests-only", file=sys.stderr)

    try:
        for j in journals:
            jname = j.get('name', '')
            jlink = j.get('link', '')
            print(f"  Scraping {journal_type}: {jname} ({start_date} — {end_date})")

            try:
                raw_articles = await parser.scrape_journal(
                    jname, jlink, start_date, end_date, browser_context=ctx
                )
                for a in raw_articles:
                    normalized = normalize_article(a, journal_type, jname)
                    if normalized.get('doi'):
                        all_articles.append(normalized)

                print(f"    {len(raw_articles)} articles found")
            except ValueError as e:
                print(f"    Connection corrupt ({jname}): {clean_error(e)}", file=sys.stderr)
            except Exception as e:
                print(f"    Error ({jname}): {clean_error(e)}", file=sys.stderr)

            time.sleep(delay)

    finally:
        parser.cleanup()
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    return all_articles


def _parse_dates(start_date_str: str, end_date_str: str) -> tuple[date, date]:
    """Parse date strings to date objects."""
    end = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else date.today()
    start = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else end - timedelta(days=7)
    return start, end


def cnsp_search(keywords: list[str], config: dict, max_results: int = 10,
                start_date: str = '', end_date: str = '',
                cnsp_journals: list[str] | None = None,
                chrome_port: int | None = None) -> list[dict]:
    """Search CNSP journals for articles matching keywords in a date range.

    Args:
        keywords: list of keyword strings
        config: config.yaml dict
        max_results: max papers to return (after keyword filtering)
        start_date: YYYY-MM-DD start (default: 7 days ago)
        end_date: YYYY-MM-DD end (default: today)
        cnsp_journals: optional filter list of journal names
        chrome_port: CDP port for Playwright browser

    Returns:
        List of normalized paper dicts (make_paper format)
    """
    start, end = _parse_dates(start_date, end_date)
    print(f"CNSP search: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
    print(f"Date range: {start} — {end}")

    all_articles: list[dict] = []

    # Scrape each journal type
    for jtype in ['nature', 'science', 'cell', 'plos']:
        cnsp_cfg = config.get('cnsp', {})
        if not cnsp_cfg.get(jtype, {}).get('enabled', True):
            print(f"  CNSP {jtype}: disabled in config")
            continue

        try:
            articles = asyncio.run(_scrape_journal_type(
                jtype, config, start, end, cnsp_journals, keywords, chrome_port
            ))
            all_articles.extend(articles)
            print(f"  CNSP {jtype}: {len(articles)} articles total")
        except Exception as e:
            print(f"  CNSP {jtype} error: {e}", file=sys.stderr)

    # Filter by keywords (client-side since CNSP sites have no keyword API)
    matched = filter_by_keywords(all_articles, keywords)
    print(f"  Keyword filter: {len(matched)}/{len(all_articles)} matched")

    # Score and rank
    matched = score_and_rank(matched, keywords)

    return matched[:max_results]


def cnsp_search_title(title: str, config: dict, max_results: int = 10,
                      start_date: str = '', end_date: str = '',
                      cnsp_journals: list[str] | None = None,
                      chrome_port: int | None = None) -> list[dict]:
    """Search CNSP by title (uses title words as keywords)."""
    keywords = [w.lower() for w in title.split() if len(w) > 2]
    return cnsp_search(keywords, config, max_results,
                       start_date, end_date, cnsp_journals, chrome_port)