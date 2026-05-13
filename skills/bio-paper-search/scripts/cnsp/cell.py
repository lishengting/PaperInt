"""CellParser — scrape Cell Press journal articles via /issues pages."""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser


class CellParser(CNSP_Parser):
    """Scrape Cell Press journal articles. Uses CDP fallback for blocked pages."""

    def __init__(self, use_browser: bool = True):
        super().__init__('cell', use_browser=use_browser)
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
                print(f"  Cell issue error ({issue_info.get('title', '?')}): {e}", file=sys.stderr)
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

                issue_url = urljoin('https://www.cell.com', href)
                title = c.get_text(strip=True)[:100]

                # Try to parse date from title or container
                issue_date = None
                date_text = c.select_one('time, .issue-date, [class*="date"]')
                if date_text:
                    try:
                        dt = dateparser.parse(date_text.get_text(strip=True))
                        if dt:
                            issue_date = dt.date()
                    except Exception:
                        pass

                if issue_date and issue_date < start_date:
                    continue

                issues.append({'url': issue_url, 'title': title, 'date': issue_date})

            except Exception:
                continue

        return issues

    async def _scrape_issue(self, issue_url: str, journal_name: str,
                             start_date: date, end_date: date,
                             browser_context) -> list[dict]:
        """Scrape articles from a single Cell issue page."""
        articles = []
        html = await self._get_page(issue_url, browser_context, timeout=60)
        if not html:
            return articles

        soup = BeautifulSoup(html, 'html.parser')

        # Find article links
        article_links = soup.select('a[href*="/article/"]')
        seen = set()

        for link in article_links:
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

                # Fetch article detail page
                detail = await self._fetch_article_detail(article_url, browser_context)
                pub_date = detail.get('date')

                if pub_date and not self._is_date_in_range(pub_date, start_date, end_date):
                    continue

                doi = self._extract_doi(article_url)

                articles.append({
                    'title': detail.get('title', title),
                    'url': article_url,
                    'abstract': detail.get('abstract', ''),
                    'date': pub_date,
                    'doi': doi,
                    'journal': journal_name,
                    'authors': detail.get('authors', ''),
                })

            except Exception as e:
                print(f"  Cell article error: {e}", file=sys.stderr)
                continue

        return articles

    async def _fetch_article_detail(self, url: str, browser_context) -> dict:
        """Fetch title, abstract, authors, date from a Cell article page."""
        html = await self._get_page(url, browser_context, timeout=30)
        if not html:
            return {}

        soup = BeautifulSoup(html, 'html.parser')

        title = ''
        title_tag = soup.find('meta', attrs={'name': 'citation_title'})
        if title_tag:
            title = title_tag.get('content', '')

        abstract = ''
        abs_elem = soup.select_one('div.section.abstract, div.article__abstract, section.abstract')
        if abs_elem:
            abstract = abs_elem.get_text(strip=True)

        authors = ''
        author_metas = soup.find_all('meta', attrs={'name': 'citation_author'})
        if author_metas:
            authors = '; '.join(m.get('content', '') for m in author_metas)

        date_val = None
        date_meta = soup.find('meta', attrs={'name': 'citation_publication_date'})
        if not date_meta:
            date_meta = soup.find('meta', attrs={'name': 'citation_online_date'})
        if date_meta:
            try:
                date_val = dateparser.parse(date_meta.get('content', '')).date()
            except Exception:
                pass

        return {'title': title, 'abstract': abstract, 'authors': authors, 'date': date_val}

    @staticmethod
    def _extract_doi(url: str) -> str:
        m = re.search(r'(10\.\d{4,}/[^\s&?]+)', url)
        if m:
            return m.group(1)
        return url.split('/')[-1]