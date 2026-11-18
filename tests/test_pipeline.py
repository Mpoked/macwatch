import dataclasses
from datetime import datetime, timezone

from macwatch.db import Database
from macwatch.models import RawListing, Verdict
from macwatch.pipeline import Pipeline, price_changed
from macwatch.sources.base import ListingGone

NOW = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)


def rss_item(source_id="1", title="MacBook Air M1 8/256", price="5 000",
             body="Prodám MacBook, volejte 777 123 456 ..."):
    return RawListing("bazos", source_id, f"https://pc.bazos.cz/inzerat/{source_id}/x.php",
                      title, price, body, False, NOW)


class FakeSource:
    name = "bazos"
    supports_enrich = True

    def __init__(self, items, details=None, gone=()):
        self.items = items
        self.details = details or {}
        self.gone = set(gone)
        self.enriched: list[str] = []

    def poll(self):
        yield from self.items

    def enrich(self, item):
        self.enriched.append(item.source_id)
        if item.source_id in self.gone:
            raise ListingGone(item.url)
        body = self.details.get(item.source_id, "Plný text, baterie 90 %, 100 cyklů.")
        return dataclasses.replace(item, body=body, body_complete=True,
                                   price_raw=item.price_raw + " Kč", locality="Brno",
                                   postal_code="60200")


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent = []
        self.contents = []
        self.ok = ok

    def send(self, embed, content=None):
        self.sent.append(embed)
        self.contents.append(content)
        return self.ok


def make(cfg, notifier=None, max_details=None):
    db = Database.open(":memory:")
    return Pipeline(cfg, db, notifier, clock=lambda: NOW, max_details=max_details), db


def test_new_bargain_is_enriched_stored_and_alerted_once(cfg):
    notifier = FakeNotifier()
    pipeline, db = make(cfg, notifier)
    source = FakeSource([rss_item()])

    findings, stats = pipeline.run_source(source)
    assert [f.assessment.verdict for f in findings] == [Verdict.KOUPIT]
    assert len(notifier.sent) == 1
    row = db.get_listing(("bazos", "1"))
    assert row["body_complete"] == 1 and row["locality"] == "Brno"
    assert row["alerted_at"] is not None
    assert (stats.items_new, stats.details_fetched, stats.alerts_sent) == (1, 1, 1)

    pipeline.run_source(source)
    assert len(notifier.sent) == 1, "stejný inzerát se nepingá dvakrát"
    assert source.enriched == ["1"], "detail se stahuje jen jednou"
    assert len(db.price_history(("bazos", "1"))) == 1, "'5 000' a '5 000 Kč' není změna ceny"


def test_contacts_are_redacted_before_storage(cfg):
    pipeline, db = make(cfg)
    source = FakeSource([rss_item()], details={"1": "Volejte +420 777 123 456, mail a@b.cz"})
    pipeline.run_source(source)
    body = db.get_listing(("bazos", "1"))["body"]
    assert "777" not in body and "a@b.cz" not in body
    assert "[telefon]" in body and "[e-mail]" in body


def test_irrelevant_titles_are_skipped(cfg):
    pipeline, db = make(cfg)
    findings, stats = pipeline.run_source(FakeSource([rss_item(title="Lenovo ThinkPad")]))
    assert findings == [] and stats.items_seen == 1 and stats.items_new == 0


def test_price_drop_is_recorded_and_can_trigger_alert(cfg):
    notifier = FakeNotifier()
    pipeline, db = make(cfg, notifier)
    pipeline.run_source(FakeSource([rss_item(price="8 000")]))
    assert notifier.sent == []
    findings, _ = pipeline.run_source(FakeSource([rss_item(price="5 500")]))
    assert [r["price_czk"] for r in db.price_history(("bazos", "1"))] == [8000, 5500]
    assert findings[0].assessment.verdict == Verdict.KOUPIT
    assert len(notifier.sent) == 1


def test_no_alert_before_full_text(cfg):
    notifier = FakeNotifier()
    pipeline, db = make(cfg, notifier, max_details=0)
    findings, _ = pipeline.run_source(FakeSource([rss_item()]))
    assert findings[0].assessment.verdict == Verdict.KOUPIT
    assert notifier.sent == [], "bez detailu nevíme o červených vlajkách"


def test_detail_budget_is_respected(cfg):
    pipeline, _ = make(cfg, max_details=2)
    source = FakeSource([rss_item(str(i)) for i in range(5)])
    _, stats = pipeline.run_source(source)
    assert stats.details_fetched == 2
    pipeline.run_source(source)
    assert len(source.enriched) == 4, "zbylé detaily se dostahují v dalším průchodu"


def test_listing_gone_before_detail(cfg):
    pipeline, db = make(cfg)
    findings, stats = pipeline.run_source(FakeSource([rss_item()], gone={"1"}))
    assert findings == []
    assert db.get_listing(("bazos", "1"))["gone_at"] is not None
    assert stats.errors == []


def test_one_broken_item_does_not_stop_the_run(cfg):
    class Broken(FakeSource):
        def enrich(self, item):
            if item.source_id == "1":
                raise ValueError("rozbitá stránka")
            return super().enrich(item)

    pipeline, db = make(cfg)
    findings, stats = pipeline.run_source(Broken([rss_item("1"), rss_item("2")]))
    assert [f.item.source_id for f in findings] == ["2"]
    assert len(stats.errors) == 1
    run = db.conn.execute("SELECT errors, items_seen FROM poll_runs").fetchone()
    assert tuple(run) == (1, 2)


