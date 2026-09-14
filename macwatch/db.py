"""SQLite: připojení, migrace schématu a zápisy."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from macwatch.models import Assessment, RawListing, Specs

log = logging.getLogger(__name__)

PARSER_VERSION = 1

_MIGRATION_NAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def bundled_migrations() -> list[Migration]:
    migrations = []
    for entry in resources.files("macwatch.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            migrations.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrace nejsou číslované souvisle od 1: {versions}")
    return migrations


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def backup_database(conn: sqlite3.Connection, path: Path, version: int, now: datetime) -> Path:
    target = path.with_name(f"{path.name}.bak-v{version}-{now.strftime('%Y%m%dT%H%M%S')}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(conn: sqlite3.Connection, migrations: list[Migration], *,
            db_path: Path | None = None, now: datetime | None = None) -> int:
    """Aplikuje chybějící migrace. Každá běží v jedné transakci.

    Před změnou existující databáze se udělá záloha vedle souboru.
    Vrací počet aplikovaných migrací.
    """
    current = schema_version(conn)
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return 0
    if current > 0 and db_path is not None:
        target = backup_database(conn, db_path, current, now or datetime.now())
        log.info("záloha databáze před migrací: %s", target)
    for migration in pending:
        log.info("aplikuji migraci %s", migration.name)
        script = f"BEGIN;\n{migration.sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            log.exception("migrace %s selhala, databáze zůstává ve verzi %d",
                          migration.name, schema_version(conn))
            raise
    return len(pending)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class Database:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def open(cls, path: Path | str, *, now: datetime | None = None) -> Database:
        in_memory = str(path) == ":memory:"
        # výchozí režim transakcí: `with conn:` potvrdí, nebo při výjimce vrátí změny
        conn = sqlite3.connect(str(path))
        if not in_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        db = cls(conn)
        migrate(conn, bundled_migrations(), db_path=None if in_memory else Path(path), now=now)
        return db

    def close(self) -> None:
        self.conn.close()

    # inzeráty

    def get_listing(self, key: tuple[str, str]) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", key).fetchone()

    def insert_listing(self, item: RawListing, price_czk: int | None, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO listings (source, source_id, url, title, price_czk, price_raw,
                       body, body_complete, category, locality, postal_code, published_at,
                       first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.source, item.source_id, item.url, item.title, price_czk, item.price_raw,
                 item.body, int(item.body_complete), item.category, item.locality,
                 item.postal_code, _iso(item.published_at), _iso(now), _iso(now)))
            self._record_price(item.key, price_czk, item.price_raw, now)

    def update_content(self, item: RawListing, now: datetime) -> None:
        """Doplní text a lokalitu, typicky po stažení detailu."""
        with self.conn:
            self.conn.execute(
                """UPDATE listings SET title = ?, body = ?, body_complete = ?,
                       locality = COALESCE(?, locality), postal_code = COALESCE(?, postal_code),
                       last_seen = ?
                   WHERE source = ? AND source_id = ?""",
                (item.title, item.body, int(item.body_complete), item.locality,
                 item.postal_code, _iso(now), *item.key))

    def touch(self, key: tuple[str, str], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET last_seen = ? WHERE source = ? AND source_id = ?",
                (_iso(now), *key))

    def update_price(self, key: tuple[str, str], price_czk: int | None, price_raw: str,
                     now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE listings SET price_czk = ?, price_raw = ?, last_seen = ?
                   WHERE source = ? AND source_id = ?""",
                (price_czk, price_raw, _iso(now), *key))
            self._record_price(key, price_czk, price_raw, now)

    def _record_price(self, key: tuple[str, str], price_czk: int | None, price_raw: str,
                      now: datetime) -> None:
        self.conn.execute(
            """INSERT INTO price_history (source, source_id, observed_at, price_czk, price_raw)
               VALUES (?, ?, ?, ?, ?)""",
            (*key, _iso(now), price_czk, price_raw))

    def mark_gone(self, key: tuple[str, str], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE listings SET gone_at = COALESCE(gone_at, ?)
                   WHERE source = ? AND source_id = ?""", (_iso(now), *key))

    def mark_alerted(self, key: tuple[str, str], now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET alerted_at = ? WHERE source = ? AND source_id = ?",
                (_iso(now), *key))

    def price_history(self, key: tuple[str, str]) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT observed_at, price_czk, price_raw FROM price_history
               WHERE source = ? AND source_id = ? ORDER BY id""", key).fetchall()

    # posouzení

    def save_assessment(self, key: tuple[str, str], specs: Specs, assessment: Assessment,
                        now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO assessments (source, source_id, parser_version,
                       assessed_at, eligible, family, chip, ram_gb, ssd_gb, year, battery_pct,
                       cycles, is_dealer, red_flags, reference_czk, reference_key,
                       reference_origin, adjusted_reference_czk, target_czk, est_profit_czk,
                       roi, score, verdict)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*key, PARSER_VERSION, _iso(now), int(specs.eligible), specs.family, specs.chip,
                 specs.ram_gb, specs.ssd_gb, specs.year, specs.battery_pct, specs.cycles,
                 int(specs.is_dealer), json.dumps(list(specs.red_flags)),
                 assessment.reference_czk, assessment.reference_key,
                 assessment.reference_origin, assessment.adjusted_reference_czk,
                 assessment.target_czk, assessment.est_profit_czk, assessment.roi,
                 assessment.score, assessment.verdict))

    # běhy

    def start_run(self, source: str, now: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO poll_runs (source, started_at) VALUES (?, ?)", (source, _iso(now)))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: datetime, *, items_seen: int, items_new: int,
                   details_fetched: int, alerts_sent: int, errors: int,
                   error_text: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE poll_runs SET finished_at = ?, items_seen = ?, items_new = ?,
                       details_fetched = ?, alerts_sent = ?, errors = ?, error_text = ?
                   WHERE id = ?""",
                (_iso(now), items_seen, items_new, details_fetched, alerts_sent, errors,
                 error_text, run_id))
