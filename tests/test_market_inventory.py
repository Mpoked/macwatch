from datetime import date, datetime, timedelta, timezone

import pytest

from macwatch.config import MarketConfig, TravelSettings
from macwatch.db import Database
from macwatch.inventory import Inventory, economics
from macwatch.market import (
    ReferenceBook,
    build_measured,
    market_overview,
    price_timeline,
    reference_book,
    weighted_median,
)
from macwatch.models import Assessment, RawListing, Specs, Verdict

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def test_weighted_median():
    assert weighted_median([(10, 1), (20, 1), (30, 1)]) == 20
    assert weighted_median([(10, 1), (20, 1)]) == 15
    assert weighted_median([(10, 1), (20, 1), (100, 3)]) == 100
    assert weighted_median([]) is None


def add_listing(db, source_id, price, first_seen, gone_at=None, family="air", chip="m1", ssd=256,
                dealer=False, flags=(), source="bazos"):
    item = RawListing(source, source_id, f"https://x/{source_id}", "MacBook", str(price), "", True,
                      first_seen)
    db.insert_listing(item, price, first_seen)
    specs = Specs(family=family, chip=chip, ssd_gb=ssd, eligible=True, is_dealer=dealer,
                  red_flags=tuple(flags))
    db.save_assessment(item.key, specs, Assessment(Verdict.SLEDOVAT), first_seen)
    if gone_at:
        db.mark_gone(item.key, gone_at)


@pytest.fixture
def db():
    return Database.open(":memory:")


class TestSoldSamples:
    def test_only_quickly_gone_clean_private_listings_count(self, db, cfg):
        start = NOW - timedelta(days=30)
        add_listing(db, "1", 9000, start, start + timedelta(days=3))
        add_listing(db, "2", 9500, start, start + timedelta(days=10))
        add_listing(db, "3", 8800, start, start + timedelta(days=5))
        add_listing(db, "slow", 5000, start, start + timedelta(days=20))      # nad 14 dní
        add_listing(db, "dealer", 5000, start, start + timedelta(days=2), dealer=True)
        add_listing(db, "broken", 5000, start, start + timedelta(days=2), flags=["icloud"])
        add_listing(db, "active", 5000, start)
        book = reference_book(db.conn, cfg.market, cfg.pricing, NOW)
        hit = book.lookup("bazos", "air", "m1", 256)
        assert hit == (9000, "bazos|air|m1", "measured", 3)

    def test_suspicious_listings_do_not_count_as_sales(self, db, cfg):
        start = NOW - timedelta(days=30)
        for i, price in enumerate((9000, 9200, 9400)):
            add_listing(db, str(i), price, start, start + timedelta(days=3))
        for i in range(3):   # krabice za pár stovek, které zmizely
            sid = f"box{i}"
            add_listing(db, sid, 200, start, start + timedelta(days=2))
            db.conn.execute("UPDATE assessments SET verdict = 'PODEZRELE' WHERE source_id = ?", (sid,))
        db.conn.commit()
        hit = reference_book(db.conn, cfg.market, cfg.pricing, NOW).lookup("bazos", "air", "m1", 256)
        assert hit[0] == 9200 and hit[3] == 3

    def test_listings_dropped_from_feed_are_not_sales(self, db, cfg):
        start = NOW - timedelta(days=30)
        for i, price in enumerate((9000, 9200, 9400)):
            add_listing(db, str(i), price, start, start + timedelta(days=3), source="facebook")
        for i in range(3):   # z feedu vypadly rychle, ale prodané nejsou
            add_listing(db, f"fb{i}", 20000, start, source="facebook")
        db.touch(("facebook", "fb0"), start + timedelta(days=2))
        db.expire_unseen("facebook", start + timedelta(days=3))
        hit = reference_book(db.conn, cfg.market, cfg.pricing, NOW).lookup("facebook", "air", "m1", 256)
        assert hit[0] == 9200 and hit[3] == 3

    def test_manual_used_until_enough_samples(self, db, cfg):
        start = NOW - timedelta(days=30)
        add_listing(db, "1", 12000, start, start + timedelta(days=3))
        book = reference_book(db.conn, cfg.market, cfg.pricing, NOW)
        assert book.lookup("bazos", "air", "m1", 256)[2] == "config"

    def test_manual_mode_ignores_measured(self, db, cfg):
        start = NOW - timedelta(days=30)
        for i in range(5):
            add_listing(db, str(i), 12000, start, start + timedelta(days=3))
        market = MarketConfig(reference_mode="manual")
        assert reference_book(db.conn, market, cfg.pricing, NOW).lookup("bazos", "air", "m1", 256)[0] == 8900

    def test_generic_group_collects_all_ssd(self, db, cfg):
        start = NOW - timedelta(days=30)
        for i, ssd in enumerate((256, 512, 1024)):
            add_listing(db, str(i), 10000 + i * 1000, start, start + timedelta(days=2), ssd=ssd)
        book = reference_book(db.conn, cfg.market, cfg.pricing, NOW)
        # 512 a 1 TB se přepočtou na 256 GB (−11 % za zdvojnásobení), pak zpět na 2 TB
        base = sorted([10000, 11000 / 1.11, 12000 / 1.11 ** 2])[1]
        price, key, origin, samples = book.lookup("bazos", "air", "m1", 2048)
        assert (key, origin, samples) == ("bazos|air|m1", "measured", 3)
        assert price == pytest.approx(base * 1.11 ** 3, abs=2)


