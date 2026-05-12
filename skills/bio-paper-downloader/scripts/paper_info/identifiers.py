"""Identifier parsing and normalization."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from paper_info.models import Identifier


DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", re.IGNORECASE)
PMID_RE = re.compile(r"\b(?:PMID[:\s]*)?(\d{6,9})\b", re.IGNORECASE)
PMCID_RE = re.compile(r"\b(?:PMCID[:\s]*)?(PMC\d+)\b", re.IGNORECASE)
ARXIV_RE = re.compile(
    r"\b(?:arXiv[:\s]*)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)\b",
    re.IGNORECASE,
)


def parse_identifier(raw: str) -> Identifier:
    """Parse a DOI, PMID, PMCID, arXiv ID, URL, or title-like query."""

    value = raw.strip()
    if not value:
        raise ValueError("identifier must not be empty")

    if value.lower().startswith(("http://", "https://")):
        return _parse_url(value)

    pmcid_match = PMCID_RE.search(value)
    if pmcid_match:
        return Identifier("pmcid", pmcid_match.group(1).upper(), raw)

    if value.lower().startswith("pmid:"):
        pmid = value.split(":", 1)[1].strip()
        if pmid.isdigit():
            return Identifier("pmid", pmid, raw)

    doi_match = DOI_RE.search(value)
    if doi_match:
        return Identifier("doi", _clean_doi(doi_match.group(1)), raw)

    if value.lower().startswith("arxiv:"):
        arxiv = value.split(":", 1)[1].strip()
        if arxiv:
            return Identifier("arxiv", arxiv, raw)

    if value.isdigit() and 6 <= len(value) <= 9:
        return Identifier("pmid", value, raw)

    arxiv_match = ARXIV_RE.fullmatch(value)
    if arxiv_match and "." in value:
        return Identifier("arxiv", arxiv_match.group(1), raw)

    return Identifier("title", value, raw)


def infer_domain(identifier: Identifier, requested: str = "auto") -> str:
    """Infer the best domain registry for an identifier."""

    if requested != "auto":
        return requested
    if identifier.kind == "arxiv":
        return "cs"
    return "biomed"


def _parse_url(url: str) -> Identifier:
    parsed = urlparse(url)
    text = unquote(url)

    doi_match = DOI_RE.search(text)
    if doi_match:
        return Identifier("doi", _clean_doi(doi_match.group(1)), url)

    pmcid_match = PMCID_RE.search(text)
    if pmcid_match:
        return Identifier("pmcid", pmcid_match.group(1).upper(), url)

    query = parse_qs(parsed.query)
    for key in ("term", "id", "pmid"):
        for item in query.get(key, []):
            if item.isdigit():
                return Identifier("pmid", item, url)

    pmid_match = PMID_RE.search(parsed.path)
    if "pubmed" in parsed.netloc.lower() and pmid_match:
        return Identifier("pmid", pmid_match.group(1), url)

    if "arxiv.org" in parsed.netloc.lower():
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return Identifier("arxiv", parts[-1].replace(".pdf", ""), url)

    return Identifier("url", url, url)


def _clean_doi(value: str) -> str:
    cleaned = value.rstrip(".,;)").lower()
    if cleaned.startswith("10.1101/"):
        cleaned = re.sub(r"v\d+(?:\.full)?(?:\.pdf)?$", "", cleaned)
    return cleaned
