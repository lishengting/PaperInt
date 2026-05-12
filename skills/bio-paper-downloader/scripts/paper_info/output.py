"""Output formatting helpers."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from paper_info.models import LookupResult, PaperMetadata, PaperRecord, SearchResult


def print_paper_record(record: PaperRecord, *, as_json: bool = False, full_abstract: bool = False) -> None:
    """Print a one-stop paper dossier."""

    data = _paper_record_dict(record, full_abstract=full_abstract)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    _print_mapping(data)


def print_explanation(result: LookupResult, *, as_json: bool = False) -> None:
    """Print accession lookup result."""

    data = dataclasses.asdict(result)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for key in ("accession", "title", "organism", "type", "samples", "submitted", "status", "source"):
        print(f"{key + ':':<12} {_format_value(data.get(key))}")


def print_find_results(results: list[SearchResult], *, as_json: bool = False) -> None:
    """Print search result list."""

    if as_json:
        print(json.dumps([dataclasses.asdict(item) for item in results], ensure_ascii=False, indent=2))
        return
    print(f"{'#':<3} {'year':<6} {'pmid':<10} {'doi/arxiv':<32} title")
    for index, item in enumerate(results, start=1):
        identifier = item.doi or item.arxiv_id
        print(
            f"{index:<3} {_format_value(item.year):<6} "
            f"{_format_value(item.pmid):<10} {_format_value(identifier):<32} {item.title}"
        )


def _paper_record_dict(record: PaperRecord, *, full_abstract: bool) -> dict[str, Any]:
    meta = record.identity
    abstract = record.abstract or "Not found"
    if not full_abstract and len(abstract) > 500:
        abstract = abstract[:497].rstrip() + "..."
    return {
        "identity": _identity_dict(meta),
        "abstract": abstract,
        "data_availability": record.data_availability or "Not found",
        "supplement": record.supplement,
        "code_repos": record.code_repos,
        "datasets": [dataclasses.asdict(item) for item in record.datasets],
        "full_text_links": [
            {"type": key, "url": value}
            for link in record.full_text_links
            for key, value in link.items()
        ],
        "sources": record.sources,
    }


def _identity_dict(meta: PaperMetadata) -> dict[str, Any]:
    return {
        "title": meta.title or "Not found",
        "authors": _format_authors(meta.authors),
        "journal": meta.journal or "Not found",
        "year": meta.year or "Not found",
        "doi": meta.doi or "Not found",
        "pmid": meta.pmid or "Not found",
        "pmcid": meta.pmcid or "Not found",
        "arxiv_id": meta.arxiv_id or "Not found",
        "preprint_server": meta.preprint_server or "Not found",
        "preprint_version": meta.preprint_version or "Not found",
    }


def _print_mapping(data: Any, indent: int = 0) -> None:
    prefix = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f"{prefix}{key}:")
                _print_mapping(value, indent + 2)
            else:
                print(f"{prefix}{key}: {_format_value(value)}")
    elif isinstance(data, list):
        if not data:
            print(f"{prefix}[]")
            return
        for item in data:
            if isinstance(item, dict):
                first = True
                for key, value in item.items():
                    marker = "-" if first else " "
                    if isinstance(value, (dict, list)):
                        print(f"{prefix}{marker} {key}:")
                        _print_mapping(value, indent + 4)
                    else:
                        print(f"{prefix}{marker} {key}: {_format_value(value)}")
                    first = False
            else:
                print(f"{prefix}- {_format_value(item)}")


def _format_authors(authors: list[str]) -> str:
    if not authors:
        return "Not found"
    if len(authors) <= 6:
        return ", ".join(authors)
    return f"{', '.join(authors[:2])}, et al. ({len(authors)} authors)"


def _format_value(value: Any) -> str:
    if value is None or value == "":
        return "Not found"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
