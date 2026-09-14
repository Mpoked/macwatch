"""Společné rozhraní zdrojů. Zdroj jen sbírá, nic nevyhodnocuje ani neukládá."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from macwatch.models import RawListing


class ListingGone(Exception):
    """Inzerát už na zdroji neexistuje."""


class Source(Protocol):
    name: str
    #: umí zdroj stáhnout plný text? Pokud ano, alert čeká na detail.
    supports_enrich: bool

    def poll(self) -> Iterator[RawListing]:
        """Levný průchod feedem. Vrací kandidáty se zkráceným textem."""
        ...

    def enrich(self, item: RawListing) -> RawListing:
        """Drahé doplnění plného textu. Když inzerát zmizel, vyhodí ListingGone."""
        ...
