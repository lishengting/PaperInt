"""Generate .info.md markdown from a PaperRecord."""

from __future__ import annotations

from paper_info.models import PaperRecord


def generate(record: PaperRecord) -> str:
    """Render a PaperRecord as a comprehensive info markdown string."""
    identity = record.identity
    lines: list[str] = []

    # Title
    title = identity.title or "Untitled"
    lines.append(f"# {title}\n")

    # Paper Identity
    lines.append("## Paper Identity\n")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    doi = identity.doi
    if doi:
        doi_md = f"[{doi}](https://doi.org/{doi})"
    else:
        doi_md = _na
    for label, value in (
        ("Title", title),
        ("Authors", _authors(identity.authors)),
        ("Journal", identity.journal),
        ("Year", identity.year),
        ("DOI", doi_md),
        ("PMID", identity.pmid),
        ("PMCID", identity.pmcid),
        ("arXiv ID", identity.arxiv_id),
        ("Preprint Server", identity.preprint_server),
        ("Preprint Version", identity.preprint_version),
    ):
        lines.append(f"| **{label}** | {_v(value)} |")
    lines.append("")

    # Abstract
    lines.append("## Abstract\n")
    lines.append(_v(record.abstract or identity.abstract))
    lines.append("")

    # Data Availability
    lines.append("## Data Availability Statement\n")
    lines.append(_v(record.data_availability))
    lines.append("")

    # Open Access Status (from Europe PMC)
    oa = identity.raw.get("pmc_oa")
    if oa:
        lines.append("## Open Access Status\n")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| **Open Access** | {'Yes' if oa.get('is_open_access') else 'No'} |")
        lines.append(f"| **PDF Available** | {'Yes' if oa.get('has_pdf') else 'No'} |")
        oa_status = oa.get("oa_status") or ""
        if oa_status:
            lines.append(f"| **OA Status** | {oa_status} |")
        license_text = oa.get("license") or ""
        if license_text:
            lines.append(f"| **License** | {license_text} |")
        oa_url = oa.get("oa_url") or ""
        if oa_url:
            lines.append(f"| **OA URL** | {oa_url} |")
        lines.append("")

    # Dataset Accessions
    lines.append("## Dataset Accessions\n")
    if record.datasets:
        lines.append("| Accession | Type | Description |")
        lines.append("|-----------|------|-------------|")
        for ds in record.datasets:
            desc = (ds.description or "").strip()
            if len(desc) > 120:
                desc = desc[:117].rstrip() + "..."
            lines.append(f"| {ds.accession} | {ds.type} | {desc or _na} |")
    else:
        lines.append("*None found*")
    lines.append("")

    # Code Repositories
    lines.append("## Code Repositories\n")
    if record.code_repos:
        for repo in record.code_repos:
            url = repo.get("url", "")
            name = _repo_display_name(url)
            lines.append(f"- [{name}]({url})")
    else:
        lines.append("*None found*")
    lines.append("")

    # Supplementary Materials
    lines.append("## Supplementary Materials\n")
    supp = record.supplement
    if supp:
        pdf = supp.get("pdf")
        if pdf and pdf != "Not found":
            lines.append(f"- **PDF**: {pdf}")
        files = supp.get("files", [])
        if files:
            lines.append("- **Files**:")
            for f in files:
                ftype = f.get("type", "unknown")
                furl = f.get("url", "")
                lines.append(f"  - [{ftype}]({furl})")
        if (not pdf or pdf == "Not found") and not files:
            lines.append("*None found*")
    else:
        lines.append("*None found*")
    lines.append("")

    # Full Text Links
    lines.append("## Full Text Links\n")
    if record.full_text_links:
        lines.append("| Type | URL |")
        lines.append("|------|-----|")
        for link in record.full_text_links:
            for ltype, lurl in link.items():
                lines.append(f"| {ltype} | {lurl} |")
    else:
        lines.append("*None found*")
    lines.append("")

    # Resolution Sources
    lines.append("## Resolution Sources\n")
    sources = record.sources or identity.sources or []
    if sources:
        lines.append(f"Cross-reference metadata aggregated from: {', '.join(sources)}.")
    else:
        lines.append("*No sources recorded*")

    return "\n".join(lines)


_na = "*Not available*"


def _v(value: str | None) -> str:
    if not value or value == "Not found":
        return _na
    return str(value)


def _authors(authors: list[str]) -> str:
    if not authors:
        return _na
    return ", ".join(authors)


def _repo_display_name(url: str) -> str:
    if url.startswith("https://github.com/"):
        return url.removeprefix("https://github.com/")
    return url