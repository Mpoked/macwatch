from datetime import datetime, timezone

import pytest

from macwatch.config import FacebookConfig
from macwatch.db import Database
from macwatch.models import Verdict
from macwatch.parsing import parse_price_czk, parse_specs
from macwatch.pipeline import Pipeline
from macwatch.sources.facebook import FacebookSource, parse_description, parse_feed
from macwatch.sources.http import FetchError, Response
from tests.conftest import read_fixture

NOW = datetime(2026, 9, 14, 13, 10, tzinfo=timezone.utc)
FEED_URL = "http://127.0.0.1:5000/rss"


def items():
    return {i.source_id: i for i in parse_feed(read_fixture("facebook", "rss_feed.xml"), NOW)}


def test_feed_items_and_ids():
    parsed = items()
    assert len(parsed) == 8
    item = parsed["1702868288072744"]
    assert item.source == "facebook"
    assert item.url == "https://www.facebook.com/marketplace/item/1702868288072744/"
    assert item.title == "MacBook Pro 13” M1 Touch Bar 512GB SSD, 8GB RAM"
    assert item.price_raw == "CZK14,490"
    assert item.body is None
    assert item.published_at is not None


def test_locality_without_country():
    parsed = items()
    assert parsed["1702868288072744"].locality == "Ostrava"
    assert parsed["1077572985134072"].locality == "Nesovice"


@pytest.mark.parametrize("raw, expected", [
    ("CZK14,490", 14490), ("CZK4,950", 4950), ("CZK21,999", 21999), ("CZK1,234", 1234)])
def test_facebook_price_format(raw, expected):
    assert parse_price_czk(raw) == expected


def test_specs_from_titles_only(rules):
    parsed = items()
    s = parse_specs(parsed["1702868288072744"].title, None, rules)
    assert (s.family, s.chip, s.ram_gb, s.ssd_gb, s.eligible) == ("pro", "m1", 8, 512, True)
    s = parse_specs(parsed["1077572985134072"].title, None, rules)
    assert (s.family, s.chip, s.year, s.eligible) == ("air", "m1", 2020, True)
    assert not parse_specs(parsed["1855270189219720"].title, None, rules).eligible  # 2017 Intel
    assert not parse_specs(parsed["1803121867802866"].title, None, rules).eligible  # jen "MacBook"


def test_description_fields():
    assert parse_description("price: CZK5,000\noriginal_price: CZK9,500\nlocation: Zlín") == \
        {"price": "CZK5,000", "original_price": "CZK9,500", "location": "Zlín"}


class FakeClient:
    def __init__(self, response):
        self.response = response

    def get(self, url):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_poll_reads_feed():
    source = FacebookSource(FacebookConfig(True, FEED_URL), FakeClient(
        Response(FEED_URL, 200, read_fixture("facebook", "rss_feed.xml"))), clock=lambda: NOW)
    assert len(list(source.poll())) == 8


def test_poll_reports_stopped_container():
    source = FacebookSource(FacebookConfig(True, FEED_URL), FakeClient(FetchError("refused")))
    with pytest.raises(FetchError, match="běží kontejner"):
        list(source.poll())


def test_pipeline_alerts_facebook_without_detail_using_bazos_reference(cfg):
    sent = []

    class Notifier:
        def send(self, embed):
            sent.append(embed)
            return True

    feed = read_fixture("facebook", "rss_feed.xml")
    source = FacebookSource(FacebookConfig(True, FEED_URL),
                            FakeClient(Response(FEED_URL, 200, feed)), clock=lambda: NOW)
    pipeline = Pipeline(cfg, Database.open(":memory:"), Notifier(), clock=lambda: NOW)
    findings, stats = pipeline.run_source(source)

    m1_air = next(f for f in findings if f.item.source_id == "1077572985134072")
    assert m1_air.price_czk == 5000
    assert m1_air.assessment.verdict == Verdict.KOUPIT  # 5000 <= 0.65 * 8900
    assert m1_air.assessment.reference_origin == "config:bazos"
    assert stats.details_fetched == 0
    titles = [e["title"] for e in sent]
    assert any("MacBook Air M1" in t for t in titles)
    assert any("Reference převzatá z Bazoše" in e["description"] for e in sent)
    assert sent[0]["footer"]["text"] == "Marketplace"