def test_own_sales_weigh_more():
    from macwatch.market import Sample
    samples = [Sample("air", "m1", 8, 256, "bazos", p, 3, "x") for p in (9000, 9200, 9400)]
    samples.append(Sample("air", "m1", 8, 256, "vlastni", 11000, 5, "x", weight=3))
    measured = build_measured(samples)
    # bez vlastního prodeje by medián byl 9 200, prodej s váhou 3 ho vytáhne na 10 200
    assert measured["bazos"]["air|m1"].price_czk == 10200
    assert measured["bazos"]["air|m1"].own_sales == 1
    assert measured["facebook"]["air|m1"].price_czk == 11000  # vlastní prodej platí všude


def test_book_falls_back_to_other_source():
    book = ReferenceBook(manual={"bazos|air|m1": 9000}, fallback_source="bazos")
    assert book.lookup("facebook", "air", "m1", 256) == (9000, "bazos|air|m1", "config:bazos", 0)


def test_market_overview_days_to_sell(db, cfg):
    start = NOW - timedelta(days=30)
    add_listing(db, "1", 9000, start, start + timedelta(days=2))
    add_listing(db, "2", 10000, start, start + timedelta(days=4))
    add_listing(db, "3", 11000, start)
    stats = {(s.source, s.group): s for s in market_overview(db.conn, cfg.market, NOW)}
    s = stats[("bazos", "air|m1|256")]
    assert (s.sold, s.median_price, s.median_days, s.active, s.median_asking) == (2, 9500, 3.0, 1, 11000)


def test_price_timeline(db):
    week1 = NOW - timedelta(days=14)
    add_listing(db, "1", 10000, week1)
    add_listing(db, "2", 12000, week1)
    db.update_price(("bazos", "1"), 9000, "9 000", week1 + timedelta(days=1))
    series = price_timeline(db.conn, "air", "m1", 256, NOW)
    assert list(series) == ["bazos"]
    week, median_price, count = series["bazos"][0]
    assert (median_price, count) == (10500, 2)


class TestInventory:
    SETTINGS = TravelSettings(consumption_l_per_100km=5.0, fuel_price_czk_per_l=40.0)  # 2 Kč/km

    def item(self, **kw):
        base = dict(bought_at="2026-09-01", buy_price_czk=8000, travel_km=100, listed_at=None,
                    sold_at=None, sale_price_czk=None, sale_fees_czk=0)
        base.update(kw)
        return base

    def test_sold_item_margins_and_roi(self):
        e = economics(self.item(listed_at="2026-09-03", sold_at="2026-09-11", sale_price_czk=11000,
                                sale_fees_czk=100), 500, self.SETTINGS, date(2026, 9, 30))
        assert e.status == "prodano"
        assert e.days_held == 10
        assert e.travel_czk == 200
        assert e.invested_czk == 8700
        assert e.gross_margin_czk == 3000
        assert e.net_margin_czk == 11000 - 8000 - 500 - 200 - 100
        assert e.roi == pytest.approx(2200 / 8700)
        assert e.annualized_roi == pytest.approx((1 + 2200 / 8700) ** 36.5 - 1)
        assert e.capital_tied_czk == 0

    def test_unsold_item_ties_capital(self):
        e = economics(self.item(listed_at="2026-09-05"), 300, self.SETTINGS, date(2026, 9, 30))
        assert (e.status, e.days_held, e.capital_tied_czk, e.roi) == ("vystaveno", 29, 8500, None)

    def test_fast_turnover_beats_slow_big_margin(self):
        fast = economics(self.item(sold_at="2026-09-04", sale_price_czk=9000, travel_km=0), 0,
                         self.SETTINGS, date(2026, 9, 30))
        slow = economics(self.item(sold_at="2026-12-10", sale_price_czk=11000, travel_km=0), 0,
                         self.SETTINGS, date(2026, 12, 30))
        assert slow.roi > fast.roi
        assert fast.annualized_roi > slow.annualized_roi

    def test_store_summary_and_cap(self, db):
        inv = Inventory(db.conn)
        a = inv.add(dict(title="Air M1", family="air", chip="m1", ssd_gb=256, bought_at="2026-09-01",
                         buy_price_czk=8000), NOW)
        inv.add_cost(a, "2026-09-02", "dily", "baterie", 1200)
        b = inv.add(dict(title="Pro M1", bought_at="2026-08-01", buy_price_czk=15000,
                         sold_at="2026-08-15", sale_price_czk=19000), NOW)
        assert inv.get(a)["costs_czk"] == 1200
        summary = inv.summary(TravelSettings(), capital_cap_czk=9000, today=date(2026, 9, 30))
        assert (summary.items, summary.in_stock, summary.sold) == (2, 1, 1)
        assert summary.capital_tied_czk == 9200
        assert summary.over_cap
        assert summary.net_profit_czk == 4000
        inv.delete(b)
        assert inv.summary(TravelSettings(), 0, date(2026, 9, 30)).sold == 0

    def test_own_sale_feeds_reference(self, db, cfg):
        inv = Inventory(db.conn)
        for price in (13000, 13500, 14000):
            inv.add(dict(title="Air", family="air", chip="m2", ssd_gb=512, bought_at="2026-09-01",
                         buy_price_czk=10000, sold_at="2026-09-10", sale_price_czk=price), NOW)
        book = reference_book(db.conn, cfg.market, cfg.pricing, NOW)
        assert book.lookup("facebook", "air", "m2", 512)[:3] == (13500, "facebook|air|m2", "measured")

    def test_unknown_cost_kind(self, db):
        inv = Inventory(db.conn)
        item = inv.add(dict(title="x", bought_at="2026-09-01", buy_price_czk=1), NOW)
        with pytest.raises(ValueError):
            inv.add_cost(item, "2026-09-01", "pivo", None, 50)
