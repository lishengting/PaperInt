#!/usr/bin/env python3
"""
Extract PDF content to Markdown via pymupdf4llm (primary) or pdftotext (fallback).

pymupdf4llm preserves document structure (headings, lists, tables) and can extract
embedded images. When images are extracted, the "most representative" one (largest
figure on early pages) is selected for inclusion in the final HTML report.

Usage:
  extract_pdf.py paper.pdf --max-chars 100000 --image-path images/ --json
  extract_pdf.py paper.pdf --extractor pdftotext --json   # force fallback
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys

from merge_split_images import merge_split_images


def extract_with_pymupdf4llm(pdf_path, max_chars, image_path):
    """Extract PDF to Markdown using pymupdf4llm, save images, select representative.

    Returns dict with: markdown, images_dir, representative_image, extractor,
    image_count, error.
    """
    import fitz

    doc = None
    try:
        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            return _empty_result('pymupdf4llm', error='PDF has no pages')

        os.makedirs(image_path, exist_ok=True)

        import pymupdf4llm
        markdown = pymupdf4llm.to_markdown(
            doc,
            write_images=True,
            image_path=image_path,
            image_format="png",
            force_text=True,
        )

        markdown = markdown[:max_chars]

        merge_count, merged_names = merge_split_images(image_path)
        if merge_count > 0:
            print(f"  Merged {merge_count} split figure(s): {', '.join(merged_names)}", file=sys.stderr)

        images_dir = os.path.basename(image_path.rstrip('/')) + '/'
        rep_image_rel = select_representative_image(image_path, images_dir)

        image_files = _list_image_files(image_path)
        image_count = len(image_files)

        return {
            'markdown': markdown,
            'images_dir': images_dir,
            'representative_image': rep_image_rel,
            'extractor': 'pymupdf4llm',
            'image_count': image_count,
            'error': None,
        }

    except Exception as e:
        return _empty_result('pymupdf4llm', error=str(e)[:200])
    finally:
        if doc is not None:
            doc.close()


def extract_with_pdftotext(pdf_path, max_chars):
    """Fallback: extract plain text via pdftotext (poppler-utils)."""
    if not os.path.exists(pdf_path):
        return _empty_result('pdftotext_fallback', error='PDF file not found')

    try:
        result = subprocess.run(
            ['pdftotext', '-layout', '-nopgbrk', pdf_path, '-'],
            capture_output=True, text=True, timeout=60,
        )
        text = result.stdout
        # Normalize whitespace (same logic as the old extract_pdf_text.sh)
        text = text.replace('\n', ' ').replace('\r', ' ')
        text = ' '.join(text.split())
        text = text.strip()
        text = text[:max_chars]

        return {
            'markdown': text,
            'images_dir': None,
            'representative_image': None,
            'extractor': 'pdftotext_fallback',
            'image_count': 0,
            'error': None,
        }
    except Exception as e:
        return _empty_result('pdftotext_fallback', error=str(e)[:200])


def select_representative_image(image_dir, images_rel_prefix, min_area=40000):
    """Select the most representative image: largest figure on earliest page.

    Sorts candidates by floor(log(area)) descending (prefer larger images),
    then by page number ascending (prefer earlier pages).
    Returns relative path like "images/paper.pdf-0003-05.png" or None.
    """
    image_files = _list_image_files(image_dir)
    if not image_files:
        return None

    candidates = []
    for fname in image_files:
        fpath = os.path.join(image_dir, fname)
        page = _parse_page_from_filename(fname)
        width, height = _get_image_size(fpath)
        if width is None:
            continue

        area = width * height
        if area < min_area:
            continue
        if width < 100 or height < 100:
            continue
        if page == 0 and area < 100000:
            continue  # title page logos are usually small

        candidates.append({
            'filename': fname,
            'page': page,
            'area': area,
            'width': width,
            'height': height,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-round(math.log10(c['area'])), c['page']))
    return images_rel_prefix + candidates[0]['filename']


def _parse_page_from_filename(filename):
    """Parse page number from pymupdf4llm image filename pattern: *-{page:04d}-*.png"""
    m = re.search(r'-(\d{4})-', filename)
    if m:
        return int(m.group(1)) - 1  # pymupdf4llm pages are 1-based
    return 99  # fallback: treat as very late page


def _get_image_size(filepath):
    """Return (width, height) by parsing the image header, or (None, None) on failure.

    Supports PNG (IHDR chunk) and JPEG (SOF marker). No external dependencies.
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(32)
            if len(header) < 24:
                return None, None
            if header[:8] == b'\x89PNG\r\n\x1a\n':
                # PNG: IHDR chunk at offset 16 (after 8-byte sig + 4-byte len + 4-byte type)
                import struct
                width, height = struct.unpack('>II', header[16:24])
                return width, height
            elif header[0:2] == b'\xff\xd8':
                # JPEG: scan for SOF0/SOF2 marker
                import struct
                f.seek(2)
                while True:
                    chunk = f.read(4)
                    if len(chunk) < 4:
                        break
                    marker, length = struct.unpack('>HH', chunk)
                    if marker in (0xFFC0, 0xFFC2):  # SOF0, SOF2
                        sof = f.read(5)
                        if len(sof) >= 5:
                            return struct.unpack('>H', sof[1:3])[0], struct.unpack('>H', sof[3:5])[0]
                    elif marker == 0xFFD9:  # EOI
                        break
                    f.seek(length - 2, 1)  # skip segment
                return None, None
            else:
                return None, None
    except Exception:
        return None, None


