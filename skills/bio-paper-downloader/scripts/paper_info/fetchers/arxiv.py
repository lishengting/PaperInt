"""arXiv fetcher via the public Atom API."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.helpers import normalize_space, year_from_date, mark_match, require_title_match
from paper_info.http import FetchError, get_text
from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class ArxivFetcher(Fetcher):
    """Fetch arXiv metadata through the public Atom API."""

    name = "arxiv"
    api_url = "https://export.arxiv.org/api/query"
    atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
    arxiv_ns = {"arxiv": "http://arxiv.org/schemas/atom"}

    def can_fetch(self, identifier: Identifier) -> bool:
        return identifier.kind in {"arxiv", "title"}

    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        params = {"max_results": 1}
        if identifier.kind == "arxiv":
            params["id_list"] = identifier.value
        else:
            params["search_query"] = f'ti:"{identifier.value}"'
        xml_text = get_text(self.api_url, params=params, timeout=timeout)
        entries = _arxiv_entries(xml_text)
        if not entries:
            raise FetchError("arXiv returned no results")
        meta = _arxiv_entry_to_metadata(entries[0], self.name)
        mark_match(meta, identifier)
        require_title_match(identifier, meta)
        return meta


def _arxiv_entries(xml_text: str) -> list[ET.Element]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError("invalid arXiv Atom response") from exc
    return root.findall("atom:entry", ArxivFetcher.atom_ns)


def _arxiv_entry_to_metadata(entry: ET.Element, source: str) -> PaperMetadata:
    meta = PaperMetadata()
    meta.title = normalize_space(_find_atom_text(entry, "title"))
    meta.authors = [
        normalize_space(author.findtext("atom:name", namespaces=ArxivFetcher.atom_ns))
        for author in entry.findall("atom:author", ArxivFetcher.atom_ns)
    ]
    meta.authors = [a for a in meta.authors if a]
    meta.year = year_from_date(_find_atom_text(entry, "published") or _find_atom_text(entry, "updated"))
    meta.abstract = normalize_space(_find_atom_text(entry, "summary"))
    meta.arxiv_id = _arxiv_id_from_entry(entry)
    doi = entry.findtext("arxiv:doi", namespaces=ArxivFetcher.arxiv_ns)
    meta.doi = doi.lower() if doi else None
    if meta.arxiv_id:
        meta.full_text_links.append({"preprint": f"https://arxiv.org/pdf/{meta.arxiv_id}"})
        meta.full_text_links.append({"arxiv": f"https://arxiv.org/abs/{meta.arxiv_id}"})
    for link in entry.findall("atom:link", ArxivFetcher.atom_ns):
        href = link.attrib.get("href")
        title = link.attrib.get("title")
        link_type = link.attrib.get("type")
        if href and (title == "pdf" or link_type == "application/pdf"):
            meta.full_text_links.append({"preprint": href})
    meta.data_availability = "Not found"
    meta.add_source(source)
    return meta


def _arxiv_entry_to_search(entry: ET.Element) -> "SearchResult":
    from paper_info.models import SearchResult

    return SearchResult(
        title=normalize_space(_find_atom_text(entry, "title")) or "Untitled",
        year=year_from_date(_find_atom_text(entry, "published") or _find_atom_text(entry, "updated")),
        doi=(entry.findtext("arxiv:doi", namespaces=ArxivFetcher.arxiv_ns) or "").lower() or None,
        arxiv_id=_arxiv_id_from_entry(entry),
        source="arxiv",
    )


def _find_atom_text(entry: ET.Element, name: str) -> str | None:
    return entry.findtext(f"atom:{name}", namespaces=ArxivFetcher.atom_ns)


def _arxiv_id_from_entry(entry: ET.Element) -> str | None:
    entry_id = _find_atom_text(entry, "id")
    if not entry_id:
        return None
    value = entry_id.rstrip("/").rsplit("/", 1)[-1]
    return value.replace(".pdf", "")
