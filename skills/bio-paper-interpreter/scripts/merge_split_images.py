#!/usr/bin/env python3
"""
Detect and merge vertically-split figures extracted by PyMuPDF.

When pymupdf4llm extracts images from a PDF, figures that span multiple
internal drawing operations can be split into separate PNG fragments.
These fragments share the same PDF filename, page number, and pixel width.

This module detects such fragments and vertically concatenates them into
a single image. Original fragments are moved to images/raw/.
"""

import os
import re
import struct

from PIL import Image


def merge_split_images(image_dir):
    """Scan image_dir for split-figure fragments and merge them vertically.

    Groups PNG images by (pdf_name, page, width). Groups with 2+ images
    are considered split figures. Images in each group are sorted by their
    filename index (ascending, handles non-consecutive indices), vertically
    concatenated, and saved as {pdf_name}-{page:04d}-{first_idx:02d}.merge.png.
    Original fragments are moved to image_dir/raw/.

    Returns (merge_count, merged_names).
    """
    if not os.path.isdir(image_dir):
        return 0, []

    png_files = [
        f for f in os.listdir(image_dir)
        if f.endswith('.png') and not f.endswith('.merge.png') and os.path.isfile(os.path.join(image_dir, f))
    ]
    if len(png_files) < 2:
        return 0, []

    groups = {}
    for fname in png_files:
        parsed = _parse_filename(fname)
        if parsed is None:
            continue
        pdf_name, page, idx = parsed
        fpath = os.path.join(image_dir, fname)
        width, _height = _get_image_size(fpath)
        if width is None or width < 100:
            continue
        key = (pdf_name, page, width)
        if key not in groups:
            groups[key] = []
        groups[key].append((idx, fname, fpath))

    merge_count = 0
    merged_names = []

    for (pdf_name, page, width), fragments in groups.items():
        if len(fragments) < 2:
            continue

        fragments.sort(key=lambda x: x[0])

        images = []
        for _idx, _fname, fpath in fragments:
            img = Image.open(fpath)
            images.append(img)

        total_height = sum(img.height for img in images)
        merged = Image.new('RGBA', (width, total_height))
        y_offset = 0
        for img in images:
            merged.paste(img, (0, y_offset))
            y_offset += img.height
            img.close()

        first_idx = fragments[0][0]
        merged_name = f'{pdf_name}-{page:04d}-{first_idx:02d}.merge.png'
        merged_path = os.path.join(image_dir, merged_name)
        merged.save(merged_path, 'PNG')

        raw_dir = os.path.join(image_dir, 'raw')
        os.makedirs(raw_dir, exist_ok=True)
        for _idx, fname, fpath in fragments:
            os.rename(fpath, os.path.join(raw_dir, fname))

        merge_count += 1
        merged_names.append(merged_name)

    return merge_count, merged_names


def _parse_filename(filename):
    """Parse pymupdf4llm image filename: {pdf_name}-{page:04d}-{idx:02d}.png

    Returns (pdf_name, page_int, idx_int) or None.
    """
    m = re.match(r'^(.+)-(\d{4})-(\d{2})\.png$', filename)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def _get_image_size(filepath):
    """Return (width, height) by parsing PNG/JPEG header, or (None, None)."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(32)
            if len(header) < 24:
                return None, None
            if header[:8] == b'\x89PNG\r\n\x1a\n':
                width, height = struct.unpack('>II', header[16:24])
                return width, height
            elif header[0:2] == b'\xff\xd8':
                f.seek(2)
                while True:
                    chunk = f.read(4)
                    if len(chunk) < 4:
                        break
                    marker, length = struct.unpack('>HH', chunk)
                    if marker in (0xFFC0, 0xFFC2):
                        sof = f.read(5)
                        if len(sof) >= 5:
                            return struct.unpack('>H', sof[1:3])[0], struct.unpack('>H', sof[3:5])[0]
                    elif marker == 0xFFD9:
                        break
                    f.seek(length - 2, 1)
                return None, None
            else:
                return None, None
    except Exception:
        return None, None


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <image_dir>", file=sys.stderr)
        sys.exit(1)
    count, names = merge_split_images(sys.argv[1])
    if count > 0:
        print(f"Merged {count} split figures:")
        for n in names:
            print(f"  {n}")
    else:
        print("No split figures found.")