import json
from datetime import datetime, timedelta, timezone

import pytest

from macwatch.config import BazosConfig, ConfigError
from macwatch.db import Database
from macwatch.lifecycle import RUN_NAME, BazosLifecycle
from macwatch.models import RawListing
from macwatch.pipeline import Pipeline
from macwatch.settings import (
    FIELDS,
    SettingsStore,
    coerce,
    load_effective_config,
    normalize_reference_key,
    validate_and_save,
)
from macwatch.sources.bazos import BazosSource, Detail, SearchHit, SearchPage, parse_search_page
from macwatch.sources.http import Response
from tests.conftest import ROOT, read_fixture

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class TestSearchPage:
    def test_first_page(self):
        page = parse_search_page(read_fixture("bazos", "search_page1.html"))
        assert (page.total, page.first, page.last) == (1450, 1, 20)
        assert len(page.hits) == 20
        hit = page.hits[0]
        assert hit.source_id == "223564587"
        assert hit.url.startswith("https://pc.bazos.cz/inzerat/223564587/")
        assert hit.price_raw == "38 999 Kč"
        assert (hit.locality, hit.postal_code) == ("Praha 7", "17000")

    def test_listing_date(self):
        from datetime import date
        page = parse_search_page(read_fixture("bazos", "search_page1.html"))
        assert page.hits[0].listed_on == date(2026, 9, 14)
        assert all(h.listed_on is not None for h in page.hits)

    def test_last_page(self):
        page = parse_search_page(read_fixture("bazos", "search_last.html"))
        assert (page.first, page.last, page.total) == (1441, 1450, 1450)
        assert {h.source_id for h in page.hits} >= {"221389428", "221379643"}

    def test_offset_is_added_to_url(self):
        requested = []

        class Client:
            def get(self, url):
                requested.append(url)
                return Response(url, 200, read_fixture("bazos", "search_page1.html"))

        cfg = BazosConfig(True, (), lifecycle_search_url="https://pc.bazos.cz/?hledat=macbook&crp=&kitx=ne")
        BazosSource(cfg, Client()).search_page(40)
        assert requested == ["https://pc.bazos.cz/?hledat=macbook&kitx=ne&crp=40"]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class StubSource:
    name = "bazos"
    supports_enrich = True

    def __init__(self, pages, details, cfg=None):
        self.pages = pages
        self.details = details
        self.cfg = cfg or BazosConfig(True, (), lifecycle_interval_hours=3, lifecycle_confirm_per_pass=10)
        self.checked = []

    def search_page(self, offset):
        return self.pages[offset // 20]

    def check_detail(self, url):
        self.checked.append(url)
        return self.details.get(url)


def hit(source_id, price="10 000 Kč"):
    return SearchHit(source_id, f"https://pc.bazos.cz/inzerat/{source_id}/x.php", "MacBook", price, None, None)


def seed(db, source_id, price, when):
    item = RawListing("bazos", source_id, f"https://pc.bazos.cz/inzerat/{source_id}/x.php",
                      "MacBook Air M1 8/256", str(price), "text", True, when)
    db.insert_listing(item, price, when)


@pytest.fixture
def env(cfg):
    clock = Clock()
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, None, clock=clock)
    return clock, db, pipeline


