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
    lifecycle_search_url: str = "https://pc.bazos.cz/?hledat=macbook&hlokalita=&humkreis=25&cenaod=&cenado=&order=&kitx=ne"
    lifecycle_interval_hours: float = 3.0
    lifecycle_max_pages: int = 100
    lifecycle_confirm_per_pass: int = 40


@dataclass(frozen=True, slots=True)
class MarketConfig:
    reference_mode: str = "auto"      # auto: naměřená cena, když je dost vzorků; manual: jen ruční tabulka
    sold_within_days: int = 14
    lookback_days: int = 120
    min_samples: int = 3
    own_sale_weight: int = 3
    ram_pct_per_doubling: float = 18.0    # přirážka za každé zdvojnásobení RAM v %
    ssd_pct_per_doubling: float = 11.0    # přirážka za každé zdvojnásobení SSD v %
    refit_interval_days: float = 7.0      # jak často přepočítat cenovou tabulku z inzerátů
    auto_percentages: bool = True         # přepočet upraví i přirážky


@dataclass(frozen=True, slots=True)
class TravelSettings:
    enabled: bool = True
    home_psc: str = ""
    max_km: float | None = None          # None znamená celá ČR bez omezení
    consumption_l_per_100km: float = 6.5
    fuel_price_czk_per_l: float = 38.0
    extra_czk_per_km: float = 0.0        # amortizace, dálniční známka, čas
    road_factor: float = 1.3             # silnice jsou delší než vzdušná čára
    round_trip: bool = True

    @property
    def czk_per_km(self) -> float:
        return self.consumption_l_per_100km / 100 * self.fuel_price_czk_per_l + self.extra_czk_per_km


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    capital_cap_czk: int = 0          # 0 znamená bez stropu


@dataclass(frozen=True, slots=True)
class FacebookConfig:
    enabled: bool
    feed_url: str = "http://127.0.0.1:5000/rss"
    config_path: Path | None = None     # config.json fb-mp-rss, upravuje se z dashboardu


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
    alert_max_age_hours: float = 3.0      # starší inzerát (od vložení nebo zlevnění) se nepingne, 0 = bez limitu
    suspicious_below_ratio: float = 0.3   # cena pod tímto podílem reference je podezřelá
    suspicious_below_czk: int = 3000      # a pod touto cenou taky, i když reference chybí
    buy_min_profit_czk: int = 0           # koupit i tehdy, když zisk po cestě dosáhne aspoň tolika Kč, 0 = vypnuto
    far_alert_min_profit_czk: int = 0     # pingnout i kus za max_km, když zisk po cestě dosáhne aspoň tolika Kč
    hot_profit_czk: int = 0               # zisk aspoň tolik Kč = ping s @here a 🔥, 0 = vypnuto
    stale_after_days: float = 21.0        # výhodný kus neprodaný déle (od vložení nebo zlevnění) je jen SLEDOVAT, 0 = vypnuto


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
    market: MarketConfig = field(default_factory=MarketConfig)
    travel: TravelSettings = field(default_factory=TravelSettings)
    inventory: InventoryConfig = field(default_factory=InventoryConfig)


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
    if not 0 <= float(data.get("suspicious_below_ratio", 0.3)) < buy:
        raise ConfigError("pricing: hranice podezřelé ceny musí být pod hranicí pro koupit")
    return PricingConfig(
        buy_ratio=buy,
        watch_ratio=watch,
        battery_min_pct=int(data.get("battery_min_pct", 85)),
        cycles_max=int(data.get("cycles_max", 500)),
        battery_penalty_czk=int(data.get("battery_penalty_czk", 0)),
        alert_dealers=bool(data.get("alert_dealers", False)),
        unknown_model_alert_below_czk=int(data.get("unknown_model_alert_below_czk", 0)),
        alert_max_age_hours=max(float(data.get("alert_max_age_hours", 3.0) or 0), 0.0),
        stale_after_days=max(float(data.get("stale_after_days", 21.0) or 0), 0.0),
        suspicious_below_ratio=float(data.get("suspicious_below_ratio", 0.3)),
        suspicious_below_czk=int(data.get("suspicious_below_czk", 3000)),
        buy_min_profit_czk=max(int(data.get("buy_min_profit_czk", 0) or 0), 0),
        far_alert_min_profit_czk=max(int(data.get("far_alert_min_profit_czk", 0) or 0), 0),
        hot_profit_czk=max(int(data.get("hot_profit_czk", 0) or 0), 0),
        fallback_source=data.get("fallback_source"),
        reference={key.lower(): value for key, value in reference.items()},
    )


