import json
import os

import pytest

from macwatch import fbconfig
from tests.test_web import HOST, NOW, get, post, text  # noqa: F401
from macwatch.web.server import App

UH = "https://www.facebook.com/marketplace/115084948502940/search/?daysSinceListed=1&sortBy=creation_time_descend&query=macbook&exact=false"


@pytest.fixture
def fb_path(tmp_path):
    path = tmp_path / "fb" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "server_ip": "0.0.0.0", "server_port": "5000", "refresh_interval_minutes": 15,
        "log_filename": "/app/config/fb-rssfeed.log",
        "url_filters": {UH: {"level1": ["macbook", "mac book"]}}}), encoding="utf-8")
    return path


def test_load(fb_path):
    _, settings = fbconfig.load(fb_path)
    assert settings.urls == [UH]
    assert settings.keywords == ["macbook", "mac book"]
    assert (settings.refresh_minutes, settings.days_since_listed, settings.sort_newest) == (15, "1", True)


def test_normalize_url_adds_and_removes_params():
    url = "https://www.facebook.com/marketplace/prague/search/?query=macbook&radius=40"
    assert fbconfig.normalize_url(url, "7", True) == \
        "https://www.facebook.com/marketplace/prague/search/?query=macbook&radius=40&daysSinceListed=7&sortBy=creation_time_descend"
    assert fbconfig.normalize_url(UH, "", False) == \
        "https://www.facebook.com/marketplace/115084948502940/search/?query=macbook&exact=false"


@pytest.mark.parametrize("url", ["http://www.facebook.com/marketplace/x", "https://evil.example/marketplace/",
                                 "https://www.facebook.com/groups/1"])
def test_rejects_non_marketplace_urls(url):
    with pytest.raises(fbconfig.FbConfigError):
        fbconfig.normalize_url(url, "1", True)


def test_save_keeps_server_settings(fb_path):
    new = fbconfig.FbSettings(urls=["https://www.facebook.com/marketplace/brno/search/?query=macbook"],
                              keywords=["MacBook", " mac book "], refresh_minutes=30, days_since_listed="7")
    fbconfig.save(fb_path, new)
    data = json.loads(fb_path.read_text())
    assert data["server_port"] == "5000" and data["log_filename"] == "/app/config/fb-rssfeed.log"
    assert data["refresh_interval_minutes"] == 30
    (url, levels), = data["url_filters"].items()
    assert "daysSinceListed=7" in url and "sortBy=creation_time_descend" in url
    assert levels == {"level1": ["macbook", "mac book"]}
    assert os.name == "nt" or oct(fb_path.stat().st_mode)[-3:] == "644"   # Windows práva nemá
    assert not list(fb_path.parent.glob(".config-*")), "po sobě nenechá dočasný soubor"


@pytest.mark.parametrize("change, message", [
    (dict(refresh_minutes=4), "5 minut"),
    (dict(urls=[]), "aspoň jednu"),
    (dict(days_since_listed="3"), "stáří"),
])
def test_save_validation(fb_path, change, message):
    base = dict(urls=[UH], keywords=["macbook"], refresh_minutes=15, days_since_listed="1", sort_newest=True)
    base.update(change)
    before = fb_path.read_text()
    with pytest.raises(fbconfig.FbConfigError, match=message):
        fbconfig.save(fb_path, fbconfig.FbSettings(**base))
    assert fb_path.read_text() == before


@pytest.fixture
def fb_app(tmp_path, config_data, fb_path):
    config_data["sources"]["facebook"]["config_path"] = str(fb_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    return App(path, ":memory:", clock=lambda: NOW)


def test_dashboard_shows_and_saves_facebook(fb_app, fb_path):
    html = text(get(fb_app, "/nastaveni"))
    assert "Facebook Marketplace" in html and "115084948502940" in html and "macbook, mac book" in html
    resp = post(fb_app, "/nastaveni/facebook", {
        "urls": UH + "\nhttps://www.facebook.com/marketplace/brno/search/?query=macbook",
        "keywords": "macbook, mac book, mbp", "refresh_minutes": "20", "days_since_listed": "1",
        "sort_newest": "on"})
    assert resp.status == 303
    assert dict(resp.headers)["Location"].endswith("#facebook")
    data = json.loads(fb_path.read_text())
    assert len(data["url_filters"]) == 2 and data["refresh_interval_minutes"] == 20
    assert all(v == {"level1": ["macbook", "mac book", "mbp"]} for v in data["url_filters"].values())


def test_dashboard_rejects_bad_facebook_values(fb_app, fb_path):
    before = fb_path.read_text()
    resp = post(fb_app, "/nastaveni/facebook", {"urls": "https://evil.example/x", "keywords": "",
                                                "refresh_minutes": "15", "days_since_listed": "1"})
    assert resp.status == 400 and "Neuloženo" in text(resp)
    assert fb_path.read_text() == before


def test_missing_config_path(tmp_path, config_data):
    config_data["sources"]["facebook"].pop("config_path", None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    app = App(path, ":memory:", clock=lambda: NOW)
    assert "není nastavená" in text(get(app, "/nastaveni"))
