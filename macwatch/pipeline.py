"""Jeden průchod: sběr ze zdrojů, uložení, posouzení a upozornění."""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from macwatch import privacy
from macwatch.config import AppConfig
from macwatch.db import Database
from macwatch.geo import Gazetteer, Place, estimate_travel
from macwatch.market import ReferenceBook, reference_book
from macwatch.models import Assessment, RawListing, Specs
from macwatch.notify import build_embed
from macwatch.parsing import normalize, parse_price_czk, parse_specs, passes_prefilter
from macwatch.pricing import assess
from macwatch.sources.base import ListingGone, Source

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, embed: dict[str, Any]) -> bool: ...


@dataclass(slots=True)
class Finding:
    item: RawListing
    price_czk: int | None
    specs: Specs
    assessment: Assessment
    alerted: bool = False


@dataclass(slots=True)
class RunStats:
    items_seen: int = 0
    items_new: int = 0
    details_fetched: int = 0
    alerts_sent: int = 0
    errors: list[str] = field(default_factory=list)


def price_changed(old_czk: int | None, old_raw: str, new_czk: int | None, new_raw: str) -> bool:
    """RSS píše ``17 399``, detail ``17 399 Kč``. To není změna ceny."""
    if old_czk is not None or new_czk is not None:
        return old_czk != new_czk
    return normalize(old_raw) != normalize(new_raw)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def row_to_item(row: sqlite3.Row) -> RawListing:
    return RawListing(
        source=row["source"], source_id=row["source_id"], url=row["url"], title=row["title"],
        price_raw=row["price_raw"], body=row["body"], body_complete=bool(row["body_complete"]),
        fetched_at=datetime.fromisoformat(row["last_seen"]), category=row["category"],
        locality=row["locality"], postal_code=row["postal_code"])


