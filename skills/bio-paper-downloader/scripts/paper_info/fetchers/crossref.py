"""Crossref fetcher."""

from __future__ import annotations

from typing import Any

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.helpers import classify_link, first, mark_match, strip_tags, first_matching_title_candidate
from paper_info.http import FetchError, get_json
from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class CrossrefFetcher(Fetcher):
    """Fetch DOI/title metadata from Crossref."""

    name = "crossref"
    base_url = "https://api.crossref.org/works"

    def can_fetch(self, identifier: Identifier) -> bool:
        return identifier.kind in {"doi", "title"}

    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        if identifier.kind == "doi":
            url = f"{self.base_url}/{identifier.value}"
            message = get_json(url, timeout=timeout).get("message", {})
            meta = _crossref_work_to_metadata(message, self.name)
            mark_match(meta, identifier)
            return meta

        data = get_json(
            self.base_url,
            params={"query.title": identifier.value, "rows": 5},
            timeout=timeout,
        )
        items = data.get("message", {}).get("items", [])
        if not items:
            raise FetchError("Crossref returned no results")
        return first_matching_title_candidate(
            identifier,
            (_crossref_work_to_metadata(item, self.name) for item in items),
            "Crossref",
        )


def _crossref_work_to_metadata(work: dict[str, Any], source: str) -> PaperMetadata:
    meta = PaperMetadata()
    meta.title = first(work.get("title"))
    meta.authors = _crossref_authors(work.get("author", []))
    meta.journal = first(work.get("container-title"))
    meta.year = _crossref_year(work)
    meta.doi = work.get("DOI", "").lower() or None
    abstract = work.get("abstract")
    if abstract:
        meta.abstract = strip_tags(abstract)
    for link in work.get("link", []) or []:
        url = link.get("URL")
        if url:
            meta.full_text_links.append({
                classify_link(
                    url,
                    default="publisher",
                    content_type=link.get("content-type"),
                    intended_application=link.get("intended-application"),
                ): url
            })
    for relation in ("is-supplemented-by", "has-supplement"):
        for item in work.get("relation", {}).get(relation, []) or []:
            relation_url = _relation_url(item)
            if relation_url:
                meta.full_text_links.append({"supplement": relation_url})
    if work.get("URL"):
        meta.full_text_links.append({"publisher": work["URL"]})
    meta.add_source(source)
    return meta


def _crossref_work_to_search(work: dict[str, Any]) -> "SearchResult":
    from paper_info.models import SearchResult

    return SearchResult(
        title=first(work.get("title")) or "Untitled",
        year=_crossref_year(work),
        journal=first(work.get("container-title")),
        doi=(work.get("DOI") or "").lower() or None,
        source="crossref",
    )


def _crossref_authors(authors: list[dict[str, Any]]) -> list[str]:
    names = []
    for author in authors:
        parts = [author.get("given"), author.get("family")]
        name = " ".join(part for part in parts if part)
        if name:
            names.append(name)
    return names


def _crossref_year(work: dict[str, Any]) -> str | None:
    for key in ("published-print", "published-online", "published", "issued"):
        date_parts = work.get(key, {}).get("date-parts")
        if date_parts and date_parts[0]:
            return str(date_parts[0][0])
    return None


def _relation_url(item: dict[str, Any]) -> str | None:
    value = item.get("id")
    if not value:
        return None
    if item.get("id-type") == "doi":
        return f"https://doi.org/{value}"
    if str(value).startswith(("http://", "https://")):
        return str(value)
    return None
