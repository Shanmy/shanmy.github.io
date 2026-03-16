#!/bin/bash
# Fetch today's cs.CV papers from arxiv and commit the result.
set -e

cd "$(dirname "$0")"

pip install -q requests beautifulsoup4

python scripts/fetch_arxiv.py

git add personal/arxiv/
git diff --staged --quiet \
  || git commit -m "ArXiv cs.CV digest $(date -u +%Y-%m-%d)"
git push
