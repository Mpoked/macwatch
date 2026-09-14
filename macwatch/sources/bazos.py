"""Zdroj Bazoš: RSS výsledků hledání a stránka detailu inzerátu."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from macwatch.config import BazosConfig
from macwatch.models import RawListing
from macwatch.parsing import split_bazos_title
from macwatch.sources.base import ListingGone
from macwatch.sources.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "bazos"

_ID = re.compile(r"/inzerat/(\d+)/")
_TAG = re.compile(r"<[^>]+>")


def listing_id(url: str) -> str | None:
    match = _ID.search(url)
    return match.group(1) if match else None


def html_to_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _category(url: str) -> str | None:
    match = re.match(r"https?://([a-z]+)\.bazos\.cz/", url)
    return match.group(1) if match and match.group(1) != "www" else None


def parse_rss(xml_text: str, fetched_at: datetime) -> list[RawListing]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        source_id = listing_id(link)
        if not source_id:
            continue
        title, price_raw = split_bazos_title(node.findtext("title") or "")
        published = None
        if pub := node.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                log.debug("nečitelné datum %r u %s", pub, link)
        body = html_to_text(node.findtext("description") or "")
        body = re.sub(r"\s*\.\.\.\s*$", "", body)  # RSS text končí výpustkou
        items.append(RawListing(
            source=SOURCE, source_id=source_id, url=link, title=title, price_raw=price_raw,
            body=body, body_complete=False, fetched_at=fetched_at,
            category=_category(link), published_at=published))
    return items


@dataclass(frozen=True, slots=True)
class Detail:
    title: str
    body: str
    price_raw: str
    locality: str | None
    postal_code: str | None


_DETAIL_TITLE = re.compile(r"<h1 class=nadpisdetail>(.*?)</h1>", re.I | re.S)
_DETAIL_BODY = re.compile(r"<div class=popisdetail>(.*?)</div>", re.I | re.S)
_DETAIL_PRICE = re.compile(r"Cena:</td>.*?<b>(.*?)</b>", re.S)
_DETAIL_PLACE = re.compile(
    r'title="Přibližná lokalita"[^>]*>\s*([\d ]{5,6})\s*</a>\s*<a [^>]*>([^<]+)</a>')
_GONE_MARKERS = ("Inzerát je již vymazán", "Inzerát byl vymazán")


class DetailParseError(ValueError):
    pass


def is_gone_page(status: int, page: str) -> bool:
    return status in (404, 410) or any(marker in page for marker in _GONE_MARKERS)


def parse_detail(page: str) -> Detail:
    """Vytáhne jen potřebná pole. Jméno a telefon prodávajícího záměrně ignoruje."""
    title = _DETAIL_TITLE.search(page)
    body = _DETAIL_BODY.search(page)
    if not title or not body:
        raise DetailParseError("stránka nemá očekávanou strukturu detailu")
    price = _DETAIL_PRICE.search(page)
    place = _DETAIL_PLACE.search(page)
    return Detail(
        title=html_to_text(title.group(1)),
        body=html_to_text(body.group(1)),
        price_raw=html_to_text(price.group(1)) if price else "",
        locality=html.unescape(place.group(2)).strip() if place else None,
        postal_code=place.group(1).replace(" ", "") if place else None,
    )


@dataclass(frozen=True, slots=True)
class SearchHit:
    source_id: str
    url: str
    title: str
    price_raw: str
    locality: str | None
    postal_code: str | None


@dataclass(frozen=True, slots=True)
class SearchPage:
    total: int | None
    first: int | None
    last: int | None
    hits: list[SearchHit]


_SEARCH_COUNT = re.compile(r"Zobrazeno\s+([\d\s]+)-([\d\s]+)\s+inzerátů\s+z\s+([\d\s]+)")
_SEARCH_TITLE = re.compile(r'<h2 class=nadpis><a href="(/inzerat/(\d+)/[^"]*)">(.*?)</a>', re.S)
_SEARCH_PRICE = re.compile(r'class="inzeratycena"><b>(.*?)</b>', re.S)
_SEARCH_PLACE = re.compile(r'class="inzeratylok">([^<]*)<br>\s*([\d ]{5,6})')


def _int(text: str) -> int:
    return int(re.sub(r"\s", "", text))


def parse_search_page(page: str, base_url: str = "https://pc.bazos.cz") -> SearchPage:
    """Stránka výsledků hledání: počty a inzeráty s cenou a lokalitou."""
    count = _SEARCH_COUNT.search(page)
    hits = []
    seen = set()
    for block in page.split('<div class="inzeraty inzeratyflex">')[1:]:
        title = _SEARCH_TITLE.search(block)
        if not title or title.group(2) in seen:
            continue
        seen.add(title.group(2))
        price = _SEARCH_PRICE.search(block)
        place = _SEARCH_PLACE.search(block)
        hits.append(SearchHit(
            source_id=title.group(2),
            url=base_url.rstrip("/") + title.group(1),
            title=html_to_text(title.group(3)),
            price_raw=html_to_text(price.group(1)) if price else "",
            locality=html.unescape(place.group(1)).strip() if place else None,
            postal_code=place.group(2).replace(" ", "") if place else None))
    if count:
        return SearchPage(_int(count.group(3)), _int(count.group(1)), _int(count.group(2)), hits)
    return SearchPage(None, None, None, hits)


class BazosSource:
    name = SOURCE
    supports_enrich = True

    def __init__(self, cfg: BazosConfig, client: PoliteClient,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.cfg = cfg
        self.client = client
        self._now = clock

    def poll(self) -> Iterator[RawListing]:
        seen: set[str] = set()
        for feed in self.cfg.feeds:
            response = self.client.get(feed)
            if response.status != 200:
                log.warning("feed %s vrátil HTTP %s", feed, response.status)
                continue
            try:
                items = parse_rss(response.text, self._now())
            except ET.ParseError as exc:
                log.error("feed %s není platné RSS: %s", feed, exc)
                continue
            log.debug("feed %s: %d položek", feed, len(items))
            for item in items:
                if item.source_id not in seen:
                    seen.add(item.source_id)
                    yield item

    def search_page(self, offset: int) -> SearchPage:
        base = self.cfg.lifecycle_search_url
        separator = "&" if "?" in base else "?"
        url = re.sub(r"([?&])crp=\d*&?", r"\1", base).rstrip("&?")
        url = f"{url}{'&' if '?' in url else separator}crp={offset}"
        response = self.client.get(url)
        if response.status != 200:
            raise ValueError(f"stránka hledání {url} vrátila HTTP {response.status}")
        origin = re.match(r"https?://[^/]+", url)
        return parse_search_page(response.text, origin.group(0) if origin else "https://pc.bazos.cz")

    def check_detail(self, url: str) -> Detail | None:
        """Detail inzerátu, nebo None, když už neexistuje."""
        response = self.client.get(url)
        if is_gone_page(response.status, response.text):
            return None
        return parse_detail(response.text)

    def enrich(self, item: RawListing) -> RawListing:
        response = self.client.get(item.url)
        if is_gone_page(response.status, response.text):
            raise ListingGone(item.url)
        detail = parse_detail(response.text)
        return replace(
            item, title=detail.title, body=detail.body, body_complete=True,
            price_raw=detail.price_raw or item.price_raw, locality=detail.locality,
            postal_code=detail.postal_code, fetched_at=self._now())
