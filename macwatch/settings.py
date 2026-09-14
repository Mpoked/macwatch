"""Nastavení upravitelné z dashboardu.

`config.json` je výchozí stav. Co se změní na webu, uloží se do tabulky `settings`
(klíč je tečková cesta v configu, hodnota JSON) a při načtení configu to přebije
soubor. Ruční referenční ceny žijí v tabulce `reference_prices`, při prvním startu
se do ní zkopírují z configu.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from macwatch.config import AppConfig, ConfigError, parse_config
from macwatch.db import Database

_SEEDED_FLAG = "_references_seeded"


@dataclass(frozen=True, slots=True)
class Field:
    path: str
    kind: str            # str, secret, int, float, bool, lines, choice
    label: str
    group: str
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()


FIELDS: tuple[Field, ...] = (
    Field("discord_webhook_url", "secret", "Discord webhook URL", "Upozornění",
          "Nastavení kanálu, Integrace, Webhooky, Kopírovat URL."),
    Field("poll_interval_seconds", "int", "Interval průchodu (s)", "Upozornění",
          "Jak často se čtou RSS feedy. Bazoš snese 180 až 300 s."),

    Field("sources.bazos.enabled", "bool", "Bazoš zapnutý", "Zdroje"),
    Field("sources.bazos.feeds", "lines", "Bazoš RSS feedy", "Zdroje", "Jeden feed na řádek."),
    Field("sources.bazos.max_details_per_poll", "int", "Detailů za průchod", "Zdroje"),
    Field("sources.bazos.lifecycle_search_url", "str", "Stránka hledání pro životní cyklus", "Zdroje",
          "Hledání bez parametru crp, stránky se doplní samy."),
    Field("sources.bazos.lifecycle_interval_hours", "float", "Kontrola zmizelých inzerátů (h)", "Zdroje"),
    Field("sources.bazos.lifecycle_confirm_per_pass", "int", "Ověření detailem za kontrolu", "Zdroje"),
    Field("sources.facebook.enabled", "bool", "Facebook Marketplace zapnutý", "Zdroje"),
    Field("sources.facebook.feed_url", "str", "Feed fb-mp-rss", "Zdroje"),

    Field("pricing.buy_ratio", "float", "Koupit pod (podíl reference)", "Posouzení", "Výchozí 0,65."),
    Field("pricing.watch_ratio", "float", "Sledovat pod (podíl reference)", "Posouzení", "Výchozí 0,80."),
    Field("pricing.battery_min_pct", "int", "Minimální zdraví baterie (%)", "Posouzení"),
    Field("pricing.cycles_max", "int", "Maximum cyklů baterie", "Posouzení"),
    Field("pricing.battery_penalty_czk", "int", "Srážka za baterii (Kč)", "Posouzení"),
    Field("pricing.alert_dealers", "bool", "Posílat i obchodníky", "Posouzení"),
    Field("pricing.unknown_model_alert_below_czk", "int", "Pingnout neznámý model pod (Kč)", "Posouzení"),

    Field("market.reference_mode", "choice", "Referenční cena", "Trh",
          choices=(("auto", "Naměřená, když je dost vzorků, jinak ruční"), ("manual", "Jen ruční tabulka"))),
    Field("market.sold_within_days", "int", "Prodáno do (dní)", "Trh",
          "Zmizení do tolika dní od vložení se počítá jako prodej."),
    Field("market.lookback_days", "int", "Počítat z posledních (dní)", "Trh"),
    Field("market.min_samples", "int", "Minimum vzorků", "Trh"),
    Field("market.own_sale_weight", "int", "Váha vlastního prodeje", "Trh",
          "Kolikrát víc váží tvůj prodej než cizí zmizelý inzerát."),

    Field("travel.enabled", "bool", "Počítat cestu", "Dojezd"),
    Field("travel.home_psc", "str", "Domovské PSČ", "Dojezd"),
    Field("travel.unlimited", "bool", "Celá ČR bez omezení", "Dojezd"),
    Field("travel.max_km", "float", "Maximální vzdálenost (km)", "Dojezd",
          "Dál se nepinguje, v přehledu zůstává."),
    Field("travel.consumption_l_per_100km", "float", "Spotřeba (l/100 km)", "Dojezd"),
    Field("travel.fuel_price_czk_per_l", "float", "Cena paliva (Kč/l)", "Dojezd"),
    Field("travel.extra_czk_per_km", "float", "Další náklady (Kč/km)", "Dojezd",
          "Opotřebení auta, dálnice, čas. Klidně 0."),
    Field("travel.road_factor", "float", "Koeficient silnice", "Dojezd",
          "Silnice je delší než vzdušná čára, obvykle 1,2 až 1,4."),
    Field("travel.round_trip", "bool", "Počítat tam i zpět", "Dojezd"),

    Field("inventory.capital_cap_czk", "int", "Strop vázaného kapitálu (Kč)", "Sklad", "0 znamená bez stropu."),
)

FIELDS_BY_PATH = {f.path: f for f in FIELDS}


def effective_value(cfg: AppConfig, path: str) -> Any:
    """Platná hodnota pole z načteného configu, včetně výchozích hodnot."""
    if path == "travel.unlimited":
        return cfg.travel.max_km is None
    parts = path.split(".")
    if parts[0] == "sources":
        node: Any = getattr(cfg, parts[1])
        parts = parts[2:]
    else:
        node = cfg
    for part in parts:
        node = getattr(node, part)
    return list(node) if isinstance(node, tuple) else node


def get_path(data: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def coerce(field: Field, raw: str | None) -> Any:
    """Převede hodnotu z formuláře na typ pole. Chybu hlásí jako ConfigError."""
    text = (raw or "").strip()
    try:
        if field.kind == "bool":
            return raw is not None and text not in ("", "0", "off", "false")
        if field.kind == "int":
            return int(text.replace(" ", "")) if text else 0
        if field.kind == "float":
            return float(text.replace(" ", "").replace(",", ".")) if text else 0.0
        if field.kind == "lines":
            return [line.strip() for line in text.splitlines() if line.strip()]
        if field.kind == "choice":
            if text not in {value for value, _ in field.choices}:
                raise ValueError(text)
            return text
        return text
    except ValueError as exc:
        raise ConfigError(f"{field.label}: neplatná hodnota {text!r}") from exc


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def overrides(self) -> dict[str, Any]:
        rows = self.db.conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows if not row["key"].startswith("_")}

    def save(self, values: dict[str, Any], now: datetime) -> None:
        with self.db.conn:
            for key, value in values.items():
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now.isoformat(timespec="seconds")))

    def references(self) -> dict[str, int]:
        rows = self.db.conn.execute("SELECT key, price_czk FROM reference_prices ORDER BY key")
        return {row["key"]: row["price_czk"] for row in rows}

    def set_reference(self, key: str, price_czk: int, now: datetime) -> None:
        key = normalize_reference_key(key)
        if price_czk <= 0:
            raise ConfigError("referenční cena musí být kladná")
        with self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO reference_prices (key, price_czk, updated_at) VALUES (?, ?, ?)",
                (key, price_czk, now.isoformat(timespec="seconds")))

    def delete_reference(self, key: str) -> None:
        with self.db.conn:
            self.db.conn.execute("DELETE FROM reference_prices WHERE key = ?", (key,))

    def seed_references(self, references: dict[str, int], now: datetime) -> None:
        """Jednou zkopíruje ruční ceny z configu. Pak je autoritou databáze."""
        flag = self.db.conn.execute("SELECT 1 FROM settings WHERE key = ?", (_SEEDED_FLAG,)).fetchone()
        if flag:
            return
        with self.db.conn:
            for key, price in references.items():
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO reference_prices (key, price_czk, updated_at) VALUES (?, ?, ?)",
                    (key.lower(), price, now.isoformat(timespec="seconds")))
            self.db.conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, 'true', ?)",
                (_SEEDED_FLAG, now.isoformat(timespec="seconds")))


def normalize_reference_key(key: str) -> str:
    parts = [part.strip().lower() for part in key.split("|")]
    if len(parts) not in (3, 4) or not all(parts):
        raise ConfigError(f"klíč {key!r} má tvar zdroj|rodina|čip nebo zdroj|rodina|čip|ssd")
    if len(parts) == 4 and not parts[3].isdigit():
        raise ConfigError(f"SSD v klíči {key!r} musí být číslo v GB, např. 512")
    return "|".join(parts)


def read_config_data(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config {path} neexistuje, zkopíruj config.example.json") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {path} není platný JSON: {exc}") from exc


def merged_data(base: dict[str, Any], store: SettingsStore,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = copy.deepcopy(base)
    for key, value in {**store.overrides(), **(extra or {})}.items():
        set_path(data, key, value)
    references = store.references()
    set_path(data, "pricing.reference", references)
    return data


def load_effective_config(config_path: Path, db: Database, now: datetime) -> AppConfig:
    base = read_config_data(config_path)
    store = SettingsStore(db)
    store.seed_references(get_path(base, "pricing.reference", {}) or {}, now)
    return parse_config(merged_data(base, store), config_path.resolve().parent)


def validate_and_save(config_path: Path, db: Database, values: dict[str, Any], now: datetime) -> AppConfig:
    """Ověří, že config s novými hodnotami projde validací, a teprve pak uloží."""
    base = read_config_data(config_path)
    store = SettingsStore(db)
    cfg = parse_config(merged_data(base, store, values), config_path.resolve().parent)
    store.save(values, now)
    return cfg
