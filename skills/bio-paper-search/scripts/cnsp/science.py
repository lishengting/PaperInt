"""ScienceParser — scrape Science journal articles via archive pages."""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser, clean_error


class ScienceParser(CNSP_Parser):
    """Scrape Science journal articles. Uses CDP fallback for blocked pages."""

    ARCHIVE_URLS = {
        'Science': 'https://www.science.org/loi/science',
        'Science Advances': 'https://www.science.org/loi/sciadv',
        'Science Immunology': 'https://www.science.org/loi/sciimmunol',
        'Science Robotics': 'https://www.science.org/loi/scirobotics',
        'Science Signaling': 'https://www.science.org/loi/signaling',
        'Science Translational Medicine': 'https://www.science.org/loi/stm',
    }

    def __init__(self, use_browser: bool = True):
        super().__init__('science', use_browser=use_browser)
        self.force_cdp = True  # science.org is JS-only, skip requests
        self.session.headers.update({
            'Referer': 'https://www.science.org/',
            'Origin': 'https://www.science.org',
        })

    async def scrape_journal(self, journal_name: str, base_url: str,
                       start_date: date, end_date: date,
                       browser_context=None) -> list[dict]:
        articles: list[dict] = []

        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()

        archive_url = self.ARCHIVE_URLS.get(journal_name)
        if not archive_url:
            archive_url = f"https://www.science.org/loi/{journal_name.lower().replace(' ', '')}"

        target_years = range(start_date.year, end_date.year + 1)

        for year in sorted(target_years):
            year_volumes = await self._get_year_volumes(archive_url, year, start_date, end_date, browser_context)
            if not year_volumes:
                continue

            for vol_info in year_volumes:
                vol_articles = await self._scrape_volume_articles(
                    vol_info['url'], journal_name, start_date, end_date, browser_context
                )
                articles.extend(vol_articles)
                time.sleep(2)

        return articles

    async def _get_year_volumes(self, archive_url: str, year: int,
                                 start_date: date, end_date: date,
                                 browser_context) -> list[dict]:
        volumes = []
        decade_start = 2020 if year >= 2020 else 2010
        year_url = f"{archive_url}/group/d{decade_start}.y{year}"

        html = await self._get_page(year_url, browser_context, timeout=60)
        if not html:
            return volumes

        soup = BeautifulSoup(html, 'html.parser')
        volume_elements = soup.select('div.col-12.col-sm-3.col-lg-2.mb-4.mb-sm-3')
        if not volume_elements:
            volume_elements = soup.select('div.past-issue, a.past-issue--loi')

        for elem in volume_elements:
            try:
                date_elem = elem.select_one('.past-issue__content__item--cover-date')
                if not date_elem:
                    continue
                date_text = date_elem.get_text(strip=True)
                try:
                    parsed = dateparser.parse(f"{date_text} {year}")
                    if not parsed:
                        continue
                    vol_date = parsed.date()
                except Exception:
                    continue

                if not self._is_date_in_range(vol_date, start_date, end_date):
                    continue

                link_elem = elem.select_one('a[href*="/toc/"]')
                if not link_elem:
                    continue

                href = link_elem.get('href')
                vol_url = urljoin('https://www.science.org', href)
                vol_title_elem = elem.select_one('.past-issue__content__item--volume')
                issue_elem = elem.select_one('.past-issue__content__item--issue')
                vol_title = ''
                if vol_title_elem:
                    vol_title += vol_title_elem.get_text(strip=True)
                if issue_elem:
                    vol_title += ' ' + issue_elem.get_text(strip=True)

                volumes.append({'url': vol_url, 'title': f"{vol_title} ({date_text})", 'date': vol_date})
            except Exception:
                continue

        return volumes

    async def _scrape_volume_articles(self, volume_url: str, journal_name: str,
                                       start_date: date, end_date: date,
                                       browser_context) -> list[dict]:
        """Scrape articles from a Science TOC page. Extracts titles/DOIs from
        the rendered DOM — no per-article HTTP requests needed."""
        articles = []
        html = await self._get_page(volume_url, browser_context, timeout=60)
        if not html:
            return articles

        soup = BeautifulSoup(html, 'html.parser')

        # Extract issue date from page meta
        page_date = None
        date_meta = (soup.find('meta', attrs={'name': 'citation_publication_date'})
                     or soup.find('meta', attrs={'name': 'citation_cover_date'}))
        if date_meta:
            try:
                page_date = dateparser.parse(date_meta.get('content', '')).date()
            except Exception:
                pass

        sections = soup.select('section.toc__section.mt-lg-2_5x.mt-2x')

        for section in sections:
            links = section.select('h3.article-title a.sans-serif.text-reset.animation-underline')
            for link_elem in links:
                try:
                    href = link_elem.get('href')
                    if not href:
                        continue
                    article_url = urljoin('https://www.science.org', href)
                    title = link_elem.get_text(strip=True)

                    if not title or len(title) < 10:
                        continue

                    doi = ''
                    if '/doi/' in article_url:
                        doi = article_url.split('/doi/')[-1].split('?')[0]

                    # Try to find authors in nearby elements
                    authors = ''
                    parent = link_elem.find_parent(['div', 'li'])
                    if parent:
                        for cls_pat in ['author', 'byline', 'contrib']:
                            elems = parent.select(f'[class*="{cls_pat}"]')
                            for e in elems:
                                text = e.get_text(strip=True)
                                if text and len(text) > 3:
                                    authors = text
                                    break
                            if authors:
                                break

                    articles.append({
                        'title': title,
                        'url': article_url,
                        'abstract': '',
                        'date': page_date,
                        'doi': doi or self._extract_doi(article_url),
                        'journal': journal_name,
                        'authors': authors,
                    })

                except Exception as e:
                    print(f"  Science article error: {clean_error(e)}", file=sys.stderr)
                    continue

        return articles

    @staticmethod
    def _extract_doi(url: str) -> str:
        m = re.search(r'(10\.\d{4,}/[^\s&?]+)', url)
        if m:
            return m.group(1)
        return url.split('/')[-1]