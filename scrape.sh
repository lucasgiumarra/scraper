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
echo "Done."
