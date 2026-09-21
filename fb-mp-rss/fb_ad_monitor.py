# Copyright (c) 2024, regek
# All rights reserved.

# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Úpravy pro macwatch (2026): parsování karet podle aktuální stránky Marketplace,
# odmítnutí cookies, okamžitá první kontrola, stabilní ID inzerátu, lokalita a cena
# ve feedu, feed za posledních 7 dní místo rozdílu od posledního dotazu,
# databáze v mountovaném adresáři, lokální geckodriver a úklid dočasných profilů.
# Seznam změn je v PATCHES.md.

from flask import Flask, Response
import sqlite3
import json
import uuid
import tzlocal
import os
import re
import glob
import shutil
import tempfile
import time
from bs4 import BeautifulSoup
import PyRSS2Gen
from datetime import datetime, timedelta, timezone
from dateutil import parser
import logging
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import ConflictingIdError
from logging.handlers import RotatingFileHandler
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

FEED_DAYS = 7
ITEM_HREF = re.compile(r'^/marketplace/item/(\d+)')
# CZK15,000 / 15 000 Kč / $1,200 / €50 / Free / Zdarma. Měna je povinná,
# jinak by se za cenu považoval titulek jako "2023".
_AMOUNT = r'\d[\d\s.,\u00a0\u202f]*'
PRICE_TEXT = re.compile(
    rf'^(?:[A-Z]{{2,3}}\$?|[$€£]|Kč)\s?{_AMOUNT}$'
    rf'|^{_AMOUNT}\s?(?:Kč|CZK|€)$'
    r'|^(?:free|zdarma)$', re.IGNORECASE)
COOKIE_DECLINE_LABELS = (
    "Decline optional cookies",
    "Odmítnout volitelné soubory cookie",
    "Only allow essential cookies",
    "Povolit jen nezbytné soubory cookie",
)


def parse_card_texts(texts):
    """Z textů karty v pořadí vytáhne (cena, původní cena, titulek, lokalita).

    Karta vypadá takto: [štítek "Just listed"], cena, [původní cena], titulek, lokalita.
    Štítky před cenou se přeskočí. Když cena chybí, vrátí None.
    """
    prices, rest = [], []
    for text in texts:
        if not text:
            continue
        if PRICE_TEXT.match(text) and not rest:
            prices.append(text)
        elif prices:
            rest.append(text)
    if not prices or not rest:
        return None
    original = prices[1] if len(prices) > 1 else None
    location = rest[1] if len(rest) > 1 else None
    return prices[0], original, rest[0], location


SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _walk_listings(node, found):
    if isinstance(node, dict):
        if node.get('__typename') == 'GroupCommerceProductItem' and 'marketplace_listing_title' in node:
            found.append(node)
        for value in node.values():
            _walk_listings(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_listings(value, found)


def _amount(price):
    if not isinstance(price, dict):
        return None, None
    return price.get('formatted_amount'), price.get('amount')


def extract_json_listings(html):
    """Inzeráty ze strukturovaného JSONu, který Facebook vkládá do stránky.

    Stabilnější než texty karet a navíc nese čas vložení, číselnou cenu,
    původní cenu, stav prodáno nebo rezervováno a způsob předání. Popis v něm není.
    """
    found = []
    for block in SCRIPT_JSON.findall(html):
        if 'marketplace_listing_title' not in block:
            continue
        try:
            _walk_listings(json.loads(block), found)
        except ValueError:
            continue
    ads, seen = [], set()
    for item in found:
        ad_id = str(item.get('id') or '')
        if not ad_id.isdigit() or ad_id in seen:
            continue
        seen.add(ad_id)
        price, amount = _amount(item.get('listing_price'))
        original, _ = _amount(item.get('strikethrough_price'))
        geo = ((item.get('location') or {}).get('reverse_geocode') or {})
        created = item.get('creation_time')
        ads.append({
            'ad_id': ad_id,
            'url': f'https://www.facebook.com/marketplace/item/{ad_id}/',
            'title': item.get('marketplace_listing_title') or '',
            'price': price or (f'{amount} CZK' if amount else ''),
            'price_amount': amount,
            'original_price': original,
            'location': geo.get('city') or None,
            'listed_at': datetime.fromtimestamp(created, timezone.utc).isoformat() if isinstance(created, int) else None,
            'is_sold': bool(item.get('is_sold')),
            'is_pending': bool(item.get('is_pending')),
            'delivery': ','.join(item.get('delivery_types') or []) or None,
        })
    return ads


def extract_ads(html):
    """JSON ze stránky, a co v něm chybí, doplní z textů karet."""
    ads = extract_json_listings(html)
    known = {ad['ad_id'] for ad in ads}
    for card in extract_cards(html):
        if card['ad_id'] not in known:
            card.update(price_amount=None, listed_at=None, is_sold=False, is_pending=False, delivery=None)
            ads.append(card)
    return ads


def extract_cards(html):
    """Vrátí seznam slovníků s inzeráty ze stránky výsledků hledání."""
    soup = BeautifulSoup(html, 'html.parser')
    ads, seen = [], set()
    for link in soup.find_all('a', href=ITEM_HREF):
        ad_id = ITEM_HREF.match(link['href']).group(1)
        if ad_id in seen:
            continue
        leaves = [span.get_text(strip=True) for span in link.find_all('span', dir='auto')
                  if span.find('span', dir='auto') is None]
        parsed = parse_card_texts(leaves)
        if parsed is None:
            continue
        seen.add(ad_id)
        price, original, title, location = parsed
        ads.append({
            'ad_id': ad_id,
            'url': f'https://www.facebook.com/marketplace/item/{ad_id}/',
            'title': title,
            'price': price,
            'original_price': original,
            'location': location,
        })
    return ads


class fbRssAdMonitor:
    def __init__(self, json_file):
        """
        Initializes the fbRssAdMonitor instance.

        Args:
            json_file (str): Config json file
        """
        self.urls_to_monitor = []
        self.url_filters = {}  # Dictionary to store filters per URL
        self.database = os.getenv('DB_FILE', 'fb-rss-feed.db')
        self.local_tz = tzlocal.get_localzone()
        self.driver = None

        self.config_file = json_file
        self.config_mtime = None
        self.job_id = None
        self.load_from_json(json_file)
        self.set_logger()
        self.init_db()
        self.app = Flask(__name__)
        self.app.add_url_rule('/rss', 'rss', self.rss)
        self.rss_feed = PyRSS2Gen.RSS2(
            title="Facebook Marketplace Ad Feed",
            link="http://monitor.local/rss",
            description="An RSS feed to monitor new ads on Facebook Marketplace",
            lastBuildDate=datetime.now(timezone.utc),
            items=[]
        )

    def set_logger(self):
        """
        Sets up logging configuration with both file and console streaming.
        Log level is fetched from the environment variable LOG_LEVEL.
        """
        self.logger = logging.getLogger(__name__)
        log_formatter = logging.Formatter('%(levelname)s:%(asctime)s:%(funcName)s:%(lineno)d::%(message)s',
                                          datefmt='%m/%d/%Y %I:%M:%S %p')

        # Get log level from environment variable, defaulting to INFO if not set
        log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
        log_level = logging.getLevelName(log_level_str)

        # File handler (rotating log)
        file_handler = RotatingFileHandler(self.log_filename, mode='w', maxBytes=10*1024*1024,
                                           backupCount=2, encoding=None, delay=0)
        file_handler.setFormatter(log_formatter)
        file_handler.setLevel(log_level)

        # Stream handler (console output)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        console_handler.setLevel(log_level)

        # Set the logger level and add handlers
        self.logger.setLevel(log_level)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def init_db(self):
        """Vytvoří tabulku, pokud chybí. Databáze může ležet v mountovaném adresáři."""
        conn = sqlite3.connect(self.database)
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ad_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    ad_id TEXT NOT NULL UNIQUE,
                    title TEXT,
                    price TEXT,
                    last_checked DATETIME
                )''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(ad_changes)')}
            new_columns = {'original_price': 'TEXT', 'location': 'TEXT', 'first_seen': 'TEXT',
                           'listed_at': 'TEXT', 'is_sold': 'INTEGER', 'is_pending': 'INTEGER',
                           'delivery': 'TEXT', 'price_amount': 'TEXT'}
            for name, kind in new_columns.items():
                if name not in columns:
                    conn.execute(f'ALTER TABLE ad_changes ADD COLUMN {name} {kind}')
            conn.commit()
        finally:
            conn.close()

    def init_selenium(self):
        """
        Initializes Selenium WebDriver with Firefox options.
        """
        try:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"
            firefox_options = FirefoxOptions()
            firefox_options.add_argument("--no-sandbox")
            firefox_options.add_argument("--disable-dev-shm-usage")
            firefox_options.add_argument("--private")
            firefox_options.add_argument("--headless")
            firefox_options.set_preference("general.useragent.override", user_agent)
            firefox_options.set_preference("dom.webdriver.enabled", False)  # Disable webdriver flag
            firefox_options.set_preference("useAutomationExtension", False)  # Disable automation extension
            firefox_options.set_preference("privacy.resistFingerprinting", True)  # Reduce fingerprinting

            # geckodriver je v image; stahování přes webdriver-manager jen jako záloha
            gecko_driver_path = os.getenv('GECKODRIVER_PATH') or shutil.which('geckodriver')
            if not gecko_driver_path:
                from webdriver_manager.firefox import GeckoDriverManager
                gecko_driver_path = GeckoDriverManager().install()
            self.driver = webdriver.Firefox(service=FirefoxService(gecko_driver_path), options=firefox_options)

        except Exception as e:
            self.logger.error(f"Error initializing Selenium: {e}")
            raise

    def quit_selenium(self):
        """Zavře prohlížeč a smaže dočasné profily Firefoxu, jinak zaplní disk."""
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception as e:
                self.logger.warning(f"Error closing Selenium: {e}")
            self.driver = None
        for path in glob.glob(os.path.join(tempfile.gettempdir(), 'rust_mozprofile*')):
            shutil.rmtree(path, ignore_errors=True)

    def setup_scheduler(self):
        """
        Setup background job to check new ads
        """
        self.job_lock = Lock()
        self.scheduler = BackgroundScheduler()
        self.job_id = str(uuid.uuid4())
        try:
            self.scheduler.add_job(
                self.check_for_new_ads,
                'interval',
                id=self.job_id,  # Unique ID for the job
                minutes=self.refresh_interval_minutes,
                misfire_grace_time=30,
                coalesce=True,
                next_run_time=datetime.now(),  # první kontrola hned po startu
            )
            self.scheduler.start()
        except ConflictingIdError:
            self.logger.warning("Job 'check_ads_job' is already scheduled. Skipping re-schedule.")

    def local_time(self, dt):
        return dt.astimezone(self.local_tz)

    def load_from_json(self, json_file):
        """
        Loads config from a JSON file, where each URL has its own filters.

        Args:
            json_file (str): Path to the JSON file.
        """
        with open(json_file, 'r') as file:
            data = json.load(file)
            self.server_ip = data['server_ip']
            self.server_port = data['server_port']
            self.refresh_interval_minutes = data['refresh_interval_minutes']
            self.log_filename = data['log_filename']
            self.url_filters = data.get('url_filters', {})
            self.urls_to_monitor = list(self.url_filters.keys())
        self.config_mtime = os.path.getmtime(json_file)

    def reload_config(self):
        """Načte změněný config bez restartu kontejneru (úpravy z dashboardu macwatch).

        Změní se adresy hledání, filtry a interval. Adresa a port serveru se mění jen restartem.
        """
        try:
            mtime = os.path.getmtime(self.config_file)
        except OSError as e:
            self.logger.error(f"Config file unavailable: {e}")
            return False
        if mtime == self.config_mtime:
            return False
        old_interval = self.refresh_interval_minutes
        try:
            with open(self.config_file, 'r') as file:
                data = json.load(file)
            refresh = int(data['refresh_interval_minutes'])
            filters = data.get('url_filters', {})
            if not isinstance(filters, dict):
                raise ValueError("url_filters must be an object")
        except (OSError, ValueError, KeyError) as e:
            self.logger.error(f"Invalid config, keeping previous settings: {e}")
            self.config_mtime = mtime
            return False
        self.refresh_interval_minutes = max(refresh, 15)
        self.url_filters = filters
        self.urls_to_monitor = list(filters.keys())
        self.config_mtime = mtime
        self.logger.info(f"Config reloaded: {len(self.urls_to_monitor)} URLs, every {self.refresh_interval_minutes} min")
        if self.refresh_interval_minutes != old_interval and self.job_id and getattr(self, 'scheduler', None):
            self.scheduler.reschedule_job(self.job_id, trigger='interval', minutes=self.refresh_interval_minutes)
        return True

    def apply_filters(self, url, title):
        """
        Applies keyword filters specific to the URL to the ad title.

        Args:
            url (str): The URL where the ad is found.
            title (str): The title of the ad.

        Returns:
            bool: True if the title matches all filters for the URL, False otherwise.
        """
        filters = self.url_filters.get(url, {})
        if not filters:
            return True

        try:
            # Iterate through filter levels in order
            level_keys = sorted(filters.keys(), key=lambda x: int(x.replace('level', '')))  # Sort levels numerically
            for level in level_keys:
                keywords = filters.get(level, [])
                if not any(keyword.lower() in title.lower() for keyword in keywords):
                    return False  # If any level fails, return False
        except Exception as e:
            self.logger.error(f"An error while processing filters for {title}: {e}")
            return False
        return True

    def save_html(self, html):
        target = os.getenv('SAVE_HTML')
        if target:
            with open(target, 'w', encoding='utf-8') as file:
                file.write(html)

    def dismiss_cookie_banner(self):
        for label in COOKIE_DECLINE_LABELS:
            buttons = self.driver.find_elements(
                By.XPATH, f"//*[@role='button' and .//span[text()='{label}']]")
            if buttons:
                try:
                    buttons[0].click()
                    self.logger.debug(f"Cookie banner dismissed: {label}")
                    return True
                except Exception as e:
                    self.logger.warning(f"Cookie banner click failed: {e}")
        return False

    def get_page_content(self, url):
        """
        Fetches the page content using Selenium.

        Args:
            url (str): The URL of the page to fetch.

        Returns:
            str: The HTML content of the page, or None if an error occurred.
        """
        try:
            self.logger.info(f"Requesting {url}")
            self.driver.get(url)
            time.sleep(5)
            self.dismiss_cookie_banner()
            try:
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/marketplace/item/"]'))
                )
            except TimeoutException:
                self.logger.warning(f"No listings appeared on {url} within 20 s")
            time.sleep(3)  # dočtení zbylých karet
            return self.driver.page_source
        except Exception as e:
            self.logger.error(f"An error occurred while fetching page content: {e}")
            return None

    def extract_ad_details(self, content, url):
        """
        Extracts ad details from the page content and applies URL-specific filters.

        Args:
            content (str): The HTML content of the page.
            url (str): The URL of the page.

        Returns:
            list: A list of ad dicts that match the filters.
        """
        try:
            self.save_html(content)
            ads = extract_ads(content)
            if ads and all(ad.get('listed_at') is None for ad in ads):
                self.logger.warning("Listing JSON not found on page, falling back to card texts")
            return [ad for ad in ads if self.apply_filters(url, ad['title'])]
        except Exception as e:
            self.logger.error(f"An error occurred while extracting ad details: {e}")
            return []

    def get_db_connection(self):
        """
        Establishes a connection to the SQLite database.

        Returns:
            sqlite3.Connection: The database connection object.
        """
        try:
            conn = sqlite3.connect(self.database)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            self.logger.error(f"Database connection error: {e}")
            raise

    def store_ads(self, conn, ads):
        """Nové inzeráty vloží, u známých aktualizuje cenu a čas poslední kontroly."""
        now = datetime.now(timezone.utc).isoformat()
        new = 0
        for ad in ads:
            cursor = conn.execute(
                '''INSERT OR IGNORE INTO ad_changes
                       (url, ad_id, title, price, original_price, location, first_seen, last_checked,
                        listed_at, is_sold, is_pending, delivery, price_amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (ad['url'], ad['ad_id'], ad['title'], ad['price'], ad['original_price'],
                 ad['location'], now, now, ad.get('listed_at'), int(ad.get('is_sold', False)),
                 int(ad.get('is_pending', False)), ad.get('delivery'), ad.get('price_amount')))
            if cursor.rowcount:
                new += 1
                self.logger.info(f"New ad detected: {ad['title']} ({ad['price']})")
            else:
                conn.execute(
                    '''UPDATE ad_changes SET title = ?, price = ?, original_price = ?,
                           location = COALESCE(?, location), last_checked = ?,
                           listed_at = COALESCE(?, listed_at), is_sold = ?, is_pending = ?,
                           delivery = COALESCE(?, delivery), price_amount = ?
                       WHERE ad_id = ?''',
                    (ad['title'], ad['price'], ad['original_price'], ad['location'], now,
                     ad.get('listed_at'), int(ad.get('is_sold', False)), int(ad.get('is_pending', False)),
                     ad.get('delivery'), ad.get('price_amount'), ad['ad_id']))
        conn.commit()
        return new

    def check_for_new_ads(self):
        """
        Checks for new ads on the monitored URLs and stores them.
        """
        if not self.job_lock.acquire(blocking=False):
            # Job is still running, skip this run
            self.logger.warning("Previous job still running, skipping this execution.")
            return
        self.reload_config()
        self.logger.info("Fetching new Ads")
        conn = None
        try:
            conn = self.get_db_connection()
            for url in self.urls_to_monitor:
                try:
                    self.init_selenium()
                    content = self.get_page_content(url)
                finally:
                    self.quit_selenium()
                if content is None:
                    continue
                ads = self.extract_ad_details(content, url)
                new = self.store_ads(conn, ads)
                self.logger.info(f"{url}: {len(ads)} ads on page, {new} new")
                time.sleep(2)
        except sqlite3.DatabaseError as e:
            self.logger.error(f"Database error: {e}")
        except Exception as e:
            self.logger.error(f"An unexpected error occurred while checking for new ads: {e}")
        finally:
            self.job_lock.release()
            self.quit_selenium()
            if conn:
                conn.close()

    def generate_rss_feed(self):
        """
        Builds the RSS feed from ads seen in the last FEED_DAYS days, newest first.

        Feed je celý stav, ne rozdíl od posledního dotazu, takže nepovedené čtení
        nic neztratí. Duplicity řeší čtenář podle guid.
        """
        self.rss_feed.items = []
        conn = None
        try:
            conn = self.get_db_connection()
            since = (datetime.now(timezone.utc) - timedelta(days=FEED_DAYS)).isoformat()
            rows = conn.execute('''
                SELECT * FROM ad_changes
                WHERE last_checked > ?
                ORDER BY COALESCE(first_seen, last_checked) DESC
                LIMIT 1000
            ''', (since,)).fetchall()
            for row in rows:
                try:
                    first_seen = parser.parse(row['first_seen'] or row['last_checked'])
                    lines = [f"price: {row['price']}"]
                    for key in ('price_amount', 'original_price', 'location', 'listed_at', 'delivery'):
                        if row[key]:
                            lines.append(f"{key}: {row[key]}")
                    # starší databáze mají sloupce jako TEXT, kde "0" je neprázdný řetězec
                    if str(row['is_sold']) == '1':
                        lines.append("is_sold: true")
                    if str(row['is_pending']) == '1':
                        lines.append("is_pending: true")
                    self.rss_feed.items.append(PyRSS2Gen.RSSItem(
                        title=row['title'],
                        link=row['url'],
                        description="\n".join(lines),
                        guid=PyRSS2Gen.Guid(row['ad_id'], isPermaLink=False),
                        pubDate=self.local_time(first_seen)
                    ))
                except ValueError as e:
                    self.logger.error(f"Error parsing date from the database: {e}")
            self.rss_feed.lastBuildDate = datetime.now(timezone.utc)
        except sqlite3.DatabaseError as e:
            self.logger.error(f"Database error while generating RSS feed: {e}")
        except Exception as e:
            self.logger.error(f"An unexpected error occurred while generating RSS feed: {e}")
        finally:
            if conn:
                conn.close()

    def rss(self):
        """
        Returns the RSS feed as a Flask Response object.

        Returns:
            flask.Response: The RSS feed in XML format.
        """
        self.generate_rss_feed()
        return Response(self.rss_feed.to_xml(encoding='utf-8'), mimetype='application/rss+xml')

    def run(self, debug_opt=False):
        """
        Starts the Flask application and scheduler.

        Args:
            debug_opt (bool, optional): Debug mode option for Flask. Defaults to False.
        """
        try:
            self.app.run(host=self.server_ip, port=self.server_port, debug=debug_opt)
        except (KeyboardInterrupt, SystemExit):
            self.scheduler.shutdown()
        finally:
            self.quit_selenium()


if __name__ == "__main__":
    # Initialize and run the ad monitor
    config_file = os.getenv('CONFIG_FILE', 'config.json')
    if not os.path.exists(config_file):
        print(f'Error: Config file {config_file} not found!!!')
        exit()
    monitor = fbRssAdMonitor(json_file=config_file)
    monitor.setup_scheduler()
    monitor.run()
