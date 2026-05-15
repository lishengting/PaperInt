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
import time
import urllib.request

# --- Token usage tracking ---
_token_usage = {}  # model -> {prompt_tokens, completion_tokens, calls}
_enhancement_calls = 0
_enhancement_model = None

def _record_usage(model, prompt_tokens, completion_tokens):
    if model not in _token_usage:
        _token_usage[model] = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
    _token_usage[model]['prompt_tokens'] += prompt_tokens
    _token_usage[model]['completion_tokens'] += completion_tokens
    _token_usage[model]['calls'] += 1

def _print_usage_summary():
    print()
    print("=" * 60)
    print("Token Usage Summary")
    print("=" * 60)
    grand = 0
    for model, u in sorted(_token_usage.items()):
        t = u['prompt_tokens'] + u['completion_tokens']
        grand += t
        print(f"  [{model}]")
        print(f"    Calls:            {u['calls']}")
        print(f"    Prompt tokens:    {u['prompt_tokens']:,}")
        print(f"    Completion tokens:{u['completion_tokens']:,}")
        print(f"    Subtotal:         {t:,}")
    if _enhancement_calls > 0:
        print(f"  [{_enhancement_model or 'enhancement'}]")
        print(f"    Calls:            {_enhancement_calls}")
        print(f"    (image generation — token count not available)")
    print(f"  ---")
    print(f"  Grand total (text models): {grand:,} tokens")
    if _enhancement_calls > 0:
        print(f"  Enhancement calls: {_enhancement_calls}")
    print("=" * 60)

def _patch_token_tracking():
    """Monkey-patch AutoFigure internals to track token usage across all models."""
    import autofigure.generator as af_gen
    import autofigure.utils.llm_client as af_llm

    # --- Patch _call_openai_compatible (generation + evaluation model) ---
    _orig_openai_call = af_gen._call_openai_compatible

    def _patched_openai_call(contents, api_key=None, model=None, base_url=None):
        from openai import OpenAI
        import io as _io, base64 as _b64
        from PIL import Image as _Image

        client = OpenAI(base_url=base_url, api_key=api_key)
        message_content = []
        for part in contents:
            if isinstance(part, str):
                message_content.append({"type": "text", "text": part})
            elif isinstance(part, _Image.Image):
                buf = _io.BytesIO()
                part.save(buf, format='PNG')
                image_b64 = _b64.b64encode(buf.getvalue()).decode('utf-8')
                message_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                })

        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": message_content}]
        )
        if completion and completion.usage:
            _record_usage(model or 'unknown',
                         completion.usage.prompt_tokens,
                         completion.usage.completion_tokens)
        return completion.choices[0].message.content if completion and completion.choices else None

    af_gen._call_openai_compatible = _patched_openai_call

    # --- Patch LLMClient.call (methodology model) ---
    _orig_llm_call = af_llm.LLMClient.call

    def _patched_llm_call(self, contents, temperature=0.7, max_tokens=None):
        from openai import OpenAI
        import io as _io, base64 as _b64
        from PIL import Image as _Image

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        message_content = []
        for part in contents:
            if isinstance(part, str):
                message_content.append({"type": "text", "text": part})
            elif isinstance(part, _Image.Image):
                buf = _io.BytesIO()
                part.save(buf, format='PNG')
                image_b64 = _b64.b64encode(buf.getvalue()).decode('utf-8')
                message_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                })

        kwargs = {"model": self.model, "messages": [{"role": "user", "content": message_content}],
                  "temperature": temperature}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        completion = client.chat.completions.create(**kwargs)
        if completion and completion.usage:
            _record_usage(self.model or 'unknown',
                         completion.usage.prompt_tokens,
                         completion.usage.completion_tokens)
        return completion.choices[0].message.content if completion and completion.choices else None

    af_llm.LLMClient.call = _patched_llm_call

    # --- Patch ImageEnhancer.enhance (enhancement model) ---
    import autofigure.enhancer as af_enhancer
    _orig_enhance = af_enhancer.ImageEnhancer.enhance

    def _patched_enhance(self, input_path, output_path=None, enhancement_input="",
                        style=None, input_type="code2prompt"):
        global _enhancement_calls, _enhancement_model
        _enhancement_model = _enhancement_model or self.config.enhancement_model
        _enhancement_calls += 1
        return _orig_enhance(self, input_path, output_path, enhancement_input,
                            style, input_type)

    af_enhancer.ImageEnhancer.enhance = _patched_enhance

    return _orig_openai_call, _orig_llm, _orig_enhance


def _call_text_llm(prompt, api_key, model, base_url):
    """Call a text-only LLM via OpenAI-compatible API with streaming progress."""
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
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

    chunks = []
    char_count = 0
    t_start = time.time()
    last_report = t_start
    stream_usage = None
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
                if now - last_report >= 3:
                    elapsed = now - t_start
                    print(f'  [stream] {len(chunks)} chunks, {char_count} chars, '
                          f'{char_count/elapsed:.0f} chars/s, {elapsed:.0f}s elapsed')
                    last_report = now
            # Capture usage from final chunk if present
            if 'usage' in data:
                stream_usage = data['usage']
        except json.JSONDecodeError:
            pass

    elapsed = time.time() - t_start
    full_text = ''.join(chunks)
    print(f'  [stream] done: {len(chunks)} chunks, {char_count} chars in {elapsed:.1f}s '
          f'({char_count/elapsed:.0f} chars/s)')

    # Record token usage
    if stream_usage:
        _record_usage(model,
                     stream_usage.get('prompt_tokens', 0),
                     stream_usage.get('completion_tokens', 0))
    else:
        # Estimate: ~3 chars per token for Chinese/English mix, prompt ~4 chars/token
        est_prompt = len(prompt) // 4
        est_completion = char_count // 3
        _record_usage(model, est_prompt, est_completion)
        print(f'  [stream] usage not in response, estimated: ~{est_prompt:,} prompt + ~{est_completion:,} completion')

    return full_text


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
        for attempt in range(3):
            print(f"  [text-repair] Attempt {attempt + 1}/3 with {repair_model}...")
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
        enhancement_api_key=api_key,
        enhancement_model=config.get('enhancement_model'),
        enhancement_provider=config.get('enhancement_provider', 'openrouter'),
        enhancement_base_url=config.get('enhancement_base_url', ''),
    )

    agent = AutoFigureAgent(af_config)

    # Monkey-patch SVG repair to use text LLM for precise XML syntax fixing
    repair_model = config.get('repair_model')
    repair_base_url = config.get('repair_base_url') or config.get('base_url', '')
    _orig_repair = _patch_svg_repair(api_key, repair_model, repair_base_url)

    # Monkey-patch AutoFigure internals to track token usage across all models
    _orig_openai, _orig_llm, _orig_enhance = _patch_token_tracking()

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
        # Restore original functions
        import autofigure.generator as af_gen
        import autofigure.utils.llm_client as af_llm
        import autofigure.enhancer as af_enhancer
        af_gen.repair_svg = _orig_repair
        af_gen._call_openai_compatible = _orig_openai
        af_llm.LLMClient.call = _orig_llm
        af_enhancer.ImageEnhancer.enhance = _orig_enhance

    _print_usage_summary()

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
    parser.add_argument('--enhancement-base-url', default=None)
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
        'enhancement_base_url': args.enhancement_base_url or '',
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