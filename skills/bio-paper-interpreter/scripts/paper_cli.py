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
from build_prompt import build_full_text_prompt, build_abstract_only_prompt, load_config


def cfg(config, path, default=None):
    parts = path.split('.')
    cur = config
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur


def log_phase(log_file, paper_id, phase, status, msg=''):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} | Phase {phase} - {status}: {paper_id}"
    if msg:
        line += f" — {msg}"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, 'a') as f:
        f.write(line + '\n')


def run_phase1(paper_dir, paper, config, log_file):
    paper_id = paper['paper_id']

    metadata_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.metadata.json')
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
        skip_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.skipped.json')
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


def run_phase2(paper_dir, paper, config, log_file):
    paper_id = paper['paper_id']
    title = paper.get('title', '')

    # Step 1: Extract PDF text
    pdf_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.pdf')
    if not os.path.exists(pdf_path):
        log_phase(log_file, paper_id, 2, 'FAILED', 'no PDF file — skipping per abstract-only rule')
        return False

    max_chars = cfg(config, 'download.pdf_text_max_chars', 100000)
    extract_script = os.path.join(SKILL_DIR, 'extract_pdf_text.sh')
    try:
        result = subprocess.run(
            ['bash', extract_script, pdf_path, '--max-chars', str(max_chars)],
            capture_output=True, text=True, timeout=60,
        )
        pdf_text = result.stdout.strip()
    except Exception as e:
        print(f"  PDF extract failed: {e}", file=sys.stderr)
        log_phase(log_file, paper_id, 2, 'FAILED', f'pdftotext error: {str(e)[:100]}')
        return False

    if len(pdf_text) < 1000:
        log_phase(log_file, paper_id, 2, 'FAILED',
                  f'insufficient PDF text ({len(pdf_text)} chars) — skipping per abstract-only rule')
        return False

    mode = 'full_text'
    print(f"  PDF text: {len(pdf_text)} chars, mode={mode}")

    # Step 2: Build prompt
    metadata_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.metadata.json')
    paper_data = dict(paper)
    if os.path.exists(metadata_path):
        with open(metadata_path) as f:
            paper_data.update(json.load(f))

    prompt = build_full_text_prompt(paper_data, config, pdf_text)

    # Step 3: Call LLM
    api_base = cfg(config, 'llm.api_base_url', 'http://localhost:8080/v1')
    model = cfg(config, 'llm.model', 'qwen3-235b-a22b')
    temperature = cfg(config, 'llm.temperature', 0.3)
    max_tokens = cfg(config, 'llm.max_tokens', 4000)
    timeout = cfg(config, 'llm.timeout_seconds', 120)
    api_key_env = cfg(config, 'llm.api_key_env', 'LLM_API_KEY')
    api_key = os.environ.get(api_key_env, '')

    if not api_key:
        print(f"  Warning: {api_key_env} not set, trying without auth", file=sys.stderr)

    body = json.dumps({
        'model': model,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [
            {'role': 'system', 'content': prompt['system_prompt']},
            {'role': 'user', 'content': prompt['user_prompt']},
        ],
    }).encode('utf-8')

    url = f"{api_base.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    })

    print(f"  Calling LLM: {model} ({url})...")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        resp_data = json.loads(resp.read().decode('utf-8'))
        content = resp_data['choices'][0]['message']['content']
    except Exception as e:
        print(f"  LLM call failed: {e}", file=sys.stderr)
        log_phase(log_file, paper_id, 2, 'FAILED', f'LLM error: {str(e)[:100]}')
        return False

    # Step 4: Save outputs
    md_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.interpret.md')
    with open(md_path, 'w') as f:
        f.write(content)

    json_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.interpret.json')
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
        'content': content,
        'tags': tag_data.get('tag_ids', []),
        'tag_labels': tag_data.get('matched_labels', []),
        'mode': mode,
        'interpreted_at': datetime.now().isoformat(),
    }
    with open(json_path, 'w') as f:
        json.dump(interpret_json, f, ensure_ascii=False, indent=2)

    conn = get_conn(config)
    mark_interpreted(conn, paper_id)
    log_phase(log_file, paper_id, 2, 'COMPLETED', f'{mode}, {len(tag_data.get("tag_ids", []))} tags')
    return True


def run_phase3(paper_dir, paper, config, log_file):
    paper_id = paper['paper_id']

    md_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.interpret.md')
    if not os.path.exists(md_path):
        log_phase(log_file, paper_id, 3, 'FAILED', 'no .interpret.md file')
        return False

    html_path = os.path.join(REPO_ROOT, paper_dir, f'{paper_id}.interpret.html')
    script = os.path.join(SKILL_DIR, 'md_to_html.py')

    try:
        result = subprocess.run(
            ['python3', script, '--input', md_path, '--output', html_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  md_to_html error: {result.stderr}", file=sys.stderr)
            log_phase(log_file, paper_id, 3, 'FAILED', result.stderr.strip()[:100])
            return False
    except Exception as e:
        log_phase(log_file, paper_id, 3, 'FAILED', str(e)[:100])
        return False

    log_phase(log_file, paper_id, 3, 'COMPLETED', f'HTML saved: {html_path}')
    return True


def process_paper(paper, config, phases, log_file):
    paper_id = paper['paper_id']
    paper_dir = paper.get('dir_name', '') or get_paper_dir(get_conn(config), paper_id) or ''
    if not paper_dir:
        print(f"  No dir_name for {paper_id}, skipping", file=sys.stderr)
        return

    print(f"\n{'='*60}")
    print(f"Paper: {paper_id}")
    print(f"Title: {(paper.get('title', '') or '')[:80]}")
    print(f"Dir: {paper_dir}")

    for phase in [1, 2, 3]:
        if str(phase) not in phases:
            continue

        print(f"  Phase {phase}...", end=' ', flush=True)
        log_phase(log_file, paper_id, phase, 'START')

        if phase == 1:
            ok = run_phase1(paper_dir, paper, config, log_file)
        elif phase == 2:
            ok = run_phase2(paper_dir, paper, config, log_file)
        elif phase == 3:
            ok = run_phase3(paper_dir, paper, config, log_file)

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
        if paper.get('status') != 'downloaded':
            print(f"Paper status is '{paper.get('status')}', expected 'downloaded'", file=sys.stderr)
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
    phases = set(phase_str.split(',')) if phase_str else {'1', '2', '3'}
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
                       help='Phases to run (1,2,3 or 1,2). Default: all three.')

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