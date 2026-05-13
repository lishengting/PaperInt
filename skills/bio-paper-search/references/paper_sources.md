# Paper Source Reference

## bioRxiv API

**Base URL**: `https://api.biorxiv.org`

### Content Detail Endpoint

```
GET https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}/{cursor}
```

- `start_date`, `end_date`: YYYY-MM-DD format
- `cursor`: 0-based pagination index (number of papers to skip)

Response is JSON with a `collection` array. Each paper object has:
- `doi`, `title`, `authors`, `abstract`, `date`, `category`, `version`

### PDF Download

Three fallback patterns tried in order:
1. `https://www.biorxiv.org/content/{doi}.full.pdf`
2. `https://www.biorxiv.org/content/{short_doi}.full.pdf` (DOI without `10.1101/` prefix)
3. `https://www.biorxiv.org/content/{doi}` (article page, may redirect)

Validate response by checking that content starts with `%PDF`.

### Rate Limits

- Recommended 3-second delay between API calls.
- Aggressive crawling may trigger IP blocks.

## arXiv API

**Base URL**: `https://export.arxiv.org/api/query`

### Search Query

```
GET https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results={n}
```

Query format for category search: `cat:q-bio.BM`

### Configured Categories

From `config.yaml` `apis.arxiv.categories`:
- `q-bio.BM` — Biomolecules
- `q-bio.GN` — Genomics
- `q-bio.MN` — Molecular Networks
- `q-bio.CB` — Cell Behavior
- `cs.CE` — Computational Engineering (includes computational biology)

### Response Format

Atom XML with namespaces:
- `http://www.w3.org/2005/Atom` (default)
- `http://arxiv.org/schemas/atom` (arXiv extensions)

Each entry contains: `id`, `title`, `summary` (abstract), `published`, `author`/`name`, `arxiv:primary_category`.

### PDF Download

URL pattern: `https://arxiv.org/pdf/{arxiv_id}.pdf`

Validate by checking file size >= 10000 bytes (arXiv returns HTML error pages as 200 OK with small body).

### Rate Limits

- arXiv asks for no more than one request every 3 seconds.
- Bulk fetching should use a single query with higher `max_results` rather than many small queries.

## Normalized Paper Schema

All downstream tools use this common JSON structure:

```json
{
  "paper_id": "unique identifier (DOI or arXiv ID)",
  "source": "biorxiv | arxiv",
  "doi": "DOI string or null",
  "arxiv_id": "arXiv ID or null",
  "title": "Paper title",
  "authors": "Author string",
  "abstract": "Abstract text",
  "date": "Publication date (YYYY-MM-DD)",
  "category": "Primary category/subject area",
  "pdf_url": "Direct PDF download URL",
  "abs_url": "Abstract/landing page URL"
}
```
