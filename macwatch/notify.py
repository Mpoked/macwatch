"""Upozornění přes Discord webhook."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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


def build_embed(*, source: str, url: str, price_czk: int, specs: Specs,
                assessment: Assessment) -> dict[str, Any]:
    heading, color = _HEADINGS.get(assessment.verdict, (assessment.verdict, 0x95A5A6))
    parts = [f"Cena: **{format_czk(price_czk)}**"]
    if assessment.adjusted_reference_czk is not None:
        parts.append(f"Reference: {format_czk(assessment.adjusted_reference_czk)}")
    if assessment.est_profit_czk is not None and assessment.roi is not None:
        parts.append(f"Odhad zisku: **+{format_czk(assessment.est_profit_czk)}** "
                     f"(ROI {assessment.roi * 100:.0f} %)")
    lines = [" · ".join(parts)]
    if specs.red_flags:
        lines.append("Riziko: " + ", ".join(specs.red_flags).replace("_", " "))
    if assessment.distance_km is not None:
        place = f" ({assessment.locality_label})" if assessment.locality_label else ""
        lines.append(f"Vzdálenost: {assessment.distance_km:.0f} km{place} · "
                     f"cesta {format_czk(assessment.travel_cost_czk or 0)}")
    origin = assessment.reference_origin or ""
    if origin.startswith("measured"):
        lines.append(f"Reference z {assessment.reference_samples} prodaných kusů")
    if ":" in origin:
        other = origin.split(":", 1)[1]
        lines.append(f"Reference převzatá z {_SOURCE_GENITIVE.get(other, other)}")
    return {
        "title": f"{heading}: {specs.model_label()}"[:256],
        "url": url,
        "description": "\n".join(lines),
        "color": color,
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

    def send(self, embed: dict[str, Any]) -> bool:
        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
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
