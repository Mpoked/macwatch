"""Cenová tabulka MacBooků: základní cena modelu a přirážky za RAM a SSD.

Cena konfigurace = základní cena modelu × (1 + přirážka RAM)^zdvojnásobení × (1 + přirážka SSD)^zdvojnásobení.
Zdvojnásobení se počítá plynule: 8 → 16 GB je 1, 16 → 24 GB je log2(1,5) ≈ 0,58.
Chybějící RAM nebo SSD se bere jako základní konfigurace modelu.

Tabulka se odhaduje z inzertních cen (logaritmická regrese s pevným efektem modelu)
a jednou týdně přepočítává. Ručně upravené a zamčené řádky přepočet nepřepíše.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

log = logging.getLogger(__name__)

DEFAULT_BASE = (8, 256)
FIT_WINDOW_DAYS = 14
MIN_GROUP_SAMPLES = 3
MIN_TOTAL_SAMPLES = 30
FIT_KEY = "_price_fit"


@dataclass(frozen=True, slots=True)
class PriceModel:
    family: str
    chip: str
    base_ram_gb: int
    base_ssd_gb: int
    base_price_czk: int
    locked: bool = False
    samples: int = 0
    updated_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.family}|{self.chip}"


def doublings(value: int | None, base: int) -> float:
    if not value or not base:
        return 0.0
    return math.log2(value / base)


def config_factor(ram_gb: int | None, ssd_gb: int | None, base_ram_gb: int, base_ssd_gb: int,
                  ram_pct: float, ssd_pct: float) -> float:
    """Kolikrát je konfigurace dražší než základ. Přirážky v procentech (18 = +18 %)."""
    return ((1 + ram_pct / 100) ** doublings(ram_gb, base_ram_gb)
            * (1 + ssd_pct / 100) ** doublings(ssd_gb, base_ssd_gb))


def price_for(model: PriceModel, ram_gb: int | None, ssd_gb: int | None,
              ram_pct: float, ssd_pct: float) -> int:
    factor = config_factor(ram_gb, ssd_gb, model.base_ram_gb, model.base_ssd_gb, ram_pct, ssd_pct)
    return round(model.base_price_czk * factor)


# odhad z inzertních cen

@dataclass(frozen=True, slots=True)
class AskingSample:
    family: str
    chip: str
    ram_gb: int
    ssd_gb: int
    price_czk: int

    @property
    def key(self) -> str:
        return f"{self.family}|{self.chip}"


@dataclass(slots=True)
class FitResult:
    ram_pct: float
    ssd_pct: float
    models: dict[str, PriceModel] = field(default_factory=dict)
    samples: int = 0
    median_error_pct: float | None = None


def remove_outliers(samples: list[AskingSample], low: float = 0.4, high: float = 2.5) -> list[AskingSample]:
    """Vyřadí ceny mimo 0,4 až 2,5 násobek mediánu modelu (poškozené kusy, překlepy, obaly)."""
    groups: dict[str, list[AskingSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.key].append(sample)
    kept = []
    for items in groups.values():
        med = median(s.price_czk for s in items)
        kept.extend(s for s in items if low * med <= s.price_czk <= high * med)
    return kept


def entry_value(values: list[int], min_share: float = 0.15) -> int:
    """Nejnižší hodnota, kterou má aspoň 15 % inzerátů: vstupní konfigurace modelu.

    Ojedinělé překlepy v titulku (Air M1 8/128) tak základ neurčí.
    """
    counts = Counter(values)
    common = [value for value, count in counts.items() if count >= min_share * len(values)]
    return min(common) if common else counts.most_common(1)[0][0]


def fit(samples: list[AskingSample], min_group: int = MIN_GROUP_SAMPLES) -> FitResult | None:
    """Odhad přirážek a základních cen. Vrací None, když je dat málo."""
    samples = remove_outliers(samples)
    groups: dict[str, list[AskingSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.key].append(sample)
    groups = {k: v for k, v in groups.items() if len(v) >= min_group}
    used = [s for items in groups.values() for s in items]
    if len(used) < MIN_TOTAL_SAMPLES:
        return None

    bases = {key: (entry_value([s.ram_gb for s in items]), entry_value([s.ssd_gb for s in items]))
             for key, items in groups.items()}

    # pevné efekty modelu: odečíst průměry ve skupině a vyřešit 2×2 normální rovnice
    sxx = [[0.0, 0.0], [0.0, 0.0]]
    sxy = [0.0, 0.0]
    means: dict[str, tuple[float, float, float]] = {}
    for key, items in groups.items():
        base_ram, base_ssd = bases[key]
        points = [(doublings(s.ram_gb, base_ram), doublings(s.ssd_gb, base_ssd), math.log(s.price_czk))
                  for s in items]
        mr = sum(p[0] for p in points) / len(points)
        ms = sum(p[1] for p in points) / len(points)
        my = sum(p[2] for p in points) / len(points)
        means[key] = (mr, ms, my)
        for r, s, y in points:
            dr, ds, dy = r - mr, s - ms, y - my
            sxx[0][0] += dr * dr
            sxx[0][1] += dr * ds
            sxx[1][1] += ds * ds
            sxy[0] += dr * dy
            sxy[1] += ds * dy
    det = sxx[0][0] * sxx[1][1] - sxx[0][1] ** 2
    if abs(det) < 1e-9:
        return None
    k_ram = (sxy[0] * sxx[1][1] - sxy[1] * sxx[0][1]) / det
    k_ssd = (sxy[1] * sxx[0][0] - sxy[0] * sxx[0][1]) / det
    # rozumné meze, aby jeden podivný týden tabulku nerozbil
    k_ram = min(max(k_ram, math.log(1.0)), math.log(1.6))
    k_ssd = min(max(k_ssd, math.log(1.0)), math.log(1.4))

    result = FitResult(ram_pct=round((math.exp(k_ram) - 1) * 100, 1),
                       ssd_pct=round((math.exp(k_ssd) - 1) * 100, 1), samples=len(used))
    errors = []
    for key, items in groups.items():
        mr, ms, my = means[key]
        base_price = math.exp(my - k_ram * mr - k_ssd * ms)
        family, chip = key.split("|", 1)
        model = PriceModel(family, chip, bases[key][0], bases[key][1], int(round(base_price, -2)),
                           samples=len(items))
        result.models[key] = model
        for s in items:
            predicted = price_for(model, s.ram_gb, s.ssd_gb, result.ram_pct, result.ssd_pct)
            errors.append(abs(predicted - s.price_czk) / s.price_czk)
    result.median_error_pct = round(median(errors) * 100, 1) if errors else None
    return result


# databáze

def load_models(conn: sqlite3.Connection) -> dict[str, PriceModel]:
    rows = conn.execute("SELECT * FROM price_models").fetchall()
    return {f"{r['family']}|{r['chip']}": PriceModel(r["family"], r["chip"], r["base_ram_gb"], r["base_ssd_gb"],
                                                      r["base_price_czk"], bool(r["locked"]), r["samples"],
                                                      r["updated_at"]) for r in rows}


def save_model(conn: sqlite3.Connection, model: PriceModel, now: datetime) -> None:
    with conn:
        conn.execute(
            """INSERT INTO price_models (family, chip, base_ram_gb, base_ssd_gb, base_price_czk, locked,
                   samples, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (family, chip) DO UPDATE SET base_ram_gb = excluded.base_ram_gb,
                   base_ssd_gb = excluded.base_ssd_gb, base_price_czk = excluded.base_price_czk,
                   locked = excluded.locked, samples = excluded.samples, updated_at = excluded.updated_at""",
            (model.family, model.chip, model.base_ram_gb, model.base_ssd_gb, model.base_price_czk,
             int(model.locked), model.samples, now.isoformat(timespec="seconds")))


def set_locked(conn: sqlite3.Connection, family: str, chip: str, locked: bool) -> None:
    with conn:
        conn.execute("UPDATE price_models SET locked = ? WHERE family = ? AND chip = ?",
                     (int(locked), family, chip))


def delete_model(conn: sqlite3.Connection, family: str, chip: str) -> None:
    with conn:
        conn.execute("DELETE FROM price_models WHERE family = ? AND chip = ?", (family, chip))


def record_snapshot(conn: sqlite3.Connection, rows: list[tuple], now: datetime) -> None:
    """rows: (source, source_id, title, price_czk, eligible, family, chip, ram_gb, ssd_gb)."""
    stamp = now.isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            """INSERT INTO asking_snapshots (source, source_id, title, price_czk, eligible, family, chip,
                   ram_gb, ssd_gb, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET title = excluded.title,
                   price_czk = excluded.price_czk, eligible = excluded.eligible, family = excluded.family,
                   chip = excluded.chip, ram_gb = excluded.ram_gb, ssd_gb = excluded.ssd_gb,
                   last_seen = excluded.last_seen""",
            [(*row, stamp) for row in rows])


def asking_samples(conn: sqlite3.Connection, now: datetime, days: int = FIT_WINDOW_DAYS) -> list[AskingSample]:
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT family, chip, ram_gb, ssd_gb, price_czk FROM asking_snapshots
           WHERE last_seen >= ? AND eligible = 1 AND chip IS NOT NULL AND ram_gb IS NOT NULL
             AND ssd_gb IS NOT NULL AND price_czk >= 2000""", (since,)).fetchall()
    return [AskingSample(r["family"], r["chip"], r["ram_gb"], r["ssd_gb"], r["price_czk"]) for r in rows]


def last_fit(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (FIT_KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def refit(conn: sqlite3.Connection, now: datetime, *, update_percentages: bool) -> FitResult | None:
    """Přepočítá tabulku z inzertních cen za posledních 14 dní. Zamčené řádky nechá být."""
    result = fit(asking_samples(conn, now))
    if result is None:
        log.warning("na přepočet cenové tabulky je málo inzerátů s RAM a SSD")
        return None
    existing = load_models(conn)
    updated = 0
    for key, model in result.models.items():
        if existing.get(key) and existing[key].locked:
            continue
        save_model(conn, model, now)
        updated += 1
    info = {"at": now.isoformat(timespec="seconds"), "samples": result.samples, "models": updated,
            "ram_pct": result.ram_pct, "ssd_pct": result.ssd_pct, "median_error_pct": result.median_error_pct}
    stamp = now.isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                     (FIT_KEY, json.dumps(info), stamp))
        if update_percentages:
            for path, value in (("market.ram_pct_per_doubling", result.ram_pct),
                                ("market.ssd_pct_per_doubling", result.ssd_pct)):
                conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                             (path, json.dumps(value), stamp))
    log.info("cenová tabulka přepočtena: %d modelů z %d inzerátů, RAM +%.1f %%, SSD +%.1f %%, chyba %s %%",
             updated, result.samples, result.ram_pct, result.ssd_pct, result.median_error_pct)
    return result


def refit_due(conn: sqlite3.Connection, now: datetime, interval_days: float) -> bool:
    info = last_fit(conn)
    if info is None:
        return True
    return now - datetime.fromisoformat(info["at"]) >= timedelta(days=interval_days)
