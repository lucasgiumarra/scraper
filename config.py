# config.py — add or remove categories here.
# Floor = minimum price to store (filters out accessories/junk listings).
# Both discover.py and api_scraper.py read from this dict.

CATEGORIES: dict[str, float] = {
    # Tech
    "laptop":               170,
    "monitor":               70,
    "mechanical keyboard":   50,
    "headphones":            30,
    "tablet":               100,
    "desktop computer":     200,
    "graphics card":        150,
    "gaming mouse":          20,
    # Home
    "air fryer":             30,
    "robot vacuum":         100,
    "coffee maker":          30,
    "stand mixer":           50,
    "pressure cooker":       30,
    "furniture":             100,
    # Wearables
    "smartwatch":            50,
    "fitness tracker":       30,
}
