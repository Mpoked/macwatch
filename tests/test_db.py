import sqlite3
from datetime import datetime, timezone

import pytest

from macwatch.db import Database, Migration, bundled_migrations, migrate, schema_version
from macwatch.models import RawListing

NOW = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)


def item(**kw):
    base = dict(source="bazos", source_id="1", url="https://pc.bazos.cz/inzerat/1/x.php",
                title="MacBook Air M1", price_raw="8 900", body="text", body_complete=False,
                fetched_at=NOW)
    base.update(kw)
    return RawListing(**base)


def test_bundled_migrations_are_consecutive():
    versions = [m.version for m in bundled_migrations()]
    assert versions == list(range(1, len(versions) + 1))


def test_fresh_database_gets_latest_schema(tmp_path):
    db = Database.open(tmp_path / "t.db")
    assert schema_version(db.conn) == len(bundled_migrations())
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"listings", "price_history", "assessments", "poll_runs"} <= tables
    db.close()


def test_reopen_is_noop_and_keeps_data(tmp_path):
    path = tmp_path / "t.db"
    db = Database.open(path)
    db.insert_listing(item(), 8900, NOW)
    db.close()
    db = Database.open(path)
    assert db.get_listing(("bazos", "1"))["price_czk"] == 8900
    assert not list(tmp_path.glob("*.bak-*"))  # žádná migrace, žádná záloha
    db.close()


def test_upgrade_backs_up_and_keeps_rows(tmp_path):
    path = tmp_path / "t.db"
    db = Database.open(path)
    db.insert_listing(item(), 8900, NOW)
    extra = Migration(len(bundled_migrations()) + 1, "9999_test.sql",
                      "ALTER TABLE listings ADD COLUMN note TEXT;")
    applied = migrate(db.conn, bundled_migrations() + [extra], db_path=path, now=NOW)
    assert applied == 1
    backups = list(tmp_path.glob("t.db.bak-v*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as old:
        assert old.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    assert db.conn.execute("SELECT note FROM listings").fetchone()[0] is None
    db.close()


def test_failed_migration_rolls_back(tmp_path):
    db = Database.open(tmp_path / "t.db")
    version = schema_version(db.conn)
    broken = Migration(version + 1, "9999_broken.sql",
                       "ALTER TABLE listings ADD COLUMN a TEXT; THIS IS NOT SQL;")
    with pytest.raises(sqlite3.Error):
        migrate(db.conn, bundled_migrations() + [broken])
    assert schema_version(db.conn) == version
    columns = {r[1] for r in db.conn.execute("PRAGMA table_info(listings)")}
    assert "a" not in columns
    db.close()


def test_price_history_and_gone():
    db = Database.open(":memory:")
    db.insert_listing(item(), 8900, NOW)
    db.update_price(("bazos", "1"), 8000, "8 000", NOW)
    assert [r["price_czk"] for r in db.price_history(("bazos", "1"))] == [8900, 8000]
    db.mark_gone(("bazos", "1"), NOW)
    assert db.get_listing(("bazos", "1"))["gone_at"] == NOW.isoformat(timespec="seconds")


def test_same_id_on_different_sources_are_distinct():
    db = Database.open(":memory:")
    db.insert_listing(item(), 8900, NOW)
    db.insert_listing(item(source="facebook"), 9500, NOW)
    assert db.get_listing(("facebook", "1"))["price_czk"] == 9500
    assert db.get_listing(("bazos", "1"))["price_czk"] == 8900
