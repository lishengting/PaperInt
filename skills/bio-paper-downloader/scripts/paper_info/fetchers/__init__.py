"""Remote metadata fetchers — public API re-exports."""

from paper_info.fetchers.base import Fetcher
from paper_info.fetchers.arxiv import ArxivFetcher
from paper_info.fetchers.biorxiv import BioRxivFetcher
from paper_info.fetchers.crossref import CrossrefFetcher
from paper_info.fetchers.europepmc import EuropePMCFetcher
from paper_info.fetchers.pubmed import PubmedFetcher
from paper_info.fetchers.search import search_biomed, search_cs
from paper_info.fetchers.lookups import lookup_ena, lookup_ncbi, query_gwas_catalog
from paper_info.fetchers.resources import lookup_dataset_resource

BIOMED_FETCHERS: list[Fetcher] = [
    BioRxivFetcher("biorxiv"),
    BioRxivFetcher("medrxiv"),
    CrossrefFetcher(),
    EuropePMCFetcher(),
    PubmedFetcher(),
]
CS_FETCHERS: list[Fetcher] = [ArxivFetcher()]
FETCHERS: dict[str, list[Fetcher]] = {
    "biomed": BIOMED_FETCHERS,
    "cs": CS_FETCHERS,
}

__all__ = [
    "Fetcher",
    "ArxivFetcher",
    "BioRxivFetcher",
    "CrossrefFetcher",
    "EuropePMCFetcher",
    "PubmedFetcher",
    "BIOMED_FETCHERS",
    "CS_FETCHERS",
    "FETCHERS",
    "lookup_dataset_resource",
    "lookup_ena",
    "lookup_ncbi",
    "query_gwas_catalog",
    "search_biomed",
    "search_cs",
]
