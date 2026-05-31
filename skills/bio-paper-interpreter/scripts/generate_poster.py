#!/usr/bin/env python3
"""
Zero-shot poster generation — English by default, optional bilingual output, no AutoFigure dependency.

Pipeline:
  One-shot SVG  (methodology_model → deepseek-v4-pro):
    1. {pid}.poster.en.svg   — text→SVG, English
    2. {pid}.poster.zh.svg   — text→SVG, Chinese
    3. {pid}.poster.en.png   — browser render of #1 (CJK-safe)
    4. {pid}.poster.zh.png   — browser render of #2 (CJK-safe)

  Direct PNG   (text→description→qwen-image-2.0-pro):
    5. {pid}.poster.direct.en.png  — English
    6. {pid}.poster.direct.zh.png  — Chinese

Usage:
  generate_poster.py --paper-dir /path/to/paper --paper-id ID --api-key KEY
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(SKILL_DIR, '..', 'references', 'prompts')

# --- Token usage tracking ---
_token_usage = {}
_enhancement_calls = 0
_enhancement_model = None


def ts_print(*args, file=None, end='\n', flush=False):
    """Print with [HH:MM:SS] timestamp prefix."""
    ts = datetime.now().strftime('[%H:%M:%S]')
    print(ts, *args, file=file, end=end, flush=flush)


def _record_usage(model, prompt_tokens, completion_tokens):
    if model not in _token_usage:
        _token_usage[model] = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
    _token_usage[model]['prompt_tokens'] += prompt_tokens
    _token_usage[model]['completion_tokens'] += completion_tokens
    _token_usage[model]['calls'] += 1


def _print_usage_summary():
    ts_print()
    ts_print("=" * 60)
    ts_print("Token Usage Summary")
    ts_print("=" * 60)
    grand = 0
    for model, u in sorted(_token_usage.items()):
        t = u['prompt_tokens'] + u['completion_tokens']
        grand += t
        ts_print(f"  [{model}]")
        ts_print(f"    Calls:            {u['calls']}")
        ts_print(f"    Prompt tokens:    {u['prompt_tokens']:,}")
        ts_print(f"    Completion tokens:{u['completion_tokens']:,}")
        ts_print(f"    Subtotal:         {t:,}")
    if _enhancement_calls > 0:
        ts_print(f"  [{_enhancement_model or 'enhancement'}]")
        ts_print(f"    Calls:            {_enhancement_calls}")
        ts_print(f"    (image generation — token count not available)")
    ts_print(f"  ---")
    ts_print(f"  Grand total (text models): {grand:,} tokens")
    if _enhancement_calls > 0:
        ts_print(f"  Enhancement calls: {_enhancement_calls}")
    ts_print("=" * 60)


# ── Text LLM call (streaming) ────────────────────────────────────────────────

def _call_text_llm(prompt, api_key, model, base_url):
    """Call a text-only LLM via OpenAI-compatible API with streaming progress."""
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.3,
        'max_tokens': 32000,
        'stream': True,
        'thinking': {'type': 'disabled'},
    }).encode('utf-8')
    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    })
    resp = urllib.request.urlopen(req, timeout=1200)

    chunks, stream_usage = [], None
    char_count, t_start, last_report = 0, time.time(), time.time()
    for line in resp:
        line_str = line.decode('utf-8').strip()
        if not line_str.startswith('data: '):
            continue
        data_str = line_str[6:]
        if data_str == '[DONE]':
            break
        try:
            data = json.loads(data_str)
            delta = data.get('choices', [{}])[0].get('delta', {})
            content = delta.get('content', '')
            if content:
                chunks.append(content)
                char_count += len(content)
                now = time.time()
                if now - last_report >= 5:
                    elapsed = now - t_start
                    ts_print(f'  [stream] {char_count:,} chars, {char_count/elapsed:.0f} chars/s, '
                          f'{elapsed:.0f}s elapsed')
                    last_report = now
            if 'usage' in data:
                stream_usage = data['usage']
        except json.JSONDecodeError:
            pass

    elapsed = time.time() - t_start
    full_text = ''.join(chunks)
    ts_print(f'  [stream] done: {char_count:,} chars in {elapsed:.1f}s '
          f'({char_count/elapsed:.0f} chars/s)')

    if stream_usage:
        _record_usage(model,
                      stream_usage.get('prompt_tokens', 0),
                      stream_usage.get('completion_tokens', 0))
    else:
        est_prompt = len(prompt) // 4
        est_completion = char_count // 3
        _record_usage(model, est_prompt, est_completion)
        ts_print(f'  [stream] usage estimated: ~{est_prompt:,}p + ~{est_completion:,}c')

    return full_text


# ── Image generation via DashScope ───────────────────────────────────────────

def _enhance_via_dashscope(output_path, prompt, api_key, model):
    """Generate an image from text prompt using DashScope multimodal-generation API."""
    import requests as _requests

    content = [{"text": prompt}]
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]}
    }
    ts_print(f"  [DashScope] Calling {model}...")
    resp = _requests.post(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, json=body, timeout=300)

    if resp.status_code != 200:
        ts_print(f"  [DashScope] API error: {resp.status_code} - {resp.text[:500]}")
        return None

    result = resp.json()
    try:
        image_url = result["output"]["choices"][0]["message"]["content"][0]["image"]
    except (KeyError, IndexError, TypeError):
        debug_path = output_path.replace(".png", "_dashscope_response.json")
        with open(debug_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        ts_print(f"  [DashScope] Unexpected response, debug: {debug_path}")
        return None

    ts_print(f"  [DashScope] Downloading image...")
    img_resp = _requests.get(image_url, timeout=60)
    if img_resp.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(img_resp.content)
        global _enhancement_calls, _enhancement_model
        _enhancement_calls += 1
        _enhancement_model = _enhancement_model or model
        usage = result.get("usage", {})
        ts_print(f"  [DashScope] {usage.get('width')}x{usage.get('height')}, "
              f"{usage.get('image_count', 1)} image(s)")
        return output_path

    ts_print(f"  [DashScope] Download failed: {img_resp.status_code}")
    return None


# ── SVG helpers ──────────────────────────────────────────────────────────────

def _extract_svg(text):
    s, e = text.find('<svg'), text.rfind('</svg>') + 6
    return text[s:e] if s != -1 and e != 5 else None


def _validate_svg(svg_code):
    try:
        import cairosvg
        cairosvg.svg2png(bytestring=svg_code.encode('utf-8'))
        return True, None
    except Exception as e:
        return False, str(e)[:200]


def _svg_to_png(svg_code, output_path):
    """Render SVG to PNG using headless Chrome (supports CJK fonts)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        ts_print(f"  playwright not available, trying cairosvg...")
        import cairosvg
        cairosvg.svg2png(bytestring=svg_code.encode('utf-8'), write_to=output_path)
        return

    # Parse viewBox from SVG for correct dimensions
    vb_match = re.search(r'viewBox=["\']([^"\']*)["\']', svg_code)
    if vb_match:
        parts = vb_match.group(1).split()
        if len(parts) >= 4:
            w, h = int(float(parts[2])), int(float(parts[3]))
        else:
            w, h = 1200, 800
    else:
        w, h = 1200, 800

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
        page.set_content(svg_code)
        page.wait_for_timeout(500)  # let fonts/text layout settle
        page.screenshot(path=output_path, full_page=True)
        browser.close()


