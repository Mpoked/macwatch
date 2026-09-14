"""Referenční ceny, srážky, cesta, skóre a verdikt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from macwatch.config import PricingConfig
from macwatch.models import Assessment, Specs, Verdict

if TYPE_CHECKING:
    from macwatch.geo import Travel
    from macwatch.market import ReferenceBook

ALERT_VERDICTS = frozenset({Verdict.KOUPIT, Verdict.KOUPIT_S_RIZIKEM, Verdict.NEZNAMY_MODEL})


def manual_book(cfg: PricingConfig) -> ReferenceBook:
    from macwatch.market import ReferenceBook
    return ReferenceBook(manual=dict(cfg.reference), mode="manual",
                         fallback_source=cfg.fallback_source)


def lookup_reference(source: str, specs: Specs,
                     cfg: PricingConfig) -> tuple[int, str, str] | None:
    """Ruční tabulka: zdroj|rodina|čip|ssd, zdroj|rodina|čip, pak totéž pro záložní zdroj."""
    hit = manual_book(cfg).lookup(source, specs.family, specs.chip, specs.ssd_gb)
    return hit[:3] if hit else None


def battery_penalty(specs: Specs, cfg: PricingConfig) -> int:
    """Srážka za opotřebenou baterii. Strhne se nejvýš jednou."""
    worn = (specs.battery_pct is not None and specs.battery_pct < cfg.battery_min_pct) or \
           (specs.cycles is not None and specs.cycles > cfg.cycles_max)
    return cfg.battery_penalty_czk if worn else 0


def assess(source: str, price_czk: int | None, specs: Specs, cfg: PricingConfig,
           book: ReferenceBook | None = None, travel: Travel | None = None) -> Assessment:
    """Posoudí inzerát.

    Cesta není filtr, ale náklad: připočte se k ceně, sníží zisk a ROI a posune verdikt.
    Kus za hranicí `max_km` se nepingne, v přehledu ale zůstane.
    """
    distance = travel.distance_km if travel else None
    travel_cost = travel.cost_czk if travel else 0
    place = travel.place.name if travel else None
    base = dict(distance_km=distance, travel_cost_czk=travel_cost if travel else None,
                locality_label=place)

    if not specs.eligible:
        return Assessment(Verdict.MIMO, notes=(specs.reason,) if specs.reason else (), **base)
    if price_czk is None or price_czk <= 0:
        return Assessment(Verdict.BEZ_CENY, **base)

    notes: list[str] = []
    if specs.red_flags:
        notes.append("riziko: " + ", ".join(specs.red_flags))
    if specs.is_dealer:
        notes.append("obchodník")
    blocked = specs.is_dealer and not cfg.alert_dealers
    if travel and not travel.within_limit:
        notes.append(f"mimo dojezd ({distance:.0f} km)")
        blocked = True
    effective_price = price_czk + travel_cost

    found = (book or manual_book(cfg)).lookup(source, specs.family, specs.chip, specs.ssd_gb)
    if found is None:
        cheap = effective_price <= cfg.unknown_model_alert_below_czk
        return Assessment(Verdict.NEZNAMY_MODEL, should_alert=cheap and not blocked,
                          notes=tuple(notes), **base)

    reference, key, origin, samples = found
    penalty = battery_penalty(specs, cfg)
    if penalty:
        notes.append(f"srážka za baterii {penalty} Kč")
    if travel_cost:
        notes.append(f"cesta {travel_cost} Kč")
    adjusted = reference - penalty
    buy_limit = round(adjusted * cfg.buy_ratio)
    watch_limit = round(adjusted * cfg.watch_ratio)
    profit = adjusted - effective_price
    roi = profit / effective_price

    if effective_price <= buy_limit:
        verdict = Verdict.KOUPIT_S_RIZIKEM if specs.red_flags else Verdict.KOUPIT
    elif effective_price <= watch_limit:
        verdict = Verdict.SLEDOVAT
    else:
        verdict = Verdict.NEVYPLATI_SE

    return Assessment(
        verdict=verdict,
        should_alert=verdict in ALERT_VERDICTS and not blocked,
        reference_czk=reference,
        reference_key=key,
        reference_origin=origin,
        adjusted_reference_czk=adjusted,
        target_czk=buy_limit,
        est_profit_czk=profit,
        roi=round(roi, 4),
        score=round(roi * 100, 1),
        notes=tuple(notes),
        reference_samples=samples,
        **base,
    )
