#!/usr/bin/env python3
"""
Generate a poster SVG from a paper PDF using AutoFigure.

Uses AutoFigure's generate_from_paper() which extracts the paper's methodology
via LLM and iteratively refines a publication-ready SVG diagram.

Usage:
  generate_poster.py paper.pdf --output-dir /path/to/output --paper-id 2605.10876
"""

import argparse
import os
import shutil
import sys


def generate_poster(pdf_path, output_dir, paper_id, config):
    """Generate a poster SVG from a paper PDF using AutoFigure.

    Args:
        pdf_path: path to the paper PDF
        output_dir: directory to save the poster SVG
        paper_id: paper identifier for naming
        config: dict with keys: api_key, provider, model, base_url,
               max_iterations, enable_enhancement

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
        generation_model=config.get('model', 'google/gemini-3.1-pro-preview'),
        generation_base_url=config.get('base_url', ''),
        output_dir=output_dir,
    )

    agent = AutoFigureAgent(af_config)
    result = agent.generate_from_paper(
        paper_path=pdf_path,
        max_iterations=config.get('max_iterations', 5),
        output_format="svg",
        enable_enhancement=config.get('enable_enhancement', False),
        topic="paper",
    )

    if result.success:
        poster_path = os.path.join(output_dir, f'{paper_id}.poster.svg')
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
    }

    result = generate_poster(args.pdf_path, args.output_dir, args.paper_id, config)
    if result['success']:
        print(f"Poster saved: {result['svg_path']}")
    else:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()