"""Dataset resource lookups: Zenodo, Figshare, Dryad, OSF."""

from __future__ import annotations

import re
from typing import Any

from paper_info.fetchers.helpers import date_prefix, join_creators, normalize_space, strip_tags
from paper_info.http import FetchError, get_json


def _extract_zenodo_id(accession: str) -> str | None:
    m = re.search(r"zenodo\S*?(\d{5,})", accession)
    return m.group(1) if m else None


def _extract_figshare_id(accession: str) -> str | None:
    m = re.search(r"figshare\S*?(\d{4,})", accession)
    return m.group(1) if m else None


def _extract_dryad_doi(accession: str) -> str | None:
    m = re.search(r"10\.5061/dryad\.\S+", accession)
    return m.group(0).rstrip(".,;)\"'") if m else None


def _extract_osf_guid(accession: str) -> str | None:
    m = re.search(r"osf\.io/([a-z0-9]+)", accession, re.IGNORECASE)
    return m.group(1) if m else None


def _format_file_size(size_bytes: int) -> str:
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


def _try_fetch(url: str, timeout: float) -> dict[str, Any] | None:
    try:
        return get_json(url, timeout=timeout)
    except FetchError:
        return None


def _extract_file_list(files_data: list[dict[str, Any]], *, dl_key: str, name_key: str) -> list[dict[str, Any]]:
    files = []
    for f in files_data:
        dl = f.get(dl_key, "")
        if isinstance(dl, dict):
            dl = dl.get("self", "")
        if dl:
            files.append({
                "name": f.get(name_key, ""),
                "size": _format_file_size(f.get("size", 0)),
                "download": dl,
            })
    return files


def lookup_zenodo(accession: str, timeout: float) -> dict[str, Any] | None:
    record_id = _extract_zenodo_id(accession)
    if not record_id:
        return None
    data = _try_fetch(f"https://zenodo.org/api/records/{record_id}", timeout)
    if data is None:
        return None

    metadata = data.get("metadata", {})
    version = metadata.get("version")
    if not version:
        relations = metadata.get("relations", {}) or data.get("relations", {})
        for vrel in relations.get("version", []):
            idx = vrel.get("index")
            if idx is not None:
                version = str(idx + 1)
                break
    files = _extract_file_list(data.get("files", []), dl_key="links", name_key="key")
    return {
        "title": metadata.get("title"),
        "description": normalize_space(strip_tags(metadata.get("description") or "")),
        "creators": join_creators(metadata.get("creators", [])),
        "published": metadata.get("publication_date"),
        "version": version,
        "files": files or None,
        "status": data.get("status") or metadata.get("access_right"),
    }


def lookup_figshare(accession: str, timeout: float) -> dict[str, Any] | None:
    article_id = _extract_figshare_id(accession)
    if not article_id:
        return None
    data = _try_fetch(f"https://api.figshare.com/v2/articles/{article_id}", timeout)
    if data is None:
        return None

    files = _extract_file_list(data.get("files", []), dl_key="download_url", name_key="name")
    return {
        "title": data.get("title"),
        "description": normalize_space(strip_tags(data.get("description") or "")),
        "creators": join_creators(data.get("authors", []), key="full_name"),
        "published": date_prefix(data.get("published_date")),
        "files": files or None,
        "status": "public" if data.get("is_public") else data.get("status", "").lower() or None,
    }


def lookup_dryad(accession: str, timeout: float) -> dict[str, Any] | None:
    doi = _extract_dryad_doi(accession)
    if not doi:
        return None
    import urllib.parse

    encoded = urllib.parse.quote(doi, safe="")
    data = _try_fetch(f"https://datadryad.org/api/v2/datasets/{encoded}", timeout)
    if data is None:
        return None

    authors = data.get("authors", [])
    names = []
    for a in authors:
        name = f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
        if name:
            names.append(name)

    return {
        "title": data.get("title"),
        "description": normalize_space(strip_tags(data.get("abstract") or "")),
        "creators": ", ".join(names) if names else None,
        "published": data.get("publicationDate"),
        "files": None,
        "status": data.get("visibility") or data.get("curationStatus"),
    }


def lookup_osf(accession: str, timeout: float) -> dict[str, Any] | None:
    guid = _extract_osf_guid(accession)
    if not guid:
        return None
    data = _try_fetch(f"https://api.osf.io/v2/nodes/{guid}/", timeout)
    if data is None:
        return None

    attrs = data.get("data", {}).get("attributes", {})
    return {
        "title": attrs.get("title"),
        "description": normalize_space(strip_tags(attrs.get("description") or "")),
        "creators": None,
        "published": date_prefix(attrs.get("date_created")),
        "files": None,
        "status": "public" if attrs.get("public") else "private",
    }


def lookup_dataset_resource(accession: str, timeout: float) -> dict[str, Any] | None:
    lower = accession.lower()
    if "zenodo" in lower:
        return lookup_zenodo(accession, timeout)
    if "figshare" in lower:
        return lookup_figshare(accession, timeout)
    if "dryad" in lower:
        return lookup_dryad(accession, timeout)
    if "osf.io" in lower:
        return lookup_osf(accession, timeout)
    return None
