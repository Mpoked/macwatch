"""Zdroj Facebook Marketplace: čte RSS z upraveného fb-mp-rss (viz ~/Projects/fb-mp-rss).

Feed nese jen titulek, cenu, původní cenu a město. Popis inzerátu v něm není,
takže posouzení stojí jen na titulku a detail se nestahuje.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from macwatch.config import FacebookConfig
from macwatch.models import RawListing
from macwatch.sources.http import FetchError, PoliteClient

log = logging.getLogger(__name__)

SOURCE = "facebook"

_ITEM_ID = re.compile(r"/marketplace/item/(\d+)")
_COUNTRY_SUFFIX = re.compile(r",\s*(?:Czech Republic|Česko|Czechia)\s*$", re.I)
_SESSION = re.compile(r"session:\s*(\w+)")


def parse_description(text: str) -> dict[str, str]:
    """Popis položky má řádky ``klíč: hodnota``."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return fields


def parse_session(xml_text: str) -> str | None:
    """Stav přihlášení scraperu z popisu kanálu: anonymous, ok, expired. Starší scraper nic nevrací."""
    description = ET.fromstring(xml_text).findtext("channel/description") or ""
    match = _SESSION.search(description)
    return match.group(1) if match else None


def parse_feed(xml_text: str, fetched_at: datetime) -> list[RawListing]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        match = _ITEM_ID.search(link)
        if not match:
            continue
        fields = parse_description(node.findtext("description") or "")
        locality = fields.get("location")
        if locality:
            locality = _COUNTRY_SUFFIX.sub("", locality)
        published = None
        if listed := fields.get("listed_at"):
            try:
                published = datetime.fromisoformat(listed)  # skutečný čas vložení na Facebooku
            except ValueError:
                log.debug("nečitelný čas vložení %r u %s", listed, link)
        elif pub := node.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                log.debug("nečitelné datum %r u %s", pub, link)
        items.append(RawListing(
            source=SOURCE, source_id=match.group(1), url=link,
            title=(node.findtext("title") or "").strip(), price_raw=fields.get("price", ""),
            body=None, body_complete=False, fetched_at=fetched_at,
            locality=locality, published_at=published,
            sold=fields.get("is_sold") == "true", reserved=fields.get("is_pending") == "true",
            image_url=fields.get("image") or None))
    return items


class FacebookSource:
    name = SOURCE
    supports_enrich = False

    def __init__(self, cfg: FacebookConfig, client: PoliteClient,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.cfg = cfg
        self.client = client
        self._now = clock
        self.session: str | None = None

    def health_notice(self) -> str | None:
        """Problém, o kterém má vědět uživatel. Pipeline ho pošle na Discord jednou."""
        if self.session == "expired":
            return ("Přihlášení do Facebooku vypršelo, Marketplace se stahuje bez účtu a ukazuje "
                    "jen část inzerátů. Vyexportuj z prohlížeče nové cookies do "
                    "~/Projects/fb-mp-rss/data/cookies.json.")
        return None

    def poll(self) -> Iterator[RawListing]:
        try:
            response = self.client.get(self.cfg.feed_url)
        except FetchError as exc:
            raise FetchError(f"fb-mp-rss neodpovídá, běží kontejner? {exc}") from exc
        if response.status != 200:
            raise FetchError(f"fb-mp-rss vrátil HTTP {response.status}")
        items = parse_feed(response.text, self._now())
        self.session = parse_session(response.text)
        log.debug("facebook feed: %d položek", len(items))
        yield from items

    def enrich(self, item: RawListing) -> RawListing:
        return item

