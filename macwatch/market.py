"""Trh z nasbíraných dat: prodejní ceny, doba prodeje, referenční ceny.

Za prodaný se považuje inzerát, který zmizel do `sold_within_days` od prvního spatření.
Inzertní cena v okamžiku zmizení je nejlepší odhad prodejní ceny, kterou z bazaru máme.
Vlastní prodeje ze skladu jsou skutečně zaplacené peníze a váží víc.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median

from macwatch.config import MarketConfig, PricingConfig
from macwatch.db import GONE_UNSEEN
from macwatch.pricemodel import DEFAULT_BASE, PriceModel, config_factor, load_models


@dataclass(frozen=True, slots=True)
class Sample:
    family: str
    chip: str
    ram_gb: int | None
    ssd_gb: int | None
    source: str         # bazos, facebook, vlastni
    price_czk: int
    days_listed: float | None
    at: str
    weight: int = 1

    @property
    def model_key(self) -> str:
        return f"{self.family}|{self.chip}"

    @property
    def group(self) -> str:
        """Skupina pro přehledy: rodina|čip|ssd."""
        return group_key(self.family, self.chip, self.ssd_gb) or self.model_key


def weighted_median(values: list[tuple[float, int]]) -> float | None:
    """Medián, kde každá hodnota platí `váha`-krát."""
    items = sorted((v, w) for v, w in values if w > 0)
    total = sum(w for _, w in items)
    if total == 0:
        return None
    half = total / 2
    running = 0
    for index, (value, weight) in enumerate(items):
        running += weight
        if running > half:
            return value
        if running == half:
            following = items[index + 1][0] if index + 1 < len(items) else value
            return (value + following) / 2
    return items[-1][0]


def group_key(family: str | None, chip: str | None, ssd_gb: int | None) -> str | None:
    if not family or not chip:
        return None
    return f"{family}|{chip}|{ssd_gb}" if ssd_gb else f"{family}|{chip}"


def sold_listing_samples(conn: sqlite3.Connection, cfg: MarketConfig, now: datetime) -> list[Sample]:
    since = (now - timedelta(days=cfg.lookback_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT l.source, l.price_czk, l.first_seen, l.gone_at,
                  julianday(l.gone_at) - julianday(COALESCE(l.published_at, l.first_seen)) AS days,
                  a.family, a.chip, a.ram_gb, a.ssd_gb
           FROM listings l JOIN assessments a USING (source, source_id)
           WHERE l.gone_at IS NOT NULL AND l.gone_at >= ?
             AND l.price_czk > 0 AND a.eligible = 1 AND a.is_dealer = 0
             AND a.red_flags = '[]' AND a.family IS NOT NULL AND a.chip IS NOT NULL
             AND a.verdict != 'PODEZRELE' AND COALESCE(l.gone_reason, '') != ?
             AND julianday(l.gone_at) - julianday(COALESCE(l.published_at, l.first_seen)) <= ?""",
        (since, GONE_UNSEEN, cfg.sold_within_days)).fetchall()
    return [Sample(r["family"], r["chip"], r["ram_gb"], r["ssd_gb"], r["source"], r["price_czk"],
                   r["days"], r["gone_at"]) for r in rows]


def own_sale_samples(conn: sqlite3.Connection, cfg: MarketConfig, now: datetime) -> list[Sample]:
    since = (now - timedelta(days=cfg.lookback_days)).date().isoformat()
    rows = conn.execute(
        """SELECT family, chip, ram_gb, ssd_gb, sale_price_czk, sold_at,
                  julianday(sold_at) - julianday(COALESCE(listed_at, bought_at)) AS days
           FROM inventory
           WHERE sold_at IS NOT NULL AND sold_at >= ? AND sale_price_czk > 0
             AND family IS NOT NULL AND chip IS NOT NULL""",
        (since,)).fetchall()
    return [Sample(r["family"], r["chip"], r["ram_gb"], r["ssd_gb"], "vlastni", r["sale_price_czk"],
                   r["days"], r["sold_at"], cfg.own_sale_weight) for r in rows]