class TestLifecycle:
    def test_seen_price_drop_and_gone(self, env):
        clock, db, pipeline = env
        for sid in ("1", "2", "3", "4"):
            seed(db, sid, 10000, T0)
        clock.now = T0 + timedelta(days=2)
        pages = [SearchPage(25, 1, 20, [hit("1"), hit("2", "8 500 Kč"), hit("99")]),
                 SearchPage(25, 21, 25, [])]
        alive = Detail("MacBook", "text", "10 000 Kč", None, None)
        source = StubSource(pages, {"https://pc.bazos.cz/inzerat/4/x.php": alive})
        stats = BazosLifecycle(source, db, pipeline, clock).run()

        assert stats.complete and stats.pages == 2
        assert stats.seen_known == 2 and stats.price_changes == 1
        assert [r["price_czk"] for r in db.price_history(("bazos", "2"))] == [10000, 8500]
        assert db.get_listing(("bazos", "3"))["gone_at"] is not None
        assert db.get_listing(("bazos", "4"))["gone_at"] is None
        assert db.get_listing(("bazos", "1"))["gone_at"] is None
        added = db.get_listing(("bazos", "99"))
        assert added is not None and added["body_complete"] == 0, "neznámý MacBook se přidá a počká na detail"
        assert stats.added == 1
        assert (stats.gone, stats.confirmed_alive) == (1, 1)

    def test_incomplete_pass_marks_nothing_gone(self, env):
        clock, db, pipeline = env
        seed(db, "1", 10000, T0)
        clock.now = T0 + timedelta(days=1)

        class Failing(StubSource):
            def search_page(self, offset):
                if offset:
                    raise ValueError("HTTP 503")
                return SearchPage(100, 1, 20, [])

        stats = BazosLifecycle(Failing([], {}), db, pipeline, clock).run()
        assert not stats.complete and stats.errors
        assert db.get_listing(("bazos", "1"))["gone_at"] is None

    def test_due_respects_interval(self, env):
        clock, db, pipeline = env
        lifecycle = BazosLifecycle(StubSource([SearchPage(0, None, None, [])], {}), db, pipeline, clock)
        assert lifecycle.due()
        run = db.start_run(RUN_NAME, clock.now)
        db.finish_run(run, clock.now, items_seen=0, items_new=0, details_fetched=0, alerts_sent=0,
                      errors=0, error_text=None)
        clock.now += timedelta(hours=2)
        assert not lifecycle.due()
        clock.now += timedelta(hours=1, minutes=1)
        assert lifecycle.due()


class TestSettings:
    def write_config(self, tmp_path, config_data):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config_data), encoding="utf-8")
        return path

    def test_overrides_and_seeded_references(self, tmp_path, config_data):
        path = self.write_config(tmp_path, config_data)
        db = Database.open(":memory:")
        cfg = load_effective_config(path, db, T0)
        assert cfg.pricing.reference["bazos|air|m1|256"] == 8900

        validate_and_save(path, db, {"discord_webhook_url": "https://discord.com/api/webhooks/1/x",
                                     "travel.consumption_l_per_100km": 7.2}, T0)
        store = SettingsStore(db)
        store.set_reference("Bazos|Pro|M2 Pro|512", 24000, T0)
        store.delete_reference("bazos|air|m1|512")
        cfg = load_effective_config(path, db, T0)
        assert cfg.discord_webhook_url.endswith("/x")
        assert cfg.travel.consumption_l_per_100km == 7.2
        assert cfg.pricing.reference["bazos|pro|m2 pro|512"] == 24000
        assert "bazos|air|m1|512" not in cfg.pricing.reference, "smazaná cena se z configu nevrátí"

    def test_invalid_value_is_not_saved(self, tmp_path, config_data):
        path = self.write_config(tmp_path, config_data)
        db = Database.open(":memory:")
        with pytest.raises(ConfigError):
            validate_and_save(path, db, {"pricing.buy_ratio": 0.9, "pricing.watch_ratio": 0.5}, T0)
        assert SettingsStore(db).overrides() == {}

    @pytest.mark.parametrize("path, raw, expected", [
        ("travel.consumption_l_per_100km", "6,8", 6.8),
        ("inventory.capital_cap_czk", "50 000", 50000),
        ("travel.unlimited", "on", True),
        ("travel.unlimited", None, False),
        ("sources.bazos.feeds", "a\n\n b \n", ["a", "b"]),
    ])
    def test_coerce(self, path, raw, expected):
        field = next(f for f in FIELDS if f.path == path)
        assert coerce(field, raw) == expected

    def test_bad_number(self):
        field = next(f for f in FIELDS if f.path == "pricing.buy_ratio")
        with pytest.raises(ConfigError):
            coerce(field, "hodně")

    def test_reference_key(self):
        assert normalize_reference_key(" Bazos | AIR | m1 | 256 ") == "bazos|air|m1|256"
        with pytest.raises(ConfigError):
            normalize_reference_key("air|m1")

    def test_every_field_exists_in_example_config_or_has_default(self, config_data, tmp_path):
        # všechna pole z formuláře musí projít validací s hodnotou z configu nebo výchozí
        path = self.write_config(tmp_path, config_data)
        db = Database.open(":memory:")
        cfg = load_effective_config(path, db, T0)
        assert cfg.market.min_samples >= 1 and cfg.travel.road_factor >= 1