def parse_market(data: dict[str, Any]) -> MarketConfig:
    mode = data.get("reference_mode", "auto")
    if mode not in ("auto", "manual"):
        raise ConfigError("market.reference_mode musí být auto nebo manual")
    cfg = MarketConfig(
        reference_mode=mode,
        sold_within_days=int(data.get("sold_within_days", 14)),
        lookback_days=int(data.get("lookback_days", 120)),
        min_samples=int(data.get("min_samples", 3)),
        own_sale_weight=int(data.get("own_sale_weight", 3)),
        ram_pct_per_doubling=float(data.get("ram_pct_per_doubling", 18.0)),
        ssd_pct_per_doubling=float(data.get("ssd_pct_per_doubling", 11.0)),
        refit_interval_days=float(data.get("refit_interval_days", 7.0)),
        auto_percentages=bool(data.get("auto_percentages", True)),
    )
    if not 0 <= cfg.ram_pct_per_doubling <= 100 or not 0 <= cfg.ssd_pct_per_doubling <= 100:
        raise ConfigError("market: přirážky za RAM a SSD musí být 0 až 100 %")
    if cfg.refit_interval_days < 1:
        raise ConfigError("market: přepočet tabulky nejčastěji jednou za den")
    if min(cfg.sold_within_days, cfg.lookback_days, cfg.min_samples, cfg.own_sale_weight) < 1:
        raise ConfigError("market: počty dní, vzorků a váha musí být aspoň 1")
    return cfg


def parse_travel(data: dict[str, Any]) -> TravelSettings:
    unlimited = bool(data.get("unlimited", False))
    max_km = data.get("max_km")
    cfg = TravelSettings(
        enabled=bool(data.get("enabled", True)),
        home_psc=str(data.get("home_psc", "") or ""),
        max_km=None if unlimited or max_km in (None, "", 0) else float(max_km),
        consumption_l_per_100km=float(data.get("consumption_l_per_100km", 6.5)),
        fuel_price_czk_per_l=float(data.get("fuel_price_czk_per_l", 38.0)),
        extra_czk_per_km=float(data.get("extra_czk_per_km", 0.0)),
        road_factor=float(data.get("road_factor", 1.3)),
        round_trip=bool(data.get("round_trip", True)),
    )
    if cfg.consumption_l_per_100km < 0 or cfg.fuel_price_czk_per_l < 0 or cfg.road_factor < 1:
        raise ConfigError("travel: spotřeba a cena paliva nesmí být záporné, road_factor aspoň 1")
    return cfg


def _resolve(base_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


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
            lifecycle_search_url=bazos.get("lifecycle_search_url", BazosConfig.lifecycle_search_url),
            lifecycle_interval_hours=float(bazos.get("lifecycle_interval_hours", 3.0)),
            lifecycle_max_pages=int(bazos.get("lifecycle_max_pages", 100)),
            lifecycle_confirm_per_pass=int(bazos.get("lifecycle_confirm_per_pass", 40)),
        ),
        facebook=FacebookConfig(
            enabled=bool(facebook.get("enabled", False)),
            feed_url=facebook.get("feed_url", "http://127.0.0.1:5000/rss"),
            config_path=_resolve(base_dir, facebook.get("config_path")),
        ),
        parsing=parse_parsing(_require(data, "parsing", "")),
        pricing=parse_pricing(_require(data, "pricing", "")),
        market=parse_market(data.get("market", {})),
        travel=parse_travel(data.get("travel", {})),
        inventory=InventoryConfig(
            capital_cap_czk=int(data.get("inventory", {}).get("capital_cap_czk", 0) or 0)),
    )


def load_config(path: Path) -> AppConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config {path} neexistuje, zkopíruj config.example.json") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {path} není platný JSON: {exc}") from exc
    return parse_config(data, path.resolve().parent)
