from itertools import product
import sqlite3
import re
import random
import time
import os
import json
from datetime import datetime
from dotenv import load_dotenv

# Web Engines
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver import Remote, ChromeOptions as Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import undetected_chromedriver as uc
from curl_cffi import requests
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
import traceback
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection as Connection


load_dotenv()


def _gather_exception_chain_text(exc: BaseException) -> str:
    """Flatten __cause__ / __context__ so urllib3/http errors under Selenium are visible."""
    parts: list[str] = []
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        parts.append(type(cur).__qualname__)
        parts.append(repr(cur))
        parts.append(str(cur))
        c = getattr(cur, "__cause__", None)
        if c is not None:
            stack.append(c)
        ctx = getattr(cur, "__context__", None)
        if ctx is not None and ctx is not c:
            stack.append(ctx)
    return " ".join(parts).lower()


def _driver_fatal_exception(exc: BaseException) -> bool:
    """
    True when the remote Selenium / Bright Data session is likely dead or the HTTP
    tunnel dropped (e.g. RemoteDisconnected). Caller should restart the driver.
    """
    text = _gather_exception_chain_text(exc)
    needles = (
        "websocket",
        "not open",
        "chrome not reachable",
        "invalid session",
        "no such session",
        "session deleted",
        "read timed out",
        "internal server error",
        "connection aborted",
        "remotedisconnected",
        "remote end closed connection",
        "connection reset",
        "broken pipe",
        "max retries exceeded",
        "newconnectionerror",
        "failed to establish",
        "actively refused",
        "unexpected_eof",
        "eof occurred",
        "bad gateway",
        "502",
        "503",
    )
    if any(n in text for n in needles):
        return True
    # Selenium often surfaces a bare "Message:" when the wire died with no JSON body
    stripped = str(exc).strip()
    if stripped in ("Message:", "") or stripped.lower() == "message:":
        return True
    return False


