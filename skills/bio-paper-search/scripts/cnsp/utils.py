"""Article normalization and keyword filtering for CNSP parsers."""

from __future__ import annotations


def normalize_article(article: dict, journal_type: str, journal_name: str) -> dict:
    """Convert a parser article dict into the standard make_paper() format."""
    doi = (article.get('doi', '') or '').strip()
    title = (article.get('title', '') or '').strip()
    authors = (article.get('authors', '') or '').strip()
    abstract = (article.get('abstract', '') or '').strip()
    url = (article.get('url', '') or '').strip()
    date_val = article.get('date', '') or article.get('pub_date', '') or ''

    # Derive PDF and abstract URLs per journal type
    if journal_type == 'nature':
        pdf_url = f"https://www.nature.com/articles/{doi}.pdf" if doi else ''
    elif journal_type == 'science':
        pdf_url = f"https://www.science.org/doi/pdf/{doi}" if doi else ''
    elif journal_type == 'cell':
        pdf_url = f"https://www.cell.com/article/{doi}/pdf" if doi else ''
    elif journal_type == 'plos':
        pdf_url = f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable" if doi else ''
    else:
        pdf_url = ''

    return {
        'paper_id': doi or '',
        'source': journal_type,
        'doi': doi or None,
        'arxiv_id': None,
        'pmid': None,
        'title': title,
        'authors': authors,
        'abstract': abstract,
        'date': str(date_val),
        'category': f"{journal_type}/{journal_name}",
        'pdf_url': pdf_url,
        'abs_url': url,
        'journal': journal_name,
    }


def filter_by_keywords(papers: list[dict], keywords: list[str]) -> list[dict]:
    """Filter papers by keyword match in title + abstract (case-insensitive)."""
    if not keywords:
        return papers
    kw_lower = [k.lower() for k in keywords]
    matched = []
    for p in papers:
        text = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
        if any(kw in text for kw in kw_lower):
            matched.append(p)
    return matched


def score_and_rank(papers: list[dict], keywords: list[str]) -> list[dict]:
    """Score papers by keyword density and sort descending."""
    if not keywords:
        return papers
    kw_lower = [k.lower() for k in keywords]
    for p in papers:
        title = (p.get('title', '') or '').lower()
        abstract = (p.get('abstract', '') or '').lower()
        score = 0
        for kw in kw_lower:
            if kw in title:
                score += 3
            if kw in abstract:
                score += 1
        p['_score'] = score
    papers.sort(key=lambda x: x.get('_score', 0), reverse=True)
    for p in papers:
        p.pop('_score', None)
    return papers