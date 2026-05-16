#!/usr/bin/env python3
"""
Bio Paper Interpreter CLI — batch interpret downloaded papers via LLM.

Usage:
  paper_cli.py                          # auto-mode: process all 'downloaded' papers
  paper_cli.py run <paper_id>           # process a single paper
  paper_cli.py run <paper_id> --phase 1 # run only specified phases (1, 2, 3, or 1,2,3)

Three-phase pipeline:
  Phase 1: Relevance filter + tag matching
  Phase 2: PDF extraction + LLM interpretation (requires LLM_API_KEY)
  Phase 3: Markdown → styled HTML conversion
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
  paper_cli.py run s41467-026-70776-7   # Interpret a single paper (all phases)
  paper_cli.py run s41467-026-70776-7 --phase 1,2  # Only run phases 1 and 2
  paper_cli.py --dry-run                # List papers without processing
"""

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))
sys.path.insert(0, SKILL_DIR)

from paper_db import get_conn, get_papers_by_status, get_paper_dir, get_paper
from paper_db import mark_interpreted, mark_skipped, update_relevance, update_tags

from filter_relevance import check_relevance
from match_tags import match_tags
from build_prompt import build_full_text_prompt, build_brief_prompt, build_abstract_only_prompt, load_config


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


def _call_llm(config, system_prompt, user_prompt):
    """Call the configured LLM and return the response text."""
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
            print(f"  Warning: LLM API key not found (checked env var ${api_key_cfg})", file=sys.stderr)

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

    print(f"  Calling LLM: {model} ({url})...")
    resp = urllib.request.urlopen(req, timeout=timeout)
    resp_data = json.loads(resp.read().decode('utf-8'))
    return resp_data['choices'][0]['message']['content']


def run_phase1(paper_path, paper, config, log_file):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)

    metadata_path = os.path.join(paper_path, f'{safe_pid}.metadata.json')
    metadata = {}
    if os.path.exists(metadata_path):
        with open(metadata_path) as f:
            metadata = json.load(f)

    combined = {**paper, **metadata}

    relevance = check_relevance(combined, config)
    passed = relevance.get('passed', False)

    if not passed:
        skipped = {
            'paper_id': paper_id,
            'title': combined.get('title', ''),
            'skipped_at': datetime.now().isoformat(),
            'reason': relevance.get('reason', ''),
            'include_matches': relevance.get('include_matches', []),
            'exclude_matches': relevance.get('exclude_matches', []),
        }
        skip_path = os.path.join(paper_path, f'{safe_pid}.skipped.json')
        with open(skip_path, 'w') as f:
            json.dump(skipped, f, ensure_ascii=False, indent=2)
        conn = get_conn(config)
        update_relevance(conn, paper_id, relevance)
        mark_skipped(conn, paper_id)
        log_phase(log_file, paper_id, 1, 'REJECTED', relevance.get('reason', ''))
        return False

    tags = match_tags(combined, config)
    conn = get_conn(config)
    update_relevance(conn, paper_id, relevance)
    update_tags(conn, paper_id, tags)
    n_tags = len(tags.get('tag_ids', []))
    labels = ', '.join(tags.get('matched_labels', []))
    log_phase(log_file, paper_id, 1, 'COMPLETED', f'{n_tags} tags: {labels}')
    return True


