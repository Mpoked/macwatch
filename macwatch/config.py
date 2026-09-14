"""Načtení a validace config.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HttpConfig:
    user_agent: str
    min_delay_seconds: float = 4.0
    timeout_seconds: float = 20.0
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class BazosConfig:
    enabled: bool
    feeds: tuple[str, ...]
    max_details_per_poll: int = 25


@dataclass(frozen=True, slots=True)
class FacebookConfig:
    enabled: bool
    feed_url: str = "http://127.0.0.1:5000/rss"


@dataclass(frozen=True, slots=True)
class ParsingRules:
    prefilter: tuple[re.Pattern[str], ...]
    families: dict[str, tuple[re.Pattern[str], ...]]
    family_words: dict[str, tuple[re.Pattern[str], ...]]
    chip_pattern: re.Pattern[str]
    min_chip_generation: int
    min_year_without_chip: int
    non_apple_cpu: tuple[re.Pattern[str], ...]
    ram_values_gb: frozenset[int]
    ssd_values_gb: tuple[int, ...]
    negations_before: frozenset[str]
    negations_after: frozenset[str]
    red_flags: dict[str, tuple[re.Pattern[str], ...]]
    dealer: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class PricingConfig:
    buy_ratio: float
    watch_ratio: float
    battery_min_pct: int
    cycles_max: int
    battery_penalty_czk: int
    alert_dealers: bool
    unknown_model_alert_below_czk: int
    fallback_source: str | None
    reference: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AppConfig:
    database: Path
    poll_interval_seconds: int
    discord_webhook_url: str
    http: HttpConfig
    bazos: BazosConfig
    facebook: FacebookConfig
    parsing: ParsingRules
    pricing: PricingConfig


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"v configu chybí klíč {where}{key}")
    return data[key]


def _compile_list(patterns: Any, where: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(patterns, list):
        raise ConfigError(f"{where} musí být seznam regulárních výrazů")
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(f"{where}: neplatný regex {pattern!r}: {exc}") from exc
    return tuple(compiled)


def _compile_map(data: Any, where: str) -> dict[str, tuple[re.Pattern[str], ...]]:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} musí být objekt")
    return {name: _compile_list(patterns, f"{where}.{name}") for name, patterns in data.items()}


def parse_parsing(data: dict[str, Any]) -> ParsingRules:
    w = "parsing."
    try:
        chip = re.compile(_require(data, "chip_pattern", w))
    except re.error as exc:
        raise ConfigError(f"parsing.chip_pattern: {exc}") from exc
    return ParsingRules(
        prefilter=_compile_list(_require(data, "prefilter", w), "parsing.prefilter"),
        families=_compile_map(_require(data, "families", w), "parsing.families"),
        family_words=_compile_map(data.get("family_words", {}), "parsing.family_words"),
        chip_pattern=chip,
        min_chip_generation=int(data.get("min_chip_generation", 1)),
        min_year_without_chip=int(data.get("min_year_without_chip", 9999)),
        non_apple_cpu=_compile_list(data.get("non_apple_cpu", []), "parsing.non_apple_cpu"),
        ram_values_gb=frozenset(int(v) for v in _require(data, "ram_values_gb", w)),
        ssd_values_gb=tuple(sorted(int(v) for v in _require(data, "ssd_values_gb", w))),
        negations_before=frozenset(data.get("negations_before", [])),
        negations_after=frozenset(data.get("negations_after", [])),
        red_flags=_compile_map(data.get("red_flags", {}), "parsing.red_flags"),
        dealer=_compile_list(data.get("dealer", []), "parsing.dealer"),
    )


def parse_pricing(data: dict[str, Any]) -> PricingConfig:
    w = "pricing."
    reference = _require(data, "reference", w)
    if not isinstance(reference, dict):
        raise ConfigError("pricing.reference musí být objekt klíč: cena")
    for key, value in reference.items():
        if key.count("|") not in (2, 3):
            raise ConfigError(f"pricing.reference: klíč {key!r} má tvar zdroj|rodina|čip[|ssd]")
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"pricing.reference: cena u {key!r} musí být kladné celé číslo")
    buy = float(_require(data, "buy_ratio", w))
    watch = float(_require(data, "watch_ratio", w))
    if not 0 < buy <= watch <= 1:
        raise ConfigError("pricing: musí platit 0 < buy_ratio <= watch_ratio <= 1")
    return PricingConfig(
        buy_ratio=buy,
        watch_ratio=watch,
        battery_min_pct=int(data.get("battery_min_pct", 85)),
        cycles_max=int(data.get("cycles_max", 500)),
        battery_penalty_czk=int(data.get("battery_penalty_czk", 0)),
        alert_dealers=bool(data.get("alert_dealers", False)),
        unknown_model_alert_below_czk=int(data.get("unknown_model_alert_below_czk", 0)),
        fallback_source=data.get("fallback_source"),
        reference={key.lower(): value for key, value in reference.items()},
    )


def parse_config(data: dict[str, Any], base_dir: Path) -> AppConfig:
    http = _require(data, "http", "")
    sources = _require(data, "sources", "")
    bazos = sources.get("bazos", {"enabled": False, "feeds": []})
    facebook = sources.get("facebook", {"enabled": False})
    database = Path(data.get("database", "macwatch.db"))
    if not database.is_absolute():
        database = base_dir / database
    webhook = os.environ.get("MACWATCH_DISCORD_WEBHOOK") or data.get("discord_webhook_url", "")
    return AppConfig(
        database=database,
        poll_interval_seconds=int(data.get("poll_interval_seconds", 240)),
        discord_webhook_url=webhook,
        http=HttpConfig(
            user_agent=_require(http, "user_agent", "http."),
            min_delay_seconds=float(http.get("min_delay_seconds", 4)),
            timeout_seconds=float(http.get("timeout_seconds", 20)),
            max_retries=int(http.get("max_retries", 3)),
        ),
        bazos=BazosConfig(
            enabled=bool(bazos.get("enabled", False)),
            feeds=tuple(bazos.get("feeds", [])),
            max_details_per_poll=int(bazos.get("max_details_per_poll", 25)),
        ),
        facebook=FacebookConfig(
            enabled=bool(facebook.get("enabled", False)),
            feed_url=facebook.get("feed_url", "http://127.0.0.1:5000/rss"),
        ),
        parsing=parse_parsing(_require(data, "parsing", "")),
        pricing=parse_pricing(_require(data, "pricing", "")),
    )


def load_config(path: Path) -> AppConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config {path} neexistuje, zkopíruj config.example.json") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {path} není platný JSON: {exc}") from exc
    return parse_config(data, path.resolve().parent)