def test_failed_notification_is_retried_next_run(cfg):
    notifier = FakeNotifier(ok=False)
    pipeline, db = make(cfg, notifier)
    pipeline.run_source(FakeSource([rss_item()]))
    assert db.get_listing(("bazos", "1"))["alerted_at"] is None
    notifier.ok = True
    pipeline.run_source(FakeSource([rss_item()]))
    assert len(notifier.sent) == 2
    assert db.get_listing(("bazos", "1"))["alerted_at"] is not None


def test_price_changed():
    assert not price_changed(17399, "17 399", 17399, "17 399 Kč")
    assert price_changed(17399, "17 399", 16000, "16 000")
    assert not price_changed(None, "Dohodou", None, "dohodou")
    assert price_changed(None, "Dohodou", 9000, "9 000")


def test_distance_and_travel_cost_are_stored(cfg):
    from macwatch.geo import default_gazetteer

    import dataclasses as dc
    cfg = dc.replace(cfg, travel=dc.replace(cfg.travel, home_psc="68601", max_km=None))
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, None, clock=lambda: NOW, gazetteer=default_gazetteer())
    source = FakeSource([rss_item()])  # detail doplní Brno, PSČ 60200
    findings, _ = pipeline.run_source(source)
    a = findings[0].assessment
    assert 60 < a.distance_km < 110
    assert a.travel_cost_czk > 0
    row = db.conn.execute("SELECT distance_km, travel_cost_czk FROM assessments").fetchone()
    assert row["distance_km"] == a.distance_km


def test_reassess_active_picks_up_new_reference(cfg):
    import dataclasses as dc
    notifier = FakeNotifier()
    pipeline, db = make(cfg, notifier)
    pipeline.run_source(FakeSource([rss_item(title="MacBook Pro M3 Max 36/1TB", price="30 000")]))
    assert notifier.sent == []
    assert db.conn.execute("SELECT verdict FROM assessments").fetchone()[0] == Verdict.NEZNAMY_MODEL

    reference = {**cfg.pricing.reference, "bazos|pro|m3 max": 60000}
    pipeline.refresh(dc.replace(cfg, pricing=dc.replace(cfg.pricing, reference=reference)))
    stats = pipeline.reassess_active({"bazos"})
    assert db.conn.execute("SELECT verdict FROM assessments").fetchone()[0] == Verdict.KOUPIT
    assert stats.alerts_sent == 1 and len(notifier.sent) == 1


def _clocked(cfg, notifier, start):
    clock = {"now": start}
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, notifier, clock=lambda: clock["now"])
    return pipeline, db, clock


def test_old_bargain_is_not_pinged_when_webhook_comes_late(cfg):
    from datetime import timedelta
    pipeline, db, clock = _clocked(cfg, None, NOW)   # bez webhooku
    old = dataclasses.replace(rss_item(), published_at=NOW)
    pipeline.run_source(FakeSource([old]))
    assert db.get_listing(("bazos", "1"))["alerted_at"] is None

    clock["now"] = NOW + timedelta(hours=5)          # webhook přidán po pěti hodinách
    notifier = FakeNotifier()
    pipeline.notifier = notifier
    pipeline.run_source(FakeSource([old]))
    pipeline.reassess_active({"bazos"})
    assert notifier.sent == [], "pět hodin starý kus už se nepingá"


def test_price_drop_makes_old_listing_fresh_again(cfg):
    from datetime import timedelta
    notifier = FakeNotifier()
    pipeline, db, clock = _clocked(cfg, notifier, NOW)
    pipeline.run_source(FakeSource([dataclasses.replace(rss_item(price="9 000"), published_at=NOW)]))
    clock["now"] = NOW + timedelta(hours=10)
    pipeline.run_source(FakeSource([dataclasses.replace(rss_item(price="5 000"), published_at=NOW)]))
    assert len(notifier.sent) == 1
    assert "Zlevněno před 0 min" in notifier.sent[0]["description"]


def test_age_limit_zero_means_no_limit(cfg):
    from datetime import timedelta
    cfg = dataclasses.replace(cfg, pricing=dataclasses.replace(cfg.pricing, alert_max_age_hours=0))
    notifier = FakeNotifier()
    pipeline, db, clock = _clocked(cfg, notifier, NOW + timedelta(days=2))
    pipeline.run_source(FakeSource([dataclasses.replace(rss_item(), published_at=NOW)]))
    assert len(notifier.sent) == 1
    assert "Vloženo před" in notifier.sent[0]["description"]


def test_long_unsold_bargain_is_only_watched_until_price_drops(cfg):
    from datetime import timedelta
    cfg = dataclasses.replace(cfg, pricing=dataclasses.replace(cfg.pricing, alert_max_age_hours=0))
    notifier = FakeNotifier()
    pipeline, db, clock = _clocked(cfg, notifier, NOW + timedelta(days=30))
    (found,), _ = pipeline.run_source(FakeSource([dataclasses.replace(rss_item(), published_at=NOW)]))
    assert found.assessment.verdict == Verdict.SLEDOVAT and notifier.sent == []
    assert "visí 30 dní bez prodeje" in found.assessment.notes
    (found,), _ = pipeline.run_source(FakeSource([dataclasses.replace(rss_item(price="4 500"), published_at=NOW)]))
    assert found.assessment.verdict == Verdict.KOUPIT and len(notifier.sent) == 1, "zlevnění = nová šance"


def test_hot_bargain_mentions_here(cfg):
    cfg = dataclasses.replace(cfg, pricing=dataclasses.replace(cfg.pricing, alert_max_age_hours=0, hot_profit_czk=1))
    notifier = FakeNotifier()
    pipeline, db, clock = _clocked(cfg, notifier, NOW)
    pipeline.run_source(FakeSource([dataclasses.replace(rss_item(), published_at=NOW)]))
    assert notifier.contents == ["@here 🔥"] and notifier.sent[0]["title"].startswith("🔥")
