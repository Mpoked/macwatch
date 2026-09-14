"""Jediný HTTP klient pro zdroje: jeden User-Agent, rozestup mezi požadavky, opakování."""

from __future__ import annotations

import gzip
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.parse import urlsplit

from macwatch.config import HttpConfig

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int
    text: str


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def _decode(raw: bytes, headers: Message) -> str:
    if headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    charset = headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _base_domain(url: str) -> str:
    """pc.bazos.cz i www.bazos.cz sdílí jeden rozestup, jde o stejný server."""
    host = urlsplit(url).hostname or ""
    if host.replace(".", "").isdigit() or ":" in host or "." not in host:
        return host  # IP adresa nebo localhost
    return ".".join(host.split(".")[-2:])


class PoliteClient:
    """Stahuje po jednom. Mezi požadavky na stejnou doménu drží minimální rozestup."""

    def __init__(self, cfg: HttpConfig, *, opener: Opener = _default_opener,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def _wait_turn(self, url: str) -> None:
        domain = _base_domain(url)
        last = self._last.get(domain)
        if last is not None:
            remaining = self.cfg.min_delay_seconds - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[domain] = self._clock()

    def get(self, url: str) -> Response:
        """Vrátí odpověď. Stavy 404 a 410 vrací normálně, zdroj je rozliší sám."""
        attempts = self.cfg.max_retries + 1
        for attempt in range(1, attempts + 1):
            self._wait_turn(url)
            request = urllib.request.Request(url, headers={
                "User-Agent": self.cfg.user_agent,
                "Accept-Encoding": "gzip",
                "Accept-Language": "cs,en;q=0.5",
            })
            try:
                with self._opener(request, self.cfg.timeout_seconds) as resp:
                    return Response(resp.geturl(), resp.status, _decode(resp.read(), resp.headers))
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    body = _decode(exc.read(), exc.headers) if exc.fp else ""
                    return Response(url, exc.code, body)
                if exc.code in RETRY_STATUSES and attempt < attempts:
                    self._backoff(url, attempt, f"HTTP {exc.code}")
                    continue
                raise FetchError(f"{url}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < attempts:
                    self._backoff(url, attempt, str(exc))
                    continue
                raise FetchError(f"{url}: {exc}") from exc
        raise FetchError(f"{url}: vyčerpány pokusy")  # sem se běh nedostane

    def _backoff(self, url: str, attempt: int, reason: str) -> None:
        delay = min(300.0, self.cfg.min_delay_seconds * (3 ** attempt))
        log.warning("%s selhalo (%s), zkusím znovu za %.0f s", url, reason, delay)
        self._sleep(delay)
