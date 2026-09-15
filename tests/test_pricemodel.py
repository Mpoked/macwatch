import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from macwatch import pricemodel
from macwatch.db import Database
from macwatch.market import Measured, ReferenceBook
from macwatch.models import Specs
from macwatch.pricemodel import AskingSample, PriceModel, config_factor, fit, price_for
from macwatch.pricing import assess
from tests.test_web import HOST, get, post, text  # noqa: F401
from macwatch.web.server import App

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_config_factor():
    assert config_factor(16, 256, 8, 256, 18, 11) == pytest.approx(1.18)
    assert config_factor(8, 1024, 8, 256, 18, 11) == pytest.approx(1.11 ** 2)
    assert config_factor(24, 512, 16, 512, 18, 11) == pytest.approx(1.18 ** math.log2(1.5))
    assert config_factor(None, None, 16, 512, 18, 11) == 1.0, "chybějící údaj = základní konfigurace"
    model = PriceModel("air", "m1", 8, 256, 9500)
    assert price_for(model, 16, 512, 18, 11) == round(9500 * 1.18 * 1.11)


def synthetic(seed=1):
    rng = random.Random(seed)
    truth = {("air", "m1"): (8, 256, 9500), ("air", "m2"): (8, 256, 13500), ("pro", "m1 pro"): (16, 512, 18000)}
    configs = {("air", "m1"): [(8, 256), (8, 512), (16, 256), (16, 512)],
               ("air", "m2"): [(8, 256), (16, 512), (24, 512)],
               ("pro", "m1 pro"): [(16, 512), (16, 1024), (32, 512), (32, 1024)]}
    samples = []
    for (family, chip), (br, bs, price) in truth.items():
        for _ in range(30):
            ram, ssd = rng.choice(configs[(family, chip)])
            value = price * 1.18 ** math.log2(ram / br) * 1.11 ** math.log2(ssd / bs) * rng.uniform(0.93, 1.07)
            samples.append(AskingSample(family, chip, ram, ssd, round(value)))
    return truth, samples


def test_fit_recovers_known_model():
    truth, samples = synthetic()
    result = fit(samples)
    assert result.ram_pct == pytest.approx(18, abs=3)
    assert result.ssd_pct == pytest.approx(11, abs=3)
    for (family, chip), (br, bs, price) in truth.items():
        model = result.models[f"{family}|{chip}"]
        estimated = price_for(model, br, bs, result.ram_pct, result.ssd_pct)
        assert estimated == pytest.approx(price, rel=0.05), (family, chip)
    assert result.median_error_pct < 5


def test_entry_value():
    assert pricemodel.entry_value([8] * 10 + [16] * 10 + [128]) == 8
    assert pricemodel.entry_value([128] + [16] * 20) == 16, "ojedinělý překlep základ neurčí"


def test_fit_ignores_outliers_and_needs_enough_data():
    _, samples = synthetic()
    polluted = samples + [AskingSample("air", "m1", 8, 256, 900), AskingSample("air", "m1", 8, 256, 90000)]
    assert fit(polluted).models["air|m1"].base_price_czk == pytest.approx(fit(samples).models["air|m1"].base_price_czk,
                                                                          rel=0.03)
    assert fit(samples[:10]) is None


def book(**kw):
    base = dict(manual={"bazos|air|m1|256": 8900}, min_samples=3, fallback_source="bazos", ram_pct=18, ssd_pct=11)
    base.update(kw)
    return ReferenceBook(**base)