def _list_image_files(directory):
    """List image files (*.png, *.jpg, *.jpeg) in a directory."""
    if not os.path.isdir(directory):
        return []
    exts = {'.png', '.jpg', '.jpeg'}
    return sorted(
        f for f in os.listdir(directory)
        if os.path.splitext(f)[1].lower() in exts
    )


def _empty_result(extractor, error=None):
    return {
        'markdown': '',
        'images_dir': None,
        'representative_image': None,
        'extractor': extractor,
        'image_count': 0,
        'error': error,
    }


def extract_pdf(pdf_path, max_chars=100000, image_path=None, mode='auto'):
    """Top-level dispatcher.

    Args:
        pdf_path: path to the PDF file
        max_chars: maximum characters in the extracted markdown/text
        image_path: directory to save extracted images (only with pymupdf4llm)
        mode: 'auto' (try pymupdf4llm, fallback pdftotext),
              'pymupdf4llm', or 'pdftotext'

    Returns the structured result dict.
    """
    if mode == 'pdftotext':
        return extract_with_pdftotext(pdf_path, max_chars)

    if mode == 'pymupdf4llm':
        if not image_path:
            image_path = os.path.join(os.path.dirname(pdf_path), 'images')
        return extract_with_pymupdf4llm(pdf_path, max_chars, image_path)

    # auto mode: try pymupdf4llm first, fall back to pdftotext
    fallback_image_path = image_path or os.path.join(os.path.dirname(pdf_path), 'images')
    result = extract_with_pymupdf4llm(pdf_path, max_chars, fallback_image_path)
    if result['error']:
        print(f"  pymupdf4llm failed ({result['error']}), falling back to pdftotext", file=sys.stderr)
        fb = extract_with_pdftotext(pdf_path, max_chars)
        fb['error'] = f"pymupdf4llm: {result['error']}; pdftotext: {fb.get('error') or 'ok'}"
        return fb
    return result


def main():
    parser = argparse.ArgumentParser(
        description='Extract PDF content to Markdown via pymupdf4llm (primary) or pdftotext (fallback)',
    )
    parser.add_argument('pdf_path', help='Path to the PDF file')
    parser.add_argument('--max-chars', type=int, default=100000,
                        help='Maximum characters in extracted text (default: 100000)')
    parser.add_argument('--image-path', default=None,
                        help='Directory to save extracted images (default: adjacent images/ dir)')
    parser.add_argument('--extractor', choices=['auto', 'pymupdf4llm', 'pdftotext'],
                        default='auto', help='Extraction mode (default: auto)')
    parser.add_argument('--json', action='store_true',
                        help='Output structured JSON instead of plain markdown')
    args = parser.parse_args()

    if not os.path.exists(args.pdf_path):
        print(f"Error: PDF not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    image_path = args.image_path
    if not image_path and args.extractor != 'pdftotext':
        image_path = os.path.join(os.path.dirname(args.pdf_path), 'images')

    result = extract_pdf(
        args.pdf_path,
        max_chars=args.max_chars,
        image_path=image_path,
        mode=args.extractor,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result['markdown'])


if __name__ == '__main__':
    main()