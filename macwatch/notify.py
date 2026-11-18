"""Upozornění přes Discord webhook."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from macwatch.models import Assessment, Specs, Verdict

log = logging.getLogger(__name__)

SOURCE_NAMES = {"bazos": "Bazoš", "facebook": "Marketplace"}
_SOURCE_GENITIVE = {"bazos": "Bazoše", "facebook": "Marketplace"}

_HEADINGS = {
    Verdict.KOUPIT: ("✅ VYPLATÍ SE", 0x2ECC71),
    Verdict.KOUPIT_S_RIZIKEM: ("⚠️ VYPLATÍ SE, S RIZIKEM", 0xE67E22),
    Verdict.NEZNAMY_MODEL: ("❓ LEVNÝ, MODEL NENÍ V TABULCE", 0x3498DB),
}


def format_czk(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " Kč"


def relative_age(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    minutes = max(int((now - moment).total_seconds() // 60), 0)
    if minutes < 60:
        return f"před {minutes} min"
    if minutes < 48 * 60:
        return f"před {minutes // 60} h {minutes % 60} min" if minutes < 6 * 60 else f"před {minutes // 60} h"
    return f"před {minutes // 1440} dny"


def suggest_offer(price_czk: int, assessment: Assessment) -> tuple[int, int] | None:
    """(nabídka, strop). Strop je nejvyšší cena, za kterou kus po cestě ještě vychází jako KOUPIT.

    Nabídka je o 10 % níž, zaokrouhlená dolů na 500 Kč, nikdy nad strop. None, když není co nabídnout.
    """
    if assessment.target_czk is None:
        return None
    ceiling = assessment.target_czk - (assessment.travel_cost_czk or 0)
    offer = min(int(price_czk * 0.9) // 500 * 500, ceiling)
    return (offer, ceiling) if 0 < offer < price_czk else None


def seller_message(specs: Specs, offer: int | None) -> str:
    text = (f"Dobrý den, mám zájem o {specs.model_label()}. Je ještě k dispozici? "
            "Pošlete prosím sériové číslo, počet cyklů baterie (Informace o systému → Napájení) "
            "a potvrďte, že je odhlášený z iCloudu a Najít.")
    if offer:
        text += f" Nabízím {format_czk(offer)} při osobním předání."
    return text


def build_embed(*, source: str, url: str, price_czk: int, specs: Specs,
                assessment: Assessment, now: datetime | None = None, hot: bool = False) -> dict[str, Any]:
    heading, color = _HEADINGS.get(assessment.verdict, (assessment.verdict, 0x95A5A6))
    if hot:
        heading = "🔥 " + heading
    offer = suggest_offer(price_czk, assessment)
    parts = [f"Cena: **{format_czk(price_czk)}**"]
    if assessment.adjusted_reference_czk is not None:
        parts.append(f"Reference: {format_czk(assessment.adjusted_reference_czk)}")
    if assessment.est_profit_czk is not None and assessment.roi is not None:
        parts.append(f"Odhad zisku: **+{format_czk(assessment.est_profit_czk)}** "
                     f"(ROI {assessment.roi * 100:.0f} %)")
    lines = [" · ".join(parts)]
    if offer:
        lines.append(f"Nabídni **{format_czk(offer[0])}**, nejvýš {format_czk(offer[1])}")
    if specs.red_flags:
        lines.append("Riziko: " + ", ".join(specs.red_flags).replace("_", " "))
    if assessment.distance_km is not None:
        place = f" ({assessment.locality_label})" if assessment.locality_label else ""
        lines.append(f"Vzdálenost: {assessment.distance_km:.0f} km{place} · "
                     f"cesta {format_czk(assessment.travel_cost_czk or 0)}")
    if assessment.fresh_since is not None:
        event = "Zlevněno" if assessment.price_dropped else "Vloženo"
        lines.append(f"{event} {relative_age(assessment.fresh_since, now)}")
    origin = assessment.reference_origin or ""
    if origin.startswith("measured"):
        lines.append(f"Reference z {assessment.reference_samples} prodaných kusů")
    elif origin == "table":
        lines.append("Reference z cenové tabulky")
    if ":" in origin:
        other = origin.split(":", 1)[1]
        lines.append(f"Reference převzatá z {_SOURCE_GENITIVE.get(other, other)}")
    return {
        "title": f"{heading}: {specs.model_label()}"[:256],
        "url": url,
        "description": "\n".join(lines),
        "color": color,
        # blok kódu jde v Discordu na mobilu zkopírovat jedním ťuknutím
        "fields": [{"name": "Zpráva prodejci",
                    "value": f"```{seller_message(specs, offer[0] if offer else None)}```"[:1024]}],
        "footer": {"text": SOURCE_NAMES.get(source, source)},
    }


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, opener: Opener = _default_opener,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 15.0,
                 max_attempts: int = 3) -> None:
        self.webhook_url = webhook_url
        self._opener = opener
        self._sleep = sleep
        self._timeout = timeout
        self._max_attempts = max_attempts

    def send(self, embed: dict[str, Any], content: str | None = None) -> bool:
        message: dict[str, Any] = {"embeds": [embed]}
        if content:
            message |= {"content": content, "allowed_mentions": {"parse": ["everyone"]}}
        payload = json.dumps(message).encode("utf-8")
        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(
                self.webhook_url, data=payload, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "macwatch"})
            try:
                with self._opener(request, self._timeout):
                    return True
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self._max_attempts:
                    retry_after = float(exc.headers.get("Retry-After", "2") or 2)
                    log.warning("Discord omezuje rychlost, čekám %.1f s", retry_after)
                    self._sleep(retry_after)
                    continue
                log.error("Discord odmítl zprávu: HTTP %s", exc.code)
                return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self._max_attempts:
                    self._sleep(2 * attempt)
                    continue
                log.error("Discord nedostupný: %s", exc)
                return False
        return False
