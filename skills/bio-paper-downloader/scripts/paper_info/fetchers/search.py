"""Multi-source paper search."""

from __future__ import annotations

from paper_info.fetchers.arxiv import ArxivFetcher, _arxiv_entries, _arxiv_entry_to_search
from paper_info.fetchers.crossref import CrossrefFetcher, _crossref_work_to_search
from paper_info.fetchers.europepmc import EuropePMCFetcher, _europepmc_result_to_search
from paper_info.http import FetchError, get_json, get_text
from paper_info.models import SearchResult


def search_biomed(query: str, limit: int, timeout: float) -> list[SearchResult]:
    """Search papers with Europe PMC and Crossref, merging results."""

    epmc_results: list[SearchResult] = []
    try:
        data = get_json(
            EuropePMCFetcher.search_url,
            params={
                "query": query,
                "format": "json",
                "pageSize": max(1, limit),
                "resultType": "lite",
            },
            timeout=timeout,
        )
        items = data.get("resultList", {}).get("result", [])
        epmc_results = [_europepmc_result_to_search(item) for item in items[:limit]]
    except FetchError:
        pass

    crossref_results: list[SearchResult] = []
    try:
        data = get_json(
            CrossrefFetcher.base_url,
            params={"query.title": query, "rows": max(1, limit)},
            timeout=timeout,
        )
        crossref_results = [
            _crossref_work_to_search(item)
            for item in data.get("message", {}).get("items", [])[:limit]
        ]
    except FetchError:
        pass

    seen_dois: set[str] = set()
    merged: list[SearchResult] = []
    for epmc, cr in zip(epmc_results, crossref_results):
        for r in (epmc, cr):
            key = r.doi or r.title.lower()
            if key in seen_dois:
                continue
            seen_dois.add(key)
            merged.append(r)
    for r in epmc_results[len(crossref_results):] + crossref_results[len(epmc_results):]:
        key = r.doi or r.title.lower()
        if key in seen_dois:
            continue
        seen_dois.add(key)
        merged.append(r)

    return merged[:limit]


def search_cs(query: str, limit: int, timeout: float) -> list[SearchResult]:
    """Search arXiv papers by title or keyword."""

    xml_text = get_text(
        ArxivFetcher.api_url,
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, limit),
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
        timeout=timeout,
    )
    return [_arxiv_entry_to_search(entry) for entry in _arxiv_entries(xml_text)[:limit]]
