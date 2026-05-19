"""
discover.py — search-based URL discovery via Bright Data Scraping Browser.

Runs Amazon/Walmart search pages (same Selenium approach as market_scraper.py)
and writes found product URLs to watchlist.json for api_scraper.py to consume.

Usage:
    python discover.py                          # all categories
    python discover.py --category laptop        # one category
    python discover.py --amazon-only
    python discover.py --walmart-only
    python discover.py --out my_watchlist.json  # custom output path
"""

import json
import os
import random
import time
import argparse
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from selenium.webdriver import Remote, ChromeOptions as Options
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection as Connection

from market_scraper import (
    _brd_fetch_page_html,
    _driver_fatal_exception,
    get_product_links_from_search_page,
)
from config import CATEGORIES

load_dotenv()

SCRAPE_CONFIG = CATEGORIES


def _get_brd_driver():
    """Open a new Bright Data Scraping Browser session."""
    auth = os.getenv("WAL_AUTH")
    if not auth:
        raise ValueError("WAL_AUTH is not set in .env")
    connection = Connection(f"https://{auth}@brd.superproxy.io:9515", "goog", "chrome")
    driver = Remote(connection, options=Options())
    driver.set_page_load_timeout(75)
    driver.set_script_timeout(75)
    return driver


def _refresh_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass
    return _get_brd_driver()


def _clean_numeric(text) -> float:
    if not text or "N/A" in str(text):
        return 0.0
    cleaned = "".join(c for c in str(text) if c.isdigit() or c == ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _extract_amazon_urls(html: str, floor: float) -> list[str]:
    """Return /dp/<ASIN> URLs for non-sponsored products above the price floor."""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select('div[data-component-type="s-search-result"]')
    if not items:
        items = [t for t in soup.select("div[data-asin]") if t.get("data-asin")]

    urls = []
    for item in items:
        try:
            if item.select_one(".puis-sponsored-label-text"):
                continue
            if "sponsored" in item.get_text(" ", strip=True).lower()[:80]:
                continue
            asin = item.get("data-asin")
            if not asin:
                continue
            price = 0.0
            offscreen = item.select_one("span.a-price span.a-offscreen")
            if offscreen:
                price = _clean_numeric(offscreen.get_text())
            else:
                whole = item.select_one(".a-price-whole")
                frac = item.select_one(".a-price-fraction")
                if whole and frac:
                    price = _clean_numeric(
                        f"{whole.get_text(strip=True).replace(',','').strip('.')}"
                        f".{frac.get_text(strip=True)}"
                    )
            if price < floor:
                continue
            urls.append(f"https://www.amazon.com/dp/{asin}")
        except Exception:
            continue
    return urls


MAX_ITEMS = 100
MAX_EMPTY = 3


def discover_amazon(config: dict[str, float]) -> dict[str, list[str]]:
    """Search Amazon across pages until MAX_ITEMS URLs or MAX_EMPTY empty pages."""
    result: dict[str, list[str]] = {}
    driver = None
    try:
        driver = _get_brd_driver()
        wait = WebDriverWait(driver, 60)
        for category, floor in config.items():
            print(f"[Amazon] Searching {category!r}…")
            urls: list[str] = []
            seen: set[str] = set()
            empty_streak = 0
            page = 1
            while len(urls) < MAX_ITEMS and empty_streak < MAX_EMPTY:
                search_url = f"https://www.amazon.com/s?k={quote_plus(category)}&page={page}"
                try:
                    driver.get(search_url)
                    try:
                        wait.until(EC.any_of(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'div[data-component-type="s-search-result"]')
                            ),
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-asin]")),
                            EC.presence_of_element_located((By.ID, "nav-logo-sprites")),
                        ))
                    except TimeoutException:
                        pass
                    time.sleep(random.uniform(2, 4))
                    html = _brd_fetch_page_html(driver)
                    new = [u for u in _extract_amazon_urls(html, floor) if u not in seen]
                    if not new:
                        empty_streak += 1
                        print(f"  Page {page}: no new results ({empty_streak}/{MAX_EMPTY} empty)")
                    else:
                        empty_streak = 0
                        seen.update(new)
                        urls.extend(new)
                        print(f"  Page {page}: +{len(new)} URLs ({len(urls)} total)")
                    page += 1
                    time.sleep(random.uniform(2, 4))
                except Exception as exc:
                    print(f"  Page {page} failed: {exc}")
                    if _driver_fatal_exception(exc):
                        driver = _refresh_driver(driver)
                    empty_streak += 1
                    page += 1
            result[category] = urls[:MAX_ITEMS]
            print(f"  Done: {len(result[category])} URL(s) for {category!r}")
            time.sleep(random.uniform(3, 6))
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return result


