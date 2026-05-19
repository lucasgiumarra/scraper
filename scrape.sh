#!/bin/bash
# scrape.sh — discover product URLs then scrape prices
#
# Usage:
#   ./scrape.sh                        # all categories, Amazon + Walmart
#   ./scrape.sh --category laptop
#   ./scrape.sh --amazon-only
#   ./scrape.sh --walmart-only
#   ./scrape.sh --category laptop --amazon-only

set -e

VENV="$(dirname "$0")/.venv/bin/activate"
WATCHLIST="watchlist.json"

source "$VENV"

echo "=== Step 1: Discovery ==="
python discover.py "$@" --out "$WATCHLIST"

echo ""
echo "=== Step 2: Scraping ==="
python api_scraper.py --watchlist "$WATCHLIST" "$@"

echo ""
echo "=== Step 3: S3 Backup ==="
python s3_backup.py || echo "  S3 backup skipped (check S3_BUCKET / AWS credentials in .env)"

echo ""
echo "Done."
