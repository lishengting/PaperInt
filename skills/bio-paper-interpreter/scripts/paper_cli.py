#!/usr/bin/env python3
"""
Bio Paper Interpreter CLI — batch interpret downloaded papers via LLM.

Usage:
  paper_cli.py                          # auto-mode: process all 'downloaded' papers
  paper_cli.py --retry-failed           # retry papers with interpret_failed status
  paper_cli.py run <paper_id>           # interpret a single paper (skips if already done)
  paper_cli.py run <paper_id> --force   # force re-interpret even if already interpreted
  paper_cli.py run <paper_id> --phase 1 # run only specified phases (1, 2, 3, 4, or comma-separated)

Four-phase pipeline:
  Phase 1: Tag matching
  Phase 2: PDF extraction + LLM interpretation (requires LLM_API_KEY)
  Phase 3: Markdown → styled HTML conversion
  Phase 4: Poster generation (3 English posters by default, bilingual with --cn/--trans)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

EXAMPLES = """
Examples:
  paper_cli.py                          # Auto-mode: interpret all downloaded papers
  paper_cli.py --retry-failed           # Retry papers with interpret_failed status
  paper_cli.py run s41467-026-70776-7   # Interpret a single paper (all phases)
  paper_cli.py run s41467-026-70776-7 --force    # Force re-interpret
  paper_cli.py run s41467-026-70776-7 --phase 1,2  # Only run phases 1 and 2
  paper_cli.py pdf ./paper.pdf --phase 1,2          # Import a local PDF and interpret it
  paper_cli.py --cn                     # Generate English + Chinese outputs
  paper_cli.py --trans                  # Generate Chinese outputs from English + PDF context
  paper_cli.py --dry-run                # List papers without processing
