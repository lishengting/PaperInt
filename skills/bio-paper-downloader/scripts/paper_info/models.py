"""Core data models for paperutils."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Identifier:
    """A normalized user-supplied paper identifier."""

    kind: str
    value: str
    raw: str


@dataclass
class PaperMetadata:
    """Merged metadata for one paper."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    year: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    preprint_server: str | None = None
    preprint_version: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str | None = None
    data_availability: str | None = None
    full_text_links: list[dict[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    match_type: str | None = None
    confidence: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def add_source(self, source: str) -> None:
        """Record a source once, preserving insertion order."""

        if source not in self.sources:
            self.sources.append(source)


@dataclass
class SearchResult:
    """Compact search result entry."""

    title: str
    year: str | None = None
    journal: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    source: str | None = None


@dataclass
class Accession:
    """A dataset or database accession related to a paper."""

    type: str
    accession: str
    description: str = ""


@dataclass
class LookupResult:
    """Basic metadata for one accession."""

    accession: str
    title: str | None = None
    organism: str | None = None
    type: str | None = None
    samples: str | None = None
    submitted: str | None = None
    status: str | None = None
    source: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class DatasetRecord:
    """Dataset entry inside a paper record."""

    accession: str
    type: str
    description: str = ""
    title: str | None = None
    organism: str | None = None
    samples: str | None = None
    status: str | None = None
    submitted: str | None = None
    source: str | None = None
    url: str | None = None
    download: str | None = None
    creators: str | None = None
    published: str | None = None
    version: str | None = None
    files: list[dict[str, Any]] | None = None


@dataclass
class PaperRecord:
    """One-stop paper dossier returned by ``paperutils get``."""

    identity: PaperMetadata
    abstract: str | None = None
    data_availability: str | None = None
    supplement: dict[str, Any] = field(default_factory=dict)
    code_repos: list[dict[str, Any]] = field(default_factory=list)
    datasets: list[DatasetRecord] = field(default_factory=list)
    full_text_links: list[dict[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
