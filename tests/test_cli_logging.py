import json
import logging

from macwatch import cli, logging_setup
from macwatch.config import BazosConfig
from macwatch.sources.bazos import BazosSource
from macwatch.sources.http import Response
from tests.conftest import ROOT, read_fixture

FEED = "https://www.bazos.cz/rss.php?rub=pc&hledat=macbook"


class FixtureClient:
    """Feed z fixture, detaily z fixtures, ostatní inzeráty jako zmizelé."""

    def get(self, url):
        if url.startswith("https://www.bazos.cz/rss.php"):
            return Response(url, 200, read_fixture("bazos", "rss_macbook.xml"))
        listing = url.split("/inzerat/")[1].split("/")[0]
        path = ROOT / "tests" / "fixtures" / "bazos" / "detail" / f"{listing}.html"
        if path.exists():
            return Response(url, 200, path.read_text(encoding="utf-8"))
        return Response(url, 410, read_fixture("bazos", "gone_410.html"))


def test_dry_run_prints_ranked_findings(monkeypatch, capsys, tmp_path, config_data):
    config_data["sources"]["bazos"]["feeds"] = [FEED]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    monkeypatch.setattr(cli, "build_sources", lambda cfg: [
        BazosSource(BazosConfig(True, (FEED,)), FixtureClient())])

    assert cli.main(["--config", str(config_path), "--dry-run", "--details", "100"]) == 0
    out = capsys.readouterr().out
    assert "Nálezy" in out
    assert "MacBook Pro M1 Pro ?/512" in out
    assert "NEVYPLATI_SE" in out
    assert not list(tmp_path.glob("*.db")), "zkušební běh nic nezapisuje na disk"


def test_invalid_config_returns_error(tmp_path, caplog):
    bad = tmp_path / "config.json"
    bad.write_text("{nejson", encoding="utf-8")
    assert cli.main(["--config", str(bad), "--once"]) == 2


def test_journal_prefix(monkeypatch, capsys):
    monkeypatch.setattr(logging_setup, "running_under_journal", lambda: True)
    logging_setup.configure()
    logging.getLogger("macwatch.test").warning("pozor\ndruhý řádek")
    err = capsys.readouterr().err
    assert "<4>macwatch.test: pozor" in err
    assert "<4>druhý řádek" in err


def test_journal_detection_checks_stderr_identity(monkeypatch, tmp_path):
    with open(tmp_path / "log", "w") as stream:
        st = __import__("os").fstat(stream.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
        assert logging_setup.running_under_journal(stream)
        monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
        assert not logging_setup.running_under_journal(stream)


def test_terminal_format_without_journal(monkeypatch, capsys):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    logging_setup.configure()
    logging.getLogger("macwatch.test").info("ahoj")
    assert "INFO    macwatch.test: ahoj" in capsys.readouterr().err