class TestLookupOrder:
    TABLE = {"air|m1": PriceModel("air", "m1", 8, 256, 9500, samples=46)}

    def test_table_scales_by_ram_and_ssd(self):
        b = book(models=self.TABLE)
        assert b.lookup("bazos", "air", "m1", 256, 8) == (9500, "model|air|m1", "table", 46)
        assert b.lookup("bazos", "air", "m1", 512, 16)[0] == round(9500 * 1.18 * 1.11)

    def test_measured_beats_table(self):
        measured = {"bazos": {"air|m1": Measured(9000, 4, 0, 8, 256)}}
        b = book(models=self.TABLE, measured=measured)
        assert b.lookup("bazos", "air", "m1", 256, 16) == (round(9000 * 1.18), "bazos|air|m1", "measured", 4)
        assert b.lookup("facebook", "air", "m1", 256, 8)[2] == "measured:bazos"

    def test_locked_beats_measured(self):
        locked = {"air|m1": PriceModel("air", "m1", 8, 256, 12000, locked=True)}
        measured = {"bazos": {"air|m1": Measured(9000, 10, 0)}}
        assert book(models=locked, measured=measured).lookup("bazos", "air", "m1", 256, 8)[:3] == \
            (12000, "model|air|m1", "manual")

    def test_too_few_measured_falls_to_table_then_legacy(self):
        measured = {"bazos": {"air|m1": Measured(9000, 2, 0)}}
        assert book(models=self.TABLE, measured=measured).lookup("bazos", "air", "m1", 256, 8)[2] == "table"
        assert book(measured=measured).lookup("bazos", "air", "m1", 256, 8)[:3] == (8900, "bazos|air|m1|256", "config")

    def test_manual_mode_skips_measured(self):
        measured = {"bazos": {"air|m1": Measured(9000, 10, 0)}}
        assert book(models=self.TABLE, measured=measured, mode="manual").lookup("bazos", "air", "m1", 256, 8)[2] == "table"


def test_assess_notes_missing_ram(cfg):
    b = book(models=TestLookupOrder.TABLE)
    a = assess("bazos", 5000, Specs(family="air", chip="m1", ssd_gb=256, eligible=True), cfg.pricing, b)
    assert any("RAM neuvedeno" in n for n in a.notes)
    assert a.reference_czk == 9500


@pytest.fixture
def db():
    return Database.open(":memory:")


def seed_snapshots(db, when):
    _, samples = synthetic()
    rows = [("bazos", str(i), f"MacBook {s.family} {s.chip}", s.price_czk, 1, s.family, s.chip, s.ram_gb, s.ssd_gb)
            for i, s in enumerate(samples)]
    pricemodel.record_snapshot(db.conn, rows, when)


class TestRefit:
    def test_refit_keeps_locked_rows_and_updates_percentages(self, db):
        seed_snapshots(db, NOW - timedelta(days=1))
        pricemodel.save_model(db.conn, PriceModel("air", "m1", 8, 256, 11111, locked=True), NOW)
        result = pricemodel.refit(db.conn, NOW, update_percentages=True)
        models = pricemodel.load_models(db.conn)
        assert models["air|m1"].base_price_czk == 11111 and models["air|m1"].locked
        assert (models["air|m2"].base_ram_gb, models["air|m2"].base_ssd_gb) == (8, 256)
        assert models["air|m2"].base_price_czk == pytest.approx(13500, rel=0.05)
        stored = {r["key"]: json.loads(r["value"]) for r in db.conn.execute("SELECT key, value FROM settings")}
        assert stored["market.ram_pct_per_doubling"] == result.ram_pct
        assert stored[pricemodel.FIT_KEY]["models"] == 2

    def test_refit_without_percentage_update(self, db):
        seed_snapshots(db, NOW)
        pricemodel.refit(db.conn, NOW, update_percentages=False)
        keys = {r["key"] for r in db.conn.execute("SELECT key FROM settings")}
        assert "market.ram_pct_per_doubling" not in keys

    def test_old_snapshots_are_ignored(self, db):
        seed_snapshots(db, NOW - timedelta(days=30))
        assert pricemodel.refit(db.conn, NOW, update_percentages=True) is None

    def test_refit_due(self, db):
        assert pricemodel.refit_due(db.conn, NOW, 7)
        seed_snapshots(db, NOW)
        pricemodel.refit(db.conn, NOW, update_percentages=True)
        assert not pricemodel.refit_due(db.conn, NOW + timedelta(days=6), 7)
        assert pricemodel.refit_due(db.conn, NOW + timedelta(days=7), 7)