def _repair_svg(svg_code, error_msg, api_key, model, base_url):
    """Standalone SVG repair via text LLM (no monkey-patching)."""
    prompt = f"""Fix the following SVG code that has a syntax error.

Error: {error_msg}

Rules:
- Output ONLY the repaired SVG code, starting with <svg> and ending with </svg>.
- Fix XML syntax: close all tags, quote all attributes, escape &<>.
- Do NOT change visual content, colors, layout, or text.

SVG to repair:
{svg_code}"""

    for attempt in range(3):
        ts_print(f"  [repair] Attempt {attempt + 1}/3...")
        try:
            repaired = _call_text_llm(prompt, api_key, model, base_url)
            if not repaired:
                continue
            fixed = _extract_svg(repaired)
            if not fixed:
                continue
            ok, err = _validate_svg(fixed)
            if ok:
                ts_print(f"  [repair] Fixed!")
                return fixed
            else:
                prompt = prompt.replace(f"Error: {error_msg}", f"Error: {err}")
        except Exception as e:
            ts_print(f"  [repair] Attempt {attempt + 1} failed: {e}")
            prompt = prompt.replace(f"Error: {error_msg}", f"Error: {e}")
    ts_print(f"  [repair] All attempts failed")
    return None


# ── Paper text loading ───────────────────────────────────────────────────────