class TestBacklog:
    def test_unknown_listings_are_added_and_intel_titles_skipped(self, env):
        from datetime import date
        clock, db, pipeline = env
        hits = [SearchHit("10", "https://pc.bazos.cz/inzerat/10/x.php", "Apple MacBook Pro 23\u201d 512GB", "10 999 Kč",
                          "Zlín", "76001", date(2026, 9, 8)),
                SearchHit("11", "https://pc.bazos.cz/inzerat/11/x.php", "MacBook Pro 2017 i5 8GB", "5 000 Kč",
                          None, None, date(2026, 9, 9)),
                SearchHit("12", "https://pc.bazos.cz/inzerat/12/x.php", "Lenovo ThinkPad", "4 000 Kč", None, None, None)]
        stats = BazosLifecycle(StubSource([SearchPage(3, 1, 3, hits)], {}), db, pipeline, clock).run()
        assert stats.added == 2
        candidate = db.get_listing(("bazos", "10"))
        assert candidate["detail_status"] is None and candidate["published_at"].startswith("2026-09-08")
        assert candidate["locality"] == "Zlín"
        assert db.get_listing(("bazos", "11"))["detail_status"] == "skip"
        assert db.get_listing(("bazos", "12")) is None

    def test_backlog_enriches_newest_first_without_old_alerts(self, cfg):
        import dataclasses

        from macwatch.pipeline import Pipeline
        from tests.test_pipeline import FakeNotifier

        clock = Clock()
        db = Database.open(":memory:")
        notifier = FakeNotifier()
        pipeline = Pipeline(cfg, db, notifier, clock=clock)
        for sid, day in (("1", 5), ("2", 9), ("3", 7)):
            when = datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc)
            item = RawListing("bazos", sid, f"https://pc.bazos.cz/inzerat/{sid}/x.php", "Apple MacBook Pro 23",
                              "5 000 Kč", None, False, when, published_at=when)
            db.insert_listing(item, 5000, when)
            db.set_status(item.key, None, when)

        class Source:
            name = "bazos"
            supports_enrich = True
            enriched = []

            def enrich(self, item):
                Source.enriched.append(item.source_id)
                return dataclasses.replace(item, body="MacBook Air 13 Apple M1, 8 GB RAM, 256 GB SSD", body_complete=True,
                                           title="Apple MacBook Air 23", price_raw="5 000 Kč")

        stats = pipeline.enrich_backlog(Source(), 2)
        assert Source.enriched == ["2", "3"], "nejnovější první, v rámci rozpočtu"
        row = db.conn.execute("SELECT chip, verdict FROM assessments WHERE source_id = '2'").fetchone()
        assert tuple(row) == ("m1", "KOUPIT")
        assert notifier.sent == [], "starý inzerát z výpisu nepingne"
        assert stats.details_fetched == 2
        pipeline.enrich_backlog(Source(), 5)
        assert Source.enriched == ["2", "3", "1"]
        assert pipeline.enrich_backlog(Source(), 5).details_fetched == 0