class UnifiedMarketDB:
    def __init__(self, db_name="market_intelligence.db"):
        self.db_name = db_name
        self._create_tables()

    def _create_tables(self):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    external_id TEXT,
                    source TEXT,
                    scrape_date DATE,
                    category TEXT,
                    brand_model TEXT,
                    price REAL,
                    review_count INTEGER,
                    availability TEXT,
                    raw_title TEXT,
                    PRIMARY KEY (external_id, source, scrape_date)
                )
            """)

    def upsert_item(self, data):
        query = """
            INSERT INTO price_history 
            (external_id, source, scrape_date, category, brand_model, price, review_count, availability, raw_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_id, source, scrape_date) DO UPDATE SET
                price=excluded.price,
                review_count=excluded.review_count,
                availability=excluded.availability
        """
        with sqlite3.connect(self.db_name) as conn:
            conn.execute(query, data)

    def get_price_drops(self):
        """
        SQL logic: Compares today's price with the average price 
        for that specific item in the past.
        """
        query = """
            WITH PriceStats AS (
                SELECT 
                    external_id, 
                    source, 
                    brand_model,
                    price as current_price,
                    AVG(price) OVER(PARTITION BY external_id, source) as avg_historical_price
                FROM price_history
            )
            SELECT source, brand_model, current_price, avg_historical_price
            FROM PriceStats
            WHERE current_price < avg_historical_price
            GROUP BY external_id, source;
        """
        with sqlite3.connect(self.db_name) as conn:
            return conn.execute(query).fetchall()

class UnifiedScraper:
    def __init__(self):
        self.db = UnifiedMarketDB()
        self.today = datetime.now().strftime('%Y-%m-%d')
        self.proxy_url = f"http://{os.getenv('BRD_USERNAME')}:{os.getenv('BRD_PASSWORD')}@brd.superproxy.io:33335"
        self.proxy_host = "brd.superproxy.io"
        self.proxy_port = 33335
        self.proxy_username = os.getenv("BRD_USERNAME")  # e.g. brd-customer-<id>-zone-<zone_name>-session-...
        self.proxy_password = os.getenv("BRD_PASSWORD")

        self.walmart_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.5",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1"
        }
    
    def _get_selenium_driver(self, use_brd=False):
        """
        Uses SeleniumAuthenticatedProxy to properly route traffic through BrightData.
        This ensures the local IP is hidden and Amazon sees the proxy's location.
        """
        if use_brd:
            print("🌐 Connecting to Bright Data Scraping Browser...")
            # Use the AUTH from your .env (make sure WAL_AUTH is set)
            auth = os.getenv('WAL_AUTH') 
            server_addr = f'https://{auth}@brd.superproxy.io:9515'
            connection = Connection(server_addr, 'goog', 'chrome')
            driver = Remote(connection, options=Options())
            return driver
        else:
            chrome_options = webdriver.ChromeOptions()
            chrome_options.add_argument("--proxy-server=http://127.0.0.1:24000")
            # chrome_options.add_argument('--ignore-certificate-errors')
            
            # chrome_options.add_argument('--allow-insecure-localhost')
            chrome_options.add_argument("--disable-blink-features=AutomationControlled") 
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_argument("--window-size=1280,800")
            chrome_options.add_argument("--no-sandbox")          # needed in WSL
            chrome_options.add_argument("--disable-dev-shm-usage")  # needed in WSL

            # Force a standard User-Agent so we don't look like a headless scraper
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

            service = Service(ChromeDriverManager().install())

            # Initialize the standard Chrome driver
            driver = webdriver.Chrome(service=service, options=chrome_options)
            # driver = uc.Chrome(options=chrome_options, version_main=146)
        
        return driver


    # --- INTEGRATED UTILITIES ---
    def _clean_product_data(self, raw_title, category):
        """Your refined brand/model extraction logic."""
        if not raw_title or raw_title == "Unknown Title":
            return "Unknown"

        # 1. Immediate Truncation
        delimiters = [r'\|', r' - ', r' with ', r': ', r', ']
        for delim in delimiters:
            raw_title = re.split(delim, raw_title, flags=re.IGNORECASE)[0]

        # 2. Expanded Noise List
        noise = [
            r'\d+\.\d+"', r'\d+\s*inch', r'\d+\s*GB', r'\d+\s*TB', r'\d+\s*mm', 
            'FHD', 'Laptop', 'RAM', 'SSD', 'Gaming', 'Mechanical', 'Wireless', 
            'Wired', 'RGB', 'Backlit', 'Keyboard', 'Monitor', 'Smartwatch', 
            'Waterproof', 'Typewriter', 'Retro', 'Hot Swappable', 'Gasket',
            '75%', '108 Keys', 'Amd', 'Intel', 'Core', 'Nvidia'
        ]
        pattern = "|".join(noise)
        clean_name = re.split(pattern, raw_title, flags=re.IGNORECASE)[0]
        clean_name = clean_name.strip(",-._ ")
        
        if len(clean_name) < 3:
            clean_name = " ".join(raw_title.split()[:3])
        return clean_name
    
    def _clean_numeric(self, text):
        """Extracts only digits and decimals from strings."""
        if not text or "N/A" in str(text):
            return 0.0
        cleaned = "".join(char for char in str(text) if char.isdigit() or char == '.')
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    def _handle_interstitial(self, driver):
        """Handles Amazon gatekeeper/interstitial pages."""
        try:
            button_xpath = "//*[contains(text(), 'Continue') or contains(@value, 'Continue')]"
            button = WebDriverWait(driver, 7).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath))
            )
            print("Gatekeeper page detected. Clicking 'Continue'...")
            button.click()
            time.sleep(2)
        except:
            pass # No gatekeeper found, move on

    def run_discovery(self, category, amazon_floor=50):
        """Runs both engines and then reports deals."""
        # 1. Amazon Engine (Selenium)
        self.scrape_amazon(category, amazon_floor)
        
        # 2. Delay to prevent cross-site correlation
        time.sleep(random.uniform(5, 10))
        
        # 3. Walmart Engine (curl_cffi)
        self.scrape_walmart(category, amazon_floor)
 
        # 4. Intelligence Reporting
        self.report_deals()

    def report_deals(self):
        """Generates a summary of price drops from the database."""
        print("\n" + "═"*50)
        print("💰 PRICE DROP ALERT: DEALS DETECTED 💰")
        print("═"*50)
        drops = self.db.get_price_drops()
        if not drops:
            print("Scanning complete. No new drops below historical averages found.")
        else:
            for source, name, current, avg in drops:
                savings = avg - current
                print(f"[{source}] {name[:40]}")
                print(f"   🔥 NOW: ${current:.2f} | 📉 AVG: ${avg:.2f} | 💸 SAVE: ${savings:.2f}")
        print("═"*50 + "\n")

    def _test_selenium_driver(self):
        driver = self._get_selenium_driver()
        try:
            print("Testing Connection") 
            driver.get("http://httpbin.org/ip")
            print("Page Content:")
            print(driver.page_source)
        finally:
            driver.quit()
    
    

        # --- UPDATED AMAZON ENGINE ---
    def scrape_amazon(self, driver, category, floor):
        print(f"🚀 STARTING AMAZON: {category.upper()}")
        wait = WebDriverWait(driver, 30)
        
        try:
            driver.get("https://www.amazon.com")
            # Check for Amazon's specialized block pages
            # if "api-services-support" in driver.page_source or "captcha" in driver.current_url.lower():
            #     print("🛑 CAPTCHA/Block detected. Solve it manually in the window.")
            #     # This pause prevents the 'finally' block from closing the browser
            #     input("Solve the captcha, then press Enter here to continue...")

            self._handle_interstitial(driver)

            search_box = wait.until(EC.element_to_be_clickable((By.ID, "twotabsearchtextbox")))
            search_box.clear()
            search_box.send_keys(category + Keys.ENTER)
            
            # Wait for search results
            wait.until(EC.presence_of_element_located((By.XPATH, '//div[@data-component-type="s-search-result"]')))
            time.sleep(random.uniform(2, 4))
            
            try:
                wait.until(EC.presence_of_element_located((By.XPATH, '//div[@data-component-type="s-search-result"]')))
                time.sleep(random.uniform(2, 4)) # Extra breathing room for images/prices to render
            except TimeoutException:
                print(f"Timeout waiting for results")

            items = driver.find_elements(By.XPATH, '//div[@data-component-type="s-search-result"]')

            for item in items:
                try:
                    is_sponsored = (
                        "Sponsored" in item.text or 
                        item.find_elements(By.CSS_SELECTOR, ".puis-sponsored-label-text") or
                        item.find_elements(By.XPATH, ".//*[contains(@aria-label, 'Sponsored')]")
                    )

                    if is_sponsored:
                        continue # SKIP IMMEDIATELY
                    
                    asin = item.get_attribute("data-asin")
                    if not asin: continue

                    # Integration 2: Clean Numeric Price
                    # try:
                    #     whole = item.find_element(By.CLASS_NAME, 'a-price-whole').text
                    #     fraction = item.find_element(By.CLASS_NAME, 'a-price-fraction').text
                    #     price = _clean_numeric(f"{whole}.{fraction}")
                    # except: 
                    #     price = 0.0

                    try:
                        price_span = item.find_element(
                            By.CSS_SELECTOR, 'span[aria-hidden="true"] .a-price-whole'
                        )
                        fraction_span = item.find_element(
                            By.CSS_SELECTOR, 'span[aria-hidden="true"] .a-price-fraction'
                        )
                        whole = price_span.text.replace(",", "").strip(".")
                        fraction = fraction_span.text.strip()
                        price = self._clean_numeric(f"{whole}.{fraction}")
                    except Exception:
                        # Fallback: grab the full offscreen price string e.g. "$1,299.99"
                        try:
                            offscreen = item.find_element(
                                By.CSS_SELECTOR, 'span.a-price span.a-offscreen'
                            ).get_attribute("innerHTML")
                            price = self._clean_numeric(offscreen)
                        except:
                            price = 0.0
                    
                    if price < floor: continue

                    # Integration 3: Clean Product Data (Title)
                    raw_title = item.find_element(By.CSS_SELECTOR, "img.s-image").get_attribute("alt")
                    brand_model = self._clean_product_data(raw_title, category)

                    try:
                        rev_text = item.find_element(By.XPATH, ".//span[contains(@class, 's-underline-text')]").text
                        reviews = int("".join(filter(str.isdigit, rev_text)))
                    except: reviews = 0

                    # Save to Unified DB
                    self.db.upsert_item((
                        asin, 'Amazon', self.today, category, 
                        brand_model, price, reviews, 'In Stock', raw_title
                    ))
                    print(f"Stored: [{asin}] {brand_model} | ${price}")

                except Exception as e:
                    print(f"Error: {e}")
                    continue
        except Exception as e:
            print(f"Error navigating... \n{e}")
        # finally:
            # driver.quit()

    def scrape_walmart(self, driver, product_url, floor):
        """
        Scrape Walmart search results using Bright Data Scraping Browser (Browser API)
        via Selenium + CDP.
        """

        def cdp(cmd, params=None):
            if params is None: params = {}
            return driver.execute('executeCdpCommand', {'cmd': cmd, 'params': params})['value']

        try:
            # Bound navigation / scripts before any network (Bright Data can hang on dead sessions)
            driver.set_page_load_timeout(75)
            driver.set_script_timeout(75)

            cdp("Page.getFrameTree")

            driver.get(product_url)
            wait = WebDriverWait(driver, 30)
            wait.until(EC.presence_of_element_located((By.ID, "__NEXT_DATA__")))

            html = None
            # Primary Attempt: CDP
            try:
                # 1. Get the Root Node ID of the current document
                root_node = cdp('DOM.getDocument')
                root_node_id = root_node['root']['nodeId']
                
                # 2. Fetch the HTML from that live node (more reliable than ResourceContent)
                outer_html = cdp('DOM.getOuterHTML', {'nodeId': root_node_id})
                html = outer_html.get('outerHTML')
            except Exception as e:
                print(f"[Walmart] CDP DOM fetch failed: {e}")

            # 3. Secure Fallback: This is your safety net
            if not html or len(html) < 500: # Walmart pages are usually very large
                print("[Walmart] CDP HTML empty or too small, using page_source fallback...")
                html = driver.page_source

            soup = BeautifulSoup(html, "html.parser")
            script_tag = soup.find("script", id="__NEXT_DATA__")
            
            if not script_tag:
                print(f"[Walmart] Error: __NEXT_DATA__ not found at {product_url}")
                return None

            data = json.loads(script_tag.string)
            initial_data = data["props"]["pageProps"]["initialData"]["data"]
            product_data = initial_data["product"]
            reviews_data = initial_data.get("reviews", {})

            product_info = {
                "price": product_data["priceInfo"]["currentPrice"]["price"],
                "review_count": reviews_data.get("totalReviewCount", 0),
                "item_id": product_data["usItemId"],
                "avg_rating": reviews_data.get("averageOverallRating", 0),
                "product_name": product_data["name"],
                "brand": product_data.get("brand", ""),     
                "availability": product_data["availabilityStatus"], 
                "image_url": product_data["imageInfo"]["thumbnailUrl"],
                "short_description": product_data.get("shortDescription", "")
            }

            print(f"\n{product_info['item_id']}: \nproduct: {product_info['product_name']}\nprice: {product_info['price']}\nreview_count: {product_info['review_count']}\n")
            return product_info

            
        except Exception as e:
            print(f"Failed to process URL: {product_url}. Error: {e!r}")
            if _driver_fatal_exception(e):
                print("[Walmart] Fatal transport/session error — restarting browser on next iteration.")
                raise e
            return None

def get_product_links_from_search_page(driver, query, page_number=1):
        def cdp(cmd, params=None):
            if params is None:
                params = {}
            return driver.execute('executeCdpCommand', {
                'cmd': cmd,
                'params': params,
            })['value']

        BASE_URL = "https://www.walmart.com"
        search_url = f"{BASE_URL}/search?q={query}&page={page_number}"

        print(f"[Walmart] Searching for: {query}")

        try:
            driver.set_page_load_timeout(75)
            driver.set_script_timeout(75)

            driver.get(search_url)
            
            # 2. Wait explicitly for the page data to load, NOT a random time.sleep()
            print(f"[Walmart] Waiting for page to render...")
            wait = WebDriverWait(driver, 45)
            wait.until(EC.presence_of_element_located((By.ID, "__NEXT_DATA__")))
            
            print(f"[Walmart] Successfully loaded {query}. Fetching DOM...")
            
            html = None
            try:
                print("[Walmart] CDP: Getting document node...")
                root_node = cdp('DOM.getDocument')
                root_node_id = root_node['root']['nodeId']
                
                print("[Walmart] CDP: Getting outer HTML...")
                outer_html = cdp('DOM.getOuterHTML', {'nodeId': root_node_id})
                html = outer_html.get('outerHTML')
            except Exception as e:
                print(f"[Walmart] Search CDP DOM fetch failed: {e}")

            # 3. Fallback to Selenium if CDP content fetch fails or is tiny
            if not html or len(html) < 500:
                print("[Walmart] Search page CDP HTML invalid, using fallback page_source...")
                html = driver.page_source
                
            print("[Walmart] Parsing links...")
            soup = BeautifulSoup(html, "html.parser")
            product_links = []

            found = False
            for a_tag in soup.find_all('a', href=True):
                a_tag_href = a_tag["href"]
                
                # Skip sponsored ad tracking links
                if "/sp/track" in a_tag_href or "adsRedirect=true" in a_tag_href:
                    continue

                if "/ip/" in a_tag_href:
                    found = True
                    if "https" in a_tag_href:
                        full_url = a_tag_href
                    else:
                        full_url = BASE_URL + a_tag_href
                    
                    # Clean the URL to remove tracking garbage at the end
                    full_url = full_url.split('?')[0] 
                    
                    if full_url not in product_links:
                        product_links.append(full_url)

            if not found:
                print(f"\n[Walmart] NO LINKS FOUND. Page preview: {soup.text[:500]}...\n")

            print(f"[Walmart] Found {len(product_links)} product links.")
            return product_links

        except Exception as e:
            print(f"Failed to get product links for query: {query}. Error {e!r}")
            err_lower = str(e).lower()
            if _driver_fatal_exception(e) or "timeout" in err_lower:
                raise e
            return []


def _refresh_walmart_driver(bot, driver):
    """Safely close Bright Data / remote driver and open a new one."""
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass
    return bot._get_selenium_driver(use_brd=True)


def _fetch_search_links_with_retries(bot, driver, category, max_attempts=3):
    """
    Walmart search often hits Selenium timeouts; those used to kill the whole run
    because get_product_links_from_search_page re-raises. Retry with a fresh driver.
    """
    current = driver
    for attempt in range(1, max_attempts + 1):
        try:
            links = get_product_links_from_search_page(current, category, page_number=1)
            return current, links
        except Exception as e:
            print(f"[Walmart] Search failed for {category!r} (attempt {attempt}/{max_attempts}): {e}")
            traceback.print_exc()
            if attempt < max_attempts:
                print("[Walmart] Restarting browser before retry...")
                current = _refresh_walmart_driver(bot, current)
            else:
                print(f"[Walmart] Giving up on search for {category!r} after {max_attempts} attempts.")
    return current, []


# --- REFINED MAIN EXECUTION ---
if __name__ == "__main__":
    # Your specific configuration
    market_pov_config = {
        "laptop": 170,
        "monitor": 70,
        "smartwatch": 50,
        "air fryer": 30,
        "robot vacuum": 100,
        "mechanical keyboard": 50
    }

    bot = UnifiedScraper()

    # Task A: Amazon (Using undetected_chromedriver)
    # print("--- Starting Amazon Phase ---")
    # amazon_driver = bot._get_selenium_driver(use_brd=False)
    # for cat, floor in market_pov_config.items():
    #     bot.scrape_amazon(amazon_driver, cat, floor)
    # amazon_driver.quit()

    # Task B: Walmart (Using Bright Data Scraping Browser)
    print("--- Starting Walmart Phase ---")
    walmart_driver = None
    product_counter = 0
    SESSION_LIMIT = 5  # Refresh driver every 5 products

    try:
        walmart_driver = bot._get_selenium_driver(use_brd=True)
        for cat, floor in market_pov_config.items():
            try:
                walmart_driver, product_links = _fetch_search_links_with_retries(
                    bot, walmart_driver, cat, max_attempts=3
                )
                if not product_links:
                    print(f"[Walmart] No links for category {cat!r}; continuing.")
                    continue

                for link in product_links:
                    if product_counter >= SESSION_LIMIT:
                        print("♻️ Rotating Session to avoid detection/fatigue...")
                        walmart_driver = _refresh_walmart_driver(bot, walmart_driver)
                        product_counter = 0

                    try:
                        result = bot.scrape_walmart(walmart_driver, link, floor)
                        if result:
                            product_counter += 1
                    except Exception as e:
                        print(f"⚠️ Scrape/session failure on product: {e}")
                        traceback.print_exc()
                        walmart_driver = _refresh_walmart_driver(bot, walmart_driver)
                        product_counter = 0

                    time.sleep(random.uniform(2, 5))
            except Exception as e:
                print(f"⚠️ Category {cat!r} aborted: {e}")
                traceback.print_exc()
                walmart_driver = _refresh_walmart_driver(bot, walmart_driver)
                product_counter = 0
                continue
    finally:
        try:
            if walmart_driver is not None:
                walmart_driver.quit()
        except Exception:
            pass

    bot.report_deals()