def _read_text_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    if len(text) >= 500:
        ts_print(f"  Text source: {os.path.basename(path)} ({len(text):,} chars)")
        return text, os.path.basename(path)
    return None, None


def _read_paper_text(paper_dir, language='en', safe_pid=None):
    """Read language-specific paper content from reports, info, extracted text, or PDF."""
    if safe_pid and language == 'zh':
        candidates = [
            f'{safe_pid}.interpret.zh.md',
            f'{safe_pid}.brief.zh.md',
            f'{safe_pid}.info.md',
            f'{safe_pid}.pdf.txt',
        ]
    elif safe_pid:
        candidates = [
            f'{safe_pid}.interpret.md',
            f'{safe_pid}.brief.md',
            f'{safe_pid}.info.md',
            f'{safe_pid}.pdf.txt',
        ]
    elif language == 'zh':
        candidates = ['interpret.zh.md', 'brief.zh.md', 'info.md', 'pdf.txt']
    else:
        candidates = ['interpret.md', 'brief.md', 'info.md', 'pdf.txt']

    for candidate in candidates:
        path = os.path.join(paper_dir, candidate)
        if os.path.exists(path):
            text, source = _read_text_file(path)
            if text:
                return text, source

    suffixes = ['.interpret.zh.md', '.brief.zh.md'] if language == 'zh' else ['.interpret.md', '.brief.md']
    suffixes.extend(['.info.md', '.pdf.txt'])
    for suffix in suffixes:
        for item in os.listdir(paper_dir):
            if item.endswith(suffix):
                text, source = _read_text_file(os.path.join(paper_dir, item))
                if text:
                    return text, source

    for item in os.listdir(paper_dir):
        if item.endswith('.pdf'):
            pdf_path = os.path.join(paper_dir, item)
            try:
                import fitz
                doc = fitz.open(pdf_path)
                text = ''
                for page in doc:
                    text += page.get_text()
                doc.close()
                if len(text) >= 500:
                    ts_print(f"  Text source: {item} via PyMuPDF ({len(text):,} chars)")
                    return text, item
            except Exception as e:
                ts_print(f"  PDF extraction failed: {e}")

    return None, f"no usable {language} text source found"


def _build_paper_context(paper_text, source_name):
    return (
        "=== PAPER CONTEXT ===\n"
        f"Text source: {source_name}\n\n"
        f"{paper_text}\n"
        "=== END PAPER CONTEXT ==="
    )


