"""Příkazová řádka: python3 -m macwatch."""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from macwatch import __version__, logging_setup
from macwatch.config import AppConfig, ConfigError, load_config
from macwatch.db import Database
from macwatch.geo import default_gazetteer
from macwatch.lifecycle import BazosLifecycle
from macwatch.pricemodel import MIN_TOTAL_SAMPLES, asking_samples, refit, refit_due
from macwatch.settings import load_effective_config
from macwatch.models import Verdict
from macwatch.notify import DiscordNotifier, format_czk
from macwatch.pipeline import Finding, Pipeline
from macwatch.sources.base import Source
from macwatch.sources.bazos import BazosSource
from macwatch.sources.facebook import FacebookSource
from macwatch.sources.http import PoliteClient

log = logging.getLogger("macwatch")

_VERDICT_ORDER = {Verdict.KOUPIT: 0, Verdict.KOUPIT_S_RIZIKEM: 1, Verdict.NEZNAMY_MODEL: 2,
                  Verdict.SLEDOVAT: 3, Verdict.NEVYPLATI_SE: 4, Verdict.BEZ_CENY: 5}


def build_sources(cfg: AppConfig, client: PoliteClient | None = None) -> list[Source]:
    client = client or PoliteClient(cfg.http)
    sources: list[Source] = []
    if cfg.bazos.enabled:
        sources.append(BazosSource(cfg.bazos, client))
    if cfg.facebook.enabled:
        sources.append(FacebookSource(cfg.facebook, client))
    return sources


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="macwatch", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"),
                        help="cesta ke config.json (výchozí ./config.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="jeden průchod do paměti, nic neuloží ani neodešle, vypíše nálezy")
    mode.add_argument("--once", action="store_true", help="jeden ostrý průchod a konec")
    mode.add_argument("--migrate", action="store_true", help="jen aplikovat migrace databáze")
    mode.add_argument("--web", action="store_true", help="spustit webový dashboard")
    mode.add_argument("--lifecycle", action="store_true",
                      help="hned spustit kontrolu životního cyklu na Bazoši a skončit")
    mode.add_argument("--refit-prices", action="store_true",
                      help="přepočítat cenovou tabulku z uložených inzertních cen a vypsat ji")
    parser.add_argument("--host", default="127.0.0.1", help="adresa dashboardu (výchozí 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="port dashboardu (výchozí 8080)")
    parser.add_argument("--details", type=int, default=None,
                        help="kolik detailů stáhnout za průchod (výchozí podle configu, "
                             "při --dry-run 10)")
    parser.add_argument("-v", "--verbose", action="store_true", help="podrobné logování")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        (f for f in findings if f.assessment.verdict != Verdict.MIMO),
        key=lambda f: (_VERDICT_ORDER.get(f.assessment.verdict, 9),
                       -(f.assessment.score if f.assessment.score is not None else -1e9)))


def format_finding(f: Finding) -> str:
    a = f.assessment
    price = format_czk(f.price_czk) if f.price_czk is not None else f.item.price_raw or "?"
    parts = [f"{a.verdict:<16}", f"{price:>10}", f"{f.specs.model_label():<28}"]
    if a.adjusted_reference_czk is not None:
        parts.append(f"ref {format_czk(a.adjusted_reference_czk)}")
    if a.est_profit_czk is not None and a.roi is not None:
        parts.append(f"zisk {a.est_profit_czk:+} Kč, ROI {a.roi * 100:.0f} %")
    if a.distance_km is not None:
        parts.append(f"{a.distance_km:.0f} km, cesta {a.travel_cost_czk} Kč")
    if a.notes:
        parts.append("(" + "; ".join(a.notes) + ")")
    if not f.item.body_complete and f.item.body is not None:  # Facebook popis nemá vůbec
        parts.append("[bez detailu]")
    parts.append(f.item.url)
    return "  ".join(parts)


