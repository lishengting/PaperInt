"""PubMed fetcher via NCBI E-utilities."""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from typing import Any

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.helpers import xml_text, mark_match, require_title_match
from paper_info.http import FetchError, get_text
from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class PubmedFetcher(Fetcher):
    """Fetch PubMed metadata via NCBI E-utilities."""

    name = "pubmed"
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def can_fetch(self, identifier: Identifier) -> bool:
        return identifier.kind in {"doi", "pmid", "pmcid", "title"}

    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        pmid = identifier.value if identifier.kind == "pmid" else self._search_pmid(identifier, timeout)
        xml_text = get_text(
            self.efetch_url,
            params={"db": "pubmed", "id": pmid, "retmode": "xml"},
            timeout=timeout,
        )
        meta = _pubmed_xml_to_metadata(xml_text, self.name)
        mark_match(meta, identifier)
        require_title_match(identifier, meta)
        return meta

    def _search_pmid(self, identifier: Identifier, timeout: float) -> str:
        term = {
            "doi": f"{identifier.value}[doi]",
            "pmcid": identifier.value,
            "title": identifier.value,
        }.get(identifier.kind, identifier.value)
        xml_text = get_text(
            self.esearch_url,
            params={"db": "pubmed", "term": term, "retmode": "xml", "retmax": 5},
            timeout=timeout,
        )
        root = ET.fromstring(xml_text)
        pmids = [node.text for node in root.findall(".//Id") if node.text]
        if identifier.kind == "title":
            return self._first_matching_pmid(identifier, pmids, timeout)
        pmid = pmids[0] if pmids else None
        if not pmid:
            raise FetchError("PubMed returned no PMID")
        return pmid

    def _first_matching_pmid(self, identifier: Identifier, pmids: list[str], timeout: float) -> str:
        from paper_info.fetchers.helpers import _title_candidate_matches as matches

        for pmid in pmids:
            xml_text = get_text(
                self.efetch_url,
                params={"db": "pubmed", "id": pmid, "retmode": "xml"},
                timeout=timeout,
            )
            meta = _pubmed_xml_to_metadata(xml_text, self.name)
            if matches(identifier, meta):
                return pmid
        raise FetchError("PubMed returned no matching PMID")


def _pubmed_xml_to_metadata(xml_text: str, source: str) -> PaperMetadata:
    root = ET.fromstring(xml_text)
    article = root.find(".//PubmedArticle")
    if article is None:
        raise FetchError("PubMed returned no article")
    meta = PaperMetadata()
    meta.pmid = article.findtext(".//MedlineCitation/PMID")
    meta.title = xml_text(article.find(".//ArticleTitle"))
    meta.journal = article.findtext(".//Journal/Title") or article.findtext(".//Journal/ISOAbbreviation")
    meta.year = (
        article.findtext(".//JournalIssue/PubDate/Year")
        or article.findtext(".//ArticleDate/Year")
        or article.findtext(".//PubMedPubDate/Year")
    )
    meta.abstract = _abstract_from_pubmed(article)
    meta.authors = _pubmed_authors(article)
    for article_id in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        id_type = (article_id.attrib.get("IdType") or "").lower()
        value = (article_id.text or "").strip()
        if id_type == "doi":
            meta.doi = value.lower()
        elif id_type == "pmc":
            meta.pmcid = value.upper()
    meta.add_source(source)
    return meta


def _pubmed_authors(article: ET.Element) -> list[str]:
    authors = []
    for author in article.findall(".//AuthorList/Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            authors.append(collective)
            continue
        last = author.findtext("LastName")
        fore = author.findtext("ForeName") or author.findtext("Initials")
        name = " ".join(part for part in (fore, last) if part)
        if name:
            authors.append(name)
    return authors


def _abstract_from_pubmed(article: ET.Element) -> str | None:
    parts = []
    for node in article.findall(".//Abstract/AbstractText"):
        label = node.attrib.get("Label")
        text = xml_text(node)
        if text and label:
            parts.append(f"{label}: {text}")
        elif text:
            parts.append(text)
    return " ".join(parts) or None
