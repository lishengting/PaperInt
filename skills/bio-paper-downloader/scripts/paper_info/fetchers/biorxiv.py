"""bioRxiv / medRxiv fetcher."""

from __future__ import annotations

from typing import Any

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.helpers import year_from_date, split_authors, mark_match
from paper_info.http import FetchError, get_json
from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class BioRxivFetcher(Fetcher):
    """Fetch bioRxiv or medRxiv metadata through the official API."""

    api_base_url = "https://api.biorxiv.org/details"

    def __init__(self, server: str) -> None:
        self.server = server
        self.name = server
        self.site_url = f"https://www.{server}.org"

    def can_fetch(self, identifier: Identifier) -> bool:
        if identifier.kind != "doi":
            return False
        if not identifier.value.startswith("10.1101/"):
            return False
        raw = identifier.raw.lower()
        if "biorxiv.org" in raw:
            return self.server == "biorxiv"
        if "medrxiv.org" in raw:
            return self.server == "medrxiv"
        return True

    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        data = get_json(
            f"{self.api_base_url}/{self.server}/{identifier.value}",
            timeout=timeout,
        )
        collection = data.get("collection", [])
        if not collection:
            raise FetchError(f"{self.server} returned no results")
        meta = _biorxiv_item_to_metadata(_latest_biorxiv_item(collection), self.server)
        mark_match(meta, identifier)
        return meta


def _latest_biorxiv_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    return max(items, key=lambda item: _version_number(item.get("version")))


def _biorxiv_item_to_metadata(item: dict[str, Any], server: str) -> PaperMetadata:
    meta = PaperMetadata()
    meta.title = item.get("title")
    meta.authors = split_authors(item.get("authors"), sep=";")
    meta.journal = server
    meta.year = year_from_date(item.get("date"))
    meta.doi = (item.get("doi") or "").lower() or None
    meta.preprint_server = server
    meta.preprint_version = str(item.get("version")) if item.get("version") else None
    meta.abstract = item.get("abstract")
    meta.data_availability = "Not found"
    if meta.doi:
        version_suffix = f"v{meta.preprint_version}" if meta.preprint_version else ""
        meta.full_text_links.append({"preprint": f"https://www.{server}.org/content/{meta.doi}{version_suffix}.full.pdf"})
        meta.full_text_links.append({"preprint": f"https://www.{server}.org/content/{meta.doi}{version_suffix}"})
    if item.get("jatsxml"):
        meta.full_text_links.append({"jatsxml": item["jatsxml"]})
    meta.raw = {
        "category": str(item.get("category") or ""),
        "license": str(item.get("license") or ""),
        "type": str(item.get("type") or ""),
    }
    meta.add_source(server)
    return meta


def _version_number(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
