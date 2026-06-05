"""High-level operations for the paperutils CLI."""

from __future__ import annotations

import concurrent.futures
from typing import Iterable

from paper_info.accessions import classify_accession, extract_accessions, extract_code_repos, extract_dataset_resources
from paper_info.fetchers import (
    FETCHERS,
    CrossrefFetcher,
    lookup_dataset_resource,
    lookup_ena,
    lookup_ncbi,
    query_gwas_catalog,
    search_biomed,
    search_cs,
)
from paper_info.identifiers import Identifier, infer_domain, parse_identifier
from paper_info.models import Accession, DatasetRecord, LookupResult, PaperMetadata, PaperRecord, SearchResult


def resolve(identifier_text: str, domain: str = "auto", timeout: float = 4.0) -> PaperMetadata:
    """Resolve a paper identifier into merged metadata."""

    identifier = parse_identifier(identifier_text)
    selected_domain = infer_domain(identifier, domain)
    fetchers = [fetcher for fetcher in FETCHERS.get(selected_domain, []) if fetcher.can_fetch(identifier)]
    if not fetchers and identifier.kind == "title":
        fetchers = [fetcher for fetcher in FETCHERS.get("biomed", []) if fetcher.can_fetch(identifier)]
    if not fetchers:
        raise ValueError(f"unsupported identifier/domain combination: {identifier.kind}/{selected_domain}")

    merged = PaperMetadata()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {
            executor.submit(fetcher.fetch, identifier, timeout): fetcher.name
            for fetcher in fetchers
        }
        done, pending = concurrent.futures.wait(futures, timeout=timeout + 0.5)
        for future in done:
            try:
                partial = future.result(timeout=0)
            except Exception:
                continue
            _merge_metadata(merged, partial)
        for future in pending:
            future.cancel()

    if not merged.sources:
        raise RuntimeError("no metadata sources returned a usable result")
    if identifier.kind == "title":
        _enrich_title_resolution_with_canonical_doi(merged, timeout)
    if not merged.data_availability:
        merged.data_availability = "Not found"
    merged.full_text_links = _dedupe_links(merged.full_text_links)
    return merged


def accessions(identifier_text: str, domain: str = "auto", timeout: float = 4.0) -> list[Accession]:
    """Resolve a paper and list related dataset accessions."""

    paper = resolve(identifier_text, domain=domain, timeout=timeout)
    found = extract_accessions(paper.data_availability)
    found.extend(query_gwas_catalog(paper, timeout=timeout))
    return _dedupe_accessions(found)


def get_paper(
    identifier_text: str,
    depth: str = "full",
    domain: str = "auto",
    timeout: float = 4.0,
) -> PaperRecord:
    """Build a one-stop paper dossier."""

    paper = resolve(identifier_text, domain=domain, timeout=timeout)
    extracted = _dedupe_accessions([
        *extract_accessions(paper.data_availability),
        *extract_dataset_resources(paper.data_availability),
    ])
    if depth == "full":
        extracted = _dedupe_accessions([*extracted, *query_gwas_catalog(paper, timeout=timeout)])
    datasets = _dataset_records(extracted, verify=depth == "full", timeout=timeout)
    return PaperRecord(
        identity=paper,
        abstract=paper.abstract,
        data_availability=paper.data_availability,
        supplement=_supplement_from_links(paper.full_text_links),
        code_repos=extract_code_repos(paper.data_availability),
        datasets=datasets,
        full_text_links=paper.full_text_links,
        sources=paper.sources,
    )


def lookup(accession: str, db: str = "auto", timeout: float = 4.0) -> LookupResult:
    """Lookup one accession in ENA or NCBI."""

    clean = accession.strip()
    kind = classify_accession(clean)
    candidates = _lookup_candidates(kind, db)
    for candidate in candidates:
        if candidate == "ena":
            result = lookup_ena(clean, timeout=timeout)
        else:
            result = lookup_ncbi(clean, candidate, timeout=timeout)
        if result:
            return result
    return LookupResult(accession=clean, type=kind, status="Not found")


def explain_accession(accession: str, db: str = "auto", timeout: float = 4.0) -> LookupResult:
    """Explain one dataset/accession identifier."""

    return lookup(accession, db=db, timeout=timeout)


def search(query: str, limit: int = 5, domain: str = "auto", timeout: float = 4.0) -> list[SearchResult]:
    """Search papers by title or keyword."""

    selected_domain = "biomed" if domain == "auto" else domain
    if selected_domain == "cs":
        return search_cs(query, limit=limit, timeout=timeout)
    if selected_domain != "biomed":
        raise ValueError(f"search domain is not implemented yet: {selected_domain}")
    return search_biomed(query, limit=limit, timeout=timeout)


def find_papers(query: str, limit: int = 5, domain: str = "auto", timeout: float = 4.0) -> list[SearchResult]:
    """Find candidate papers."""

    return search(query, limit=limit, domain=domain, timeout=timeout)


