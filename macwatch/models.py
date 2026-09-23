"""Datové typy sdílené mezi zdroji, parserem, posouzením a databází."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawListing:
    """Inzerát tak, jak ho vrátí zdroj. Nic v něm není vyhodnocené."""

    source: str
    source_id: str
    url: str
    title: str
    price_raw: str
    body: str | None
    body_complete: bool
    fetched_at: datetime
    category: str | None = None
    locality: str | None = None
    postal_code: str | None = None
    published_at: datetime | None = None
    sold: bool = False          # zdroj hlásí prodáno
    reserved: bool = False      # zdroj hlásí rezervováno

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)


@dataclass(frozen=True, slots=True)
class Specs:
    """Konfigurace vytažená z textu inzerátu."""

    family: str | None = None
    chip: str | None = None
    ram_gb: int | None = None
    ssd_gb: int | None = None
    year: int | None = None
    battery_pct: int | None = None
    cycles: int | None = None
    is_dealer: bool = False
    red_flags: tuple[str, ...] = ()
    eligible: bool = False
    reason: str | None = None

    def model_label(self) -> str:
        """Lidsky čitelný název, např. ``MacBook Air M2 16/512``."""
        family = {"air": "Air", "pro": "Pro"}.get(self.family or "", "")
        parts = ["MacBook"]
        if family:
            parts.append(family)
        parts.append(self.chip.upper().replace(" PRO", " Pro").replace(" MAX", " Max")
                     .replace(" ULTRA", " Ultra") if self.chip else "čip ?")
        ram = str(self.ram_gb) if self.ram_gb else "?"
        if self.ssd_gb is None:
            ssd = "?"
        elif self.ssd_gb >= 1024:
            ssd = f"{self.ssd_gb // 1024}TB"
        else:
            ssd = str(self.ssd_gb)
        parts.append(f"{ram}/{ssd}")
        return " ".join(parts)


class Verdict:
    KOUPIT = "KOUPIT"
    KOUPIT_S_RIZIKEM = "KOUPIT_S_RIZIKEM"
    SLEDOVAT = "SLEDOVAT"
    NEVYPLATI_SE = "NEVYPLATI_SE"
    NEZNAMY_MODEL = "NEZNAMY_MODEL"
    BEZ_CENY = "BEZ_CENY"
    PODEZRELE = "PODEZRELE"   # podezřele nízká cena: příslušenství, díl nebo podvod
    OVERIT = "OVERIT"         # titulek bez čipu a bez popisu (Facebook), může to být M1 i Intel
    MIMO = "MIMO"


@dataclass(frozen=True, slots=True)
class Assessment:
    verdict: str
    should_alert: bool = False
    reference_czk: int | None = None
    reference_key: str | None = None
    reference_origin: str | None = None
    adjusted_reference_czk: int | None = None
    target_czk: int | None = None
    est_profit_czk: int | None = None
    roi: float | None = None
    score: float | None = None
    notes: tuple[str, ...] = field(default=())
    distance_km: float | None = None
    travel_cost_czk: int | None = None
    reference_samples: int | None = None
    locality_label: str | None = None
    fresh_since: datetime | None = None   # vložení nebo zlevnění, pro upozornění
    price_dropped: bool = False
