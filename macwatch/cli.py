"""Příkazová řádka: python3 -m macwatch."""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
from pathlib import Path

from macwatch import __version__, logging_setup
from macwatch.config import AppConfig, ConfigError, load_config
from macwatch.db import Database
from macwatch.models import Verdict
from macwatch.notify import DiscordNotifier, format_czk
from macwatch.pipeline import Finding, Pipeline
from macwatch.sources.base import Source
from macwatch.sources.bazos import BazosSource
from macwatch.sources.http import PoliteClient

log = logging.getLogger("macwatch")

_VERDICT_ORDER = {Verdict.KOUPIT: 0, Verdict.KOUPIT_S_RIZIKEM: 1, Verdict.NEZNAMY_MODEL: 2,
                  Verdict.SLEDOVAT: 3, Verdict.NEVYPLATI_SE: 4, Verdict.BEZ_CENY: 5}


def build_sources(cfg: AppConfig) -> list[Source]:
    client = PoliteClient(cfg.http)
    sources: list[Source] = []
    if cfg.bazos.enabled:
        sources.append(BazosSource(cfg.bazos, client))
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
    if a.notes:
        parts.append("(" + "; ".join(a.notes) + ")")
    if not f.item.body_complete:
        parts.append("[bez detailu]")
    parts.append(f.item.url)
    return "  ".join(parts)


def run_dry(cfg: AppConfig, details: int | None) -> int:
    db = Database.open(":memory:")
    pipeline = Pipeline(cfg, db, notifier=None, max_details=10 if details is None else details)
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


def run_service(cfg: AppConfig, *, once: bool, details: int | None) -> int:
    db = Database.open(cfg.database)
    notifier = DiscordNotifier(cfg.discord_webhook_url) if cfg.discord_webhook_url else None
    if notifier is None:
        log.warning("discord_webhook_url není nastavený, upozornění se nebudou posílat")
    pipeline = Pipeline(cfg, db, notifier, max_details=details)
    sources = build_sources(cfg)
    if not sources:
        log.error("žádný zdroj není zapnutý")
        return 2

    stop = threading.Event()

    def _terminate(signum, _frame):
        log.info("přijat signál %s, končím po dokončení průchodu", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    log.info("macwatch %s běží, databáze %s, interval %d s",
             __version__, cfg.database, cfg.poll_interval_seconds)
    try:
        while not stop.is_set():
            for source in sources:
                if stop.is_set():
                    break
                pipeline.run_source(source)
            if once:
                break
            # rozptyl, ať požadavky nechodí v přesně stejném rytmu
            wait = cfg.poll_interval_seconds * random.uniform(0.85, 1.15)
            stop.wait(wait)
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
        return run_dry(cfg, args.details)
    if args.migrate:
        Database.open(cfg.database).close()
        log.info("databáze %s je aktuální", cfg.database)
        return 0
    return run_service(cfg, once=args.once, details=args.details)


if __name__ == "__main__":
    sys.exit(main())