def discover_walmart(config: dict[str, float]) -> dict[str, list[str]]:
    """Search Walmart across pages until MAX_ITEMS URLs or MAX_EMPTY empty pages."""
    result: dict[str, list[str]] = {}
    driver = None
    try:
        driver = _get_brd_driver()
        for category in config:
            print(f"[Walmart] Searching {category!r}…")
            urls: list[str] = []
            seen: set[str] = set()
            empty_streak = 0
            page = 1
            while len(urls) < MAX_ITEMS and empty_streak < MAX_EMPTY:
                try:
                    new = [
                        u for u in get_product_links_from_search_page(driver, category, page_number=page)
                        if u not in seen
                    ]
                    if not new:
                        empty_streak += 1
                        print(f"  Page {page}: no new results ({empty_streak}/{MAX_EMPTY} empty)")
                    else:
                        empty_streak = 0
                        seen.update(new)
                        urls.extend(new)
                        print(f"  Page {page}: +{len(new)} URLs ({len(urls)} total)")
                    page += 1
                    time.sleep(random.uniform(2, 4))
                except Exception as exc:
                    print(f"  Page {page} failed: {exc}")
                    if _driver_fatal_exception(exc):
                        driver = _refresh_driver(driver)
                    empty_streak += 1
                    page += 1
            result[category] = urls[:MAX_ITEMS]
            print(f"  Done: {len(result[category])} URL(s) for {category!r}")
            time.sleep(random.uniform(3, 6))
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Discover product URLs and write them to a watchlist file"
    )
    parser.add_argument("--amazon-only", action="store_true")
    parser.add_argument("--walmart-only", action="store_true")
    parser.add_argument("--category", metavar="NAME", help=f"One of: {', '.join(SCRAPE_CONFIG)}")
    parser.add_argument("--out", default="watchlist.json", metavar="FILE")
    args = parser.parse_args()

    if args.amazon_only and args.walmart_only:
        parser.error("Use only one of --amazon-only or --walmart-only")

    if args.category:
        if args.category not in SCRAPE_CONFIG:
            parser.error(
                f"Unknown category {args.category!r}. "
                f"Choose from: {', '.join(SCRAPE_CONFIG)}"
            )
        config = {args.category: SCRAPE_CONFIG[args.category]}
    else:
        config = SCRAPE_CONFIG

    # Merge into existing file so re-runs don't wipe other categories
    watchlist: dict[str, dict[str, list[str]]] = {"amazon": {}, "walmart": {}}
    if os.path.exists(args.out):
        with open(args.out) as fh:
            watchlist = json.load(fh)

    if not args.walmart_only:
        watchlist["amazon"].update(discover_amazon(config))

    if not args.amazon_only:
        watchlist["walmart"].update(discover_walmart(config))

    with open(args.out, "w") as fh:
        json.dump(watchlist, fh, indent=2)

    total_amz = sum(len(v) for v in watchlist["amazon"].values())
    total_wal = sum(len(v) for v in watchlist["walmart"].values())
    print(f"\nWrote {args.out}: {total_amz} Amazon URL(s), {total_wal} Walmart URL(s)")
    print(f"Next: python api_scraper.py --watchlist {args.out}")
