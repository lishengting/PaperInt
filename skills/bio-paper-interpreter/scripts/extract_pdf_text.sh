#!/usr/bin/env bash
# Extract plain text from a PDF file using pdftotext (poppler-utils).
# Outputs normalized plain text on stdout, truncated to max_chars.
set -euo pipefail

MAX_CHARS=100000
PDF_FILE=""

usage() {
    cat <<'EOF'
Usage: extract_pdf_text.sh <pdf_file> [--max-chars N]

Extract plain text from a PDF using pdftotext. Outputs normalized text
on stdout, truncated to --max-chars (default 100000).

Options:
  --max-chars N   Maximum characters to output (default 100000)
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-chars)
            MAX_CHARS="$2"
            shift 2
            ;;
        --help|-h)
            usage
            ;;
        *)
            if [[ -z "$PDF_FILE" ]]; then
                PDF_FILE="$1"
                shift
            else
                echo "Error: unexpected argument: $1" >&2
                usage
            fi
            ;;
    esac
done

if [[ -z "$PDF_FILE" ]]; then
    echo "Error: no PDF file specified" >&2
    usage
fi

if [[ ! -f "$PDF_FILE" ]]; then
    echo "Error: file not found: $PDF_FILE" >&2
    exit 1
fi

if ! command -v pdftotext &>/dev/null; then
    echo "Error: pdftotext not found. Install poppler-utils." >&2
    exit 1
fi

TEMP_TXT=$(mktemp /tmp/pdf_extract_XXXXXX.txt)
trap 'rm -f "$TEMP_TXT"' EXIT

# Extract text with layout preservation, no page breaks
pdftotext -layout -nopgbrk "$PDF_FILE" "$TEMP_TXT" 2>/dev/null || {
    echo "Warning: pdftotext returned non-zero for $PDF_FILE" >&2
}

if [[ ! -s "$TEMP_TXT" ]]; then
    echo "Warning: no text extracted from $PDF_FILE" >&2
    exit 0
fi

# Normalize whitespace: collapse multiple spaces/newlines, trim
tr '\n\r' ' ' < "$TEMP_TXT" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//' | \
    head -c "$MAX_CHARS"

echo  # trailing newline
