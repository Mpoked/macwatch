"""Zdroj Sbazar.cz: čte veřejné JSON API, které používá jejich web.

Hledání vrací celý seznam MacBooků v Notebookách (stovky kusů, 200 na stránku), takže
každý průchod vidí všechno. Co ve výpisu den chybí, zmizelo (viz `cli.Service._collect`).
Výpis nemá popis, ten se bere z detailu.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from macwatch.config import SbazarConfig
from macwatch.models import RawListing
from macwatch.sources.base import ListingGone
from macwatch.sources.http import FetchError, PoliteClient

log = logging.getLogger(__name__)

SOURCE = "sbazar"
API = "https://www.sbazar.cz/api/v1/items"
PAGE_SIZE = 200
PRAGUE = ZoneInfo("Europe/Prague")


def _published(value: str | None) -> datetime | None:
    """API dává místní čas bez zóny."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=PRAGUE).astimezone(timezone.utc) if value else None
    except ValueError:
        return None


def to_listing(row: dict[str, Any], fetched_at: datetime) -> RawListing:
    price = row.get("price")
    price_raw = "dohodou" if row.get("price_by_agreement") or not price else f"{price} Kč"
    return RawListing(
        source=SOURCE, source_id=str(row["id"]), url=f"https://www.sbazar.cz/inzerat/{row['seo_name']}",
        title=(row.get("name") or "").strip(), price_raw=price_raw, body=row.get("description"),
        body_complete="description" in row, fetched_at=fetched_at,
        locality=(row.get("locality") or {}).get("municipality") or None,
        published_at=_published(row.get("create_date")), reserved=bool(row.get("is_reserved")))


def parse_search(text: str, fetched_at: datetime) -> tuple[list[RawListing], int]:
    """(inzeráty, celkový počet ve výpisu)."""
    data = json.loads(text)
    return [to_listing(r, fetched_at) for r in data["results"]], int(data["pagination"]["total"])


class SbazarSource:
    name = SOURCE
    supports_enrich = True

    def __init__(self, cfg: SbazarConfig, client: PoliteClient,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.cfg = cfg
        self.client = client
        self._now = clock

    def poll(self) -> Iterator[RawListing]:
        offset, total = 0, 1
        while offset < total:
            response = self.client.get(f"{self.cfg.search_url}&limit={PAGE_SIZE}&offset={offset}")
            if response.status != 200:
                raise FetchError(f"sbazar hledání vrátilo HTTP {response.status}")
            items, total = parse_search(response.text, self._now())
            if not items:
                break
            yield from items
            offset += len(items)

    def enrich(self, item: RawListing) -> RawListing:
        response = self.client.get(f"{API}/{item.source_id}")
        if response.status in (404, 410):
            raise ListingGone(item.url)
        if response.status != 200:
            raise FetchError(f"sbazar detail {item.source_id}: HTTP {response.status}")
        row = json.loads(response.text)["result"]
        if row.get("status") != "active" or row.get("deactivation_reason"):
            raise ListingGone(item.url)
        return replace(to_listing(row, self._now()), body=row.get("description") or "", body_complete=True)