def test_lifecycle_records_asking_snapshots(cfg):
    from macwatch.lifecycle import BazosLifecycle
    from macwatch.pipeline import Pipeline
    from macwatch.sources.bazos import SearchHit, SearchPage
    from tests.test_lifecycle_settings import StubSource

    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, None, clock=lambda: NOW)
    hits = [SearchHit("1", "u1", "MacBook Air M2 16GB 512GB", "18 000 Kč", None, None),
            SearchHit("2", "u2", "Lenovo ThinkPad", "5 000 Kč", None, None)]
    BazosLifecycle(StubSource([SearchPage(2, 1, 2, hits)], {}), db, pipeline, lambda: NOW).run()
    rows = {r["source_id"]: dict(r) for r in db.conn.execute("SELECT * FROM asking_snapshots")}
    assert rows["1"]["eligible"] == 1 and (rows["1"]["chip"], rows["1"]["ram_gb"], rows["1"]["ssd_gb"]) == ("m2", 16, 512)
    assert rows["1"]["price_czk"] == 18000
    assert rows["2"]["eligible"] == 0


@pytest.fixture
def app(tmp_path, config_data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    return App(path, ":memory:", clock=lambda: NOW)


class TestPricesPage:
    def test_refit_button_fills_table(self, app):
        seed_snapshots(app._shared_db, NOW)
        resp = post(app, "/ceny/prepocitat", {})
        assert resp.status == 303
        html = text(get(app, "/ceny"))
        assert "MacBook Air M1" in html and "MacBook Pro M1 Pro" in html
        assert "z 30 inzerátů" in html

    def test_refit_button_without_data(self, app):
        resp = post(app, "/ceny/prepocitat", {})
        assert "m%C3%A1lo" in dict(resp.headers)["Location"]

    def test_save_locks_and_unlock(self, app):
        post(app, "/ceny/model", {"family": "air", "chip": "M3 ", "base_ram_gb": "8", "base_ssd_gb": "256",
                                  "base_price_czk": "16 500"})
        model = pricemodel.load_models(app._shared_db.conn)["air|m3"]
        assert (model.base_price_czk, model.locked) == (16500, True)
        assert "zamčeno ručně" in text(get(app, "/ceny"))
        post(app, "/ceny/model/odemknout", {"family": "air", "chip": "m3"})
        assert not pricemodel.load_models(app._shared_db.conn)["air|m3"].locked
        post(app, "/ceny/model/smazat", {"family": "air", "chip": "m3"})
        assert "air|m3" not in pricemodel.load_models(app._shared_db.conn)

    def test_invalid_model(self, app):
        resp = post(app, "/ceny/model", {"family": "imac", "chip": "m3", "base_ram_gb": "8", "base_ssd_gb": "256",
                                         "base_price_czk": "1"})
        assert "Neulo" in dict(resp.headers)["Location"] or "Neulo%C5%BEeno" in dict(resp.headers)["Location"]
        assert pricemodel.load_models(app._shared_db.conn) == {}

    def test_percentages_save_and_recompute(self, app):
        from tests.test_web import seed_listing
        key = seed_listing(app)  # Air M1 8/256
        pricemodel.save_model(app._shared_db.conn, PriceModel("air", "m1", 8, 256, 10000, locked=True), NOW)
        post(app, "/ceny/prirazky", {"ram_pct": "20", "ssd_pct": "10"})
        cfg_values = {r["key"]: json.loads(r["value"]) for r in app._shared_db.conn.execute("SELECT key, value FROM settings")}
        assert cfg_values["market.ram_pct_per_doubling"] == 20.0
        assert cfg_values["market.auto_percentages"] is False
        ref = app._shared_db.conn.execute("SELECT reference_czk FROM assessments WHERE source_id = ?", (key[1],)).fetchone()[0]
        assert ref == 10000
