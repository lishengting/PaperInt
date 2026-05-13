"""PLOSParser — scrape PLOS journal articles via search API."""

from __future__ import annotations

import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser


class PLOSParser(CNSP_Parser):
    """Scrape PLOS journal articles via search API. Uses CDP for JS-rendered results."""

    JOURNAL_CODE_MAP = {
        'PLOSOne': 'plosone',
        'PLOSBiology': 'plosbiology',
        'PLOSMedicine': 'plosmedicine',
        'PLOSGenetics': 'plosgenetics',
        'PLOSComputationalBiology': 'ploscompbiol',
        'PLOSPathogens': 'plospathogens',
        'PLOSNegTropicalDiseases': 'plosntds',
        'PLOSDigitalHealth': 'digitalhealth',
        'PLOSGlobalPublicHealth': 'globalpublichealth',
        'PLOSClimate': 'climate',
        'PLOSWater': 'water',
        'PLOSSustainabilityTransformation': 'sustainabilitytransformation',
        'PLOSComplexSystems': 'complexsystems',
        'PLOSMentalHealth': 'mentalhealth',
    }

    def __init__(self, use_browser: bool = True):
        super().__init__('plos', use_browser=use_browser)

    def scrape_journal(self, journal_name: str, base_url: str,
                       start_date: date, end_date: date,
                       browser_context=None) -> list[dict]:
        articles: list[dict] = []

        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()

        journal_code = journal_name.replace(' ', '').replace('PLOS', 'PLOS')
        journal_path = self.JOURNAL_CODE_MAP.get(journal_code, 'plosone')

        page = 1
        while True:
            search_url = self._build_search_url(journal_code, journal_path, start_date, end_date, page)
            page_articles = self._scrape_search_page(search_url, journal_name, browser_context)
            if not page_articles:
                break
            articles.extend(page_articles)
            page += 1
            time.sleep(2)

        return articles

    @staticmethod
    def _build_search_url(journal_code: str, journal_path: str,
                          start_date: date, end_date: date, page: int) -> str:
        """Build PLOS search URL with date and pagination params."""
        params = (
            f"filterJournals={journal_code}"
            f"&filterStartDate={start_date.strftime('%Y-%m-%d')}"
            f"&filterEndDate={end_date.strftime('%Y-%m-%d')}"
            f"&resultsPerPage=60"
            f"&q="
            f"&sortOrder=DATE_NEWEST_FIRST"
            f"&page={max(1, page)}"
        )
        return f"https://journals.plos.org/{journal_path}/search?{params}"

    async def _scrape_search_page(self, search_url: str, journal_name: str,
                                   browser_context) -> list[dict]:
        """Scrape a single page of PLOS search results."""
        articles = []
        html = await self._get_page(search_url, browser_context, timeout=60)
        if not html:
            return articles

        soup = BeautifulSoup(html, 'html.parser')

        # Find articles by data-doi attribute
        doi_elements = soup.find_all('dt', attrs={'data-doi': True})
        if not doi_elements:
            doi_elements = soup.find_all(attrs={'data-doi': True})

        for elem in doi_elements:
            try:
                data_doi = elem.get('data-doi', '')
                if not data_doi:
                    continue

                doi_url = f"https://doi.org/{data_doi}"
                detail = self._fetch_article_detail(doi_url)
                if not detail or not detail.get('title'):
                    continue

                articles.append({
                    'title': detail.get('title', ''),
                    'url': doi_url,
                    'abstract': detail.get('abstract', ''),
                    'date': detail.get('date'),
                    'doi': data_doi,
                    'journal': journal_name,
                    'authors': detail.get('authors', ''),
                })

            except Exception as e:
                print(f"  PLOS article error: {e}", file=sys.stderr)
                continue

        return articles

    def _fetch_article_detail(self, doi_url: str) -> dict | None:
        """Fetch article details from a PLOS article page via DOI."""
        try:
            resp = self.session.get(doi_url, timeout=30)
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')

            title = ''
            title_elem = (
                soup.find('h1', id='artTitle')
                or soup.find('h1', class_='title')
                or soup.find('h1', id='title')
                or soup.find('h1')
            )
            if title_elem:
                title = title_elem.get_text(strip=True)

            abstract = ''
            abs_elem = (
                soup.find('div', class_='abstract')
                or soup.find('div', id='abstract')
                or soup.find('section', class_='abstract')
            )
            if abs_elem:
                abstract = abs_elem.get_text(strip=True)
            if not abstract:
                intro = soup.find('div', class_='introduction') or soup.find('section', class_='introduction')
                if intro:
                    abstract = intro.get_text(strip=True)

            pub_date = None
            date_elem = soup.find('time', class_='published')
            if date_elem:
                try:
                    pub_date = dateparser.parse(date_elem.get_text(strip=True)).date()
                except Exception:
                    pass
            if not pub_date:
                date_meta = soup.find('meta', attrs={'name': 'citation_publication_date'})
                if date_meta:
                    try:
                        pub_date = dateparser.parse(date_meta.get('content', '')).date()
                    except Exception:
                        pass

            authors = ''
            author_metas = soup.find_all('meta', attrs={'name': 'citation_author'})
            if author_metas:
                authors = '; '.join(m.get('content', '') for m in author_metas)

            return {'title': title, 'abstract': abstract, 'date': pub_date, 'authors': authors}

        except Exception as e:
            print(f"  PLOS detail error: {e}", file=sys.stderr)
            return None