def _load_prompt(name):
    prompt_path = os.path.join(PROMPT_DIR, f'{name}.yaml')
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    import yaml
    with open(prompt_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def _build_prompt(name, fields):
    prompt = _load_prompt(name)
    return f"{prompt['system']}\n\n{prompt['user_template'].format(**fields)}"


# ── Prompt builders ──────────────────────────────────────────────────────────

_SVG_PROMPT_EN = """You are an expert scientific illustrator. Based on the following research paper content, create a publication-quality SVG poster diagram that summarizes the paper's core methodology, workflow, and key findings.

The SVG should be a clear, visually appealing scientific figure suitable for a conference poster.

Requirements:
1. **Language**: ALL text labels, titles, annotations MUST be in English.
2. **Layout**: Use a logical flow — top-to-bottom or left-to-right — showing inputs → methods → outputs → key results.
3. **Colors**: Professional, harmonious palette (3-5 colors max). Light backgrounds for boxes, darker borders. Use gradients sparingly.
4. **Elements**: <rect rx="6">, <text>, <path>, <g>, <defs>/<marker> for arrowheads. Group related elements with <g>.
5. **Typography**: font-family="Arial, sans-serif". Bold for titles, normal for body. 12-16px body, 18-24px headings.
6. **Arrows**: Solid arrows (→) with marker-end connecting workflow steps.
7. **Canvas**: ~1000×700px, white background (#ffffff).
8. **Structure**: Title at top → main workflow in center → key results/metrics as callout boxes at bottom.
9. **CRITICAL**: Output ONLY valid SVG code. Start directly with <svg> and end with </svg>. No markdown fences, no explanation.

PAPER CONTENT:
{paper_text}"""

_SVG_PROMPT_ZH = """你是一位专业的科学插画师。请根据以下研究论文内容，创建一张适合会议海报的出版质量SVG图表，总结论文的核心方法、工作流程和关键发现。

要求：
1. **语言**：所有文字标签、标题、注释必须使用中文。
2. **布局**：逻辑清晰的流程——从上到下或从左到右——展示 输入→方法→输出→关键结果。
3. **配色**：专业和谐的配色（最多3-5种颜色）。方框浅色背景+深色边框。谨慎使用渐变。
4. **元素**：<rect rx="6">、<text>、<path>、<g>、<defs>/<marker>定义箭头。用<g>分组相关元素。
5. **字体**：font-family="Arial, sans-serif"。标题加粗，正文正常。正文12-16px，标题18-24px。
6. **箭头**：实线箭头（→）带marker-end连接流程步骤。
7. **画布**：~1000×700px，白色背景（#ffffff）。
8. **结构**：顶部标题→中间主流程→底部关键结果/指标卡片。
9. **关键**：只输出有效的SVG代码。直接以<svg>开头，以</svg>结尾。不要markdown围栏，不要解释。

论文内容：
{paper_text}"""

_DESC_PROMPT_EN = """You are a scientific figure designer. Read the paper excerpt below and create a detailed visual description for a single poster figure that summarizes the paper's core methodology and key findings.

Requirements:
- ALL text in the description and labels must be in English.
- Describe a SINGLE cohesive figure with clear panel layout (A, B, C...).
- Include specific data types, algorithm names, performance metrics from the paper.
- Specify color scheme, arrows, labels, chart types.
- Use the paper's ACTUAL methods and results — do NOT invent generic content.
- Under 500 words, output ONLY the visual description.

Paper Excerpt:
{paper_text}"""

_DESC_PROMPT_ZH = """你是一位科学图表设计师。阅读以下论文摘录，为一张海报图创建详细的视觉描述，总结论文的核心方法和关键发现。

要求：
- 描述和标签中的所有文字必须使用中文。
- 描述一张具有清晰面板布局（A、B、C……）的完整图表。
- 包含论文中具体的数据类型、算法名称、性能指标。
- 指定配色方案、箭头、标签、图表类型。
- 使用论文的真实方法和结果——不要编造通用内容。
- 500字以内，只输出视觉描述。

论文摘录：
{paper_text}"""


# ── Generators ───────────────────────────────────────────────────────────────

def generate_oneshot_svg(paper_text, language, api_key, model, base_url):
    """Zero-shot: paper text → SVG via text LLM.
    language: 'en' or 'zh'. Returns svg_code or None."""
    label = 'EN' if language == 'en' else 'ZH'
    prompt_template = _SVG_PROMPT_EN if language == 'en' else _SVG_PROMPT_ZH
    prompt = prompt_template.format(paper_text=paper_text[:15000])

    ts_print(f"\n{'─'*50}")
    ts_print(f"  One-shot SVG [{label}] — {model}")
    ts_print(f"  Prompt: {len(prompt):,} chars")
    ts_print(f"{'─'*50}")

    t0 = time.time()
    response = _call_text_llm(prompt, api_key, model, base_url)
    svg = _extract_svg(response)
    if not svg:
        ts_print(f"  ERROR: No SVG in response!")
        return None

    ts_print(f"  SVG: {len(svg):,} chars in {time.time()-t0:.1f}s")

    ok, err = _validate_svg(svg)
    if not ok:
        ts_print(f"  SVG syntax error: {err}")
        fixed = _repair_svg(svg, err, api_key, model, base_url)
        if fixed:
            svg = fixed
        else:
            ts_print(f"  Repair failed, using original (may not render as PNG)")
    else:
        ts_print(f"  SVG validates OK")

    return svg


def translate_svg_to_zh(svg_en, api_key, model, base_url):
    """Translate an English SVG poster into a Chinese SVG poster."""
    prompt = _build_prompt('svg_translate_zh', {'source_svg': svg_en})

    ts_print(f"\n{'─'*50}")
    ts_print(f"  SVG translation [EN→ZH] — {model}")
    ts_print(f"{'─'*50}")

    response = _call_text_llm(prompt, api_key, model, base_url)
    svg = _extract_svg(response)
    if not svg:
        ts_print("  ERROR: No SVG in translation response!")
        return None

    ok, err = _validate_svg(svg)
    if not ok:
        ts_print(f"  Translated SVG syntax error: {err}")
        fixed = _repair_svg(svg, err, api_key, model, base_url)
        if fixed:
            svg = fixed
        else:
            ts_print("  Repair failed, using translated SVG as-is (may not render as PNG)")
    else:
        ts_print("  Translated SVG validates OK")
    return svg


def generate_visual_description(paper_text, language, api_key, text_model, text_base):
    """Generate a direct-poster visual description."""
    label = 'EN' if language == 'en' else 'ZH'
    desc_template = _DESC_PROMPT_EN if language == 'en' else _DESC_PROMPT_ZH

    ts_print(f"\n{'─'*50}")
    ts_print(f"  Direct description [{label}] — {text_model}")
    ts_print(f"{'─'*50}")

    desc_prompt = desc_template.format(paper_text=paper_text[:15000])
    ts_print("  Generating figure description...")
    t0 = time.time()
    description = _call_text_llm(desc_prompt, api_key, text_model, text_base)
    ts_print(f"  Description: {len(description):,} chars in {time.time()-t0:.1f}s")
    return description


def translate_description_to_zh(paper_context, source_description, api_key, text_model, text_base):
    prompt = _build_prompt('poster_desc_translate_zh', {
        'paper_context': paper_context,
        'source_description': source_description,
    })
    ts_print(f"\n{'─'*50}")
    ts_print(f"  Direct description translation [EN→ZH] — {text_model}")
    ts_print(f"{'─'*50}")
    description = _call_text_llm(prompt, api_key, text_model, text_base)
    ts_print(f"  Chinese description: {len(description):,} chars")
    return description


def generate_direct_png_from_description(description, language, api_key, enh_model, output_path):
    """Generate a direct PNG from a prepared visual description."""
    ts_print("  Generating image from description...")
    if language == 'zh':
        enh_prompt = f"请根据以下描述创建一张专业科学海报图。图中所有文字必须是中文：\n\n{description}"
    else:
        enh_prompt = f"Create a professional scientific poster figure based on this description. All text in the image must be in English:\n\n{description}"
    return _enhance_via_dashscope(output_path, enh_prompt, api_key, enh_model)


def generate_direct_png(paper_text, language, api_key, text_model, text_base, enh_model, output_path):
    """Two-step: paper text → figure description → image."""
    description = generate_visual_description(paper_text, language, api_key, text_model, text_base)
    return generate_direct_png_from_description(description, language, api_key, enh_model, output_path)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Zero-shot poster generation — English by default, optional bilingual output',
    )
    parser.add_argument('--paper-dir', required=True, help='Paper directory')
    parser.add_argument('--paper-id', required=True, help='Paper identifier')
    parser.add_argument('--api-key', required=True, help='LLM API key')
    parser.add_argument('--methodology-model', default='deepseek-v4-pro')
    parser.add_argument('--methodology-base-url',
                        default='https://dashscope.aliyuncs.com/compatible-mode/v1')
    parser.add_argument('--enhancement-model', default='qwen-image-2.0-pro')
    parser.add_argument('--lang', default='en',
                        choices=['en', 'zh', 'both'],
                        help='Which language(s) to generate (default: en)')
    parser.add_argument('--trans', action='store_true',
                        help='Generate Chinese SVG/direct poster from English outputs')
    parser.add_argument('--en', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    safe_pid = re.sub(r'[/\\:*?"<>|]', '_', str(args.paper_id))[:200]
    api_key = args.api_key
    meth_model = args.methodology_model
    meth_base = args.methodology_base_url
    enh_model = args.enhancement_model
    lang = args.lang

    results = []
    requested_languages = ['en', 'zh'] if lang == 'both' else [lang]
    languages = list(requested_languages)
    if args.trans and 'zh' in languages and 'en' not in languages:
        languages.insert(0, 'en')

    text_cache = {}
    english_svg = None
    english_description = None
    english_context = None

    def get_text(language):
        if language not in text_cache:
            paper_text, source = _read_paper_text(args.paper_dir, language, safe_pid)
            if not paper_text:
                ts_print(f"Error: {source}", file=sys.stderr)
                sys.exit(1)
            text_cache[language] = (paper_text, source)
        return text_cache[language]

    def save_svg_and_png(svg, language):
        svg_path = os.path.join(args.paper_dir, f'{safe_pid}.poster.{language}.svg')
        with open(svg_path, 'w', encoding='utf-8') as f:
            f.write(svg)
        ts_print(f"  Saved: {svg_path} ({len(svg):,} chars)")
        results.append(svg_path)
        try:
            png_path = os.path.join(args.paper_dir, f'{safe_pid}.poster.{language}.png')
            _svg_to_png(svg, png_path)
            ts_print(f"  Rendered: {png_path} ({os.path.getsize(png_path):,} bytes)")
            results.append(png_path)
        except Exception as e:
            ts_print(f"  Render failed: {e}")

    def save_direct_png(description, language):
        direct_path = os.path.join(args.paper_dir, f'{safe_pid}.poster.direct.{language}.png')
        DIRECT_MAX_RETRIES = 3
        result = None
        for attempt in range(DIRECT_MAX_RETRIES):
            result = generate_direct_png_from_description(description, language, api_key, enh_model, direct_path)
            if result and os.path.exists(result):
                break
            if attempt < DIRECT_MAX_RETRIES - 1:
                ts_print(f"  Direct PNG [{language}] attempt {attempt+1} failed, retrying...")
        if result and os.path.exists(result):
            if result != direct_path:
                os.rename(result, direct_path)
            ts_print(f"  Saved: {direct_path} ({os.path.getsize(direct_path):,} bytes)")
            results.append(direct_path)
        elif result:
            ts_print(f"  Saved: {result}")
            results.append(result)
        else:
            ts_print(f"  FAILED: Direct generation ({language}) after {DIRECT_MAX_RETRIES} attempts")

    for lang in languages:
        if lang == 'en':
            paper_text, source = get_text('en')
            english_context = _build_paper_context(paper_text, source)
            english_svg = generate_oneshot_svg(paper_text, 'en', api_key, meth_model, meth_base)
            if english_svg:
                save_svg_and_png(english_svg, 'en')
            else:
                ts_print("  FAILED: SVG generation (en)")
            english_description = generate_visual_description(paper_text, 'en', api_key, meth_model, meth_base)
            save_direct_png(english_description, 'en')
            continue

        if args.trans:
            if english_svg is None:
                paper_text, source = get_text('en')
                english_context = _build_paper_context(paper_text, source)
                english_svg = generate_oneshot_svg(paper_text, 'en', api_key, meth_model, meth_base)
                if english_svg:
                    save_svg_and_png(english_svg, 'en')
                else:
                    ts_print("  FAILED: SVG generation (en)")
            if english_svg:
                svg = translate_svg_to_zh(english_svg, api_key, meth_model, meth_base)
                if svg:
                    save_svg_and_png(svg, 'zh')
                else:
                    ts_print("  FAILED: SVG translation (zh)")
            if english_description is None:
                paper_text, source = get_text('en')
                english_context = english_context or _build_paper_context(paper_text, source)
                english_description = generate_visual_description(paper_text, 'en', api_key, meth_model, meth_base)
            zh_description = translate_description_to_zh(english_context, english_description,
                                                         api_key, meth_model, meth_base)
            save_direct_png(zh_description, 'zh')
            continue

        paper_text, source = get_text('zh')
        svg = generate_oneshot_svg(paper_text, 'zh', api_key, meth_model, meth_base)
        if svg:
            save_svg_and_png(svg, 'zh')
        else:
            ts_print("  FAILED: SVG generation (zh)")
        description = generate_visual_description(paper_text, 'zh', api_key, meth_model, meth_base)
        save_direct_png(description, 'zh')

    _print_usage_summary()

    ts_print(f"\n{'='*60}")
    ts_print(f"Results: {len(results)} files generated")
    for p in results:
        ts_print(f"  {p}")
    ts_print(f"{'='*60}")


if __name__ == '__main__':
    main()