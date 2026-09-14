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


@dataclass(frozen=True, slots=True)
class Sample:
    group: str          # rodina|čip|ssd
    source: str         # bazos, facebook, vlastni
    price_czk: int
    days_listed: float | None
    at: str
    weight: int = 1


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
                  julianday(l.gone_at) - julianday(l.first_seen) AS days,
                  a.family, a.chip, a.ssd_gb
           FROM listings l JOIN assessments a USING (source, source_id)
           WHERE l.gone_at IS NOT NULL AND l.gone_at >= ?
             AND l.price_czk > 0 AND a.eligible = 1 AND a.is_dealer = 0
             AND a.red_flags = '[]'
             AND julianday(l.gone_at) - julianday(l.first_seen) <= ?""",
        (since, cfg.sold_within_days)).fetchall()
    samples = []
    for row in rows:
        key = group_key(row["family"], row["chip"], row["ssd_gb"])
        if key:
            samples.append(Sample(key, row["source"], row["price_czk"], row["days"], row["gone_at"]))
    return samples


def own_sale_samples(conn: sqlite3.Connection, cfg: MarketConfig, now: datetime) -> list[Sample]:
    since = (now - timedelta(days=cfg.lookback_days)).date().isoformat()
    rows = conn.execute(
        """SELECT family, chip, ssd_gb, sale_price_czk, sold_at,
                  julianday(sold_at) - julianday(COALESCE(listed_at, bought_at)) AS days
           FROM inventory
           WHERE sold_at IS NOT NULL AND sold_at >= ? AND sale_price_czk > 0""",
        (since,)).fetchall()
    samples = []
    for row in rows:
        key = group_key(row["family"], row["chip"], row["ssd_gb"])
        if key:
            samples.append(Sample(key, "vlastni", row["sale_price_czk"], row["days"], row["sold_at"],
                                  cfg.own_sale_weight))
    return samples


@dataclass(frozen=True, slots=True)
class Measured:
    price_czk: int
    samples: int         # počet vzorků bez vah
    own_sales: int


@dataclass(slots=True)
class ReferenceBook:
    """Referenční ceny pro posouzení. Klíč bez zdroje: rodina|čip[|ssd]."""

    manual: dict[str, int]
    measured: dict[str, dict[str, Measured]] = field(default_factory=dict)   # zdroj -> skupina -> hodnota
    mode: str = "auto"
    min_samples: int = 3
    fallback_source: str | None = None

    def lookup(self, source: str, family: str | None, chip: str | None,
               ssd_gb: int | None) -> tuple[int, str, str, int] | None:
        """Vrátí (cena, klíč, původ, počet vzorků). Původ: measured, config, config:zdroj."""
        if not family or not chip:
            return None
        groups = [f"{family}|{chip}|{ssd_gb}"] if ssd_gb else []
        groups.append(f"{family}|{chip}")
        sources = [source]
        if self.fallback_source and self.fallback_source != source:
            sources.append(self.fallback_source)
        for src in sources:
            suffix = "" if src == source else f":{src}"
            for group in groups:
                if self.mode == "auto":
                    hit = self.measured.get(src, {}).get(group)
                    if hit and hit.samples >= self.min_samples:
                        return hit.price_czk, f"{src}|{group}", "measured" + suffix, hit.samples
                key = f"{src}|{group}"
                if key in self.manual:
                    return self.manual[key], key, "config" + suffix, 0
        return None


def build_measured(samples: list[Sample]) -> dict[str, dict[str, Measured]]:
    """Seskupí vzorky po zdrojích a skupinách. Vlastní prodeje se přidají ke každému zdroji.

    Vzorek s SSD se počítá i do obecné skupiny rodina|čip.
    """
    by_source: dict[str, dict[str, list[Sample]]] = defaultdict(lambda: defaultdict(list))
    sources = {s.source for s in samples if s.source != "vlastni"} | {"bazos", "facebook"}
    for sample in samples:
        targets = sources if sample.source == "vlastni" else {sample.source}
        generic = "|".join(sample.group.split("|")[:2])
        for src in targets:
            by_source[src][sample.group].append(sample)
            if generic != sample.group:
                by_source[src][generic].append(sample)
    result: dict[str, dict[str, Measured]] = {}
    for src, groups in by_source.items():
        result[src] = {}
        for group, items in groups.items():
            value = weighted_median([(s.price_czk, s.weight) for s in items])
            if value is None:
                continue
            result[src][group] = Measured(round(value), len(items),
                                         sum(1 for s in items if s.source == "vlastni"))
    return result


def reference_book(conn: sqlite3.Connection, market: MarketConfig, pricing: PricingConfig,
                   now: datetime) -> ReferenceBook:
    samples = sold_listing_samples(conn, market, now) + own_sale_samples(conn, market, now)
    return ReferenceBook(manual=dict(pricing.reference), measured=build_measured(samples),
                         mode=market.reference_mode, min_samples=market.min_samples,
                         fallback_source=pricing.fallback_source)


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
           WHERE l.gone_at IS NULL AND a.eligible = 1 AND a.is_dealer = 0 AND l.price_czk > 0""").fetchall()
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