"""

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))
sys.path.insert(0, SKILL_DIR)

from paper_db import (get_conn, get_papers_by_status, get_paper_dir, get_paper,
                      mark_downloaded, mark_interpreted, mark_interpret_failed,
                      update_tags, update_translations, load_cnsp_journal_set,
                      filter_cnsp_papers, load_cns_journal_set,
                      upsert_single_paper)

from match_tags import match_tags
from build_prompt import (build_full_text_prompt, build_brief_prompt,
                          build_abstract_only_prompt, build_template_prompt,
                          build_paper_context, load_config)


def cfg(config, path, default=None):
    parts = path.split('.')
    cur = config
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', '_', str(name))[:200]


DOWNLOADER_SCRIPTS = os.path.join(REPO_ROOT, 'skills', 'bio-paper-downloader', 'scripts')
DOI_PATTERN = re.compile(r'\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b', re.IGNORECASE)
LOCAL_UNRESOLVED_ISSN = '0000-0000'


def title_to_dirname(title):
    if not title:
        return 'unknown'
    safe = re.sub(r'[()]+', '-', str(title))
    safe = re.sub(r'[/\\:*?"<>|\s;]+', '_', safe).strip('_')
    return safe[:256] or 'unknown'


def _dedupe_strings(values):
    result = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _normalize_doi_value(value):
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', text, flags=re.IGNORECASE)
    match = DOI_PATTERN.search(text)
    doi = match.group(1) if match else text
    doi = doi.strip().rstrip('.,;)').lower()
    return doi if DOI_PATTERN.fullmatch(doi) else ''


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_embedded_title(title):
    text = ' '.join(str(title or '').split())
    if not text:
        return ''
    lower = text.lower()
    generic = {'untitled', 'unknown', 'none', 'pdf', 'document'}
    if lower in generic or lower.endswith('.doc') or lower.endswith('.docx'):
        return ''
    if lower.startswith(('microsoft word -', 'untitled document')):
        return ''
    return text[:300]


def _title_candidates_from_text(text):
    lines = []
    for raw in (text or '').splitlines()[:40]:
        line = ' '.join(raw.split())
        if not 15 <= len(line) <= 240:
            continue
        lower = line.lower()
        if lower in {'abstract', 'keywords', 'introduction'}:
            continue
        if lower.startswith(('doi:', 'http', 'www.', 'arxiv:', 'pmid:', 'issn')):
            continue
        if re.search(r'\b(journal|volume|issue|copyright|license|published)\b', lower) and len(line) < 80:
            continue
        lines.append(line)
        if len(lines) >= 5:
            break
    candidates = []
    if lines:
        candidates.append(lines[0])
    if len(lines) >= 2 and len(lines[0]) < 120:
        joined = f'{lines[0]} {lines[1]}'
        if len(joined) <= 240:
            candidates.append(joined)
    candidates.extend(lines[1:3])
    return _dedupe_strings(candidates)


def extract_pdf_hints(pdf_path):
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError('PyMuPDF is required to inspect local PDFs') from e

    doc = fitz.open(pdf_path)
    try:
        if doc.page_count < 1:
            raise RuntimeError('PDF has no pages')
        metadata = dict(doc.metadata or {})
        text_parts = []
        for i in range(min(2, doc.page_count)):
            text_parts.append(doc.load_page(i).get_text('text')[:5000])
    finally:
        doc.close()

    first_pages_text = '\n'.join(text_parts)
    metadata_text = ' '.join(str(v or '') for v in metadata.values())
    doi_candidates = _dedupe_strings(
        _normalize_doi_value(m.group(1))
        for m in DOI_PATTERN.finditer(f'{metadata_text}\n{first_pages_text}')
    )
    title_candidates = _dedupe_strings([
        _clean_embedded_title(metadata.get('title')),
        *_title_candidates_from_text(first_pages_text),
    ])
    return {
        'embedded_metadata': metadata,
        'embedded_title': _clean_embedded_title(metadata.get('title')),
        'embedded_authors': ' '.join(str(metadata.get('author') or '').split()),
        'first_pages_text': first_pages_text[:10000],
        'doi_candidates': doi_candidates,
        'title_candidates': title_candidates,
    }


def _load_paper_info_modules():
    if DOWNLOADER_SCRIPTS not in sys.path:
        sys.path.insert(0, DOWNLOADER_SCRIPTS)
    from paper_info import info_md, resolver
    return resolver, info_md


def _normalize_title_for_match(title):
    text = re.sub(r'[^a-z0-9]+', ' ', str(title or '').lower())
    return ' '.join(text.split())


def _title_similarity(a, b):
    a_tokens = set(_normalize_title_for_match(a).split())
    b_tokens = set(_normalize_title_for_match(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _raw_metadata_value(raw, keys):
    sources = []
    if isinstance(raw, dict):
        sources.append(raw)
        for nested_key in ('message', 'record', 'result'):
            nested = raw.get(nested_key)
            if isinstance(nested, dict):
                sources.append(nested)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, list):
                value = '; '.join(str(item) for item in value if item)
            if value:
                return str(value)
    return ''


def _source_url_for_paper(doi, arxiv_id):
    if doi:
        return f'https://doi.org/{doi}'
    if arxiv_id:
        return f'https://arxiv.org/abs/{arxiv_id}'
    return ''


def _paper_from_record(record, paper_id_override=None):
    identity = record.identity
    doi = _normalize_doi_value(identity.doi or '')
    arxiv_id = identity.arxiv_id or ''
    pmid = identity.pmid or ''
    pmcid = identity.pmcid or ''
    source_url = _source_url_for_paper(doi, arxiv_id)
    paper_id = paper_id_override or doi or arxiv_id or pmid or pmcid or ''
    authors = ', '.join(identity.authors or [])
    paper = {
        'paper_id': paper_id,
        'source': (identity.sources or ['paper_info'])[0],
        'doi': doi,
        'arxiv_id': arxiv_id,
        'pmid': pmid,
        'title': identity.title or paper_id or 'untitled',
        'authors': authors,
        'abstract': identity.abstract or record.abstract or '',
        'date': identity.year or '',
        'category': identity.journal or identity.preprint_server or '',
        'pdf_url': '',
        'abs_url': source_url,
        'source_url': source_url,
        'journal': identity.journal or '',
        'pmcid': pmcid,
        'paper_info_sources': identity.sources,
        'paper_info_match_type': identity.match_type or '',
        'paper_info_confidence': identity.confidence,
    }
    issn = _raw_metadata_value(identity.raw, ('issn', 'ISSN', 'issns'))
    if issn:
        paper['issn'] = issn
    if identity.raw:
        paper['paper_info_raw'] = identity.raw
    return paper


def _candidate_identifier(candidate):
    for attr, prefix in (('doi', ''), ('pmid', 'pmid:'), ('pmcid', ''), ('arxiv_id', 'arxiv:')):
        value = getattr(candidate, attr, None)
        if value:
            return f'{prefix}{value}'
    return getattr(candidate, 'title', '') or ''


def _fallback_pdf_paper(args, hints, pdf_sha256, resolver_error=''):
    local_pdf_id = f'local_pdf_{pdf_sha256[:16]}'
    title_candidates = _dedupe_strings([getattr(args, 'title', ''), *hints.get('title_candidates', [])])
    doi_candidates = _dedupe_strings([_normalize_doi_value(getattr(args, 'doi', '')), *hints.get('doi_candidates', [])])
    title = title_candidates[0] if title_candidates else local_pdf_id
    doi = doi_candidates[0] if doi_candidates else ''
    virtual_doi = f'local-pdf:{pdf_sha256[:16]}'
    return {
        'paper_id': getattr(args, 'paper_id', None) or local_pdf_id,
        'source': 'local_pdf',
        'doi': doi,
        'arxiv_id': '',
        'pmid': '',
        'title': title,
        'authors': hints.get('embedded_authors', ''),
        'abstract': '',
        'date': '',
        'category': '',
        'pdf_url': '',
        'abs_url': '',
        'source_url': '',
        'issn': LOCAL_UNRESOLVED_ISSN,
        'issn_is_virtual': True,
        'local_unresolved': True,
        'local_pdf_id': local_pdf_id,
        'pdf_sha256': pdf_sha256,
        'virtual_doi': virtual_doi,
        'doi_is_virtual': True,
        'resolver_error': resolver_error,
    }


def resolve_pdf_metadata(args, hints, pdf_sha256, existing_local=None):
    doi_candidates = _dedupe_strings([_normalize_doi_value(getattr(args, 'doi', '')), *hints.get('doi_candidates', [])])
    title_candidates = _dedupe_strings([getattr(args, 'title', ''), *hints.get('title_candidates', [])])
    paper_id_override = getattr(args, 'paper_id', None) or (existing_local or {}).get('paper_id')
    last_error = ''

    try:
        resolver, _info_md = _load_paper_info_modules()
    except Exception as e:
        paper = _fallback_pdf_paper(args, hints, pdf_sha256, str(e))
        return paper, None, {'status': 'fallback', 'error': str(e), 'identifier': '', 'doi_candidates': doi_candidates, 'title_candidates': title_candidates}

    for doi in doi_candidates:
        try:
            record = resolver.get_paper(doi, depth='full', timeout=8.0)
            paper = _paper_from_record(record, paper_id_override=paper_id_override)
            return paper, record, {'status': 'resolved', 'identifier': doi, 'method': 'doi', 'doi_candidates': doi_candidates, 'title_candidates': title_candidates}
        except Exception as e:
            last_error = str(e)

    for title in title_candidates:
        try:
            candidates = resolver.find_papers(title, limit=5, domain='auto', timeout=8.0)
            best = None
            best_score = 0.0
            for candidate in candidates:
                score = _title_similarity(title, getattr(candidate, 'title', ''))
                if score > best_score:
                    best = candidate
                    best_score = score
            if best and best_score >= 0.75:
                identifier = _candidate_identifier(best)
                record = resolver.get_paper(identifier, depth='full', timeout=8.0)
                paper = _paper_from_record(record, paper_id_override=paper_id_override)
                return paper, record, {
                    'status': 'resolved',
                    'identifier': identifier,
                    'method': 'title',
                    'title_score': best_score,
                    'doi_candidates': doi_candidates,
                    'title_candidates': title_candidates,
                }
        except Exception as e:
            last_error = str(e)

    paper = _fallback_pdf_paper(args, hints, pdf_sha256, last_error)
    return paper, None, {'status': 'fallback', 'error': last_error, 'identifier': '', 'doi_candidates': doi_candidates, 'title_candidates': title_candidates}


def _build_pdf_import_metadata(paper, hints, pdf_path, pdf_sha256, resolver_info):
    local_pdf_id = f'local_pdf_{pdf_sha256[:16]}'
    metadata = dict(paper)
    metadata['_pdf_import'] = {
        'original_path': os.path.abspath(pdf_path),
        'imported_at': datetime.now().isoformat(),
        'resolver_status': resolver_info.get('status', ''),
        'resolver_identifier': resolver_info.get('identifier', ''),
        'resolver_method': resolver_info.get('method', ''),
        'resolver_error': resolver_info.get('error', ''),
        'doi_candidates': resolver_info.get('doi_candidates') or hints.get('doi_candidates', []),
        'title_candidates': resolver_info.get('title_candidates') or hints.get('title_candidates', []),
        'embedded_pdf_metadata': hints.get('embedded_metadata', {}),
        'pdf_sha256': pdf_sha256,
        'local_pdf_id': metadata.get('local_pdf_id') or local_pdf_id,
        'virtual_issn': metadata.get('issn') if metadata.get('issn_is_virtual') else '',
        'virtual_doi': metadata.get('virtual_doi', ''),
    }
    return metadata


def _build_pdf_import_dir(paper, safe_pid):
    data_dir = os.path.join(REPO_ROOT, 'data')
    dirname = title_to_dirname(paper.get('title', ''))
    paper_dir = os.path.join(data_dir, dirname)
    if os.path.isdir(paper_dir):
        meta_file = os.path.join(paper_dir, f'{safe_pid}.metadata.json')
        if not os.path.exists(meta_file):
            dirname = f'{dirname}_{safe_pid}'[:256]
            paper_dir = os.path.join(data_dir, dirname)
    return dirname, paper_dir


def _same_file(src, dst):
    try:
        return os.path.exists(dst) and os.path.samefile(src, dst)
    except OSError:
        return False


def _fallback_info_md(paper, metadata):
    pdf_import = metadata.get('_pdf_import', {})
    lines = [
        f"# {paper.get('title') or paper.get('paper_id') or 'Local PDF'}",
        '',
        '## Paper Identity',
        '',
        '| Field | Value |',
        '|-------|-------|',
        f"| **Paper ID** | {paper.get('paper_id', '') or '*Not available*'} |",
        f"| **Title** | {paper.get('title', '') or '*Not available*'} |",
        f"| **Authors** | {paper.get('authors', '') or '*Not available*'} |",
        f"| **DOI** | {paper.get('doi', '') or '*Not available*'} |",
        f"| **ISSN** | {paper.get('issn', '') or '*Not available*'} |",
        f"| **Local PDF ID** | {paper.get('local_pdf_id', '') or pdf_import.get('local_pdf_id', '') or '*Not available*'} |",
        '',
        '## Local Import',
        '',
        f"- Source: {paper.get('source', 'local_pdf')}",
        f"- Original path: {pdf_import.get('original_path', '')}",
        f"- PDF SHA-256: {pdf_import.get('pdf_sha256', '')}",
        f"- Resolver status: {pdf_import.get('resolver_status', '') or 'fallback'}",
    ]
    if pdf_import.get('resolver_error'):
        lines.append(f"- Resolver error: {pdf_import['resolver_error']}")
    if paper.get('local_unresolved'):
        lines.append(f"- Local unresolved sentinel ISSN: {LOCAL_UNRESOLVED_ISSN}")
    return '\n'.join(lines) + '\n'


def _write_pdf_import_files(pdf_path, paper_dir, safe_pid, metadata, record):
    os.makedirs(paper_dir, exist_ok=True)
    dest_pdf = os.path.join(paper_dir, f'{safe_pid}.pdf')
    if not _same_file(pdf_path, dest_pdf):
        shutil.copy2(pdf_path, dest_pdf)

    metadata_path = os.path.join(paper_dir, f'{safe_pid}.metadata.json')
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2, default=str)

    info_path = os.path.join(paper_dir, f'{safe_pid}.info.md')
    try:
        _resolver, info_md = _load_paper_info_modules()
        content = info_md.generate(record) if record else _fallback_info_md(metadata, metadata)
    except Exception:
        content = _fallback_info_md(metadata, metadata)
    _write_text(info_path, content)
    return dest_pdf


def _print_existing_results(paper, config):
    paper_id = paper.get('paper_id', '')
    safe_pid = sanitize(paper_id)
    paper_dir = paper.get('dir_name', '') or get_paper_dir(get_conn(config), paper_id) or ''
    ts_print(f"  [exists] already interpreted: {paper_id}")
    if paper_dir:
        paper_path = os.path.join(REPO_ROOT, 'data', paper_dir)
        ts_print(f"  Dir: {paper_dir}")
        for suffix in ('interpret.md', 'brief.md', 'interpret.html', 'brief.html', 'interpret.json'):
            path = os.path.join(paper_path, f'{safe_pid}.{suffix}')
            if os.path.exists(path):
                ts_print(f"  {path}")
    return 0


def log_phase(log_file, paper_id, phase, status, msg=''):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} | Phase {phase} - {status}: {paper_id}"
    if msg:
        line += f" — {msg}"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, 'a') as f:
        f.write(line + '\n')


def ts_print(*args, file=None, end='\n', flush=False):
    """Print with [HH:MM:SS] timestamp prefix."""
    ts = datetime.now().strftime('[%H:%M:%S]')
    print(ts, *args, file=file, end=end, flush=flush)


def _log_llm_result(label, usage, elapsed):
    ts_print(
        f"  LLM done [{label}]: duration={elapsed:.1f}s, "
        f"prompt_tokens={usage.get('prompt_tokens', 0)}, "
        f"completion_tokens={usage.get('completion_tokens', 0)}, "
        f"total_tokens={usage.get('total_tokens', 0)}"
    )


def _call_llm(config, system_prompt, user_prompt, label='general'):
    """Call the configured LLM and return (response_text, usage_dict)."""
    api_base = cfg(config, 'llm.api_base_url', 'http://localhost:8080/v1')
    model = cfg(config, 'llm.model', 'qwen3-235b-a22b')
    temperature = cfg(config, 'llm.temperature', 0.3)
    max_tokens = cfg(config, 'llm.max_tokens', 4000)
    timeout = cfg(config, 'llm.timeout_seconds', 120)
    api_key_cfg = cfg(config, 'llm.api_key_env', 'LLM_API_KEY')
    api_key = os.environ.get(api_key_cfg, '')
    if not api_key:
        if api_key_cfg and ' ' not in api_key_cfg and len(api_key_cfg) > 20:
            api_key = api_key_cfg
        else:
            ts_print(f"  Warning: LLM API key not found (checked env var ${api_key_cfg})", file=sys.stderr)

    body = json.dumps({
        'model': model,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }).encode('utf-8')

    url = f"{api_base.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    })

    ts_print(f"  Calling LLM [{label}]: {model} ({url})...")
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=timeout)
    resp_data = json.loads(resp.read().decode('utf-8'))
    elapsed = time.time() - t0
    usage = resp_data.get('usage', {})
    usage_data = {
        'prompt_tokens': usage.get('prompt_tokens', 0),
        'completion_tokens': usage.get('completion_tokens', 0),
        'total_tokens': usage.get('total_tokens', 0),
        'elapsed_seconds': elapsed,
    }
    _log_llm_result(label, usage_data, elapsed)
    return resp_data['choices'][0]['message']['content'], usage_data


def _strip_json_fence(content: str) -> str:
    content = (content or '').strip()
    content = re.sub(r'^```(?:json)?\s*', '', content)
    content = re.sub(r'\s*```$', '', content)
    return content.strip()


def _load_json_object(content: str) -> dict:
    content = _strip_json_fence(content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start = content.find('{')
        end = content.rfind('}')
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(content[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError('LLM response is not a JSON object')
    return data


def _write_text(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content or '')


def _build_translation_prompt(prompt_name, paper_context, source_markdown=None, source_description=None):
    fields = {
        'paper_context': paper_context,
        'source_markdown': source_markdown or '',
        'source_description': source_description or '',
    }
    return build_template_prompt(prompt_name, fields)


def _translate_paper_metadata(config, paper_data):
    title = (paper_data.get('title') or '').strip()
    abstract = (paper_data.get('abstract') or paper_data.get('Abstract') or '').strip()
    existing_title_zh = (paper_data.get('title_zh') or '').strip()
    existing_abstract_zh = (paper_data.get('abstract_zh') or '').strip()
    if existing_title_zh and (existing_abstract_zh or not abstract):
        return {
            'title_zh': existing_title_zh,
            'abstract_zh': existing_abstract_zh,
        }, None
    if not title and not abstract:
        return {}, None

    system_prompt = (
        'You are a precise academic translator. Translate research paper metadata '
        'into Simplified Chinese. Return valid JSON only. Do not include Markdown, '
        'comments, or extra text.'
    )
    user_prompt = (
        'Translate the following paper title and abstract into Simplified Chinese.\n\n'
        'Rules:\n'
        '- Preserve scientific meaning exactly.\n'
        '- Do not invent information.\n'
        '- Keep gene/protein/drug names, abbreviations, database names, and statistical notation accurate.\n'
        '- If a field is empty, return an empty string for that field.\n'
        '- Return exactly this JSON object shape: '
        '{"title_zh": "...", "abstract_zh": "..."}\n\n'
        f'Title:\n{title}\n\n'
        f'Abstract:\n{abstract}\n'
    )
    content, usage = _call_llm(config, system_prompt, user_prompt, label='metadata_translate_zh')
    data = _load_json_object(content)
    translations = {
        'title_zh': data.get('title_zh') if isinstance(data.get('title_zh'), str) else '',
        'abstract_zh': data.get('abstract_zh') if isinstance(data.get('abstract_zh'), str) else '',
    }
    if existing_title_zh and not translations['title_zh']:
        translations['title_zh'] = existing_title_zh
    if existing_abstract_zh and not translations['abstract_zh']:
        translations['abstract_zh'] = existing_abstract_zh
    translations = {key: value.strip() for key, value in translations.items() if value.strip()}
    return translations, usage


def _log_phase2_tokens(usages):
    """Print token usage summary for Phase 2 LLM calls."""
    parts = []
    total = 0
    for label, usage in usages:
        if usage:
            t = usage.get('total_tokens', 0)
            total += t
            elapsed = usage.get('elapsed_seconds')
            if elapsed is not None:
                parts.append(f"{label}={t} tokens/{elapsed:.1f}s")
            else:
                parts.append(f"{label}={t}")
    if total > 0:
        ts_print(f"  Phase 2 tokens: {', '.join(parts)}, total={total}")


def _validate_pdf_content_llm(config, pdf_text, paper):
    """Use LLM to check whether extracted PDF content matches the paper's title and abstract.

    Returns (passed: bool, confidence: float, reason: str, usage: dict, doc_type: str | None).
    """
    title = (paper.get('title') or '').strip()
    abstract = (paper.get('abstract') or '').strip()
    if not title and not abstract:
        return True, 1.0, 'no title or abstract to validate', {}, None
    if not pdf_text or len(pdf_text.strip()) < 200:
        return True, 1.0, 'PDF text too short to validate', {}, None

    header_chars = cfg(config, 'interpreter.pdf_content_validation.header_chars', 4000)
    pdf_sample = pdf_text[:header_chars]

    system_prompt = (
        "You are a rigorous research paper validator. Your task is to determine whether "
        "a given PDF content is a genuine research article that matches the expected paper. "
        "You must reject corrections, errata, supplementary materials, editorials, "
        "commentaries, advertisements, and any document that is not an original research paper."
    )

    formatted_title = title if title else '(not available)'
    formatted_abstract = abstract[:1500] if abstract else '(not available)'

    user_prompt = (
        "I will give you:\n"
        "1. The expected paper title and abstract\n"
        "2. The first portion of text extracted from a downloaded PDF\n\n"
        "First, classify the PDF document type as one of:\n"
        "- research_article: a full original research paper with methods, results, figures\n"
        "- author_correction: a correction or erratum to a previously published article\n"
        "- supplementary_material: supplemental figures, tables, or methods\n"
        "- editorial_commentary: opinion piece, editorial, commentary, or letter to editor\n"
        "- advertisement: promotional or advertising content\n"
        "- other: does not fit any category above\n\n"
        "Then determine whether this PDF is the actual research paper. "
        "Reject (match=false) if the document type is NOT research_article, "
        "or if the content does not match the expected title/abstract.\n\n"
        "Rejection criteria:\n"
        "- The document type is NOT research_article\n"
        "- The PDF is a correction, erratum, supplement, editorial, or advertisement\n"
        "- The title/abstract don't match the PDF content\n"
        "- The PDF lacks hallmarks of a research article (methods, results, data)\n\n"
        f"=== EXPECTED PAPER ===\n"
        f"Title: {formatted_title}\n"
        f"Abstract: {formatted_abstract}\n\n"
        f"=== PDF CONTENT (first {len(pdf_sample)} chars) ===\n"
        f"{pdf_sample}\n\n"
        "Respond with ONLY a JSON object (no markdown, no code fences):\n"
        '{"match": true/false, "confidence": 0.0-1.0, '
        '"doc_type": "research_article|author_correction|supplementary_material|editorial_commentary|advertisement|other", '
        '"reason": "brief explanation in English"}'
    )

    api_base = cfg(config, 'llm.api_base_url', 'http://localhost:8080/v1')
    model = cfg(config, 'llm.model', 'deepseek-v4-pro')
    api_key_cfg = cfg(config, 'llm.api_key_env', 'LLM_API_KEY')
    api_key = os.environ.get(api_key_cfg, '')
    if not api_key:
        if api_key_cfg and ' ' not in api_key_cfg and len(api_key_cfg) > 20:
            api_key = api_key_cfg

    body = json.dumps({
        'model': model,
        'temperature': 0,
        'max_tokens': 256,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }).encode('utf-8')

    url = f"{api_base.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    })

    label = 'validate_pdf_content'
    ts_print(f"  Calling LLM [{label}]: {model} ({url})...")
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=cfg(config, 'interpreter.pdf_content_validation.timeout', 60))
    resp_data = json.loads(resp.read().decode('utf-8'))
    elapsed = time.time() - t0
    content = resp_data['choices'][0]['message']['content'].strip()

    # Parse JSON from response (strip code fences if present)
    content = re.sub(r'^```(?:json)?\s*', '', content)
    content = re.sub(r'\s*```$', '', content)
    result = json.loads(content)

    match = result.get('match', False)
    confidence = float(result.get('confidence', 0.5))
    reason = result.get('reason', 'no reason provided')
    doc_type = result.get('doc_type')
    usage_raw = resp_data.get('usage', {})
    usage = {
        'prompt_tokens': usage_raw.get('prompt_tokens', 0),
        'completion_tokens': usage_raw.get('completion_tokens', 0),
        'total_tokens': usage_raw.get('total_tokens', 0),
        'elapsed_seconds': elapsed,
    }
    _log_llm_result(label, usage, elapsed)
    return match, confidence, reason, usage, doc_type


def run_phase1(paper_path, paper, config, log_file):
    """Assign topic tags to paper. Always passes — keywords are for search only."""
    paper_id = paper['paper_id']

    metadata_path = os.path.join(paper_path, f'{sanitize(paper_id)}.metadata.json')
    metadata = {}
    if os.path.exists(metadata_path):
        with open(metadata_path) as f:
            metadata = json.load(f)

    combined = {**metadata, **paper}

    tags = match_tags(combined, config)
    conn = get_conn(config)
    update_tags(conn, paper_id, tags)
    n_tags = len(tags.get('tag_ids', []))
    labels = ', '.join(tags.get('matched_labels', []))
    log_phase(log_file, paper_id, 1, 'COMPLETED', f'{n_tags} tags: {labels}')
    return True, f'{n_tags} tags: {labels}'


def run_phase2(paper_path, paper, config, log_file, cn=False, trans=False,
               skip_existing_en=False):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)
    title = paper.get('title', '')

    pdf_path = os.path.join(paper_path, f'{safe_pid}.pdf')
    if not os.path.exists(pdf_path):
        error = f'no PDF file at {pdf_path}'
        log_phase(log_file, paper_id, 2, 'FAILED', error)
        mark_interpret_failed(get_conn(config), paper_id, error,
                              category='missing_pdf', subtype='pdf_file_missing',
                              tags=['missing_pdf'], metadata={'pdf_path': pdf_path})
        return False, error

    max_chars = cfg(config, 'download.pdf_text_max_chars', 100000)
    extractor_mode = cfg(config, 'download.pdf_extraction.extractor', 'auto')
    image_subdir = cfg(config, 'download.pdf_extraction.image_dir', 'images')
    image_dir_full = os.path.join(paper_path, image_subdir)

    extract_script = os.path.join(SKILL_DIR, 'extract_pdf.py')
    extract_timeouts = cfg(config, 'interpreter.pdf_extract_timeouts', [120, 240, 240])
    extract_data = None
    last_error = None
    for attempt, timeout_val in enumerate(extract_timeouts):
        mode = extractor_mode if attempt < 2 else 'pdftotext'
        try:
            result = subprocess.run(
                [sys.executable, extract_script, pdf_path,
                 '--max-chars', str(max_chars),
                 '--image-path', image_dir_full,
                 '--extractor', mode,
                 '--json'],
                capture_output=True, text=True, timeout=timeout_val,
            )
            extract_data = json.loads(result.stdout)
            break
        except subprocess.TimeoutExpired:
            last_error = f'timed out after {timeout_val}s with {mode} (attempt {attempt+1}/{len(extract_timeouts)})'
            ts_print(f"  {last_error}", file=sys.stderr)
        except Exception as e:
            last_error = str(e)[:100]
            break

    if extract_data is None:
        ts_print(f"  PDF extract failed: {last_error}", file=sys.stderr)
        log_phase(log_file, paper_id, 2, 'FAILED', f'extract error: {last_error}')
        subtype = 'extract_timeout' if last_error and 'timed out' in last_error else 'pdf_extract_failed'
        mark_interpret_failed(get_conn(config), paper_id, f'PDF extract: {last_error}',
                              category='content_extraction', subtype=subtype,
                              tags=['pdf_extraction'], metadata={'last_error': last_error})
        return False, f'extract error: {last_error}'

    pdf_text = extract_data.get('markdown', '')
    extractor_used = extract_data.get('extractor', 'unknown')
    rep_image = extract_data.get('representative_image')
    image_count = extract_data.get('image_count', 0)
    txt_path = os.path.join(paper_path, f'{safe_pid}.pdf.txt')
    _write_text(txt_path, pdf_text)

    if len(pdf_text) < 1000:
        error = f'insufficient text ({len(pdf_text)} chars, extractor={extractor_used})'
        log_phase(log_file, paper_id, 2, 'FAILED', error)
        mark_interpret_failed(get_conn(config), paper_id, error,
                              category='content_extraction', subtype='insufficient_text',
                              tags=['insufficient_text'],
                              metadata={'text_chars': len(pdf_text), 'extractor': extractor_used})
        return False, error

    validation_enabled = cfg(config, 'interpreter.pdf_content_validation.enabled', True)
    val_usage = None
    if validation_enabled:
        try:
            passed, confidence, reason, val_usage, doc_type = _validate_pdf_content_llm(config, pdf_text, paper)
            if not passed:
                subtype = doc_type if doc_type and doc_type != 'research_article' else 'title_abstract_mismatch'
                log_phase(log_file, paper_id, 2, 'FAILED', reason)
                mark_interpret_failed(get_conn(config), paper_id, reason,
                                      category='non_paper', subtype=subtype,
                                      tags=[subtype], metadata={
                                          'doc_type': doc_type,
                                          'confidence': confidence,
                                          'validator_reason': reason,
                                      })
                return False, reason
            ts_print(f"  PDF validation: {reason}")
        except Exception as e:
            ts_print(f"  PDF validation error (proceeding anyway): {e}", file=sys.stderr)

    mode = 'full_text'
    ts_print(f"  PDF text: {len(pdf_text)} chars, mode={mode}, extractor={extractor_used}, images={image_count}")
    ts_print(f"  Saved extracted text: {os.path.basename(txt_path)}")

    metadata_path = os.path.join(paper_path, f'{safe_pid}.metadata.json')
    paper_data = dict(paper)
    if os.path.exists(metadata_path):
        with open(metadata_path, encoding='utf-8') as f:
            paper_data.update(json.load(f))

    extract_meta = {
        'representative_image': rep_image,
        'image_count': image_count,
    }
    paper_context = build_paper_context(paper_data, pdf_text, extract_meta)

    translations = {}
    usage_entries = [('validate', val_usage)]
    if cn or trans:
        translation_usage = None
        try:
            translations, translation_usage = _translate_paper_metadata(config, paper_data)
            if translations:
                ts_print("  Metadata translation: ready")
        except Exception as e:
            ts_print(f"  LLM call failed (metadata translation, proceeding): {e}", file=sys.stderr)
        usage_entries.append(('metadata_translate', translation_usage))

    tag_data = {}
    try:
        conn = get_conn(config)
        row = conn.execute("SELECT matched_tags FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
        if row and row[0]:
            tag_data = json.loads(row[0])
    except Exception:
        pass

    def save_interpret_json(path, content, language, translation_source=None):
        interpret_json = {
            'paper_id': paper_id,
            'doi': paper.get('doi', ''),
            'title': title,
            'title_zh': translations.get('title_zh', ''),
            'abstract': paper_data.get('abstract', paper.get('abstract', '')),
            'abstract_zh': translations.get('abstract_zh', ''),
            'content': content,
            'language': language,
            'tags': tag_data.get('tag_ids', []),
            'tag_labels': tag_data.get('matched_labels', []),
            'mode': mode,
            'extractor': extractor_used,
            'representative_image': rep_image,
            'image_count': image_count,
            'pdf_text_path': os.path.basename(txt_path),
            'interpreted_at': datetime.now().isoformat(),
        }
        if translation_source:
            interpret_json['translation_source'] = translation_source
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(interpret_json, f, ensure_ascii=False, indent=2)

    interpret_content = None
    interpret_ok = False
    interpret_usage = None
    md_path = os.path.join(paper_path, f'{safe_pid}.interpret.md')
    interpret_json_path = os.path.join(paper_path, f'{safe_pid}.interpret.json')
    if skip_existing_en and os.path.exists(md_path):
        with open(md_path, encoding='utf-8') as f:
            interpret_content = f.read()
        interpret_ok = True
        ts_print(f"  Reusing existing: {os.path.basename(md_path)}")
    else:
        try:
            interpret_prompt = build_full_text_prompt(
                paper_data, config, pdf_text, extract_meta,
                prompt_name='interpret_en', language='en')
            interpret_content, interpret_usage = _call_llm(config, interpret_prompt['system_prompt'],
                                                            interpret_prompt['user_prompt'],
                                                            label='interpret_en')
            _write_text(md_path, interpret_content)
            save_interpret_json(interpret_json_path, interpret_content, 'en')
            interpret_ok = True
        except Exception as e:
            ts_print(f"  LLM call failed (interpret_en): {e}", file=sys.stderr)
    usage_entries.append(('interpret_en', interpret_usage))

    brief_content = None
    brief_ok = False
    brief_usage = None
    brief_path = os.path.join(paper_path, f'{safe_pid}.brief.md')
    if skip_existing_en and os.path.exists(brief_path):
        with open(brief_path, encoding='utf-8') as f:
            brief_content = f.read()
        brief_ok = True
        ts_print(f"  Reusing existing: {os.path.basename(brief_path)}")
    else:
        try:
            brief_prompt = build_brief_prompt(
                paper_data, config, pdf_text, extract_meta,
                prompt_name='brief_en', language='en')
            brief_content, brief_usage = _call_llm(config, brief_prompt['system_prompt'],
                                                    brief_prompt['user_prompt'],
                                                    label='brief_en')
            _write_text(brief_path, brief_content)
            brief_ok = True
        except Exception as e:
            ts_print(f"  LLM call failed (brief_en): {e}", file=sys.stderr)
    usage_entries.append(('brief_en', brief_usage))

    if not interpret_ok and not brief_ok:
        error = 'both English interpret and brief LLM calls failed'
        log_phase(log_file, paper_id, 2, 'FAILED', error)
        mark_interpret_failed(get_conn(config), paper_id, error,
                              category='llm_api', subtype='all_required_llm_calls_failed',
                              tags=['interpret_en_failed', 'brief_en_failed'])
        return False, error

    zh_interpret_ok = False
    zh_brief_ok = False
    if cn or trans:
        if trans:
            if interpret_ok:
                zh_usage = None
                try:
                    prompt = _build_translation_prompt('translate_interpret_zh', paper_context,
                                                       source_markdown=interpret_content)
                    zh_content, zh_usage = _call_llm(config, prompt['system_prompt'], prompt['user_prompt'],
                                                     label='translate_interpret_zh')
                    _write_text(os.path.join(paper_path, f'{safe_pid}.interpret.zh.md'), zh_content)
                    save_interpret_json(os.path.join(paper_path, f'{safe_pid}.interpret.zh.json'),
                                        zh_content, 'zh', 'en+paper_context')
                    zh_interpret_ok = True
                except Exception as e:
                    ts_print(f"  LLM call failed (translate_interpret_zh): {e}", file=sys.stderr)
                usage_entries.append(('translate_interpret_zh', zh_usage))
            if brief_ok:
                zh_usage = None
                try:
                    prompt = _build_translation_prompt('translate_brief_zh', paper_context,
                                                       source_markdown=brief_content)
                    zh_content, zh_usage = _call_llm(config, prompt['system_prompt'], prompt['user_prompt'],
                                                     label='translate_brief_zh')
                    _write_text(os.path.join(paper_path, f'{safe_pid}.brief.zh.md'), zh_content)
                    zh_brief_ok = True
                except Exception as e:
                    ts_print(f"  LLM call failed (translate_brief_zh): {e}", file=sys.stderr)
                usage_entries.append(('translate_brief_zh', zh_usage))
        else:
            zh_usage = None
            try:
                prompt = build_full_text_prompt(
                    paper_data, config, pdf_text, extract_meta,
                    prompt_name='interpret', language='zh')
                zh_content, zh_usage = _call_llm(config, prompt['system_prompt'], prompt['user_prompt'],
                                                 label='interpret_zh_direct')
                _write_text(os.path.join(paper_path, f'{safe_pid}.interpret.zh.md'), zh_content)
                save_interpret_json(os.path.join(paper_path, f'{safe_pid}.interpret.zh.json'),
                                    zh_content, 'zh', 'paper')
                zh_interpret_ok = True
            except Exception as e:
                ts_print(f"  LLM call failed (interpret_zh): {e}", file=sys.stderr)
            usage_entries.append(('interpret_zh', zh_usage))

            zh_usage = None
            try:
                prompt = build_brief_prompt(
                    paper_data, config, pdf_text, extract_meta,
                    prompt_name='brief', language='zh')
                zh_content, zh_usage = _call_llm(config, prompt['system_prompt'], prompt['user_prompt'],
                                                 label='brief_zh_direct')
                _write_text(os.path.join(paper_path, f'{safe_pid}.brief.zh.md'), zh_content)
                zh_brief_ok = True
            except Exception as e:
                ts_print(f"  LLM call failed (brief_zh): {e}", file=sys.stderr)
            usage_entries.append(('brief_zh', zh_usage))

    conn = get_conn(config)
    if translations.get('title_zh') or translations.get('abstract_zh'):
        update_translations(conn, paper_id,
                            translations.get('title_zh'),
                            translations.get('abstract_zh'))
    mark_interpreted(conn, paper_id)

    extra = []
    if not interpret_ok:
        extra.append('interpret_en failed')
    if not brief_ok:
        extra.append('brief_en failed')
    if cn or trans:
        if not zh_interpret_ok:
            extra.append('interpret_zh failed')
        if not zh_brief_ok:
            extra.append('brief_zh failed')
    lang_msg = 'en+zh' if cn or trans else 'en'
    log_phase(log_file, paper_id, 2, 'COMPLETED',
              f'{mode} ({lang_msg})' + (f' ({", ".join(extra)})' if extra else ''))
    _log_phase2_tokens(usage_entries)
    return True, f'{mode} ({lang_msg})'


def run_phase3(paper_path, paper, config, log_file):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)
    script = os.path.join(SKILL_DIR, 'md_to_html.py')
    all_ok = True
    last_error = None

    # Read representative image path from interpret.json if available
    rep_image_abs = None
    interpret_json_path = os.path.join(paper_path, f'{safe_pid}.interpret.json')
    if os.path.exists(interpret_json_path):
        try:
            with open(interpret_json_path) as f:
                ij = json.load(f)
                rep_image_rel = ij.get('representative_image')
                if rep_image_rel:
                    rep_image_abs = os.path.join(paper_path, rep_image_rel)
                    if not os.path.exists(rep_image_abs):
                        rep_image_abs = None
        except Exception:
            pass

    targets = [
        ('interpret', 'en', f'{safe_pid}.interpret.md', f'{safe_pid}.interpret.html'),
        ('brief', 'en', f'{safe_pid}.brief.md', f'{safe_pid}.brief.html'),
        ('interpret.zh', 'zh', f'{safe_pid}.interpret.zh.md', f'{safe_pid}.interpret.zh.html'),
        ('brief.zh', 'zh', f'{safe_pid}.brief.zh.md', f'{safe_pid}.brief.zh.html'),
    ]
    for name, lang, md_name, html_name in targets:
        md_path = os.path.join(paper_path, md_name)
        if not os.path.exists(md_path):
            continue

        html_path = os.path.join(paper_path, html_name)
        cmd = [sys.executable, script, '--input', md_path, '--output', html_path,
               '--lang', lang]
        if rep_image_abs:
            cmd.extend(['--image', rep_image_abs])
        poster_path = os.path.join(paper_path, f'{safe_pid}.poster.{lang}.png')
        if os.path.exists(poster_path):
            cmd.extend(['--poster', poster_path])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                err_msg = result.stderr.strip() or f'exit code {result.returncode}'
                ts_print(f"  md_to_html error ({name}): {err_msg}", file=sys.stderr)
                log_phase(log_file, paper_id, 3, 'FAILED', f'{name}: {err_msg[:100]}')
                last_error = f'{name}: {err_msg[:100]}'
                all_ok = False
        except Exception as e:
            log_phase(log_file, paper_id, 3, 'FAILED', f'{name}: {str(e)[:100]}')
            last_error = f'{name}: {str(e)[:100]}'
            all_ok = False

    if all_ok:
        log_phase(log_file, paper_id, 3, 'COMPLETED', f'HTML saved: {paper_path}')
        return True, 'OK'
    return False, last_error or 'unknown error'


def run_phase4(paper_path, paper, config, log_file, cn=False, trans=False,
               skip_existing_en=False):
    """Generate posters: English by default, bilingual with --cn/--trans."""
    paper_id = paper['paper_id']

    af_config = cfg(config, 'autofigure', {})
    api_key_env = af_config.get('api_key_env', 'LLM_API_KEY')
    api_key = os.environ.get(api_key_env, '')
    if not api_key and len(api_key_env) > 20:
        api_key = api_key_env

    if not api_key:
        log_phase(log_file, paper_id, 4, 'SKIPPED', 'no API key')
        return True, 'no API key (skipped)'

    script = os.path.join(SKILL_DIR, 'generate_poster.py')
    meth_model = af_config.get('methodology_model') or cfg(config, 'llm.model', 'deepseek-v4-pro')
    meth_base = af_config.get('methodology_base_url') or cfg(config, 'llm.api_base_url', '')
    enh_model = af_config.get('enhancement_model', 'qwen-image-2.0-pro')

    if skip_existing_en and (cn or trans):
        lang = 'zh'
    else:
        lang = 'both' if (cn or trans) else 'en'
    cmd = [sys.executable, script,
           '--paper-dir', paper_path,
           '--paper-id', paper_id,
           '--api-key', api_key,
           '--methodology-model', meth_model,
           '--methodology-base-url', meth_base,
           '--enhancement-model', enh_model,
           '--lang', lang]
    if trans:
        cmd.append('--trans')

    try:
        result = subprocess.run(cmd, timeout=3600,
                                env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        if result.returncode != 0:
            log_phase(log_file, paper_id, 4, 'FAILED', f'exit code {result.returncode}')
            return False, f'exit code {result.returncode}'
    except subprocess.TimeoutExpired:
        log_phase(log_file, paper_id, 4, 'FAILED', 'timeout')
        return False, 'timeout'
    except Exception as e:
        log_phase(log_file, paper_id, 4, 'FAILED', str(e)[:100])
        return False, str(e)[:100]

    n = '6' if (cn or trans) else '3'
    mode = 'bilingual posters generated' if (cn or trans) else 'English posters generated'
    log_phase(log_file, paper_id, 4, 'COMPLETED', f'{n} {mode}')
    return True, f'{n} {mode}'


def process_paper(paper, config, phases, log_file, cn=False, trans=False,
                  skip_existing_en=False):
    paper_id = paper['paper_id']
    paper_dir = paper.get('dir_name', '') or get_paper_dir(get_conn(config), paper_id) or ''
    if not paper_dir:
        ts_print(f"  No dir_name for {paper_id}, skipping", file=sys.stderr)
        return

    paper_path = os.path.join(REPO_ROOT, 'data', paper_dir)

    ts_print(f"\n{'='*60}")
    ts_print(f"Paper: {paper_id}")
    ts_print(f"Title: {(paper.get('title', '') or '')[:80]}")
    ts_print(f"Dir: {paper_dir}")

    for phase in [1, 2, 3, 4]:
        if str(phase) not in phases:
            continue

        ts_print(f"  Phase {phase}...", end=' ', flush=True)
        log_phase(log_file, paper_id, phase, 'START')

        if phase == 1:
            ok, msg = run_phase1(paper_path, paper, config, log_file)
        elif phase == 2:
            ok, msg = run_phase2(paper_path, paper, config, log_file, cn=cn, trans=trans,
                                   skip_existing_en=skip_existing_en)
        elif phase == 3:
            ok, msg = run_phase3(paper_path, paper, config, log_file)
        elif phase == 4:
            ok, msg = run_phase4(paper_path, paper, config, log_file, cn=cn, trans=trans,
                                   skip_existing_en=skip_existing_en)
            if ok:
                run_phase3(paper_path, paper, config, log_file)  # re-embed with posters

        if ok:
            ts_print(f'OK ({msg})')
        else:
            ts_print(f'FAILED: {msg}')
            break


def _get_paper_by_doi(conn, doi):
    if not doi:
        return None
    row = conn.execute("SELECT paper_id FROM papers WHERE doi = ?", (doi,)).fetchone()
    return get_paper(conn, row['paper_id']) if row else None


def _merge_canonical_metadata(metadata, canonical):
    merged = dict(metadata)
    if not canonical:
        return merged
    for key in ('title', 'authors', 'abstract', 'doi', 'pmid', 'arxiv_id', 'source', 'source_url', 'pdf_url'):
        if canonical.get(key) and not merged.get(key):
            merged[key] = canonical[key]
    merged['paper_id'] = canonical.get('paper_id') or merged.get('paper_id')
    return merged


def _target_dir_for_paper(paper, safe_pid):
    if paper.get('dir_name'):
        dirname = paper['dir_name']
        return dirname, os.path.join(REPO_ROOT, 'data', dirname)
    return _build_pdf_import_dir(paper, safe_pid)


def _parse_phases(args):
    phase_str = getattr(args, 'phase', None)
    return set(phase_str.split(',')) if phase_str else {'1', '2', '3', '4'}


def _update_paper_fields(conn, paper_id, updates):
    """Update specific paper fields in the DB, overwriting existing values."""
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [datetime.now().isoformat(), paper_id]
    conn.execute(f"UPDATE papers SET {set_clause}, updated_at = ? WHERE paper_id = ?", values)
    conn.commit()


def cmd_pdf(args, config):
    pdf_path = os.path.abspath(args.pdf_path)
    if not os.path.isfile(pdf_path):
        ts_print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    try:
        pdf_sha256 = _sha256_file(pdf_path)
        hints = extract_pdf_hints(pdf_path)
    except Exception as e:
        ts_print(f"Invalid PDF: {e}", file=sys.stderr)
        return 1

    conn = get_conn(config)
    local_pdf_id = f'local_pdf_{pdf_sha256[:16]}'
    existing_local = get_paper(conn, getattr(args, 'paper_id', None) or local_pdf_id)
    cn = getattr(args, 'cn', False) or getattr(args, 'trans', False)
    if existing_local and existing_local.get('status') == 'interpreted' and not args.force and not getattr(args, 'doi', None):
        if not cn:
            return _print_existing_results(existing_local, config)
        safe_pid = sanitize(existing_local.get('paper_id', ''))
        paper_dir = existing_local.get('dir_name', '') or get_paper_dir(conn, existing_local.get('paper_id', '')) or ''
        if paper_dir:
            zh_path = os.path.join(REPO_ROOT, 'data', paper_dir, f'{safe_pid}.interpret.zh.md')
            if os.path.exists(zh_path):
                return _print_existing_results(existing_local, config)
        phases = {'2', '3', '4'}
        log_file = os.path.join(REPO_ROOT, 'data', 'execution_log.md')
        trans = getattr(args, 'trans', False)
        ts_print(f"Incremental: adding Chinese outputs for {existing_local.get('paper_id')}")
        ts_print(f"Phases: {sorted(phases)}")
        ts_print(f"Languages: English + Chinese" + (' (translated)' if trans else ''))
        process_paper(existing_local, config, phases, log_file, cn=True, trans=trans,
                      skip_existing_en=True)
        return 0

    paper, record, resolver_info = resolve_pdf_metadata(args, hints, pdf_sha256, existing_local=existing_local)
    if not paper.get('paper_id'):
        paper['paper_id'] = getattr(args, 'paper_id', None) or local_pdf_id
    metadata = _build_pdf_import_metadata(paper, hints, pdf_path, pdf_sha256, resolver_info)

    if getattr(args, 'dry_run', False):
        existing = existing_local or _get_paper_by_doi(conn, metadata.get('doi'))
        display_paper = _merge_canonical_metadata(metadata, existing)
        safe_pid = sanitize(display_paper.get('paper_id', ''))
        dirname, paper_dir = _target_dir_for_paper(display_paper, safe_pid)
        ts_print("Would import local PDF:")
        ts_print(f"  paper_id: {display_paper.get('paper_id', '')}")
        ts_print(f"  title: {(display_paper.get('title', '') or '')[:120]}")
        ts_print(f"  doi: {display_paper.get('doi', '') or '(none)'}")
        ts_print(f"  status: {(existing or {}).get('status', 'new')}")
        ts_print(f"  resolver: {resolver_info.get('status', '')}")
        ts_print(f"  target: {os.path.join(paper_dir, f'{safe_pid}.pdf')}")
        return 0

    canonical_id = upsert_single_paper(conn, metadata, by_doi=bool(metadata.get('doi')))
    if not canonical_id:
        ts_print("Failed to create database row for local PDF", file=sys.stderr)
        return 1

    canonical = get_paper(conn, canonical_id)
    if canonical and canonical.get('status') == 'interpreted' and not args.force:
        return _print_existing_results(canonical, config)

    metadata = _merge_canonical_metadata(metadata, canonical)
    metadata['paper_id'] = canonical_id
    safe_pid = sanitize(canonical_id)
    dirname, paper_dir = _target_dir_for_paper(metadata if metadata.get('dir_name') else (canonical or metadata), safe_pid)
    metadata['dir_name'] = dirname

    dest_pdf = _write_pdf_import_files(pdf_path, paper_dir, safe_pid, metadata, record)
    prev_status = (canonical or {}).get('status')
    mark_downloaded(conn, canonical_id, dirname, metadata_updates=metadata)
    if resolver_info.get('status') == 'resolved':
        resolver_fields = {}
        for key in ('title', 'authors', 'abstract', 'doi', 'pmid', 'arxiv_id'):
            if metadata.get(key) and metadata.get(key) != (canonical or {}).get(key, ''):
                resolver_fields[key] = metadata[key]
        if resolver_fields:
            _update_paper_fields(conn, canonical_id, resolver_fields)
            ts_print(f"  Updated {len(resolver_fields)} field(s) from resolver: {', '.join(resolver_fields)}")
    if prev_status == 'interpreted':
        conn.execute("UPDATE papers SET status = 'interpreted', interpret_date = ?, updated_at = ? WHERE paper_id = ?",
                     [datetime.now().isoformat(), datetime.now().isoformat(), canonical_id])
        conn.commit()
    paper = get_paper(conn, canonical_id) or metadata
    paper['dir_name'] = dirname

    phases = _parse_phases(args)
    log_file = os.path.join(REPO_ROOT, 'data', 'execution_log.md')
    trans = getattr(args, 'trans', False)
    cn = getattr(args, 'cn', False) or trans
    ts_print(f"Imported local PDF: {dest_pdf}")
    ts_print(f"Paper: {canonical_id}")
    ts_print(f"Phases: {sorted(phases)}")
    ts_print(f"Languages: {'English + Chinese' if cn else 'English'}" + (' (translated)' if trans else ''))
    process_paper(paper, config, phases, log_file, cn=cn, trans=trans)
    return 0


def cmd_run(args, config):
    conn = get_conn(config)

    paper_id = getattr(args, 'paper_id', None)
    if paper_id:
        paper = get_paper(conn, paper_id)
        if not paper:
            ts_print(f"Paper not found: {paper_id}", file=sys.stderr)
            return 1
        if not args.force and paper.get('status') == 'interpreted':
            ts_print(f"  [skip] already interpreted (use --force to re-interpret)")
            return 0
        papers = [paper]
    else:
        if args.retry_failed:
            papers = get_papers_by_status(conn, 'interpret_failed')
        else:
            papers = get_papers_by_status(conn, 'downloaded')
        limit = getattr(args, 'limit', None)

        cnsp_only = getattr(args, 'cnsp', False)
        if cnsp_only:
            config['__config_path__'] = config.get('__config_path__', 'config.yaml')
            cnsp_names = load_cnsp_journal_set(config)
            before = len(papers)
            papers = filter_cnsp_papers(papers, cnsp_names)
            ts_print(f"CNSP filter: {before} -> {len(papers)} papers")

        cns_only = getattr(args, 'cns', False)
        if cns_only:
            config['__config_path__'] = config.get('__config_path__', 'config.yaml')
            cns_names = load_cns_journal_set(config)
            before = len(papers)
            papers = filter_cnsp_papers(papers, cns_names)
            ts_print(f"CNS filter: {before} -> {len(papers)} papers")

        if limit:
            papers = papers[:limit]

    if not papers:
        ts_print("No papers to interpret.")
        return 0

    phase_str = getattr(args, 'phase', None)
    phases = set(phase_str.split(',')) if phase_str else {'1', '2', '3', '4'}
    log_file = os.path.join(REPO_ROOT, 'data', 'execution_log.md')

    ts_print(f"Papers to interpret: {len(papers)}")
    ts_print(f"Phases: {sorted(phases)}")
    trans = getattr(args, 'trans', False)
    cn = getattr(args, 'cn', False) or trans
    ts_print(f"Languages: {'English + Chinese' if cn else 'English'}" + (' (translated)' if trans else ''))
    for i, paper in enumerate(papers):
        ts_print(f"\n[{i+1}/{len(papers)}]", end='')
        process_paper(paper, config, phases, log_file, cn=cn, trans=trans)
        if i < len(papers) - 1:
            time.sleep(1)

    return 0


def main():
    sys.stdout.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser(
        prog='paper_cli.py',
        description='Bio Paper Interpreter — batch interpret downloaded papers via LLM.\n'
                    'Auto-mode (no subcommand): process all "downloaded" papers.',
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--config', default=os.path.join(REPO_ROOT, 'config.yaml'),
                   help='Path to shared YAML config file')
    p.add_argument('--dry-run', action='store_true',
                   help='List papers that would be processed, then exit')
    p.add_argument('--limit', '-n', type=int, default=None,
                   help='Max number of papers to process')
    p.add_argument('--cnsp', action='store_true',
                   help='Only interpret papers published in C/N/S/P journals')
    p.add_argument('--cns', action='store_true',
                   help='Only interpret papers published in C/N/S journals (excludes PLOS)')
    p.add_argument('--retry-failed', action='store_true',
                   help='Retry papers with interpret_failed status')
    p.add_argument('--cn', action='store_true',
                   help='Also generate Chinese reports, HTML, and posters')
    p.add_argument('--trans', action='store_true',
                   help='Generate Chinese outputs from English outputs plus PDF context (implies --cn)')
    p.add_argument('--en', action='store_true', help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest='cmd', title='commands',
                           description='"run" a single paper, "pdf" a local file, or omit for auto-mode')

    run_p = sub.add_parser('run', help='Run interpretation on a single paper',
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    run_p.add_argument('paper_id', help='Paper ID to interpret')
    run_p.add_argument('--phase', default=None,
                       help='Phases to run (1,2,3,4 or 1,2). Default: all four.')
    run_p.add_argument('-f', '--force', action='store_true',
                       help='Force re-interpret even if already interpreted')
    run_p.add_argument('--cn', action='store_true',
                       help='Also generate Chinese reports, HTML, and posters')
    run_p.add_argument('--trans', action='store_true',
                       help='Generate Chinese outputs from English outputs plus PDF context (implies --cn)')
    run_p.add_argument('--en', action='store_true', help=argparse.SUPPRESS)

    pdf_p = sub.add_parser('pdf', help='Import a local PDF and interpret it',
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    pdf_p.add_argument('pdf_path', help='Path to local PDF file')
    pdf_p.add_argument('--paper-id', default=None,
                       help='Override paper ID. Defaults to resolved ID or local PDF content hash.')
    pdf_p.add_argument('--title', default=None,
                       help='Override/inject title if metadata search fails')
    pdf_p.add_argument('--doi', default=None,
                       help='Override/inject DOI for metadata resolution')
    pdf_p.add_argument('--phase', default=None,
                       help='Phases to run (1,2,3,4 or 1,2). Default: all four.')
    pdf_p.add_argument('-f', '--force', action='store_true',
                       help='Re-import/overwrite PDF artifacts and re-interpret if already interpreted')
    pdf_p.add_argument('--dry-run', action='store_true',
                       help='Show planned import without writing files or changing the database')
    pdf_p.add_argument('--cn', action='store_true',
                       help='Also generate Chinese reports, HTML, and posters')
    pdf_p.add_argument('--trans', action='store_true',
                       help='Generate Chinese outputs from English outputs plus PDF context (implies --cn)')
    pdf_p.add_argument('--en', action='store_true', help=argparse.SUPPRESS)

    args = p.parse_args()

    config = load_config(args.config)
    config['__config_path__'] = args.config

    if args.cmd == 'pdf':
        return cmd_pdf(args, config)

    if args.dry_run:
        conn = get_conn(config)
        if args.retry_failed:
            papers = get_papers_by_status(conn, 'interpret_failed')
        else:
            papers = get_papers_by_status(conn, 'downloaded')
        if args.cnsp:
            config['__config_path__'] = config.get('__config_path__', 'config.yaml')
            cnsp_names = load_cnsp_journal_set(config)
            papers = filter_cnsp_papers(papers, cnsp_names)
        if args.cns:
            config['__config_path__'] = config.get('__config_path__', 'config.yaml')
            cns_names = load_cns_journal_set(config)
            papers = filter_cnsp_papers(papers, cns_names)
        if args.limit:
            papers = papers[:args.limit]
        ts_print(f"Would process {len(papers)} paper(s):")
        for p in papers:
            ts_print(f"  {p['paper_id']}  {(p.get('title', '') or '')[:70]}")
        return 0

    return cmd_run(args, config)


if __name__ == '__main__':
    sys.exit(main())