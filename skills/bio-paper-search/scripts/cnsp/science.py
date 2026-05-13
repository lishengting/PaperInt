"""ScienceParser — scrape Science journal articles via archive pages."""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser


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
        articles = []
        html = await self._get_page(volume_url, browser_context, timeout=60)
        if not html:
            return articles

        soup = BeautifulSoup(html, 'html.parser')
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

                    doi = ''
                    if '/doi/' in article_url:
                        doi = article_url.split('/doi/')[-1].split('?')[0]

                    article_data = await self._fetch_article_detail(article_url, browser_context)
                    pub_date = article_data.get('date') or end_date

                    if pub_date and not self._is_date_in_range(pub_date, start_date, end_date):
                        continue

                    articles.append({
                        'title': title,
                        'url': article_url,
                        'abstract': article_data.get('abstract', ''),
                        'date': pub_date,
                        'doi': doi or self._extract_doi(article_url),
                        'journal': journal_name,
                        'authors': article_data.get('authors', ''),
                    })
                except Exception as e:
                    err = str(e)
                    if len(err) > 100:
                        err = err[:100] + '...'
                    print(f"  Science article error: {err}", file=sys.stderr)
                    continue

        return articles

    async def _fetch_article_detail(self, url: str, browser_context) -> dict:
        """Fetch abstract and authors from a Science article page."""
        html = await self._get_page(url, browser_context, timeout=30)
        if not html:
            return {}

        soup = BeautifulSoup(html, 'html.parser')
        abstract = ''
        abs_elem = soup.select_one('div.section.abstract, div.abstract, div.article-section__abstract')
        if abs_elem:
            abstract = abs_elem.get_text(strip=True)

        authors = ''
        author_metas = soup.find_all('meta', attrs={'name': 'citation_author'})
        if author_metas:
            authors = '; '.join(m.get('content', '') for m in author_metas)

        date_val = None
        date_meta = soup.find('meta', attrs={'name': 'citation_online_date'})
        if date_meta:
            try:
                date_val = dateparser.parse(date_meta.get('content', '')).date()
            except Exception:
                pass

        return {'abstract': abstract, 'authors': authors, 'date': date_val}

    @staticmethod
    def _extract_doi(url: str) -> str:
        m = re.search(r'(10\.\d{4,}/[^\s&?]+)', url)
        if m:
            return m.group(1)
        return url.split('/')[-1]