@dataclass(frozen=True, slots=True)
class Measured:
    price_czk: int       # cena přepočtená na základní konfiguraci
    samples: int         # počet vzorků bez vah
    own_sales: int
    base_ram_gb: int = DEFAULT_BASE[0]
    base_ssd_gb: int = DEFAULT_BASE[1]


@dataclass(slots=True)
class ReferenceBook:
    """Referenční ceny pro posouzení.

    Pořadí: zamčený řádek cenové tabulky, naměřené prodeje (přepočtené na konfiguraci),
    řádek tabulky odhadnutý z inzerátů, staré ruční ceny s klíčem zdroj|rodina|čip[|ssd].
    """

    manual: dict[str, int]
    measured: dict[str, dict[str, Measured]] = field(default_factory=dict)   # zdroj -> rodina|čip -> hodnota
    mode: str = "auto"
    min_samples: int = 3
    fallback_source: str | None = None
    models: dict[str, PriceModel] = field(default_factory=dict)
    ram_pct: float = 18.0
    ssd_pct: float = 11.0

    def _scale(self, base_price: int, base_ram: int, base_ssd: int, ram_gb: int | None,
               ssd_gb: int | None) -> int:
        return round(base_price * config_factor(ram_gb, ssd_gb, base_ram, base_ssd, self.ram_pct, self.ssd_pct))

    def lookup(self, source: str, family: str | None, chip: str | None, ssd_gb: int | None,
               ram_gb: int | None = None) -> tuple[int, str, str, int] | None:
        """Vrátí (cena, klíč, původ, počet vzorků).

        Původ: manual (zamčená tabulka), measured[:zdroj], table (tabulka z inzerátů),
        config[:zdroj] (staré ruční ceny).
        """
        if not family or not chip:
            return None
        key = f"{family}|{chip}"
        sources = [source]
        if self.fallback_source and self.fallback_source != source:
            sources.append(self.fallback_source)
        model = self.models.get(key)
        if model and model.locked:
            price = self._scale(model.base_price_czk, model.base_ram_gb, model.base_ssd_gb, ram_gb, ssd_gb)
            return price, f"model|{key}", "manual", 0
        if self.mode == "auto":
            for src in sources:
                hit = self.measured.get(src, {}).get(key)
                if hit and hit.samples >= self.min_samples:
                    suffix = "" if src == source else f":{src}"
                    price = self._scale(hit.price_czk, hit.base_ram_gb, hit.base_ssd_gb, ram_gb, ssd_gb)
                    return price, f"{src}|{key}", "measured" + suffix, hit.samples
        if model:
            price = self._scale(model.base_price_czk, model.base_ram_gb, model.base_ssd_gb, ram_gb, ssd_gb)
            return price, f"model|{key}", "table", model.samples
        groups = ([f"{key}|{ssd_gb}"] if ssd_gb else []) + [key]
        for src in sources:
            suffix = "" if src == source else f":{src}"
            for group in groups:
                manual_key = f"{src}|{group}"
                if manual_key in self.manual:
                    return self.manual[manual_key], manual_key, "config" + suffix, 0
        return None


def build_measured(samples: list[Sample], models: dict[str, PriceModel] | None = None,
                   ram_pct: float = 18.0, ssd_pct: float = 11.0) -> dict[str, dict[str, Measured]]:
    """Seskupí prodeje po zdrojích a modelech a přepočte je na základní konfiguraci modelu.

    Díky přepočtu se sečtou všechny konfigurace jednoho modelu (8/256 i 16/512).
    Vlastní prodeje se přidají ke každému zdroji.
    """
    models = models or {}
    by_source: dict[str, dict[str, list[Sample]]] = defaultdict(lambda: defaultdict(list))
    sources = {s.source for s in samples if s.source != "vlastni"} | {"bazos", "facebook"}
    for sample in samples:
        targets = sources if sample.source == "vlastni" else {sample.source}
        for src in targets:
            by_source[src][sample.model_key].append(sample)
    result: dict[str, dict[str, Measured]] = {}
    for src, groups in by_source.items():
        result[src] = {}
        for key, items in groups.items():
            model = models.get(key)
            base_ram, base_ssd = (model.base_ram_gb, model.base_ssd_gb) if model else DEFAULT_BASE
            normalized = [(s.price_czk / config_factor(s.ram_gb, s.ssd_gb, base_ram, base_ssd, ram_pct, ssd_pct),
                           s.weight) for s in items]
            value = weighted_median(normalized)
            if value is None:
                continue
            result[src][key] = Measured(round(value), len(items),
                                        sum(1 for s in items if s.source == "vlastni"), base_ram, base_ssd)
    return result


