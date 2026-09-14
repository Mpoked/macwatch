import json
from datetime import datetime, timezone

import pytest

from macwatch.config import BazosConfig
from macwatch.models import RawListing
from macwatch.parsing import parse_price_czk, parse_specs
from macwatch.sources.base import ListingGone
from macwatch.sources.bazos import BazosSource, is_gone_page, listing_id, parse_detail, parse_rss
from macwatch.sources.http import Response
from tests.conftest import FIXTURES, read_fixture

NOW = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)
EXPECTED = {k: v for k, v in json.loads(read_fixture("bazos", "expected.json")).items()
            if not k.startswith("_")}


def test_listing_id():
    assert listing_id("https://pc.bazos.cz/inzerat/223774609/macbook-pro.php") == "223774609"
    assert listing_id("https://pc.bazos.cz/notebook/") is None


class TestRss:
    def test_parses_all_items(self):
        items = parse_rss(read_fixture("bazos", "rss_macbook.xml"), NOW)
        assert len(items) == 50
        assert len({i.source_id for i in items}) == 50

    def test_item_fields(self):
        items = parse_rss(read_fixture("bazos", "rss_macbook.xml"), NOW)
        item = next(i for i in items if i.source_id == "223774609")
        assert item.source == "bazos"
        assert item.title == 'MacBook Pro 14" M1 Pro 10/16 - 512GB (2021)'
        assert item.price_raw == "17 399"
        assert item.category == "pc"
        assert item.body_complete is False
        assert "<img" not in item.body
        assert item.published_at is not None and item.published_at.tzinfo is not None


class TestDetail:
    @pytest.mark.parametrize("listing", sorted(EXPECTED))
    def test_against_hand_checked_values(self, rules, listing):
        expected = EXPECTED[listing]
        detail = parse_detail(read_fixture("bazos", "detail", f"{listing}.html"))
        specs = parse_specs(detail.title, detail.body, rules)
        actual = {
            "price_czk": parse_price_czk(detail.price_raw),
            "locality": detail.locality,
            "postal_code": detail.postal_code,
            "eligible": specs.eligible,
            "family": specs.family,
            "chip": specs.chip,
            "ram_gb": specs.ram_gb,
            "ssd_gb": specs.ssd_gb,
            "year": specs.year,
            "battery_pct": specs.battery_pct,
            "cycles": specs.cycles,
            "is_dealer": specs.is_dealer,
            "red_flags": list(specs.red_flags),
        }
        assert {k: actual[k] for k in expected} == expected

    def test_detail_ignores_seller_name(self):
        detail = parse_detail(read_fixture("bazos", "detail", "223774609.html"))
        assert "Prodejce" not in detail.body
        assert detail.body.startswith("Prodám lehce použitý MacBook.")

    def test_gone_page(self):
        assert is_gone_page(410, read_fixture("bazos", "gone_410.html"))
        assert not is_gone_page(200, read_fixture("bazos", "detail", "223774609.html"))


class FakeClient:
    def __init__(self, pages: dict[str, Response]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        return self.pages[url]


class TestSource:
    FEED = "https://www.bazos.cz/rss.php?rub=pc&hledat=macbook"
    FEED2 = "https://www.bazos.cz/rss.php?rub=pc&hledat=mac+book"

    def make(self, pages):
        cfg = BazosConfig(enabled=True, feeds=(self.FEED, self.FEED2))
        return BazosSource(cfg, FakeClient(pages), clock=lambda: NOW)

    def test_poll_deduplicates_across_feeds(self):
        rss = read_fixture("bazos", "rss_macbook.xml")
        source = self.make({self.FEED: Response(self.FEED, 200, rss),
                            self.FEED2: Response(self.FEED2, 200, rss)})
        assert len(list(source.poll())) == 50

    def test_poll_skips_failed_feed(self):
        rss = read_fixture("bazos", "rss_macbook.xml")
        source = self.make({self.FEED: Response(self.FEED, 404, ""),
                            self.FEED2: Response(self.FEED2, 200, rss)})
        assert len(list(source.poll())) == 50

    def test_enrich_fills_full_text_and_locality(self):
        url = "https://pc.bazos.cz/inzerat/223774609/x.php"
        page = read_fixture("bazos", "detail", "223774609.html")
        source = self.make({url: Response(url, 200, page)})
        item = RawListing("bazos", "223774609", url, "MacBook", "17 399", "zkrácený", False, NOW)
        enriched = source.enrich(item)
        assert enriched.body_complete
        assert "Stav baterie" in enriched.body
        assert (enriched.locality, enriched.postal_code) == ("Praha 7", "17000")
        assert enriched.price_raw == "17 399 Kč"

    def test_enrich_gone(self):
        url = "https://pc.bazos.cz/inzerat/1/x.php"
        source = self.make({url: Response(url, 410, read_fixture("bazos", "gone_410.html"))})
        with pytest.raises(ListingGone):
            source.enrich(RawListing("bazos", "1", url, "MacBook", "1", "", False, NOW))
