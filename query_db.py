"""
query_db.py — Interactive DB explorer for market_intelligence.db

Usage:
    python query_db.py                     # interactive menu
    python query_db.py --query prices      # run one query and exit
    python query_db.py --query drops
    python query_db.py --query history
    python query_db.py --query summary
    python query_db.py --query cheapest
    python query_db.py --query category --filter laptop
    python query_db.py --query search --filter macbook
"""

import os
import sqlite3
import argparse
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("DB")


def connect():
    return sqlite3.connect(DB)


def q_summary(conn, _=None):
    """Row counts, date range, categories."""
    print("\n=== DATABASE SUMMARY ===")
    for row in conn.execute("SELECT source, COUNT(*), MIN(scrape_date), MAX(scrape_date) FROM price_history GROUP BY source"):
        print(f"  {row[0]:<10} {row[1]:>4} rows   {row[2]} → {row[3]}")
    print()
    print("  Categories:")
    for row in conn.execute("SELECT category, source, COUNT(DISTINCT external_id) FROM price_history GROUP BY category, source ORDER BY category, source"):
        print(f"    {row[1]:<10} {row[0]:<25} {row[2]} products")


def q_prices(conn, _=None):
    """Current price for every item (latest scrape date per item)."""
    print("\n=== CURRENT PRICES ===")
    rows = conn.execute("""
        SELECT source, category, brand_model, price, scrape_date
        FROM price_history
        WHERE (external_id, source, scrape_date) IN (
            SELECT external_id, source, MAX(scrape_date)
            FROM price_history GROUP BY external_id, source
        )
        ORDER BY category, source, price
    """).fetchall()
    cur_cat = None
    for source, cat, name, price, dt in rows:
        if cat != cur_cat:
            print(f"\n  -- {cat.upper()} --")
            cur_cat = cat
        print(f"  [{source:<7}] ${price:<9.2f} {name[:50]}  ({dt})")


def q_drops(conn, _=None):
    """Items priced below their historical average."""
    print("\n=== PRICE DROPS (below historical average) ===")
    rows = conn.execute("""
        WITH stats AS (
            SELECT external_id, source, brand_model, category,
                   price AS current_price,
                   AVG(price) OVER (PARTITION BY external_id, source) AS avg_price,
                   scrape_date
            FROM price_history
        )
        SELECT source, category, brand_model, current_price, avg_price,
               avg_price - current_price AS saving,
               scrape_date
        FROM stats
        WHERE current_price < avg_price
          AND scrape_date = (SELECT MAX(scrape_date) FROM price_history ph2
                             WHERE ph2.external_id = stats.external_id
                               AND ph2.source = stats.source)
        GROUP BY external_id, source
        ORDER BY saving DESC
    """).fetchall()
    if not rows:
        print("  No drops found.")
    for source, cat, name, cur, avg, save, dt in rows:
        print(f"  [{source:<7}] {name[:40]:<40} NOW ${cur:.2f}  AVG ${avg:.2f}  SAVE ${save:.2f}  ({cat})")


def q_history(conn, _=None):
    """Price history for items that have been scraped more than once."""
    print("\n=== PRICE HISTORY (multi-date items) ===")
    rows = conn.execute("""
        SELECT source, brand_model, scrape_date, price
        FROM price_history
        WHERE external_id IN (
            SELECT external_id FROM price_history
            GROUP BY external_id HAVING COUNT(DISTINCT scrape_date) > 1
        )
        ORDER BY source, brand_model, scrape_date
    """).fetchall()
    cur = None
    for source, name, dt, price in rows:
        key = (source, name)
        if key != cur:
            print(f"\n  [{source}] {name[:55]}")
            cur = key
        print(f"    {dt}  ${price:.2f}")


def q_cheapest(conn, _=None):
    """Top 10 cheapest current prices per source."""
    print("\n=== CHEAPEST RIGHT NOW (top 10 per source) ===")
    for source in ("Amazon", "Walmart"):
        print(f"\n  -- {source} --")
        rows = conn.execute("""
            SELECT brand_model, category, price, scrape_date
            FROM price_history
            WHERE source = ?
              AND (external_id, source, scrape_date) IN (
                  SELECT external_id, source, MAX(scrape_date)
                  FROM price_history GROUP BY external_id, source
              )
            ORDER BY price ASC LIMIT 10
        """, (source,)).fetchall()
        for name, cat, price, dt in rows:
            print(f"  ${price:<9.2f} {name[:45]}  ({cat})")


def q_category(conn, filter_val=None):
    """All current prices for a specific category."""
    cat = filter_val or input("  Category: ").strip()
    print(f"\n=== {cat.upper()} — CURRENT PRICES ===")
    rows = conn.execute("""
        SELECT source, brand_model, price, review_count, scrape_date
        FROM price_history
        WHERE LOWER(category) = LOWER(?)
          AND (external_id, source, scrape_date) IN (
              SELECT external_id, source, MAX(scrape_date)
              FROM price_history GROUP BY external_id, source
          )
        ORDER BY source, price
    """, (cat,)).fetchall()
    if not rows:
        print(f"  No data for category '{cat}'.")
    for source, name, price, reviews, dt in rows:
        rev = f"{reviews:,} reviews" if reviews else "no reviews"
        print(f"  [{source:<7}] ${price:<9.2f} {name[:45]}  {rev}")


def q_search(conn, filter_val=None):
    """Search product names across all history."""
    term = filter_val or input("  Search term: ").strip()
    print(f"\n=== SEARCH: '{term}' ===")
    rows = conn.execute("""
        SELECT source, category, brand_model, price, scrape_date
        FROM price_history
        WHERE LOWER(raw_title) LIKE LOWER(?)
           OR LOWER(brand_model) LIKE LOWER(?)
        ORDER BY source, category, price
    """, (f"%{term}%", f"%{term}%")).fetchall()
    if not rows:
        print("  No matches.")
    for source, cat, name, price, dt in rows:
        print(f"  [{source:<7}] ${price:<9.2f} {name[:45]}  ({cat}, {dt})")


QUERIES = {
    "summary":  q_summary,
    "prices":   q_prices,
    "drops":    q_drops,
    "history":  q_history,
    "cheapest": q_cheapest,
    "category": q_category,
    "search":   q_search,
}

MENU = """
  1. summary   — row counts, date range, categories
  2. prices    — current price for every item
  3. drops     — items below their historical average
  4. history   — price changes over time
  5. cheapest  — top 10 cheapest per source
  6. category  — all prices for one category
  7. search    — search by product name
  q. quit
"""


def interactive(conn):
    key_map = {"1": "summary", "2": "prices", "3": "drops","4": "history", "5": "cheapest", "6": "category", "7": "search"}
    while True:
        print(MENU)
        choice = input("  Choose: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            break
        name = key_map.get(choice, choice)
        fn = QUERIES.get(name)
        if fn:
            fn(conn)
        else:
            print(f"  Unknown query '{choice}'. Try a number or name from the menu.")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explore market_intelligence.db")
    parser.add_argument("--query", choices=list(QUERIES), metavar="NAME",
                        help=f"Query to run: {', '.join(QUERIES)}")
    parser.add_argument("--filter", metavar="VALUE",
                        help="Filter value for 'category' or 'search' queries")
    args = parser.parse_args()

    conn = connect()

    if args.query:
        QUERIES[args.query](conn, args.filter)
    else:
        interactive(conn)
