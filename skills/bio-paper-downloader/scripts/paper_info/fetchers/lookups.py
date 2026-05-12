"""Accession lookups: GWAS Catalog, ENA, NCBI."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from paper_info.fetchers.pubmed import PubmedFetcher
from paper_info.http import FetchError, get_json, get_text
from paper_info.models import Accession, LookupResult, PaperMetadata


def query_gwas_catalog(paper: PaperMetadata, timeout: float) -> list[Accession]:
    """Query GWAS Catalog associations by PMID when available."""

    if not paper.pmid:
        return []
    url = "https://www.ebi.ac.uk/gwas/rest/api/studies/search/findByPubmedId"
    try:
        data = get_json(url, params={"pubmedId": paper.pmid}, timeout=timeout)
    except FetchError:
        return []
    studies = data.get("_embedded", {}).get("studies", [])
    accessions = []
    for study in studies:
        accession = study.get("accessionId")
        if accession:
            trait = study.get("diseaseTrait", {}).get("trait") or study.get("initialSampleSize") or ""
            accessions.append(Accession("GWAS", accession, str(trait)))
    return accessions


def lookup_ena(accession: str, timeout: float) -> LookupResult | None:
    """Lookup an accession in the ENA Portal API."""

    fields = [
        "study_accession",
        "secondary_study_accession",
        "sample_accession",
        "run_accession",
        "experiment_accession",
        "scientific_name",
        "study_title",
        "experiment_title",
        "submitted_ftp",
        "first_public",
        "status",
    ]
    try:
        text = get_text(
            "https://www.ebi.ac.uk/ena/portal/api/search",
            params={
                "result": "read_run",
                "query": f'accession="{accession}"',
                "fields": ",".join(fields),
                "format": "tsv",
                "limit": 1,
            },
            timeout=timeout,
        )
    except FetchError:
        return None
    rows = [line.split("\t") for line in text.splitlines() if line.strip()]
    if len(rows) < 2:
        return None
    header, row = rows[0], rows[1]
    data = dict(zip(header, row))
    return LookupResult(
        accession=accession,
        title=data.get("study_title") or data.get("experiment_title"),
        organism=data.get("scientific_name"),
        type="ENA read run/study",
        submitted=data.get("first_public"),
        status=data.get("status"),
        source="ena",
    )


def lookup_ncbi(accession: str, db: str, timeout: float) -> LookupResult | None:
    """Lookup an accession in NCBI using esearch + esummary."""

    try:
        xml_text = get_text(
            PubmedFetcher.esearch_url,
            params={"db": db, "term": accession, "retmode": "xml", "retmax": 1},
            timeout=timeout,
        )
        root = ET.fromstring(xml_text)
        uid = root.findtext(".//Id")
        if not uid:
            return None
        summary = get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": db, "id": uid, "retmode": "json"},
            timeout=timeout,
        )
    except (FetchError, ET.ParseError):
        return None
    item = summary.get("result", {}).get(uid, {})
    return LookupResult(
        accession=accession,
        title=item.get("title") or item.get("expacc") or item.get("bioproject"),
        organism=item.get("organism") or item.get("taxname"),
        type=db,
        samples=_sample_count(item.get("runs") or item.get("samples")),
        submitted=item.get("submissiondate") or item.get("createdate"),
        status=item.get("status"),
        source=f"ncbi:{db}",
        extra={k: str(v) for k, v in item.items() if isinstance(v, (str, int))},
    )


def _sample_count(value: Any) -> str | None:
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        stripped = value.strip()
        if stripped.startswith("<"):
            return _parse_ncbi_xml_stats(stripped)
        return stripped
    return None


def _parse_ncbi_xml_stats(xml_str: str) -> str | None:
    try:
        wrapped = f"<root>{xml_str}</root>"
        root = ET.fromstring(wrapped)
        elements = root.findall("Run") or root.findall("Sample")
        if not elements:
            return None
        count = len(elements)
        parts = [f"{count} run{'s' if count > 1 else ''}"]
        total_spots = sum(int(el.get("total_spots", 0)) for el in elements)
        total_bases = sum(int(el.get("total_bases", 0)) for el in elements)
        if total_spots:
            parts.append(_human_number(total_spots, "spots"))
        if total_bases:
            parts.append(_human_bases(total_bases))
        return ", ".join(parts)
    except ET.ParseError:
        return xml_str


def _human_number(n: int, unit: str) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}G {unit}"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M {unit}"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K {unit}"
    return f"{n} {unit}"


def _human_bases(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}G bases"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M bases"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K bases"
    return f"{n} bases"
