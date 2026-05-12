"""Europe PMC fetcher and availability-text extraction."""

from __future__ import annotations

import re
from typing import Any

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.helpers import classify_link, first_matching_title_candidate, split_authors, strip_tags, normalize_space
from paper_info.http import FetchError, get_json, get_text
from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class EuropePMCFetcher(Fetcher):
    """Fetch metadata, data availability, and links from Europe PMC."""

    name = "europepmc"
    search_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def can_fetch(self, identifier: Identifier) -> bool:
        return identifier.kind in {"doi", "pmid", "pmcid", "title"}

    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        query = _europepmc_query(identifier)
        data = get_json(
            self.search_url,
            params={
                "query": query,
                "format": "json",
                "pageSize": 5,
                "resultType": "core",
            },
            timeout=timeout,
        )
        results = data.get("resultList", {}).get("result", [])
        if not results:
            raise FetchError("Europe PMC returned no results")
        meta = first_matching_title_candidate(
            identifier,
            (_europepmc_result_to_metadata(result, self.name) for result in results),
            "Europe PMC",
        )
        if not meta.data_availability and meta.pmcid:
            meta.data_availability = _fetch_pmc_availability_text(meta.pmcid, timeout)
        return meta


def _europepmc_result_to_metadata(result: dict[str, Any], source: str) -> PaperMetadata:
    meta = PaperMetadata()
    meta.title = result.get("title")
    meta.authors = split_authors(result.get("authorString"))
    meta.journal = result.get("journalTitle")
    meta.year = result.get("pubYear")
    meta.doi = (result.get("doi") or "").lower() or None
    meta.pmid = result.get("pmid")
    meta.pmcid = result.get("pmcid")
    meta.abstract = result.get("abstractText")
    meta.data_availability = result.get("dataAvailability") or None
    for link in _europepmc_links(result):
        meta.full_text_links.append(link)
    _store_pmc_oa(result, meta)
    meta.add_source(source)
    return meta


def _store_pmc_oa(result: dict[str, Any], meta: PaperMetadata) -> None:
    """Store Europe PMC open-access status in meta.raw."""
    oa_info: dict[str, Any] = {}
    is_oa = result.get("isOpenAccess")
    if is_oa:
        oa_info["is_open_access"] = is_oa == "Y"
    has_pdf = result.get("hasPDF")
    if has_pdf:
        oa_info["has_pdf"] = has_pdf == "Y"
    oa_detail = result.get("openAccessInfo") or result.get("openAccess")
    if oa_detail:
        oa_info["license"] = oa_detail.get("license")
        oa_info["oa_status"] = oa_detail.get("status")
        oa_info["oa_url"] = oa_detail.get("selfUrl") or oa_detail.get("url")
    if oa_info:
        meta.raw["pmc_oa"] = oa_info


def _europepmc_result_to_search(result: dict[str, Any]) -> "SearchResult":
    from paper_info.models import SearchResult

    return SearchResult(
        title=result.get("title") or "Untitled",
        year=result.get("pubYear"),
        journal=result.get("journalTitle"),
        doi=(result.get("doi") or "").lower() or None,
        pmid=result.get("pmid"),
        pmcid=result.get("pmcid"),
        source="europepmc",
    )


def _europepmc_query(identifier: Identifier) -> str:
    if identifier.kind == "doi":
        return f'DOI:"{identifier.value}"'
    if identifier.kind == "pmid":
        return f"EXT_ID:{identifier.value} AND SRC:MED"
    if identifier.kind == "pmcid":
        return f"PMCID:{identifier.value}"
    return identifier.value


