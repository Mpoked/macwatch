from datetime import datetime, timezone
from pathlib import Path

import pytest

from macwatch.config import SbazarConfig
from macwatch.sources.base import ListingGone
from macwatch.sources.http import Response
from macwatch.sources.sbazar import SbazarSource, parse_search

FIX = Path(__file__).parent / "fixtures" / "sbazar"
NOW = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        for key, (status, text) in self.pages.items():
            if key in url:
                return Response(url, status, text)
        raise AssertionError(url)


def read(name):
    return (FIX / name).read_text("utf-8")


def test_parse_search():
    items, total = parse_search(read("search.json"), NOW)
    assert total == 3 and len(items) == 3
    pro = items[1]
    assert pro.key == ("sbazar", "235981185")
    assert pro.url == "https://www.sbazar.cz/inzerat/235981185-macbook-pro-16-m2-pro-16gb512gb"
    assert pro.price_raw == "29900 Kč" and pro.locality == "České Budějovice"
    assert pro.published_at == datetime(2026, 11, 18, 13, 49, 40, tzinfo=timezone.utc), "Praha je v listopadu +1"
    assert not pro.body_complete
    assert items[2].price_raw == "dohodou"


def test_poll_walks_all_pages():
    client = FakeClient({"offset=0": (200, read("search.json"))})
    items = list(SbazarSource(SbazarConfig(enabled=True), client, lambda: NOW).poll())
    assert len(items) == 3 and len(client.urls) == 1, "total 3 = jedna stránka"


def test_enrich_reads_description_and_detects_gone():
    item = parse_search(read("search.json"), NOW)[0][1]
    source = SbazarSource(SbazarConfig(enabled=True), FakeClient({"/235981185": (200, read("detail.json"))}), lambda: NOW)
    full = source.enrich(item)
    assert full.body_complete and "M2 Pro" in full.body
    gone = SbazarSource(SbazarConfig(enabled=True), FakeClient({"/235981185": (404, "")}), lambda: NOW)
    with pytest.raises(ListingGone):
        gone.enrich(item)