def run_phase2(paper_path, paper, config, log_file):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)
    title = paper.get('title', '')

    # Step 1: Extract PDF content (now uses pymupdf4llm with pdftotext fallback)
    pdf_path = os.path.join(paper_path, f'{safe_pid}.pdf')
    if not os.path.exists(pdf_path):
        log_phase(log_file, paper_id, 2, 'FAILED', 'no PDF file — skipping per abstract-only rule')
        return False

    max_chars = cfg(config, 'download.pdf_text_max_chars', 100000)
    extractor_mode = cfg(config, 'download.pdf_extraction.extractor', 'auto')
    image_subdir = cfg(config, 'download.pdf_extraction.image_dir', 'images')
    image_dir_full = os.path.join(paper_path, image_subdir)

    extract_script = os.path.join(SKILL_DIR, 'extract_pdf.py')
    try:
        result = subprocess.run(
            [sys.executable, extract_script, pdf_path,
             '--max-chars', str(max_chars),
             '--image-path', image_dir_full,
             '--extractor', extractor_mode,
             '--json'],
            capture_output=True, text=True, timeout=120,
        )
        extract_data = json.loads(result.stdout)
    except Exception as e:
        print(f"  PDF extract failed: {e}", file=sys.stderr)
        log_phase(log_file, paper_id, 2, 'FAILED', f'extract error: {str(e)[:100]}')
        return False

    pdf_text = extract_data.get('markdown', '')
    extractor_used = extract_data.get('extractor', 'unknown')
    rep_image = extract_data.get('representative_image')
    image_count = extract_data.get('image_count', 0)

    if len(pdf_text) < 1000:
        log_phase(log_file, paper_id, 2, 'FAILED',
                  f'insufficient text ({len(pdf_text)} chars, extractor={extractor_used})')
        return False

    mode = 'full_text'
    print(f"  PDF text: {len(pdf_text)} chars, mode={mode}, extractor={extractor_used}, images={image_count}")

    # Step 2: Build prompts and call LLM for both interpret and brief
    metadata_path = os.path.join(paper_path, f'{safe_pid}.metadata.json')
    paper_data = dict(paper)
    if os.path.exists(metadata_path):
        with open(metadata_path) as f:
            paper_data.update(json.load(f))

    extract_meta = {
        'representative_image': rep_image,
        'image_count': image_count,
    }

    # Generate structured interpretation (.interpret.md)
    interpret_prompt = build_full_text_prompt(paper_data, config, pdf_text, extract_meta)
    try:
        interpret_content = _call_llm(config, interpret_prompt['system_prompt'],
                                      interpret_prompt['user_prompt'])
        md_path = os.path.join(paper_path, f'{safe_pid}.interpret.md')
        with open(md_path, 'w') as f:
            f.write(interpret_content)
        interpret_ok = True
    except Exception as e:
        print(f"  LLM call failed (interpret): {e}", file=sys.stderr)
        interpret_content = None
        interpret_ok = False

    # Generate brief article (.brief.md)
    brief_prompt = build_brief_prompt(paper_data, config, pdf_text, extract_meta)
    try:
        brief_content = _call_llm(config, brief_prompt['system_prompt'],
                                  brief_prompt['user_prompt'])
        brief_path = os.path.join(paper_path, f'{safe_pid}.brief.md')
        with open(brief_path, 'w') as f:
            f.write(brief_content)
        brief_ok = True
    except Exception as e:
        print(f"  LLM call failed (brief): {e}", file=sys.stderr)
        brief_content = None
        brief_ok = False

    if not interpret_ok and not brief_ok:
        log_phase(log_file, paper_id, 2, 'FAILED', 'both interpret and brief LLM calls failed')
        return False

    # Save interpret.json
    if interpret_ok:
        json_path = os.path.join(paper_path, f'{safe_pid}.interpret.json')
        tag_data = {}
        try:
            conn = get_conn(config)
            row = conn.execute("SELECT matched_tags FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
            if row and row[0]:
                tag_data = json.loads(row[0])
        except Exception:
            pass

        interpret_json = {
            'paper_id': paper_id,
            'doi': paper.get('doi', ''),
            'title': title,
            'content': interpret_content,
            'tags': tag_data.get('tag_ids', []),
            'tag_labels': tag_data.get('matched_labels', []),
            'mode': mode,
            'extractor': extractor_used,
            'representative_image': rep_image,
            'image_count': image_count,
            'interpreted_at': datetime.now().isoformat(),
        }
        with open(json_path, 'w') as f:
            json.dump(interpret_json, f, ensure_ascii=False, indent=2)

    conn = get_conn(config)
    mark_interpreted(conn, paper_id)
    extra = []
    if not interpret_ok:
        extra.append('interpret failed')
    if not brief_ok:
        extra.append('brief failed')
    log_phase(log_file, paper_id, 2, 'COMPLETED', f'{mode}' + (f' ({", ".join(extra)})' if extra else ''))
    return True


def run_phase3(paper_path, paper, config, log_file):
    paper_id = paper['paper_id']
    safe_pid = sanitize(paper_id)
    script = os.path.join(SKILL_DIR, 'md_to_html.py')
    all_ok = True

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

    for name in ('interpret', 'brief'):
        md_path = os.path.join(paper_path, f'{safe_pid}.{name}.md')
        if not os.path.exists(md_path):
            continue

        html_path = os.path.join(paper_path, f'{safe_pid}.{name}.html')
        cmd = [sys.executable, script, '--input', md_path, '--output', html_path]
        if rep_image_abs:
            cmd.extend(['--image', rep_image_abs])
        poster_path = os.path.join(paper_path, f'{safe_pid}.poster.zh.png')
        if os.path.exists(poster_path):
            cmd.extend(['--poster', poster_path])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                print(f"  md_to_html error ({name}): {result.stderr}", file=sys.stderr)
                all_ok = False
        except Exception as e:
            log_phase(log_file, paper_id, 3, 'FAILED', f'{name}: {str(e)[:100]}')
            all_ok = False

    if all_ok:
        log_phase(log_file, paper_id, 3, 'COMPLETED', f'HTML saved: {paper_path}')
    return all_ok


def run_phase4(paper_path, paper, config, log_file):
    """Generate 6 posters: 2 one-shot SVG + 2 rendered PNG + 2 direct PNG."""
    paper_id = paper['paper_id']

    af_config = cfg(config, 'autofigure', {})
    api_key_env = af_config.get('api_key_env', 'LLM_API_KEY')
    api_key = os.environ.get(api_key_env, '')
    if not api_key and len(api_key_env) > 20:
        api_key = api_key_env

    if not api_key:
        log_phase(log_file, paper_id, 4, 'SKIPPED', 'no API key')
        return True

    script = os.path.join(SKILL_DIR, 'generate_poster.py')
    meth_model = af_config.get('methodology_model') or cfg(config, 'llm.model', 'deepseek-v4-pro')
    meth_base = af_config.get('methodology_base_url') or cfg(config, 'llm.api_base_url', '')
    enh_model = af_config.get('enhancement_model', 'qwen-image-2.0-pro')

    cmd = [sys.executable, script,
           '--paper-dir', paper_path,
           '--paper-id', paper_id,
           '--api-key', api_key,
           '--methodology-model', meth_model,
           '--methodology-base-url', meth_base,
           '--enhancement-model', enh_model]

    try:
        result = subprocess.run(cmd, timeout=1200,
                                env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        if result.returncode != 0:
            log_phase(log_file, paper_id, 4, 'FAILED', f'exit code {result.returncode}')
            return False
    except subprocess.TimeoutExpired:
        log_phase(log_file, paper_id, 4, 'FAILED', 'timeout')
        return False
    except Exception as e:
        log_phase(log_file, paper_id, 4, 'FAILED', str(e)[:100])
        return False

    log_phase(log_file, paper_id, 4, 'COMPLETED', '6 posters generated')
    return True


def process_paper(paper, config, phases, log_file):
    paper_id = paper['paper_id']
    paper_dir = paper.get('dir_name', '') or get_paper_dir(get_conn(config), paper_id) or ''
    if not paper_dir:
        print(f"  No dir_name for {paper_id}, skipping", file=sys.stderr)
        return

    paper_path = os.path.join(REPO_ROOT, 'data', paper_dir)

    print(f"\n{'='*60}")
    print(f"Paper: {paper_id}")
    print(f"Title: {(paper.get('title', '') or '')[:80]}")
    print(f"Dir: {paper_dir}")

    for phase in [1, 2, 3, 4]:
        if str(phase) not in phases:
            continue

        print(f"  Phase {phase}...", end=' ', flush=True)
        log_phase(log_file, paper_id, phase, 'START')

        if phase == 1:
            ok = run_phase1(paper_path, paper, config, log_file)
        elif phase == 2:
            ok = run_phase2(paper_path, paper, config, log_file)
        elif phase == 3:
            ok = run_phase3(paper_path, paper, config, log_file)
        elif phase == 4:
            ok = run_phase4(paper_path, paper, config, log_file)

        if ok:
            print('OK')
        else:
            print('FAILED/REJECTED')
            if phase == 1:
                break   # rejected — stop this paper
            # For phase 2/3 failures, continue to next phase anyway


def cmd_run(args, config):
    conn = get_conn(config)

    paper_id = getattr(args, 'paper_id', None)
    if paper_id:
        paper = get_paper(conn, paper_id)
        if not paper:
            print(f"Paper not found: {paper_id}", file=sys.stderr)
            return 1
        papers = [paper]
    else:
        papers = get_papers_by_status(conn, 'downloaded')
        limit = getattr(args, 'limit', None)
        if limit:
            papers = papers[:limit]

    if not papers:
        print("No downloaded papers to interpret.")
        return 0

    phase_str = getattr(args, 'phase', None)
    phases = set(phase_str.split(',')) if phase_str else {'1', '2', '3', '4'}
    log_file = os.path.join(REPO_ROOT, 'data', 'execution_log.md')

    print(f"Papers to interpret: {len(papers)}")
    print(f"Phases: {sorted(phases)}")
    for i, paper in enumerate(papers):
        print(f"\n[{i+1}/{len(papers)}]", end='')
        process_paper(paper, config, phases, log_file)
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

    sub = p.add_subparsers(dest='cmd', title='commands',
                           description='"run" a single paper, or omit for auto-mode')

    run_p = sub.add_parser('run', help='Run interpretation on a single paper',
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    run_p.add_argument('paper_id', help='Paper ID to interpret')
    run_p.add_argument('--phase', default=None,
                       help='Phases to run (1,2,3,4 or 1,2). Default: all four.')

    args = p.parse_args()

    config = load_config(args.config)

    if args.dry_run:
        conn = get_conn(config)
        papers = get_papers_by_status(conn, 'downloaded')
        if args.limit:
            papers = papers[:args.limit]
        print(f"Would process {len(papers)} paper(s):")
        for p in papers:
            print(f"  {p['paper_id']}  {(p.get('title', '') or '')[:70]}")
        return 0

    return cmd_run(args, config)


if __name__ == '__main__':
    sys.exit(main())