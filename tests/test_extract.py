"""Testy parsování karet. Spuštění v kontejneru: python3 -m unittest discover -s tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fb_ad_monitor import extract_ads, extract_cards, extract_json_listings, parse_card_texts  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'search_uh_newest.html')


class ParseCardTexts(unittest.TestCase):
    def test_badge_before_price_is_skipped(self):
        self.assertEqual(parse_card_texts(['Just listed', 'CZK4,950', 'Macbook Air 13”', 'Ostrava']),
                         ('CZK4,950', None, 'Macbook Air 13”', 'Ostrava'))

    def test_reduced_price(self):
        self.assertEqual(parse_card_texts(['CZK5,000', 'CZK9,500', 'MacBook 15 pro 2018 i7', 'Rajhradice']),
                         ('CZK5,000', 'CZK9,500', 'MacBook 15 pro 2018 i7', 'Rajhradice'))

    def test_czech_price_format(self):
        self.assertEqual(parse_card_texts(['9 000 Kč', 'MacBook Air M1', 'Brno'])[0], '9 000 Kč')

    def test_free(self):
        self.assertEqual(parse_card_texts(['Zdarma', 'Obal na MacBook'])[0], 'Zdarma')

    def test_title_with_numbers_is_not_price(self):
        self.assertEqual(parse_card_texts(['CZK20,500', '2023', 'Zlín']),
                         ('CZK20,500', None, '2023', 'Zlín'))

    def test_without_price(self):
        self.assertIsNone(parse_card_texts(['Just listed', 'MacBook']))


class ExtractCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding='utf-8') as f:
            cls.ads = extract_cards(f.read())

    def test_all_cards_found(self):
        self.assertEqual(len(self.ads), 8)
        self.assertEqual(len({ad['ad_id'] for ad in self.ads}), 8)

    def test_reduced_card(self):
        ad = next(a for a in self.ads if a['ad_id'] == '1087640027290472')
        self.assertEqual(ad, {
            'ad_id': '1087640027290472',
            'url': 'https://www.facebook.com/marketplace/item/1087640027290472/',
            'title': 'MacBook 15 pro 2018 i7',
            'price': 'CZK5,000',
            'original_price': 'CZK9,500',
            'location': 'Rajhradice',
        })

    def test_just_listed_card(self):
        ad = next(a for a in self.ads if a['ad_id'] == '1702868288072744')
        self.assertEqual((ad['title'], ad['price'], ad['location']),
                         ('MacBook Pro 13” M1 Touch Bar 512GB SSD, 8GB RAM', 'CZK14,490',
                          'Ostrava, Czech Republic'))


JSON_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'search_uh_json.html')


class ExtractJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(JSON_FIXTURE, encoding='utf-8') as f:
            cls.html = f.read()
        cls.ads = {ad['ad_id']: ad for ad in extract_json_listings(cls.html)}

    def test_all_listings(self):
        self.assertEqual(len(self.ads), 9)

    def test_fields(self):
        ad = self.ads['1152292163893963']
        self.assertEqual(ad['price'], 'CZK10,490')
        self.assertEqual(ad['price_amount'], '10490.00')
        self.assertEqual(ad['location'], 'Opava')
        self.assertEqual(ad['listed_at'], '2026-09-14T14:36:22+00:00')
        self.assertEqual((ad['is_sold'], ad['is_pending'], ad['delivery']), (False, False, 'IN_PERSON'))
        self.assertTrue(ad['title'].startswith('🍎 MacBook Air 13” M1'))

    def test_reduced_price(self):
        self.assertEqual(self.ads['1087640027290472']['original_price'], 'CZK9,500')

    def test_same_ids_as_cards(self):
        self.assertEqual(set(self.ads), {c['ad_id'] for c in extract_cards(self.html)})

    def test_falls_back_to_cards_without_json(self):
        with open(FIXTURE, encoding='utf-8') as f:
            html = f.read()
        stripped = SCRIPT_RE.sub('', html)
        ads = extract_ads(stripped)
        self.assertEqual(len(ads), 8)
        self.assertTrue(all(ad['listed_at'] is None for ad in ads))


SCRIPT_RE = __import__('re').compile(r'<script type="application/json"[^>]*>.*?</script>', __import__('re').S)


class FeedFlags(unittest.TestCase):
    def test_zero_flags_are_not_reported(self):
        import json
        import tempfile
        from fb_ad_monitor import fbRssAdMonitor

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump({"server_ip": "0.0.0.0", "server_port": "5000", "refresh_interval_minutes": 15,
                           "log_filename": os.path.join(tmp, 'log'), "url_filters": {}}, f)
            db = os.path.join(tmp, 'db.sqlite')
            # stará databáze, kde stavové sloupce vznikly jako TEXT
            import sqlite3
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ad_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, "
                         "ad_id TEXT NOT NULL UNIQUE, title TEXT, price TEXT, last_checked DATETIME, "
                         "is_sold TEXT, is_pending TEXT)")
            conn.commit()
            conn.close()
            os.environ['DB_FILE'] = db
            monitor = fbRssAdMonitor(path)
            with open(JSON_FIXTURE, encoding='utf-8') as f:
                ads = extract_ads(f.read())
            ads[0]['is_pending'] = True
            conn = monitor.get_db_connection()
            monitor.store_ads(conn, ads)
            conn.close()
            monitor.generate_rss_feed()
            xml = monitor.rss_feed.to_xml(encoding='utf-8')
            self.assertEqual(xml.count('is_pending: true'), 1)
            self.assertNotIn('is_sold: true', xml)


class ReloadConfig(unittest.TestCase):
    def test_changes_are_picked_up_and_interval_has_floor(self):
        import json
        import tempfile
        import time
        from fb_ad_monitor import fbRssAdMonitor

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            base = {"server_ip": "0.0.0.0", "server_port": "5000", "refresh_interval_minutes": 15,
                    "log_filename": os.path.join(tmp, 'log'), "url_filters": {"https://a": {}}}
            with open(path, 'w') as f:
                json.dump(base, f)
            os.environ['DB_FILE'] = os.path.join(tmp, 'db.sqlite')
            monitor = fbRssAdMonitor(path)
            self.assertFalse(monitor.reload_config())
            time.sleep(0.01)
            base.update(refresh_interval_minutes=5, url_filters={"https://b": {"level1": ["macbook"]}})
            with open(path, 'w') as f:
                json.dump(base, f)
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertTrue(monitor.reload_config())
            self.assertEqual(monitor.urls_to_monitor, ["https://b"])
            self.assertEqual(monitor.refresh_interval_minutes, 15)

            with open(path, 'w') as f:
                f.write("{rozbitý json")
            os.utime(path, (time.time() + 10, time.time() + 10))
            self.assertFalse(monitor.reload_config())
            self.assertEqual(monitor.urls_to_monitor, ["https://b"])


if __name__ == '__main__':
    unittest.main()