def run_dry(cfg: AppConfig, details: int | None) -> int:
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, notifier=None, max_details=10 if details is None else details,
                        gazetteer=default_gazetteer())
    all_findings: list[Finding] = []
    errors = 0
    for source in build_sources(cfg):
        findings, stats = pipeline.run_source(source)
        all_findings.extend(findings)
        errors += len(stats.errors)
    ranked = sort_findings(all_findings)
    ignored = len(all_findings) - len(ranked)
    print(f"\nNálezy ({len(ranked)}, mimo rozsah {ignored}), seřazeno podle verdiktu a ROI:")
    for finding in ranked:
        print("  " + format_finding(finding))
    return 1 if errors and not all_findings else 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Service:
    """Dlouho běžící služba. Nastavení z dashboardu se projeví při dalším průchodu."""

    def __init__(self, config_path: Path, db: Database, details: int | None) -> None:
        self.config_path = config_path
        self.db = db
        self.cfg = load_effective_config(config_path, db, _utcnow())
        self.client = PoliteClient(self.cfg.http)
        self.notifier_url: str | None = None  # None, ať se chybějící webhook ohlásí při startu
        self.notifier: DiscordNotifier | None = None
        self.pipeline = Pipeline(self.cfg, db, None, max_details=details, gazetteer=default_gazetteer())
        self._apply(self.cfg)

    def _apply(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.client.cfg = cfg.http
        if cfg.discord_webhook_url != self.notifier_url:
            self.notifier_url = cfg.discord_webhook_url
            self.notifier = DiscordNotifier(cfg.discord_webhook_url) if cfg.discord_webhook_url else None
            if self.notifier is None:
                log.warning("Discord webhook není nastavený, upozornění se nebudou posílat")
        self.pipeline.notifier = self.notifier
        self.pipeline.refresh(cfg)

    def reload(self) -> None:
        try:
            self._apply(load_effective_config(self.config_path, self.db, _utcnow()))
        except ConfigError as exc:
            log.error("nové nastavení je neplatné, jedu se starým: %s", exc)

    def run_once(self, stop: threading.Event | None = None, lifecycle: str = "auto") -> None:
        sources = build_sources(self.cfg, self.client)
        if not sources:
            log.error("žádný zdroj není zapnutý")
        for source in sources:
            if stop is not None and stop.is_set():
                return
            _, stats = self.pipeline.run_source(source)
            if isinstance(source, BazosSource):
                limit = self.pipeline.max_details if self.pipeline.max_details is not None \
                    else source.cfg.max_details_per_poll
                self.pipeline.enrich_backlog(source, limit - stats.details_fetched)
        self.pipeline.refresh()
        self.pipeline.reassess_active({s.name for s in sources if s.supports_enrich})
        bazos = next((s for s in sources if isinstance(s, BazosSource)), None)
        if bazos is None or lifecycle == "never":
            return
        checker = BazosLifecycle(bazos, self.db, self.pipeline, _utcnow)
        if lifecycle == "force" or checker.due():
            checker.run()
        if refit_due(self.db.conn, _utcnow(), self.cfg.market.refit_interval_days) and \
                len(asking_samples(self.db.conn, _utcnow())) >= MIN_TOTAL_SAMPLES:
            if refit(self.db.conn, _utcnow(), update_percentages=self.cfg.market.auto_percentages):
                self.reload()
                self.pipeline.reassess_active({s.name for s in sources if s.supports_enrich})


def run_service(config_path: Path, *, once: bool, details: int | None) -> int:
    base = load_config(config_path)
    db = Database.open(base.database)
    service = Service(config_path, db, details)

    stop = threading.Event()

    def _terminate(signum, _frame):
        log.info("přijat signál %s, končím po dokončení průchodu", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    log.info("macwatch %s běží, databáze %s, interval %d s",
             __version__, base.database, service.cfg.poll_interval_seconds)
    try:
        while not stop.is_set():
            service.run_once(stop)
            if once:
                break
            # rozptyl, ať požadavky nechodí v přesně stejném rytmu
            stop.wait(service.cfg.poll_interval_seconds * random.uniform(0.85, 1.15))
            service.reload()
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging_setup.configure(args.verbose)
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    if args.dry_run and not args.config.exists() and example.exists():
        log.warning("%s neexistuje, zkušební běh použije %s", args.config, example)
        args.config = example
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if args.dry_run:
        if cfg.database.exists():
            # zkušební běh respektuje nastavení z dashboardu, ale nic nezapíše
            db = Database.open(cfg.database)
            try:
                cfg = load_effective_config(args.config, db, _utcnow())
            finally:
                db.close()
        return run_dry(cfg, args.details)
    if args.migrate:
        Database.open(cfg.database).close()
        log.info("databáze %s je aktuální", cfg.database)
        return 0
    if args.web:
        from macwatch.web.server import serve
        return serve(args.config, cfg, args.host, args.port)
    if args.refit_prices:
        db = Database.open(cfg.database)
        try:
            cfg = load_effective_config(args.config, db, _utcnow())
            result = refit(db.conn, _utcnow(), update_percentages=cfg.market.auto_percentages)
            if result is None:
                log.error("málo inzertních cen, nejdřív spusť --lifecycle")
                return 1
            print(f"RAM +{result.ram_pct} % a SSD +{result.ssd_pct} % za zdvojnásobení, "
                  f"{result.samples} inzerátů, medián chyby {result.median_error_pct} %")
            for key, model in sorted(result.models.items()):
                print(f"  {key:<16} {model.base_ram_gb}/{model.base_ssd_gb}  {model.base_price_czk:>7} Kč"
                      f"  ({model.samples} inzerátů)")
        finally:
            db.close()
        return 0
    if args.lifecycle:
        db = Database.open(cfg.database)
        try:
            Service(args.config, db, args.details).run_once(lifecycle="force")
        finally:
            db.close()
        return 0
    return run_service(args.config, once=args.once, details=args.details)


if __name__ == "__main__":
    sys.exit(main())
