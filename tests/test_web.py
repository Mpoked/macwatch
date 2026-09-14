import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from macwatch.db import Database
from macwatch.inventory import Inventory
from macwatch.models import Assessment, RawListing, Specs, Verdict
from macwatch.settings import SettingsStore, load_effective_config
from macwatch.web.server import App, Request, same_origin
from macwatch.web.views import FormError, parse_item_form

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
HOST = {"host": "mac.example.cz", "origin": "https://mac.example.cz"}


class FakeNotifier:
    sent: list = []

    def __init__(self, url):
        self.url = url

    def send(self, embed):
        FakeNotifier.sent.append((self.url, embed))
        return True


@pytest.fixture
def app(tmp_path, config_data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    FakeNotifier.sent = []
    return App(path, ":memory:", clock=lambda: NOW, notifier_factory=FakeNotifier)


def get(app, path, **query):
    return app.handle(Request("GET", path, {k: str(v) for k, v in query.items()}, {}, dict(HOST)))


def post(app, path, form, headers=None):
    return app.handle(Request("POST", path, {}, {k: str(v) for k, v in form.items()},
                              headers if headers is not None else dict(HOST)))


def text(resp):
    return resp.body.decode("utf-8")


def location(resp):
    return dict(resp.headers)["Location"]


def seed_listing(app, source_id="1", price=5000, verdict=Verdict.KOUPIT, roi=0.6, title="MacBook Air M1 8/256"):
    db = app._shared_db
    item = RawListing("bazos", source_id, f"https://pc.bazos.cz/inzerat/{source_id}/x.php", title,
                      f"{price} Kč", "Prodám, <script>alert(1)</script>", True, NOW, locality="Brno",
                      postal_code="60200")
    db.insert_listing(item, price, NOW - timedelta(hours=2))
    specs = Specs(family="air", chip="m1", ram_gb=8, ssd_gb=256, eligible=True)
    db.save_assessment(item.key, specs, Assessment(
        verdict, True, reference_czk=8900, adjusted_reference_czk=8900, est_profit_czk=8900 - price,
        roi=roi, score=roi * 100, reference_origin="config", distance_km=42.0, travel_cost_czk=400), NOW)
    return item.key


class TestPages:
    @pytest.mark.parametrize("path", ["/", "/shortlist", "/trh", "/ceny", "/sklad", "/sklad/novy",
                                      "/nastaveni", "/stav", "/zdravi"])
    def test_pages_render(self, app, path):
        seed_listing(app)
        resp = get(app, path)
        assert resp.status == 200, text(resp)

    def test_findings_sorted_by_score_and_escaped(self, app):
        seed_listing(app, "1", 5000, roi=0.3, title="Horší <b>kus</b>")
        seed_listing(app, "2", 4000, roi=0.9, title="Lepší kus")
        html = text(get(app, "/"))
        assert html.index("Lepší kus") < html.index("Horší")
        assert "<b>kus</b>" not in html and "&lt;b&gt;kus&lt;/b&gt;" in html
        assert "42 km" in html

    def test_findings_filters(self, app):
        seed_listing(app, "1", verdict=Verdict.NEVYPLATI_SE, title="Drahý kus")
        assert "Drahý kus" not in text(get(app, "/"))
        assert "Drahý kus" in text(get(app, "/", show="vse"))
        assert "Drahý kus" not in text(get(app, "/", show="vse", km=10))

    def test_detail_escapes_body(self, app):
        seed_listing(app)
        html = text(get(app, "/inzerat/bazos/1"))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert get(app, "/inzerat/bazos/999").status == 404

    def test_unknown_page_and_method(self, app):
        assert get(app, "/neexistuje").status == 404
        assert post(app, "/", {}).status == 405


class TestSecurity:
    def test_cross_site_post_rejected(self, app):
        resp = post(app, "/nastaveni", {"poll_interval_seconds": "60"},
                    {"host": "mac.example.cz", "origin": "https://evil.example"})
        assert resp.status == 403

    def test_post_without_origin_rejected(self, app):
        assert post(app, "/ceny/smazat", {"key": "x"}, {"host": "mac.example.cz"}).status == 403

    def test_forwarded_host(self):
        req = Request("POST", "/", headers={"host": "127.0.0.1:8080", "x-forwarded-host": "mac.example.cz",
                                           "origin": "https://mac.example.cz"})
        assert same_origin(req)

    def test_open_redirect_blocked(self, app):
        key = seed_listing(app)
        resp = post(app, "/shortlist/prepnout", {"source": key[0], "source_id": key[1], "next": "//evil.example"})
        assert location(resp).startswith("/?")


class TestShortlist:
    def test_toggle_and_note(self, app):
        seed_listing(app)
        resp = post(app, "/shortlist/prepnout", {"source": "bazos", "source_id": "1", "next": "/"})
        assert resp.status == 303
        assert "Lepší" not in text(get(app, "/shortlist")) and "MacBook Air M1 8/256" in text(get(app, "/shortlist"))
        post(app, "/shortlist/poznamka", {"source": "bazos", "source_id": "1", "note": "zavolat večer"})
        assert "zavolat večer" in text(get(app, "/shortlist"))
        post(app, "/shortlist/prepnout", {"source": "bazos", "source_id": "1"})
        assert "Shortlist je prázdný" in text(get(app, "/shortlist"))


class TestSettings:
    def form(self, app, **changes):
        html = text(get(app, "/nastaveni"))
        form = {}
        for name, value in re.findall(r'<input id="[^"]+" name="([^"]+)" type="(?:text|number)" value="([^"]*)"', html):
            form[name] = value.replace("&#x27;", "'").replace("&amp;", "&")
        for name in re.findall(r'<input type=checkbox name="([^"]+)" checked>', html):
            form[name] = "on"
        for name, body in re.findall(r'<textarea id="[^"]+" name="([^"]+)">(.*?)</textarea>', html, re.S):
            form[name] = body.replace("&amp;", "&")
        for name, value in re.findall(r'<select id="[^"]+" name="([^"]+)">.*?<option value="([^"]*)" selected>', html, re.S):
            form[name] = value
        form.update(changes)
        return form

    def test_save_webhook_consumption_and_home(self, app):
        form = self.form(app, **{"discord_webhook_url": "https://discord.com/api/webhooks/1/abc",
                                 "travel.consumption_l_per_100km": "7,5", "travel.home_psc": "68601",
                                 "travel.max_km": "120"})
        resp = post(app, "/nastaveni", form)
        assert resp.status == 303, text(resp)
        cfg = load_effective_config(app.config_path, app._shared_db, NOW)
        assert cfg.discord_webhook_url.endswith("/abc")
        assert cfg.travel.consumption_l_per_100km == 7.5
        assert cfg.travel.max_km == 120
        html = text(get(app, "/nastaveni"))
        assert "abc" not in html.split("končí")[0], "webhook se v HTML celý nevypisuje"
        assert "okres Uherské Hradiště" in html

        # prázdné pole webhook nechá, zaškrtnutí ho smaže
        post(app, "/nastaveni", self.form(app))
        assert load_effective_config(app.config_path, app._shared_db, NOW).discord_webhook_url.endswith("/abc")
        post(app, "/nastaveni", self.form(app, clear_webhook="on"))
        assert load_effective_config(app.config_path, app._shared_db, NOW).discord_webhook_url == ""

    def test_invalid_settings_show_error(self, app):
        resp = post(app, "/nastaveni", self.form(app, **{"pricing.buy_ratio": "0.95", "pricing.watch_ratio": "0.5"}))
        assert resp.status == 400
        assert "Neuloženo" in text(resp)

    def test_unlimited_distance(self, app):
        post(app, "/nastaveni", self.form(app, **{"travel.unlimited": "on", "travel.max_km": "50"}))
        assert load_effective_config(app.config_path, app._shared_db, NOW).travel.max_km is None

    def test_webhook_test_message(self, app):
        assert "není nastavený" in unquote(location(post(app, "/nastaveni/test-webhook", {})))
        post(app, "/nastaveni", self.form(app, discord_webhook_url="https://discord.com/api/webhooks/9/z"))
        resp = post(app, "/nastaveni/test-webhook", {})
        assert "odeslána" in unquote(location(resp))
        assert FakeNotifier.sent[-1][0].endswith("/9/z")


class TestPrices:
    def test_add_edit_delete(self, app):
        get(app, "/ceny")  # první načtení zkopíruje ceny z configu
        post(app, "/ceny/ulozit", {"key": "bazos|pro|m2 pro|512", "price_czk": "24000"})
        assert SettingsStore(app._shared_db).references()["bazos|pro|m2 pro|512"] == 24000
        post(app, "/ceny/ulozit", {"key": "bazos|pro|m2 pro|512", "price_czk": "23500"})
        assert SettingsStore(app._shared_db).references()["bazos|pro|m2 pro|512"] == 23500
        post(app, "/ceny/smazat", {"key": "bazos|pro|m2 pro|512"})
        assert "bazos|pro|m2 pro|512" not in SettingsStore(app._shared_db).references()

    def test_invalid_key(self, app):
        resp = post(app, "/ceny/ulozit", {"key": "air|m1", "price_czk": "9000"})
        assert "Neuloženo" in unquote(location(resp))


class TestInventoryWeb:
    def test_create_from_listing_add_cost_sell(self, app):
        seed_listing(app)
        prefill = text(get(app, "/sklad/novy", source="bazos", source_id="1"))
        assert 'value="5000"' in prefill and 'value="84"' in prefill  # 42 km tam i zpět

        resp = post(app, "/sklad/novy", {"title": "Air M1", "family": "air", "chip": "m1", "ssd_gb": "256",
                                         "bought_at": "2026-09-01", "buy_price_czk": "5000", "travel_km": "84",
                                         "source": "bazos", "source_id": "1"})
        assert resp.status == 303
        item_url = location(resp).split("?")[0]
        post(app, item_url + "/naklad", {"spent_at": "2026-09-02", "kind": "dily", "description": "baterie",
                                         "amount_czk": "900"})
        form = {"title": "Air M1", "family": "air", "chip": "m1", "ssd_gb": "256", "bought_at": "2026-09-01",
                "buy_price_czk": "5000", "travel_km": "84", "sold_at": "2026-09-10", "sale_price_czk": "9500"}
        assert post(app, item_url, form).status == 303
        page = text(get(app, "/sklad"))
        assert "prodáno" in page
        item = Inventory(app._shared_db.conn).items()[0]
        assert item["costs_czk"] == 900 and item["sale_price_czk"] == 9500

    def test_capital_cap_warning(self, app):
        Inventory(app._shared_db.conn).add(dict(title="Pro", bought_at="2026-09-01", buy_price_czk=30000), NOW)
        SettingsStore(app._shared_db).save({"inventory.capital_cap_czk": 20000}, NOW)
        assert "překračuje strop" in text(get(app, "/sklad"))

    def test_form_validation(self):
        with pytest.raises(FormError):
            parse_item_form({"title": "x", "bought_at": "", "buy_price_czk": "100"})
        with pytest.raises(FormError):
            parse_item_form({"title": "x", "bought_at": "2026-09-10", "buy_price_czk": "100",
                             "sold_at": "2026-09-01", "sale_price_czk": "200"})
        with pytest.raises(FormError):
            parse_item_form({"title": "x", "bought_at": "10.9.2026", "buy_price_czk": "100"})

    def test_invalid_form_rerenders(self, app):
        resp = post(app, "/sklad/novy", {"title": "", "bought_at": "2026-09-01", "buy_price_czk": "1"})
        assert resp.status == 400 and "povinné" in text(resp)


def test_status_warns_when_service_stale(app):
    db = app._shared_db
    run = db.start_run("bazos", NOW - timedelta(hours=2))
    db.finish_run(run, NOW - timedelta(hours=2), items_seen=1, items_new=0, details_fetched=0,
                  alerts_sent=0, errors=0, error_text=None)
    assert "neběžel přes 30 minut" in text(get(app, "/stav"))


def test_real_http_server_roundtrip(tmp_path, config_data, monkeypatch):
    """Skutečný server na náhodném portu, spojení jen na localhost."""
    import http.client
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from macwatch.web.server import make_handler

    monkeypatch.undo()  # testy jinak síť blokují, tady potřebujeme loopback
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    db_path = tmp_path / "web.db"
    Database.open(db_path).close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(path, db_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/nastaveni")
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200 and "Discord webhook" in body
        assert resp.getheader("X-Frame-Options") == "DENY"
        conn.request("POST", "/ceny/ulozit", body="key=bazos%7Cair%7Cm2%7C256&price_czk=15000",
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                              "Origin": f"http://127.0.0.1:{server.server_address[1]}"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 303
        db = Database.open(db_path)
        assert SettingsStore(db).references()["bazos|air|m2|256"] == 15000
        db.close()
    finally:
        server.shutdown()
        server.server_close()
    _ = socket


def test_javascript_url_is_not_rendered_as_link(app):
    item = Inventory(app._shared_db.conn).add(dict(title="x", bought_at="2026-09-01", buy_price_czk=1,
                                                   listing_url="javascript:alert(1)"), NOW)
    html = text(get(app, f"/sklad/{item}"))
    assert 'href="javascript' not in html
