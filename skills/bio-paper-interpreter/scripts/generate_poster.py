#!/usr/bin/env python3
"""
Generate a poster SVG from a paper PDF using AutoFigure.

Uses AutoFigure's generate_from_paper() which extracts the paper's methodology
via LLM and iteratively refines a publication-ready SVG diagram.

Usage:
  generate_poster.py paper.pdf --output-dir /path/to/output --paper-id 2605.10876
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request


def _call_text_llm(prompt, api_key, model, base_url):
    """Call a text-only LLM via OpenAI-compatible API. Returns response text."""
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': 32000,
    }).encode('utf-8')
    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return resp['choices'][0]['message']['content']


def _patch_svg_repair(api_key, repair_model, repair_base_url):
    """Monkey-patch AutoFigure's SVG repair to use a text LLM instead of the vision model.

    Qwen VL models generate SVG with minor XML syntax errors. The vision model
    can't reliably fix its own mistakes. A text LLM (like deepseek-v4-pro)
    is much better at precise XML syntax repair.
    """
    import autofigure.generator as af_gen
    import cairosvg

    original_repair_svg = af_gen.repair_svg

    def patched_repair_svg(svg_code, error_message):
        if not repair_model:
            return original_repair_svg(svg_code, error_message)

        prompt = f"""
You are a professional SVG code debugging expert. The following SVG code has an error during parsing. Please fix it.

**Error Message:**
{error_message}

**Broken SVG Code:**
```xml
{svg_code}
```

**Requirements:**
1.  Carefully analyze the error message and the code to locate the issue.
2.  Fix syntax errors, such as unclosed tags, unescaped special characters, incorrect attributes, etc.
3.  Ensure the repaired code is well-formed XML.
4.  Do not change the visual content of the SVG; only perform syntax repairs.
5.  **Please output the complete, repaired SVG code directly, without any other explanation.**

