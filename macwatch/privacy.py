"""Maskování kontaktů v textu inzerátu před uložením."""

from __future__ import annotations

import dataclasses
import re

from macwatch.models import RawListing

PHONE_PLACEHOLDER = "[telefon]"
EMAIL_PLACEHOLDER = "[e-mail]"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# české číslo: volitelná předvolba, pak 9 číslic ve skupinách po třech nebo vcelku
_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+|00)\s?420[\s.-]?)?[1-9]\d{2}[\s.-]?\d{3}[\s.-]?\d{3}(?![\w])")


def redact_text(text: str | None) -> str | None:
    if not text:
        return text
    text = _EMAIL.sub(EMAIL_PLACEHOLDER, text)
    return _PHONE.sub(PHONE_PLACEHOLDER, text)


def redact(item: RawListing) -> RawListing:
    return dataclasses.replace(item, title=redact_text(item.title) or item.title,
                               body=redact_text(item.body))
