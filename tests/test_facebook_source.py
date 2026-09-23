from datetime import datetime, timezone

import pytest

from macwatch.config import FacebookConfig
from macwatch.db import Database
from macwatch.models import Verdict
from macwatch.parsing import parse_price_czk, parse_specs
from macwatch.pipeline import Pipeline
from macwatch.sources.facebook import FacebookSource, parse_description, parse_feed, parse_session
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


class TestJsonFeed:
    """Feed z fb-mp-rss, který čte JSON ze stránky: čas vložení, cena, stav."""

    def items(self):
        return {i.source_id: i for i in parse_feed(read_fixture("facebook", "rss_feed_json.xml"), NOW)}

    def test_listed_at_is_real_listing_time(self):
        item = self.items()["1152292163893963"]
        assert item.published_at == datetime(2026, 9, 14, 14, 36, 22, tzinfo=timezone.utc)
        assert item.locality == "Opava"
        assert parse_price_czk(item.price_raw) == 10490

    def test_flags_default_false(self):
        assert not any(i.sold or i.reserved for i in self.items().values())

    def test_sold_and_reserved_flags(self):
        xml = read_fixture("facebook", "rss_feed_json.xml")
        xml = xml.replace("price: CZK10,490", "price: CZK10,490\nis_pending: true", 1)
        xml = xml.replace("price: CZK4,950", "price: CZK4,950\nis_sold: true", 1)
        parsed = {i.source_id: i for i in parse_feed(xml, NOW)}
        assert parsed["1152292163893963"].reserved
        assert parsed["2340073290138692"].sold


def test_pipeline_sold_goes_to_market_data_and_reserved_is_not_alerted(cfg):
    sent = []

    class Notifier:
        def send(self, embed):
            sent.append(embed)
            return True

    xml = read_fixture("facebook", "rss_feed_json.xml")
    # levný Air M1 (1077572985134072) označíme jako rezervovaný, Rose Gold jako prodaný
    def flag(xml, source_id, line):
        start = xml.index(f"/marketplace/item/{source_id}/")
        at = xml.index("price: ", start)
        return xml[:at] + line + "\n" + xml[at:]

    xml = flag(xml, "1077572985134072", "is_pending: true")
    xml = flag(xml, "2340073290138692", "is_sold: true")
    source = FacebookSource(FacebookConfig(True, FEED_URL), FakeClient(Response(FEED_URL, 200, xml)),
                            clock=lambda: NOW)
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, Notifier(), clock=lambda: NOW)
    findings, _ = pipeline.run_source(source)
    reserved = next(f for f in findings if f.item.source_id == "1077572985134072")
    assert reserved.item.reserved
    assert reserved.assessment.should_alert and not reserved.alerted
    assert db.get_listing(reserved.item.key)["status"] == "reserved"
    sold = db.get_listing(("facebook", "2340073290138692"))
    assert sold["gone_at"] is not None and sold["gone_reason"] == "prodáno"
    assert all("Rose Gold" not in e["title"] for e in sent)
    pipeline.reassess_active(set())
    assert db.get_listing(reserved.item.key)["alerted_at"] is None, "rezervovaný se nepingne ani při přepočtu"


def feed_with_session(state):
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            f'<description>Facebook Marketplace ads, session: {state}</description></channel></rss>')


def test_session_state_from_channel():
    assert parse_session(feed_with_session("expired")) == "expired"
    assert parse_session(read_fixture("facebook", "rss_feed.xml")) is None  # starší scraper


class RecordingNotifier:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, embed):
        self.sent.append(embed)
        return self.ok


def test_expired_login_is_reported_once_and_again_after_recovery(cfg):
    client = FakeClient(Response(FEED_URL, 200, feed_with_session("expired")))
    source = FacebookSource(FacebookConfig(True, FEED_URL), client, clock=lambda: NOW)
    notifier = RecordingNotifier()
    pipeline = Pipeline(cfg, Database.open(":memory:"), notifier, clock=lambda: NOW)
    pipeline.run_source(source)
    pipeline.run_source(source)
    assert len(notifier.sent) == 1
    assert "cookies" in notifier.sent[0]["description"]
    client.response = Response(FEED_URL, 200, feed_with_session("ok"))
    pipeline.run_source(source)
    client.response = Response(FEED_URL, 200, feed_with_session("expired"))
    pipeline.run_source(source)
    assert len(notifier.sent) == 2


def test_failed_login_notice_is_retried(cfg):
    source = FacebookSource(FacebookConfig(True, FEED_URL), FakeClient(
        Response(FEED_URL, 200, feed_with_session("expired"))), clock=lambda: NOW)
    notifier = RecordingNotifier(ok=False)
    pipeline = Pipeline(cfg, Database.open(":memory:"), notifier, clock=lambda: NOW)
    pipeline.run_source(source)
    notifier.ok = True
    pipeline.run_source(source)
    assert len(notifier.sent) == 2
