"""Fáze B: životní cyklus inzerátů na Bazoši.

1. Projde všechny stránky hledání (každých `lifecycle_interval_hours`).
   U známých inzerátů zapíše, že pořád existují, a zachytí změnu ceny.
2. Když průchod doběhl celý, známé aktivní inzeráty, které se v hledání neobjevily,
   ověří přes detail. Detail vrátí 410 nebo hlášku o smazání: zapíše `gone_at`.
   Detail existuje (třeba jiný titulek nebo kategorie): inzerát žije dál.

Facebook bez přihlášení zmizení inzerátu spolehlivě nepozná, proto jen Bazoš.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from macwatch.db import Database
from macwatch.parsing import parse_price_czk
from macwatch.pipeline import Pipeline, RunStats, price_changed
from macwatch.sources.bazos import BazosSource

log = logging.getLogger(__name__)

RUN_NAME = "bazos:lifecycle"
PAGE_SIZE = 20
RETRY_AFTER = timedelta(minutes=30)


@dataclass(slots=True)
class LifecycleStats:
    pages: int = 0
    complete: bool = False
    seen_known: int = 0
    price_changes: int = 0
    confirmed_alive: int = 0
    gone: int = 0
    errors: list[str] = field(default_factory=list)


class BazosLifecycle:
    def __init__(self, source: BazosSource, db: Database, pipeline: Pipeline,
                 clock: Callable[[], datetime]) -> None:
        self.source = source
        self.db = db
        self.pipeline = pipeline
        self._now = clock

    def due(self) -> bool:
        """Po úspěšné kontrole čeká celý interval, po nepovedené aspoň RETRY_AFTER."""
        last = self.db.conn.execute(
            """SELECT finished_at, errors FROM poll_runs WHERE source = ? AND finished_at IS NOT NULL
               ORDER BY id DESC LIMIT 1""", (RUN_NAME,)).fetchone()
        if last is None:
            return True
        wait = timedelta(hours=self.source.cfg.lifecycle_interval_hours)
        if last["errors"]:
            wait = min(wait, RETRY_AFTER)
        return self._now() - datetime.fromisoformat(last["finished_at"]) >= wait

    def run(self) -> LifecycleStats:
        stats = LifecycleStats()
        started = self._now()
        run_id = self.db.start_run(RUN_NAME, started)
        alert_stats = RunStats()
        try:
            self._search_pass(stats, started, alert_stats)
            if stats.complete:
                self._confirm_unseen(stats, started, alert_stats)
            else:
                log.warning("kontrola cyklu nedoběhla celá, zmizelé inzeráty se tentokrát neurčují")
        except Exception as exc:
            log.exception("kontrola životního cyklu selhala")
            stats.errors.append(str(exc))
        finally:
            self.db.finish_run(
                run_id, self._now(), items_seen=stats.seen_known, items_new=0,
                details_fetched=stats.confirmed_alive + stats.gone,
                alerts_sent=alert_stats.alerts_sent, errors=len(stats.errors) + len(alert_stats.errors),
                error_text="\n".join(stats.errors + alert_stats.errors)[:4000] or None)
        log.info("životní cyklus: %d stránek%s, %d známých, %d změn ceny, %d zmizelo, %d ověřeno živých",
                 stats.pages, "" if stats.complete else " (neúplné)", stats.seen_known,
                 stats.price_changes, stats.gone, stats.confirmed_alive)
        return stats

    def _record_price(self, key, price_raw: str, stats: LifecycleStats, alert_stats: RunStats) -> None:
        row = self.db.get_listing(key)
        new_price = parse_price_czk(price_raw)
        if price_raw and price_changed(row["price_czk"], row["price_raw"], new_price, price_raw):
            log.info("změna ceny při kontrole %s: %s -> %s", row["url"], row["price_raw"], price_raw)
            self.db.update_price(key, new_price, price_raw, self._now())
            stats.price_changes += 1
            self.pipeline.reassess(key, supports_enrich=True, stats=alert_stats)

    def _search_pass(self, stats: LifecycleStats, started: datetime, alert_stats: RunStats) -> None:
        offset = 0
        for _ in range(self.source.cfg.lifecycle_max_pages):
            page = self.source.search_page(offset)
            stats.pages += 1
            for hit in page.hits:
                key = ("bazos", hit.source_id)
                if self.db.get_listing(key) is None:
                    continue
                stats.seen_known += 1
                self.db.mark_checked(key, self._now())
                self._record_price(key, hit.price_raw, stats, alert_stats)
            if page.total is not None and page.last is not None and page.last >= page.total:
                stats.complete = True
                return
            if not page.hits or page.total is None:
                # neúplný průchod se zapíše jako chyba, ať se kontrola zopakuje brzy
                stats.errors.append(f"stránka hledání {offset} bez inzerátů nebo bez počtu")
                return
            offset += PAGE_SIZE
        stats.errors.append(f"dosažen limit {self.source.cfg.lifecycle_max_pages} stránek hledání")

    def _confirm_unseen(self, stats: LifecycleStats, started: datetime, alert_stats: RunStats) -> None:
        budget = self.source.cfg.lifecycle_confirm_per_pass
        for row in self.db.unseen_active("bazos", started, budget):
            key = (row["source"], row["source_id"])
            try:
                detail = self.source.check_detail(row["url"])
            except Exception as exc:
                stats.errors.append(f"{row['url']}: {exc}")
                continue
            if detail is None:
                self.db.mark_gone(key, self._now(), "zmizel")
                stats.gone += 1
            else:
                self.db.mark_checked(key, self._now())
                stats.confirmed_alive += 1
                self._record_price(key, detail.price_raw, stats, alert_stats)
