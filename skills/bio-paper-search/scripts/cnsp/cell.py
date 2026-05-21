"""CellParser — scrape Cell Press journal articles via /issues pages."""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser, clean_error


class CellParser(CNSP_Parser):
    """Scrape Cell Press journal articles. Uses CDP fallback for blocked pages."""

    def __init__(self, use_browser: bool = True):
        super().__init__('cell', use_browser=use_browser)
        self.force_cdp = True  # cell.com is JS-only, skip requests
        self.session.headers.update({
            'Referer': 'https://www.cell.com/',
            'Origin': 'https://www.cell.com',
        })

    async def scrape_journal(self, journal_name: str, base_url: str,
                       start_date: date, end_date: date,
                       browser_context=None) -> list[dict]:
        articles: list[dict] = []

        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()

        # Build issues URL from base (/home -> /issues)
        issues_url = base_url.replace('/home', '/issues')
        if not issues_url.endswith('/issues'):
            issues_url = urljoin(base_url, 'issues')

        issue_links = await self._get_issue_links(issues_url, base_url, start_date, end_date, browser_context)
        if not issue_links:
            return articles

        for i, issue_info in enumerate(issue_links):
            try:
                issue_articles = await self._scrape_issue(
                    issue_info['url'], journal_name, start_date, end_date, browser_context
                )
                articles.extend(issue_articles)
                if i < len(issue_links) - 1:
                    time.sleep(2)
            except Exception as e:
                print(f"  Cell issue error ({issue_info.get('title', '?')}): {clean_error(e)}", file=sys.stderr)
                continue

        return articles

    async def _get_issue_links(self, primary_url: str, base_url: str,
                                start_date: date, end_date: date,
                                browser_context) -> list[dict]:
        """Get issue links from /issues page, with fallback to /archive."""
        urls = [
            primary_url,
            base_url.replace('/home', '/archive'),
            base_url.replace('/home', '/archive') + '?isCoverWidget=true',
        ]

        for url in urls:
            print(f"  Cell issues: {url}", file=sys.stderr)
            html = await self._get_page(url, browser_context, timeout=60)
            if not html:
                continue
            links = self._parse_issue_links(html, start_date, end_date)
            if links:
                return links

        return []

    def _parse_issue_links(self, html: str, start_date: date, end_date: date) -> list[dict]:
        """Parse issue links from Cell issues page HTML."""
        soup = BeautifulSoup(html, 'html.parser')
        issues = []

        # Look for issue containers — try multiple selectors
        containers = (
            soup.select('div.issue-list__item, a.issue-list__item')
            or soup.select('div.js-issue-list-item, a.js-issue-list-item')
            or soup.select('div[class*="issue-list"] a[href*="/issue"]')
            or soup.select('a[href*="/issue"]')
        )

        for c in containers:
            try:
                href = c.get('href', '') if c.name == 'a' else ''
                if not href:
                    link = c.select_one('a[href*="/issue"]')
                    if link:
                        href = link.get('href', '')

                if not href or '/issue' not in href:
                    continue

                # Skip the /issues listing page itself, keep only specific issues
                if href.rstrip('/').endswith('/issues'):
                    continue

                issue_url = urljoin('https://www.cell.com', href)
                title = c.get_text(strip=True)[:100]

                # Try to parse date from container text
                issue_date = None
                date_text = c.select_one('time, .issue-date, [class*="date"]')
                if date_text:
                    try:
                        dt = dateparser.parse(date_text.get_text(strip=True))
                        if dt:
                            issue_date = dt.date()
                    except Exception:
                        pass

                # Fallback: extract date from link text (handles "Issue 5May 07, 2026p889")
                if not issue_date:
                    m = re.search(r'(\d{1,2})?\s*([A-Z][a-z]+)\s*(\d{1,2}),?\s*(\d{4})', title)
                    if m:
                        try:
                            issue_date = datetime.strptime(
                                f"{m.group(2)} {m.group(3)} {m.group(4)}",
                                '%B %d %Y'
                            ).date()
                        except ValueError:
                            pass

                # Fallback: extract year from PII (e.g. S0002-9297(25) → 2025)
                if not issue_date:
                    m = re.search(r'\((\d{2})\)', href)
                    if m:
                        pii_year = 2000 + int(m.group(1))
                        # PII year is approximate — use Jan 1 as placeholder
                        issue_date = date(pii_year, 1, 1)

                if issue_date and issue_date < start_date:
                    continue

                issues.append({'url': issue_url, 'title': title, 'date': issue_date})

            except Exception:
                continue

        return issues

    async def _scrape_issue(self, issue_url: str, journal_name: str,
                             start_date: date, end_date: date,
                             browser_context) -> list[dict]:
        """Scrape articles from a single Cell issue page. Extracts titles from
        h3 a[href*=\"/fulltext/\"] links — no per-article HTTP requests needed."""
        articles = []
        html = await self._get_page(issue_url, browser_context, timeout=60)
        print(f"  Cell issue: {issue_url}", file=sys.stderr)
        if not html:
            return articles

        soup = BeautifulSoup(html, 'html.parser')

        # Extract issue date and filter by date range
        issue_date = None
        date_meta = soup.find('meta', attrs={'name': 'citation_publication_date'})
        if date_meta:
            try:
                issue_date = dateparser.parse(date_meta.get('content', '')).date()
            except Exception:
                pass

        if issue_date and not self._is_date_in_range(issue_date, start_date, end_date):
            return articles

        seen = set()
        for link in soup.select('h3 a[href*="/fulltext/"]'):
            try:
                href = link.get('href', '')
                if not href:
                    continue
                article_url = urljoin('https://www.cell.com', href)
                if article_url in seen:
                    continue
                seen.add(article_url)

                title = link.get_text(strip=True)
                if not title or len(title) < 10:
                    continue

                doi = self._extract_doi(article_url)

                articles.append({
                    'title': title,
                    'url': article_url,
                    'abstract': '',
                    'date': issue_date,
                    'doi': doi,
                    'journal': journal_name,
                    'authors': '',
                })

            except Exception as e:
                print(f"  Cell article error: {clean_error(e)}", file=sys.stderr)
                continue

        return articles

    @staticmethod
    def _extract_doi(url: str) -> str:
        m = re.search(r'(10\.\d{4,}/[^\s&?]+)', url)
        if m:
            return m.group(1)
        return url.split('/')[-1]