**Strict Syntax Check:**
- All attribute values must be enclosed in double quotes.
- All tags must be properly closed; self-closing tags should end with "/>".
- Special characters must be escaped: & -> &amp;, < -> &lt;, > -> &gt;.
- Ensure tags are nested correctly with no syntax errors.
- Use only the basic SVG namespace: xmlns="http://www.w3.org/2000/svg"
- Avoid additional namespace declarations like xmlns:xlink or xmlns:xml
- Output format must be strictly: <svg>...</svg>
"""
        for attempt in range(2):
            print(f"  [text-repair] Attempt {attempt + 1}/2 with {repair_model}...")
            try:
                repaired = _call_text_llm(prompt, api_key, repair_model, repair_base_url)
                if not repaired:
                    continue

                svg_start = repaired.find('<svg')
                svg_end = repaired.rfind('</svg>') + 6
                if svg_start == -1 or svg_end == 5:
                    continue

                repaired_svg = repaired[svg_start:svg_end]
                processed = af_gen.preprocess_svg_for_cairo(repaired_svg)
                cairosvg.svg2png(bytestring=processed.encode('utf-8'))
                print(f"  [text-repair] SVG fixed and validated!")
                return processed
            except Exception as e:
                print(f"  [text-repair] Attempt {attempt + 1} failed: {e}")
                prompt = prompt.replace(
                    f"**Error Message:**\n{error_message}",
                    f"**Previous repair failed, new error message:**\n{e}"
                )

        print("  [text-repair] Text LLM repair failed, falling back to vision model...")
        return original_repair_svg(svg_code, error_message)

    af_gen.repair_svg = patched_repair_svg
    return original_repair_svg


def generate_poster(pdf_path, output_dir, paper_id, config):
    """Generate a poster SVG from a paper PDF using AutoFigure.

    Args:
        pdf_path: path to the paper PDF
        output_dir: directory to save the poster SVG
        paper_id: paper identifier for naming
        config: dict with keys:
            api_key, provider, model, base_url, max_iterations,
            enable_enhancement, methodology_model, methodology_base_url,
            enhancement_model, enhancement_provider

    Returns:
        {success: bool, svg_path: str|None, error: str|None}
    """
    from autofigure import AutoFigureAgent, Config

    api_key = config.get('api_key', '')
    if not api_key:
        return {'success': False, 'svg_path': None, 'error': 'no API key configured'}

    af_config = Config(
        generation_api_key=api_key,
        generation_provider=config.get('provider', 'openrouter'),
        generation_model=config.get('model'),
        generation_base_url=config.get('base_url', ''),
        output_dir=output_dir,
        methodology_model=config.get('methodology_model'),
        methodology_base_url=config.get('methodology_base_url'),
        enhancement_model=config.get('enhancement_model'),
        enhancement_provider=config.get('enhancement_provider', 'openrouter'),
    )

    agent = AutoFigureAgent(af_config)

    # Monkey-patch SVG repair to use text LLM for precise XML syntax fixing
    repair_model = config.get('repair_model')
    repair_base_url = config.get('repair_base_url') or config.get('base_url', '')
    _orig_repair = _patch_svg_repair(api_key, repair_model, repair_base_url)

    try:
        result = agent.generate_from_paper(
            paper_path=pdf_path,
            max_iterations=config.get('max_iterations', 5),
            output_format="svg",
            enable_enhancement=config.get('enable_enhancement', False),
            methodology_api_key=api_key,
            methodology_model=config.get('methodology_model'),
            methodology_base_url=config.get('methodology_base_url'),
        )
    finally:
        # Restore original repair function
        import autofigure.generator as af_gen
        af_gen.repair_svg = _orig_repair

    if result.success:
        safe_pid = re.sub(r'[/\\:*?"<>|]', '_', str(paper_id))[:200]
        poster_path = os.path.join(output_dir, f'{safe_pid}.poster.svg')
        if result.svg_path and os.path.exists(result.svg_path):
            if os.path.abspath(result.svg_path) != os.path.abspath(poster_path):
                shutil.move(result.svg_path, poster_path)
        return {'success': True, 'svg_path': poster_path, 'error': None}

    return {'success': False, 'svg_path': None, 'error': 'AutoFigure generation failed'}


def main():
    parser = argparse.ArgumentParser(
        description='Generate a poster SVG from a paper PDF using AutoFigure',
    )
    parser.add_argument('pdf_path', help='Path to the paper PDF')
    parser.add_argument('--output-dir', required=True, help='Output directory for the poster SVG')
    parser.add_argument('--paper-id', required=True, help='Paper identifier for naming')
    parser.add_argument('--api-key', default=None, help='API key (or set AUTOFIGURE_API_KEY env var)')
    parser.add_argument('--provider', default='openrouter',
                        choices=['openrouter', 'gemini', 'bianxie'])
    parser.add_argument('--model', default='google/gemini-3.1-pro-preview')
    parser.add_argument('--base-url', default='')
    parser.add_argument('--max-iterations', type=int, default=5)
    parser.add_argument('--enable-enhancement', action='store_true')
    parser.add_argument('--methodology-model', default=None)
    parser.add_argument('--methodology-base-url', default=None)
    parser.add_argument('--enhancement-model', default=None)
    parser.add_argument('--enhancement-provider', default='openrouter')
    parser.add_argument('--repair-model', default=None,
                        help='Text LLM for SVG XML repair (default: same as methodology-model)')
    parser.add_argument('--repair-base-url', default=None)
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get('AUTOFIGURE_API_KEY', '')
    if not api_key:
        print("Error: no API key. Set AUTOFIGURE_API_KEY env var or use --api-key.", file=sys.stderr)
        sys.exit(1)

    config = {
        'api_key': api_key,
        'provider': args.provider,
        'model': args.model,
        'base_url': args.base_url,
        'max_iterations': args.max_iterations,
        'enable_enhancement': args.enable_enhancement,
        'methodology_model': args.methodology_model,
        'methodology_base_url': args.methodology_base_url,
        'enhancement_model': args.enhancement_model,
        'enhancement_provider': args.enhancement_provider,
        'repair_model': args.repair_model,
        'repair_base_url': args.repair_base_url,
    }

    result = generate_poster(args.pdf_path, args.output_dir, args.paper_id, config)
    if result['success']:
        print(f"Poster saved: {result['svg_path']}")
    else:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()