def _merge_metadata(target: PaperMetadata, source: PaperMetadata) -> None:
    # Field precedence is encoded by only filling empty values, except Europe PMC's
    # data availability is authoritative and PubMed/Europe PMC abstracts may fill gaps.
    for field_name in (
        "title",
        "journal",
        "year",
        "doi",
        "arxiv_id",
        "preprint_server",
        "preprint_version",
        "pmid",
        "pmcid",
        "issn",
    ):
        if _should_replace_field(target, source, field_name):
            setattr(target, field_name, getattr(source, field_name))

    if source.authors and (not target.authors or _is_higher_confidence(source, target)):
        target.authors = source.authors

    if source.abstract and (
        not target.abstract
        or "europepmc" in source.sources
        or _is_higher_confidence(source, target)
    ):
        target.abstract = source.abstract

    if "europepmc" in source.sources and source.data_availability:
        target.data_availability = source.data_availability

    # Europe PMC open-access status is authoritative
    if "europepmc" in source.sources and source.raw.get("pmc_oa"):
        target.raw["pmc_oa"] = source.raw["pmc_oa"]

    target.full_text_links.extend(source.full_text_links)
    for source_name in source.sources:
        target.add_source(source_name)
    if source.confidence > target.confidence:
        target.confidence = source.confidence
        target.match_type = source.match_type


def _enrich_title_resolution_with_canonical_doi(meta: PaperMetadata, timeout: float) -> None:
    if not meta.doi:
        return
    try:
        canonical = CrossrefFetcher().fetch(Identifier("doi", meta.doi, meta.doi), timeout)
    except Exception:
        return
    _merge_metadata(meta, canonical)


def _should_replace_field(target: PaperMetadata, source: PaperMetadata, field_name: str) -> bool:
    source_value = getattr(source, field_name)
    if not source_value:
        return False
    target_value = getattr(target, field_name)
    if not target_value:
        return True
    if field_name in {"doi", "pmid", "pmcid", "arxiv_id"}:
        return _is_higher_confidence(source, target)
    if field_name in {"title", "journal", "year", "preprint_server", "preprint_version"}:
        return _is_higher_confidence(source, target)
    return False


def _is_higher_confidence(source: PaperMetadata, target: PaperMetadata) -> bool:
    return source.confidence > target.confidence


def _dedupe_links(links: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    unique = []
    for link in links:
        if not link:
            continue
        key, value = next(iter(link.items()))
        marker = (key, value)
        if marker not in seen:
            unique.append({key: value})
            seen.add(marker)
    return unique


def _dedupe_accessions(items: Iterable[Accession]) -> list[Accession]:
    seen = set()
    unique = []
    for item in items:
        marker = item.accession.upper()
        if marker not in seen:
            unique.append(item)
            seen.add(marker)
    return unique


def _dataset_records(items: Iterable[Accession], verify: bool, timeout: float) -> list[DatasetRecord]:
    records = []
    for item in items:
        record = DatasetRecord(
            accession=item.accession,
            type=item.type,
            description=item.description,
            url=_resource_url(item.accession),
            download=_resource_url(item.accession),
        )
        if verify:
            if record.url is None:
                detail = explain_accession(item.accession, timeout=timeout)
                record.title = detail.title
                record.organism = detail.organism
                record.samples = detail.samples
                record.status = detail.status
                record.submitted = detail.submitted
                record.source = detail.source
            elif record.type in {"Zenodo", "Figshare", "Dryad", "OSF", "Dataset DOI"}:
                detail = lookup_dataset_resource(item.accession, timeout=timeout)
                if detail:
                    record.title = detail.get("title")
                    record.description = detail.get("description") or record.description
                    record.creators = detail.get("creators")
                    record.published = detail.get("published")
                    record.version = detail.get("version")
                    record.files = detail.get("files")
                    if detail.get("status"):
                        record.status = detail["status"]
        records.append(record)
    return records


def _resource_url(accession: str) -> str | None:
    if accession.startswith(("http://", "https://")):
        return accession
    if accession.lower().startswith("10."):
        return f"https://doi.org/{accession}"
    if accession.upper().startswith("CNP"):
        return f"https://db.cngb.org/search/project/{accession.upper()}"
    return None


def _supplement_from_links(links: list[dict[str, str]]) -> dict[str, object]:
    pdf = None
    files = []
    seen_files = set()
    for link in links:
        if not link:
            continue
        link_type, url = next(iter(link.items()))
        normalized_type = _supplement_link_type(link_type, url)
        if pdf is None and normalized_type == "pdf":
            pdf = url
        if normalized_type in {"supplement", "jatsxml", "xml", "source"}:
            marker = (normalized_type, url)
            if marker not in seen_files:
                files.append({"type": normalized_type, "url": url})
                seen_files.add(marker)
    return {"pdf": pdf or "Not found", "files": files}


def _supplement_link_type(link_type: str, url: str) -> str:
    text = f"{link_type} {url}".lower()
    if "jatsxml" in text or "source.xml" in text:
        return "jatsxml"
    if "xml" in text:
        return "xml"
    if any(marker in text for marker in ("supplement", "supplementary", "suppl", "suppinfo")):
        return "supplement"
    if link_type in {"publisher", "preprint", "pmc"} or url.lower().endswith(".pdf"):
        return "pdf"
    return link_type


def _lookup_candidates(kind: str, requested_db: str) -> list[str]:
    if requested_db != "auto":
        return [_normalize_ncbi_db(requested_db)]
    if kind in {"ENA", "SRA"}:
        return ["ena", "sra"]
    if kind == "GEO":
        return ["gds"]
    if kind == "BioProject":
        return ["bioproject"]
    if kind == "Assembly":
        return ["assembly"]
    return ["ena", "sra", "gds", "bioproject"]


def _normalize_ncbi_db(db: str) -> str:
    mapping = {
        "geo": "gds",
        "sra": "sra",
        "ena": "ena",
        "bioproject": "bioproject",
        "assembly": "assembly",
    }
    return mapping.get(db, db)
