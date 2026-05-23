"""NatureParser — scrape Nature journal research articles."""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .base import CNSP_Parser, clean_error


class NatureParser(CNSP_Parser):
    """Scrape Nature journal articles. Pure requests — no browser needed."""

    def __init__(self, use_browser: bool = False):
        super().__init__('nature', use_browser=False)

    async def scrape_journal(self, journal_name: str, base_url: str,
                       start_date: date, end_date: date,
                       browser_context=None) -> list[dict]:
        """Scrape all research articles from a Nature journal in date range."""
        articles: list[dict] = []

        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()

        for article_type in ['research-articles']:
            page_url = urljoin(base_url, article_type)
            print(f"  Nature: {page_url}", file=sys.stderr)
            ret_date = None

            while page_url:
                try:
                    resp = self.session.get(page_url, timeout=30)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, 'html.parser')
                except Exception as e:
                    print(f"  Nature request error: {clean_error(e)}", file=sys.stderr)
                    break

                cards = soup.find_all('article', class_='c-card')
                for card in cards:
                    try:
                        title_tag = card.find('h3', class_='c-card__title')
                        if not title_tag:
                            continue
                        a = title_tag.find('a', href=True)
                        title = a.get_text(strip=True)
                        link = urljoin('https://www.nature.com', a['href'])

                        time_tag = card.find('time')
                        raw_date = time_tag.get('datetime', time_tag.get_text(strip=True)) if time_tag else ''
                        try:
                            pub_date = dateparser.parse(raw_date)
                            if pub_date:
                                pub_date = pub_date.date()
                        except Exception:
                            pub_date = None

                        ret_date = pub_date

                        if pub_date:
                            if start_date and pub_date < start_date:
                                continue
                            if end_date and pub_date > end_date:
                                continue

                        time.sleep(0.6)

                        abstract, authors = self._fetch_abstract_and_authors(link)
                        doi = self._extract_doi(link)

                        articles.append({
                            'title': title,
                            'url': link,
                            'abstract': abstract,
                            'date': pub_date,
                            'doi': doi,
                            'journal': journal_name,
                            'authors': authors,
                        })

                    except Exception as e:
                        print(f"  Nature card error: {clean_error(e)}", file=sys.stderr)
                        continue

                # Pagination
                next_page = None
                li_next = soup.find('li', attrs={'data-test': 'page-next'})
                if li_next:
                    a_tag = li_next.find('a', class_='c-pagination__link', href=True)
                    if a_tag:
                        next_page = urljoin('https://www.nature.com', a_tag['href'])

                if next_page:
                    if ret_date and start_date and ret_date < start_date:
                        page_url = None
                    else:
                        page_url = next_page
                        print(f"  Nature next: {page_url}", file=sys.stderr)
                else:
                    page_url = None

        return articles

    def _extract_doi(self, url: str) -> str:
        """Extract DOI from a Nature article URL."""
        m = re.search(r'(10\.\d{4,}/[^\s&]+)', url)
        if m:
            return m.group(1).rstrip('.')
        return ''

    def _fetch_abstract_and_authors(self, url: str) -> tuple[str, str]:
        """Fetch abstract and authors from a Nature article page."""
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'html.parser')
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt == 2:
                    return '', ''
                time.sleep(2 * (attempt + 1))

        abstract = self._extract_abstract(soup, url)
        authors = self._extract_authors(soup)
        return abstract, authors

    def _extract_abstract(self, soup, url: str) -> str:
        abstract_tag = soup.find('div', {'id': 'Abs1-content'})
        if abstract_tag:
            return abstract_tag.get_text(strip=True)

        for tag, cls in [
            ('p', 'article__teaser'),
            ('div', 'article__teaser'),
            ('div', 'c-article-section__content'),
            ('div', 'c-article-body main-content'),
        ]:
            divs = soup.find_all(tag, class_=cls)
            if divs:
                return ' '.join(d.get_text(strip=True) for d in divs)

        return ''

    def _extract_authors(self, soup) -> str:
        author_metas = soup.find_all('meta', attrs={'name': 'citation_author'})
        if author_metas:
            return '; '.join(m.get('content', '') for m in author_metas)

        authors_elem = soup.select_one(
            'div.c-author-list, ul.c-author-list, div.authors, '
            '.author-list, span.authors, .c-article-author-list, '
            'div.c-article-header__authors'
        )
        if authors_elem:
            links = authors_elem.find_all('a')
            if links:
                names = []
                for link in links:
                    text = link.get_text(strip=True)
                    if (not text.startswith('http') and 'orcid' not in text.lower()
                            and 1 < len(text) < 100):
                        names.append(text)
                if names:
                    authors = '; '.join(names)
                else:
                    authors = authors_elem.get_text(strip=True)
            else:
                authors = authors_elem.get_text(strip=True)

            authors = re.sub(r'Show\s*authors?', '', authors, flags=re.IGNORECASE)
            authors = re.sub(r'View\s*ORCID\s*Profile', '', authors, flags=re.IGNORECASE)
            authors = re.sub(r'Authors?\s*&?\s*Affiliations?', '', authors, flags=re.IGNORECASE)
            authors = re.sub(r';\s*;+', ';', authors)
            authors = re.sub(r'^\s*;\s*|\s*;\s*$', '', authors)
            authors = re.sub(r'\s+', ' ', authors).strip()
            return authors

        return ''