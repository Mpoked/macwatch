"""Referenční ceny, srážky, skóre a verdikt."""

from __future__ import annotations

from macwatch.config import PricingConfig
from macwatch.models import Assessment, Specs, Verdict

ALERT_VERDICTS = frozenset({Verdict.KOUPIT, Verdict.KOUPIT_S_RIZIKEM, Verdict.NEZNAMY_MODEL})


def lookup_reference(source: str, specs: Specs,
                     cfg: PricingConfig) -> tuple[int, str, str] | None:
    """Najde referenční cenu. Vrací (cena, klíč, původ).

    Pořadí: zdroj|rodina|čip|ssd, zdroj|rodina|čip, a pak totéž pro
    záložní zdroj (výchozí Bazoš), dokud pro daný zdroj nejsou vlastní data.
    """
    if not specs.family or not specs.chip:
        return None
    sources = [source]
    if cfg.fallback_source and cfg.fallback_source != source:
        sources.append(cfg.fallback_source)
    for src in sources:
        keys = []
        if specs.ssd_gb:
            keys.append(f"{src}|{specs.family}|{specs.chip}|{specs.ssd_gb}")
        keys.append(f"{src}|{specs.family}|{specs.chip}")
        for key in keys:
            if key in cfg.reference:
                origin = "config" if src == source else f"config:{src}"
                return cfg.reference[key], key, origin
    return None


def battery_penalty(specs: Specs, cfg: PricingConfig) -> int:
    """Srážka za opotřebenou baterii. Strhne se nejvýš jednou."""
    worn = (specs.battery_pct is not None and specs.battery_pct < cfg.battery_min_pct) or \
           (specs.cycles is not None and specs.cycles > cfg.cycles_max)
    return cfg.battery_penalty_czk if worn else 0


def assess(source: str, price_czk: int | None, specs: Specs, cfg: PricingConfig) -> Assessment:
    if not specs.eligible:
        return Assessment(Verdict.MIMO, notes=(specs.reason,) if specs.reason else ())
    if price_czk is None or price_czk <= 0:
        return Assessment(Verdict.BEZ_CENY)

    notes: list[str] = []
    if specs.red_flags:
        notes.append("riziko: " + ", ".join(specs.red_flags))
    if specs.is_dealer:
        notes.append("obchodník")
    dealer_blocked = specs.is_dealer and not cfg.alert_dealers

    found = lookup_reference(source, specs, cfg)
    if found is None:
        cheap = price_czk <= cfg.unknown_model_alert_below_czk
        return Assessment(Verdict.NEZNAMY_MODEL, should_alert=cheap and not dealer_blocked,
                          notes=tuple(notes))

    reference, key, origin = found
    penalty = battery_penalty(specs, cfg)
    if penalty:
        notes.append(f"srážka za baterii {penalty} Kč")
    adjusted = reference - penalty
    buy_limit = round(adjusted * cfg.buy_ratio)
    watch_limit = round(adjusted * cfg.watch_ratio)
    profit = adjusted - price_czk
    roi = profit / price_czk

    if price_czk <= buy_limit:
        verdict = Verdict.KOUPIT_S_RIZIKEM if specs.red_flags else Verdict.KOUPIT
    elif price_czk <= watch_limit:
        verdict = Verdict.SLEDOVAT
    else:
        verdict = Verdict.NEVYPLATI_SE

    return Assessment(
        verdict=verdict,
        should_alert=verdict in ALERT_VERDICTS and not dealer_blocked,
        reference_czk=reference,
        reference_key=key,
        reference_origin=origin,
        adjusted_reference_czk=adjusted,
        target_czk=buy_limit,
        est_profit_czk=profit,
        roi=round(roi, 4),
        score=round(roi * 100, 1),
        notes=tuple(notes),
    )
