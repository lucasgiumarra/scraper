"""
api_scraper.py — Bright Data Web Scraper API (dataset trigger/poll pattern).

Replaces the Selenium Scraping Browser used in market_scraper.py. Instead of
controlling a remote browser, you POST search URLs to Bright Data's pre-built
Amazon and Walmart collectors, poll until the snapshot is ready, then download
structured JSON. No HTML parsing needed.

How to set up
─────────────
1. In the Bright Data dashboard go to Web Scraper API (left nav).
2. Find the pre-built "Amazon Search" and "Walmart Search" collectors.
3. Copy each Dataset ID (shown in the URL or the collector's settings panel).
4. Get your API token: brightdata.com → top-right avatar → API Token.
5. Add to .env:

    BRD_TOKEN        = <your API token>
    AMZ_DATASET_ID   = <dataset ID for Amazon Search collector>
    WAL_DATASET_ID   = <dataset ID for Walmart Search collector>

Usage (mirrors market_scraper.py CLI):
    python api_scraper.py
    python api_scraper.py --amazon-only
    python api_scraper.py --walmart-only
    python api_scraper.py --category laptop
"""

import os
import re
import json
import argparse
from datetime import datetime

import requests
from dotenv import load_dotenv

from market_scraper import UnifiedMarketDB
from config import CATEGORIES

load_dotenv()

_API_BASE = "https://api.brightdata.com/datasets/v3"

# ── API helpers ───────────────────────────────────────────────────────────────