def _europepmc_links(result: dict[str, Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    pmcid = result.get("pmcid")
    if pmcid:
        links.append({"pmc": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"})
    doi = result.get("doi")
    if doi:
        links.append({"publisher": f"https://doi.org/{doi}"})
    for item in result.get("fullTextUrlList", {}).get("fullTextUrl", []) or []:
        url = item.get("url")
        if url:
            label = classify_link(
                url,
                default=(item.get("site") or "full_text").lower().replace(" ", "_"),
                content_type=item.get("documentStyle"),
                intended_application=item.get("availability"),
            )
            links.append({label: url})
    return links


# -- PMC availability-text extraction ----------------------------------------


def _fetch_pmc_availability_text(pmcid: str, timeout: float) -> str | None:
    try:
        html_text = get_text(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", timeout=timeout)
    except FetchError:
        return None
    return _extract_availability_from_html(html_text)


def _extract_availability_from_html(html_text: str) -> str | None:
    start_markers = (
        "data availability statement",
        "data availability",
        "data and code availability",
        "data and materials availability",
        "availability of data and materials",
        "availability of data",
    )
    candidates = _availability_candidates_from_headings(html_text, start_markers)
    if not candidates:
        text = _html_to_text(html_text)
        lower = text.lower()
        candidates = _availability_candidates(text, lower, start_markers)
    candidates = [c for c in candidates if _availability_score(c)[0] > 0]
    candidates = [c for c in candidates if not _is_pmc_page_chrome(c)]
    if not candidates:
        return None
    return max(candidates, key=_availability_score)[:4000]


def _availability_candidates_from_headings(html_text: str, start_markers: tuple[str, ...]) -> list[str]:
    heading_re = re.compile(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1>")
    headings = list(heading_re.finditer(html_text))
    candidates = []
    for index, heading in enumerate(headings):
        heading_text = normalize_space(strip_tags(heading.group(2))).lower().rstrip(".:")
        if not any(marker == heading_text or heading_text.startswith(f"{marker}.") for marker in start_markers):
            continue
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(html_text)
        section_text = _html_to_text(html_text[heading.start():section_end]).strip()
        if section_text:
            candidates.append(section_text)
    return candidates


def _is_pmc_page_chrome(section: str) -> bool:
    lower = section.lower()
    chrome_markers = (
        "data availability statements, or supplementary materials included in this article",
        "actions. view on publisher site",
        "resources. similar articles",
        "follow ncbi",
        "national library of medicine",
        "add to collections",
    )
    return any(marker in lower for marker in chrome_markers)


def _availability_candidates(text: str, lower: str, start_markers: tuple[str, ...]) -> list[str]:
    stop_markers = (
        "code availability",
        "supplementary materials",
        "supplementary information",
        "acknowledgements",
        "author contributions",
        "competing interests",
        "conflict of interest",
        "ethics declarations",
        "footnotes",
        "references",
        "actions",
        "resources",
    )
    candidates = []
    for marker in start_markers:
        start = 0
        while True:
            start = lower.find(marker, start)
            if start == -1:
                break
            end = _first_marker_position(lower, stop_markers, start + len(marker) + 1)
            section = text[start:end].strip() if end is not None else text[start:].strip()
            if section:
                candidates.append(section)
            start += len(marker)
    return candidates


def _availability_score(section: str) -> tuple[int, int]:
    lower = section.lower()
    score = 0
    if "http://" in lower or "https://" in lower:
        score += 4
    if re.search(r"\b(?:gcst\d{6,}|gse\d{3,}|sr[aprxr]\d{3,}|prjna\d{3,}|cngb|cnp\d{3,})\b", lower):
        score += 4
    if re.search(r"\b10\.(?:5281|6084|5061|17605)/[-._;()/:a-z0-9]+\b", lower):
        score += 4
    if "available" in lower:
        score += 1
    return score, -len(section)


def _html_to_text(html_text: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", " ", html_text)
    cleaned = re.sub(r"(?i)<br\s*/?>", " ", cleaned)
    cleaned = re.sub(r"(?i)</(?:p|div|section|article|h[1-6]|li)>", ". ", cleaned)
    return strip_tags(cleaned)


def _first_marker_position(text: str, markers: tuple[str, ...], start: int) -> int | None:
    positions = [text.find(marker, start) for marker in markers]
    positions = [p for p in positions if p != -1]
    return min(positions) if positions else None
