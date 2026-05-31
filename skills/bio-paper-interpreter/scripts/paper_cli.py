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
import json
import os
import re
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
  paper_cli.py --cn                     # Generate English + Chinese outputs
  paper_cli.py --trans                  # Generate Chinese outputs from English + PDF context
  paper_cli.py --dry-run                # List papers without processing
"""

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))
sys.path.insert(0, SKILL_DIR)

from paper_db import (get_conn, get_papers_by_status, get_paper_dir, get_paper,
                      mark_interpreted, mark_interpret_failed, update_tags,
                      update_translations, load_cnsp_journal_set,
                      filter_cnsp_papers, load_cns_journal_set)

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

    Returns (passed: bool, confidence: float, reason: str, usage: dict).
    """
    title = (paper.get('title') or '').strip()
    abstract = (paper.get('abstract') or '').strip()
    if not title and not abstract:
        return True, 1.0, 'no title or abstract to validate', {}
    if not pdf_text or len(pdf_text.strip()) < 200:
        return True, 1.0, 'PDF text too short to validate', {}

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
    usage_raw = resp_data.get('usage', {})
    usage = {
        'prompt_tokens': usage_raw.get('prompt_tokens', 0),
        'completion_tokens': usage_raw.get('completion_tokens', 0),
        'total_tokens': usage_raw.get('total_tokens', 0),
        'elapsed_seconds': elapsed,
    }
    _log_llm_result(label, usage, elapsed)
    return match, confidence, reason, usage


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


def run_phase2(paper_path, paper, config, log_file, cn=False, trans=False):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)
    title = paper.get('title', '')

    pdf_path = os.path.join(paper_path, f'{safe_pid}.pdf')
    if not os.path.exists(pdf_path):
        log_phase(log_file, paper_id, 2, 'FAILED', f'no PDF file at {pdf_path}')
        mark_interpret_failed(get_conn(config), paper_id, f'no PDF file at {pdf_path}')
        return False, f'no PDF file at {pdf_path}'

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
        mark_interpret_failed(get_conn(config), paper_id, f'PDF extract: {last_error}')
        return False, f'extract error: {last_error}'

    pdf_text = extract_data.get('markdown', '')
    extractor_used = extract_data.get('extractor', 'unknown')
    rep_image = extract_data.get('representative_image')
    image_count = extract_data.get('image_count', 0)
    txt_path = os.path.join(paper_path, f'{safe_pid}.pdf.txt')
    _write_text(txt_path, pdf_text)

    if len(pdf_text) < 1000:
        log_phase(log_file, paper_id, 2, 'FAILED',
                  f'insufficient text ({len(pdf_text)} chars, extractor={extractor_used})')
        mark_interpret_failed(get_conn(config), paper_id,
                              f'insufficient text ({len(pdf_text)} chars, extractor={extractor_used})')
        return False, f'insufficient text ({len(pdf_text)} chars, extractor={extractor_used})'

    validation_enabled = cfg(config, 'interpreter.pdf_content_validation.enabled', True)
    val_usage = None
    if validation_enabled:
        try:
            passed, confidence, reason, val_usage = _validate_pdf_content_llm(config, pdf_text, paper)
            if not passed:
                log_phase(log_file, paper_id, 2, 'FAILED', reason)
                mark_interpret_failed(get_conn(config), paper_id, reason)
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
    try:
        interpret_prompt = build_full_text_prompt(
            paper_data, config, pdf_text, extract_meta,
            prompt_name='interpret_en', language='en')
        interpret_content, interpret_usage = _call_llm(config, interpret_prompt['system_prompt'],
                                                        interpret_prompt['user_prompt'],
                                                        label='interpret_en')
        md_path = os.path.join(paper_path, f'{safe_pid}.interpret.md')
        _write_text(md_path, interpret_content)
        save_interpret_json(os.path.join(paper_path, f'{safe_pid}.interpret.json'),
                            interpret_content, 'en')
        interpret_ok = True
    except Exception as e:
        ts_print(f"  LLM call failed (interpret_en): {e}", file=sys.stderr)
    usage_entries.append(('interpret_en', interpret_usage))

    brief_content = None
    brief_ok = False
    brief_usage = None
    try:
        brief_prompt = build_brief_prompt(
            paper_data, config, pdf_text, extract_meta,
            prompt_name='brief_en', language='en')
        brief_content, brief_usage = _call_llm(config, brief_prompt['system_prompt'],
                                                brief_prompt['user_prompt'],
                                                label='brief_en')
        brief_path = os.path.join(paper_path, f'{safe_pid}.brief.md')
        _write_text(brief_path, brief_content)
        brief_ok = True
    except Exception as e:
        ts_print(f"  LLM call failed (brief_en): {e}", file=sys.stderr)
    usage_entries.append(('brief_en', brief_usage))

    if not interpret_ok and not brief_ok:
        log_phase(log_file, paper_id, 2, 'FAILED', 'both English interpret and brief LLM calls failed')
        mark_interpret_failed(get_conn(config), paper_id, 'both English interpret and brief LLM calls failed')
        return False, 'both English interpret and brief LLM calls failed'

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


def run_phase4(paper_path, paper, config, log_file, cn=False, trans=False):
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


def process_paper(paper, config, phases, log_file, cn=False, trans=False):
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
            ok, msg = run_phase2(paper_path, paper, config, log_file, cn=cn, trans=trans)
        elif phase == 3:
            ok, msg = run_phase3(paper_path, paper, config, log_file)
        elif phase == 4:
            ok, msg = run_phase4(paper_path, paper, config, log_file, cn=cn, trans=trans)
            if ok:
                run_phase3(paper_path, paper, config, log_file)  # re-embed with posters

        if ok:
            ts_print(f'OK ({msg})')
        else:
            ts_print(f'FAILED: {msg}')
            break


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
                           description='"run" a single paper, or omit for auto-mode')

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

    args = p.parse_args()

    config = load_config(args.config)
    config['__config_path__'] = args.config

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