class Pipeline:
    def __init__(self, cfg: AppConfig, db: Database, notifier: Notifier | None, *,
                 clock: Callable[[], datetime] = _utcnow, max_details: int | None = None,
                 gazetteer: Gazetteer | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self._now = clock
        self.max_details = max_details
        self.gazetteer = gazetteer
        self.book: ReferenceBook | None = None
        self.home: Place | None = None
        self.refresh()

    def refresh(self, cfg: AppConfig | None = None) -> None:
        """Načte nové nastavení, spočítá referenční ceny z dat a domovské místo."""
        if cfg is not None:
            self.cfg = cfg
        self.book = reference_book(self.db.conn, self.cfg.market, self.cfg.pricing, self._now())
        self.home = None
        if self.gazetteer and self.cfg.travel.enabled and self.cfg.travel.home_psc:
            self.home = self.gazetteer.by_postal_code(self.cfg.travel.home_psc)
            if self.home is None:
                log.warning("domovské PSČ %s nenalezeno, vzdálenost se nepočítá",
                            self.cfg.travel.home_psc)

    def evaluate(self, item: RawListing, price: int | None) -> tuple[Specs, Assessment]:
        specs = parse_specs(item.title, item.body, self.cfg.parsing)
        travel = None
        if self.home and self.gazetteer:
            place = self.gazetteer.locate(item.postal_code, item.locality)
            travel = estimate_travel(self.cfg.travel, self.home, place)
        return specs, assess(item.source, price, specs, self.cfg.pricing, self.book, travel)

    def run_source(self, source: Source) -> tuple[list[Finding], RunStats]:
        stats = RunStats()
        findings: list[Finding] = []
        run_id = self.db.start_run(source.name, self._now())
        budget = self.max_details if self.max_details is not None else \
            getattr(getattr(source, "cfg", None), "max_details_per_poll", 25)
        try:
            for item in source.poll():
                stats.items_seen += 1
                if not passes_prefilter(item.title, self.cfg.parsing):
                    continue
                try:
                    finding = self._process(source, item, stats, budget)
                except ListingGone:
                    log.info("inzerát zmizel dřív, než jsme stáhli detail: %s", item.url)
                    continue
                except Exception as exc:  # jeden vadný inzerát nesmí shodit celý průchod
                    log.exception("chyba při zpracování %s", item.url)
                    stats.errors.append(f"{item.url}: {exc}")
                    continue
                if finding:
                    findings.append(finding)
        except Exception as exc:
            log.exception("zdroj %s selhal", source.name)
            stats.errors.append(f"{source.name}: {exc}")
        finally:
            self.db.finish_run(
                run_id, self._now(), items_seen=stats.items_seen, items_new=stats.items_new,
                details_fetched=stats.details_fetched, alerts_sent=stats.alerts_sent,
                errors=len(stats.errors), error_text="\n".join(stats.errors)[:4000] or None)
        log.info("%s: %d položek, %d nových, %d detailů, %d upozornění, %d chyb",
                 source.name, stats.items_seen, stats.items_new, stats.details_fetched,
                 stats.alerts_sent, len(stats.errors))
        return findings, stats

    def _process(self, source: Source, item: RawListing, stats: RunStats,
                 budget: int) -> Finding | None:
        now = self._now()
        item = privacy.redact(item)
        row = self.db.get_listing(item.key)
        price = parse_price_czk(item.price_raw)

        if row is None:
            stats.items_new += 1
            self.db.insert_listing(item, price, now)
            complete = item.body_complete
            alerted = False
        else:
            if price_changed(row["price_czk"], row["price_raw"], price, item.price_raw):
                log.info("změna ceny %s: %s -> %s", item.url, row["price_raw"], item.price_raw)
                self.db.update_price(item.key, price, item.price_raw, now)
            else:
                self.db.touch(item.key, now)
                price = row["price_czk"]
            complete = bool(row["body_complete"])
            alerted = row["alerted_at"] is not None
            # RSS nese zkrácený text, uložený plný text z detailu je lepší
            item = dataclasses.replace(item, title=row["title"], body=row["body"],
                                       body_complete=complete, locality=row["locality"],
                                       postal_code=row["postal_code"])

        if source.supports_enrich and not complete:
            if stats.details_fetched < budget:
                stats.details_fetched += 1
                try:
                    item = privacy.redact(source.enrich(item))
                except ListingGone:
                    self.db.mark_gone(item.key, now, "smazán před stažením detailu")
                    raise
                self.db.update_content(item, now)
                detail_price = parse_price_czk(item.price_raw)
                stored = self.db.get_listing(item.key)
                if price_changed(stored["price_czk"], stored["price_raw"],
                                 detail_price, item.price_raw):
                    self.db.update_price(item.key, detail_price, item.price_raw, now)
                price = detail_price
            else:
                log.debug("rozpočet detailů vyčerpán, %s počká na další průchod", item.url)

        return self._assess_and_alert(item, price, source.supports_enrich, alerted, stats, now)

    def _assess_and_alert(self, item: RawListing, price: int | None, supports_enrich: bool,
                          alerted: bool, stats: RunStats, now: datetime) -> Finding:
        specs, assessment = self.evaluate(item, price)
        self.db.save_assessment(item.key, specs, assessment, now)
        finding = Finding(item, price, specs, assessment)
        # bez plného textu nevíme o červených vlajkách, s upozorněním se počká
        ready = item.body_complete or not supports_enrich
        if assessment.should_alert and ready and not alerted and price is not None:
            finding.alerted = self._alert(finding, stats)
        return finding

    def reassess(self, key: tuple[str, str], supports_enrich: bool, stats: RunStats) -> Finding | None:
        """Znovu posoudí uložený inzerát, třeba po zlevnění zjištěném při kontrole cyklu."""
        row = self.db.get_listing(key)
        if row is None:
            return None
        return self._assess_and_alert(row_to_item(row), row["price_czk"], supports_enrich,
                                      row["alerted_at"] is not None, stats, self._now())

    def reassess_active(self, enrich_sources: set[str]) -> RunStats:
        """Přepočítá posouzení všech aktivních inzerátů.

        Projeví se tak nové nastavení, nová referenční cena i naměřené ceny z trhu.
        Kus, který se díky tomu nově vyplatí, dostane upozornění.
        """
        stats = RunStats()
        rows = self.db.conn.execute("SELECT * FROM listings WHERE gone_at IS NULL").fetchall()
        for row in rows:
            try:
                self._assess_and_alert(row_to_item(row), row["price_czk"], row["source"] in enrich_sources,
                                       row["alerted_at"] is not None, stats, self._now())
            except Exception as exc:
                log.exception("přepočet %s selhal", row["url"])
                stats.errors.append(f"{row['url']}: {exc}")
        log.debug("přepočteno %d aktivních inzerátů, %d upozornění", len(rows), stats.alerts_sent)
        return stats

    def _alert(self, finding: Finding, stats: RunStats) -> bool:
        if self.notifier is None:
            return False
        embed = build_embed(source=finding.item.source, url=finding.item.url,
                            price_czk=finding.price_czk or 0, specs=finding.specs,
                            assessment=finding.assessment)
        if self.notifier.send(embed):
            self.db.mark_alerted(finding.item.key, self._now())
            stats.alerts_sent += 1
            return True
        stats.errors.append(f"upozornění se nepodařilo odeslat: {finding.item.url}")
        return False
