"""Shared helpers and title-matching utilities for fetchers."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

from paper_info.identifiers import Identifier
from paper_info.matching import titles_match
from paper_info.models import PaperMetadata


def strip_tags(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(text)).split())


def normalize_space(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.split())


def year_from_date(value: str | None) -> str | None:
    if value and len(value) >= 4 and value[:4].isdigit():
        return value[:4]
    return None


def year_from_text(text: str) -> str | None:
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    return match.group(1) if match else None


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff")


def _url_looks_like_pdf(url: str, content_type: str | None) -> bool:
    lowered = url.lower()
    if (content_type or "").lower().startswith("application/pdf"):
        return True
    if lowered.endswith(".pdf"):
        return True
    return any(marker in lowered for marker in (".pdf", "/pdfft", "showpdf", "main.pdf"))


def _url_looks_like_image_asset(url: str) -> bool:
    lowered = url.lower()
    path = lowered.split("?", 1)[0].split("#", 1)[0]
    if any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    return "ars.els-cdn.com/content/image/" in lowered


def classify_link(
    url: str,
    default: str,
    content_type: str | None = None,
    intended_application: str | None = None,
) -> str:
    haystack = " ".join(
        value.lower()
        for value in (url, content_type or "", intended_application or "")
        if value
    )
    if any(marker in haystack for marker in ("supplement", "supplementary", "suppl", "suppinfo")):
        return "supplement"
    if any(marker in haystack for marker in ("jats", "xml", "source.xml")):
        return "jatsxml"
    if _url_looks_like_image_asset(url):
        return "asset"
    if _url_looks_like_pdf(url, content_type):
        return default
    return default


def first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str):
        return value
    return None


def xml_text(node: object | None) -> str | None:
    import xml.etree.ElementTree as ET

    if node is None or not isinstance(node, ET.Element):
        return None
    text = "".join(node.itertext())
    return " ".join(html.unescape(text).split()) or None


def date_prefix(value: str | None) -> str | None:
    if value and len(value) >= 10:
        return value[:10]
    return None


def join_creators(creators: list[dict[str, Any]], *, key: str = "name") -> str | None:
    names = [c.get(key, "") for c in creators if c.get(key)]
    return ", ".join(names) if names else None


def split_authors(author_string: str | None, sep: str = ",") -> list[str]:
    if not author_string:
        return []
    return [part.strip() for part in author_string.split(sep) if part.strip()]


# -- Title matching helpers --------------------------------------------------


def mark_match(meta: PaperMetadata, identifier: Identifier) -> None:
    if identifier.kind == "title":
        meta.match_type = "title"
        meta.confidence = _title_confidence(meta)
    elif identifier.kind in {"doi", "pmid", "pmcid", "arxiv"}:
        meta.match_type = identifier.kind
        meta.confidence = 100
    else:
        meta.match_type = identifier.kind
        meta.confidence = 60


def require_title_match(identifier: Identifier, meta: PaperMetadata) -> None:
    if identifier.kind == "title" and not titles_match(identifier.value, meta.title):
        raise ValueError("title candidate did not match query closely enough")


def first_matching_title_candidate(
    identifier: Identifier,
    candidates: Iterable[PaperMetadata],
    source_name: str,
) -> PaperMetadata:
    for meta in candidates:
        mark_match(meta, identifier)
        if identifier.kind != "title" or _title_candidate_matches(identifier, meta):
            return meta
    raise ValueError(f"{source_name} returned no matching title candidates")


def _title_candidate_matches(identifier: Identifier, meta: PaperMetadata) -> bool:
    if not titles_match(identifier.value, meta.title):
        return False
    query_year = year_from_text(identifier.value)
    if query_year and meta.year and meta.year != query_year:
        return False
    return True


def _title_confidence(meta: PaperMetadata) -> int:
    if "europepmc" in meta.sources:
        return 70
    if "pubmed" in meta.sources:
        return 65
    if "arxiv" in meta.sources:
        return 60
    return 40
