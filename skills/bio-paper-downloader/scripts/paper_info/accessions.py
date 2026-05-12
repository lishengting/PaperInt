"""Accession extraction and classification."""

from __future__ import annotations

import re

from paper_info.models import Accession


ACCESSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GEO", re.compile(r"\bGSE\d{3,}\b", re.IGNORECASE)),
    ("GEO", re.compile(r"\bGPL\d{3,}\b", re.IGNORECASE)),
    ("GEO", re.compile(r"\bGSM\d{3,}\b", re.IGNORECASE)),
    ("SRA", re.compile(r"\bSRP\d{3,}\b", re.IGNORECASE)),
    ("SRA", re.compile(r"\bSRA\d{3,}\b", re.IGNORECASE)),
    ("SRA", re.compile(r"\bSRX\d{3,}\b", re.IGNORECASE)),
    ("SRA", re.compile(r"\bSRR\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERA\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERP\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERS\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERR\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERX\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bERZ\d{3,}\b", re.IGNORECASE)),
    ("ENA", re.compile(r"\bPRJEB\d{3,}\b", re.IGNORECASE)),
    ("BioProject", re.compile(r"\bPRJNA\d{3,}\b", re.IGNORECASE)),
    ("BioProject", re.compile(r"\bPRJDB\d{3,}\b", re.IGNORECASE)),
    ("Assembly", re.compile(r"\bGCA_\d{9}(?:\.\d+)?\b", re.IGNORECASE)),
    ("Assembly", re.compile(r"\bGCF_\d{9}(?:\.\d+)?\b", re.IGNORECASE)),
    ("dbGaP", re.compile(r"\bphs\d{6}(?:\.\w+)?\b", re.IGNORECASE)),
    ("GWAS", re.compile(r"\bGCST\d{6,}\b", re.IGNORECASE)),
    ("ArrayExpress", re.compile(r"\bE-[A-Z]{4}-\d+\b", re.IGNORECASE)),
    ("CNGB", re.compile(r"\bCNP\d{6,}\b", re.IGNORECASE)),
]

URL_RE = re.compile(r"https?://[^\s<>()\]\"']+", re.IGNORECASE)
DATASET_DOI_RE = re.compile(
    r"\b10\.(?:5281|6084|5061|17605)/[-._;()/:A-Z0-9]+\b",
    re.IGNORECASE,
)

DATASET_HOST_TYPES = {
    "db.cngb.org": "CNGB",
    "zenodo.org": "Zenodo",
    "figshare.com": "Figshare",
    "datadryad.org": "Dryad",
    "dryad.org": "Dryad",
    "osf.io": "OSF",
}


def extract_accessions(text: str | None) -> list[Accession]:
    """Extract known accession formats from free text."""

    if not text:
        return []
    found: dict[str, Accession] = {}
    for acc_type, pattern in ACCESSION_PATTERNS:
        for match in pattern.findall(text):
            accession = match.upper()
            if accession not in found:
                found[accession] = Accession(acc_type, accession, _sentence_for(text, match))
    return list(found.values())


def extract_code_repos(text: str | None) -> list[dict[str, str]]:
    """Extract GitHub repositories from free text."""

    repos: dict[str, dict[str, str]] = {}
    for url in _urls(text):
        normalized = _normalize_url(url)
        repo_url = _github_repo_url(normalized)
        if repo_url and repo_url not in repos:
            repos[repo_url] = {
                "url": repo_url,
                "source": "data_availability",
            }
    return list(repos.values())


def extract_dataset_resources(text: str | None) -> list[Accession]:
    """Extract dataset repository URLs and dataset DOIs from free text."""

    if not text:
        return []
    found: dict[str, Accession] = {}
    for url in _urls(text):
        normalized = _normalize_url(url)
        resource_type = _dataset_type_for_url(normalized)
        if resource_type:
            found[normalized] = Accession(resource_type, normalized, _sentence_for(text, url))
    for match in DATASET_DOI_RE.findall(text):
        doi = _clean_resource_doi(match)
        resource_type = _dataset_type_for_doi(doi)
        found.setdefault(doi, Accession(resource_type, doi, _sentence_for(text, match)))
    return list(found.values())


def classify_accession(accession: str) -> str:
    """Return a coarse database/type label for an accession."""

    value = accession.strip().upper()
    for acc_type, pattern in ACCESSION_PATTERNS:
        if pattern.fullmatch(value):
            return acc_type
    if value.startswith("PRJ"):
        return "BioProject"
    return "unknown"


def _sentence_for(text: str, needle: str) -> str:
    start = max(text.lower().find(needle.lower()), 0)
    left = text.rfind(".", 0, start)
    right = text.find(".", start)
    if left == -1:
        left = 0
    else:
        left += 1
    if right == -1:
        right = min(len(text), start + 120)
    sentence = " ".join(text[left:right].strip().split())
    return sentence[:180]


def _urls(text: str | None) -> list[str]:
    if not text:
        return []
    return URL_RE.findall(text)


def _normalize_url(url: str) -> str:
    return url.rstrip(".,;)")


def _github_repo_url(url: str) -> str | None:
    match = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/#?\s]+)", url, re.IGNORECASE)
    if not match:
        return None
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    return f"https://github.com/{owner}/{repo}"


def _dataset_type_for_url(url: str) -> str | None:
    lower = url.lower()
    for host, resource_type in DATASET_HOST_TYPES.items():
        if host in lower:
            return resource_type
    return None


def _dataset_type_for_doi(doi: str) -> str:
    lower = doi.lower()
    if lower.startswith("10.5281/zenodo."):
        return "Zenodo"
    if lower.startswith("10.6084/m9.figshare."):
        return "Figshare"
    if lower.startswith("10.5061/dryad."):
        return "Dryad"
    if lower.startswith("10.17605/osf.io/"):
        return "OSF"
    return "Dataset DOI"


def _clean_resource_doi(value: str) -> str:
    return value.rstrip(".,;)").lower()