def reference_book(conn: sqlite3.Connection, market: MarketConfig, pricing: PricingConfig,
                   now: datetime) -> ReferenceBook:
    samples = sold_listing_samples(conn, market, now) + own_sale_samples(conn, market, now)
    try:
        models = load_models(conn)
    except sqlite3.OperationalError:  # databáze před migrací 0004
        models = {}
    return ReferenceBook(manual=dict(pricing.reference),
                         measured=build_measured(samples, models, market.ram_pct_per_doubling,
                                                 market.ssd_pct_per_doubling),
                         mode=market.reference_mode, min_samples=market.min_samples,
                         fallback_source=pricing.fallback_source, models=models,
                         ram_pct=market.ram_pct_per_doubling, ssd_pct=market.ssd_pct_per_doubling)


# přehledy pro dashboard

@dataclass(frozen=True, slots=True)
class GroupStats:
    group: str
    source: str
    sold: int
    median_price: int | None
    median_days: float | None
    mean_days: float | None
    active: int
    median_asking: int | None


def market_overview(conn: sqlite3.Connection, cfg: MarketConfig, now: datetime) -> list[GroupStats]:
    samples = sold_listing_samples(conn, cfg, now)
    sold: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    for s in samples:
        sold[(s.source, s.group)].append(s)
    active_rows = conn.execute(
        """SELECT l.source, l.price_czk, a.family, a.chip, a.ssd_gb
           FROM listings l JOIN assessments a USING (source, source_id)
           WHERE l.gone_at IS NULL AND a.eligible = 1 AND a.is_dealer = 0 AND l.price_czk > 0
             AND a.verdict != 'PODEZRELE'""").fetchall()
    active: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in active_rows:
        key = group_key(row["family"], row["chip"], row["ssd_gb"])
        if key:
            active[(row["source"], key)].append(row["price_czk"])
    stats = []
    for source, group in sorted(set(sold) | set(active)):
        items = sold.get((source, group), [])
        days = [s.days_listed for s in items if s.days_listed is not None]
        asking = active.get((source, group), [])
        stats.append(GroupStats(
            group=group, source=source, sold=len(items),
            median_price=round(median([s.price_czk for s in items])) if items else None,
            median_days=round(median(days), 1) if days else None,
            mean_days=round(mean(days), 1) if days else None,
            active=len(asking),
            median_asking=round(median(asking)) if asking else None))
    return stats


def price_timeline(conn: sqlite3.Connection, family: str, chip: str, ssd_gb: int | None,
                   now: datetime, days: int = 180) -> dict[str, list[tuple[str, int, int]]]:
    """Týdenní medián inzertní ceny po zdrojích: {zdroj: [(týden, medián, počet)]}.

    Každý inzerát se započítá cenou platnou na konci týdne, kdy byl vidět.
    """
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    ssd_clause, params = ("AND a.ssd_gb = ?", [ssd_gb]) if ssd_gb else ("", [])
    rows = conn.execute(
        f"""SELECT l.source, strftime('%Y-%W', ph.observed_at) AS week, l.source_id,
                   ph.price_czk
            FROM price_history ph
            JOIN listings l USING (source, source_id)
            JOIN assessments a USING (source, source_id)
            WHERE a.family = ? AND a.chip = ? {ssd_clause} AND ph.observed_at >= ?
              AND ph.price_czk > 0 AND a.is_dealer = 0
            ORDER BY ph.id""",
        [family, chip, *params, since]).fetchall()
    last: dict[tuple[str, str, str], int] = {}
    for row in rows:
        last[(row["source"], row["week"], row["source_id"])] = row["price_czk"]
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for (source, week, _), price in last.items():
        buckets[(source, week)].append(price)
    result: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for (source, week), prices in sorted(buckets.items(), key=lambda kv: kv[0][1]):
        result[source].append((week, round(median(prices)), len(prices)))
    return dict(result)
