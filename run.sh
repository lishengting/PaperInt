#!/bin/bash
. venv/bin/activate

python3 skills/bio-paper-search/scripts/paper_cli.py search -k "microbiome" --days 2
python3 skills/bio-paper-downloader/scripts/paper_cli.py --cns 
python3 skills/bio-paper-interpreter/scripts/paper_cli.py --cns 
python3 skills/bio-paper-db-viewer/scripts/paper_cli.py list --cns

# python3 skills/bio-paper-db-viewer/scripts/paper_web.py --host 0.0.0.0 --port 8765