def _auth_headers() -> dict:
    token = os.getenv("BRD_TOKEN")
    if not token:
        raise EnvironmentError("BRD_TOKEN is not set in .env")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _scrape(dataset_id: str, urls: list[str]) -> list[dict]:
    """
    POST URLs to /datasets/v3/scrape and return results directly.
    notify=false makes this synchronous — Bright Data blocks until done.
    Each url entry can include optional zipcode/language fields per BRD docs.
    """
    inputs = [{"url": u} for u in urls]
    resp = requests.post(
        f"{_API_BASE}/scrape",
        params={"dataset_id": dataset_id, "notify": "false", "include_errors": "true"},
        headers=_auth_headers(),
        json={"input": inputs},
        timeout=120,
    )
    resp.raise_for_status()
    # Bright Data returns NDJSON (one JSON object per line), not a JSON array
    lines = [line for line in resp.text.strip().splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    # If it was a plain JSON array after all, unwrap it
    if len(records) == 1 and isinstance(records[0], list):
        return records[0]
    return records


# ── Scraper class ─────────────────────────────────────────────────────────────

class ApiScraper:
    """Scrape Amazon and Walmart via Bright Data Web Scraper API into UnifiedMarketDB."""

    def __init__(self):
        self.db = UnifiedMarketDB()
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.amz_dataset = os.getenv("AMZ_DATASET_ID")
        self.wal_dataset = os.getenv("WAL_DATASET_ID")

    # Duplicated from UnifiedScraper — kept local to avoid coupling

    def _clean_numeric(self, text) -> float:
        if not text or "N/A" in str(text):
            return 0.0
        cleaned = "".join(c for c in str(text) if c.isdigit() or c == ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    def _clean_product_data(self, raw_title: str) -> str:
        if not raw_title or raw_title == "Unknown Title":
            return "Unknown"
        for delim in [r"\|", r" - ", r" with ", r": ", r", "]:
            raw_title = re.split(delim, raw_title, flags=re.IGNORECASE)[0]
        noise = [
            r'\d+\.\d+"', r"\d+\s*inch", r"\d+\s*GB", r"\d+\s*TB", r"\d+\s*mm",
            "FHD", "Laptop", "RAM", "SSD", "Gaming", "Mechanical", "Wireless",
            "Wired", "RGB", "Backlit", "Keyboard", "Monitor", "Smartwatch",
            "Waterproof", "Typewriter", "Retro", "Hot Swappable", "Gasket",
            "75%", "108 Keys", "Amd", "Intel", "Core", "Nvidia",
        ]
        clean = re.split("|".join(noise), raw_title, flags=re.IGNORECASE)[0].strip(",-._ ")
        return clean if len(clean) >= 3 else " ".join(raw_title.split()[:3])

    # ── Amazon ────────────────────────────────────────────────────────────────

    def run_amazon(self, watchlist: dict[str, list[str]], floors: dict[str, float]):
        """watchlist = {category: [product_url, ...]}"""
        if not self.amz_dataset:
            raise EnvironmentError("AMZ_DATASET_ID is not set in .env")
        print("--- Starting Amazon Phase (Web Scraper API) ---")
        for category, urls in watchlist.items():
            floor = floors.get(category, 0)
            ceiling = floor * 30
            print(f"[Amazon] Scraping {len(urls)} product(s) in {category!r}…")
            try:
                records = _scrape(self.amz_dataset, urls)
                stored = self._store_amazon_records(records, category, floor, ceiling)
                print(f"[Amazon] Stored {stored} items for {category!r}")
            except Exception as e:
                print(f"[Amazon] Failed for {category!r}: {e}")

    def _store_amazon_records(self, records: list[dict], category: str, floor: float, ceiling: float) -> int:
        stored = 0
        for r in records:
            try:
                if r.get("error"):
                    print(f"  [Amazon] API error for record: {r.get('error')}")
                    continue

                asin = r.get("asin") or r.get("id")
                if not asin:
                    continue

                price = self._clean_numeric(
                    r.get("final_price") or r.get("price") or 0
                )
                if price < floor or price > ceiling:
                    continue

                raw_title = r.get("title") or r.get("name") or "Unknown Title"
                brand_model = self._clean_product_data(raw_title)
                reviews = int(self._clean_numeric(
                    r.get("reviews_count") or r.get("number_of_reviews") or 0
                ))

                self.db.upsert_item((
                    asin, "Amazon", self.today, category,
                    brand_model, price, reviews, "In Stock", raw_title,
                ))
                print(f"  Stored: [{asin}] {brand_model} | ${price}")
                stored += 1
            except Exception as e:
                print(f"  [Amazon] Record error: {e}")
        return stored

    # ── Walmart ───────────────────────────────────────────────────────────────

    def run_walmart(self, watchlist: dict[str, list[str]], floors: dict[str, float]):
        """watchlist = {category: [product_url, ...]}"""
        if not self.wal_dataset:
            raise EnvironmentError("WAL_DATASET_ID is not set in .env")
        print("--- Starting Walmart Phase (Web Scraper API) ---")
        for category, urls in watchlist.items():
            floor = floors.get(category, 0)
            ceiling = floor * 30
            print(f"[Walmart] Scraping {len(urls)} product(s) in {category!r}…")
            try:
                records = _scrape(self.wal_dataset, urls)
                stored = self._store_walmart_records(records, category, floor, ceiling)
                print(f"[Walmart] Stored {stored} items for {category!r}")
            except Exception as e:
                print(f"[Walmart] Failed for {category!r}: {e}")

    def _store_walmart_records(self, records: list[dict], category: str, floor: float, ceiling: float) -> int:
        stored = 0
        for r in records:
            try:
                item_id = str(
                    r.get("sku") or r.get("product_id") or r.get("item_id") or r.get("id") or ""
                )
                if not item_id:
                    continue

                price = self._clean_numeric(
                    r.get("final_price") or r.get("price") or 0
                )
                if price < floor or price > ceiling:
                    continue

                raw_title = r.get("product_name") or r.get("title") or r.get("name") or "Unknown Title"
                brand_model = self._clean_product_data(raw_title)
                reviews = int(self._clean_numeric(
                    r.get("review_count") or r.get("reviews_count") or 0
                ))
                availability = str(r.get("availability") or r.get("availability_text") or "Unknown")

                self.db.upsert_item((
                    item_id, "Walmart", self.today, category,
                    brand_model, price, reviews, availability, raw_title,
                ))
                print(f"  Stored: [{item_id}] {brand_model} | ${price}")
                stored += 1
            except Exception as e:
                print(f"  [Walmart] Record error: {e}")
        return stored

    # ── Reporting ─────────────────────────────────────────────────────────────

    def report_deals(self):
        print("\n" + "=" * 50)
        print("PRICE DROP ALERT")
        print("=" * 50)
        drops = self.db.get_price_drops()
        if not drops:
            print("No new drops below historical averages found.")
        else:
            for source, name, current, avg in drops:
                savings = avg - current
                print(f"[{source}] {name[:40]}")
                print(f"   NOW: ${current:.2f} | AVG: ${avg:.2f} | SAVE: ${savings:.2f}")
        print("=" * 50 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_watchlist(path: str, category: str | None) -> tuple[dict, dict]:
    """Load amazon/walmart URL dicts from a watchlist.json produced by discover.py."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    amz = data.get("amazon", {})
    wal = data.get("walmart", {})
    if category:
        amz = {category: amz.get(category, [])}
        wal = {category: wal.get(category, [])}
    return amz, wal


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bright Data Web Scraper API client")
    parser.add_argument("--amazon-only", action="store_true")
    parser.add_argument("--walmart-only", action="store_true")
    parser.add_argument("--category", metavar="NAME",
                        help=f"One of: {', '.join(CATEGORIES)}")
    parser.add_argument("--watchlist", metavar="FILE",
                        help="Path to watchlist.json from discover.py")
    args = parser.parse_args()

    if args.amazon_only and args.walmart_only:
        parser.error("Use only one of --amazon-only or --walmart-only")

    if args.watchlist:
        amz_wl, wal_wl = _load_watchlist(args.watchlist, args.category)
    else:
        parser.error("--watchlist is required. Run discover.py first to generate watchlist.json.")

    scraper = ApiScraper()

    if not args.walmart_only:
        scraper.run_amazon(amz_wl, CATEGORIES)
    if not args.amazon_only:
        scraper.run_walmart(wal_wl, CATEGORIES)

    # scraper.report_deals()
