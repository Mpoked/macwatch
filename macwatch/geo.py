"""Lokalita offline: PSČ a názvy obcí na souřadnice, vzdálenost a cena cesty.

Data: GeoNames postal codes CZ (CC BY 4.0), v macwatch/data/cz_postal.tsv.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from macwatch.config import TravelSettings
from macwatch.parsing import normalize


@dataclass(frozen=True, slots=True)
class Place:
    postal_code: str
    name: str
    lat: float
    lon: float
    exact: bool = True
    district: str = ""


@dataclass(frozen=True, slots=True)
class Travel:
    place: Place
    distance_km: float     # po silnici, jedna cesta
    cost_czk: int          # celá cesta podle nastavení (tam i zpět)
    within_limit: bool


class Gazetteer:
    def __init__(self, rows: list[Place]) -> None:
        self.by_psc: dict[str, list[Place]] = {}
        self.by_name: dict[str, list[Place]] = {}
        for place in rows:
            self.by_psc.setdefault(place.postal_code, []).append(place)
            self.by_name.setdefault(normalize(place.name), []).append(place)
        self._codes = sorted(self.by_psc)
        self._names = sorted(self.by_name)

    @classmethod
    def from_text(cls, text: str) -> Gazetteer:
        rows = []
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            psc, name, district, lat, lon = line.split("\t")
            rows.append(Place(psc, name, float(lat), float(lon), True, district))
        return cls(rows)

    def _center(self, psc: str, exact: bool) -> Place:
        places = self.by_psc[psc]
        # u PSČ s více obcemi vezmi střed, jména spoj zkráceně
        lat = sum(p.lat for p in places) / len(places)
        lon = sum(p.lon for p in places) / len(places)
        district = places[0].district
        name = places[0].name if len(places) == 1 else f"okres {district}"
        return Place(psc, name, lat, lon, exact, district)

    def by_postal_code(self, psc: str | None) -> Place | None:
        """Přesný kód, jinak číselně nejbližší existující kód (česká PSČ jsou řazená územně)."""
        digits = re.sub(r"\D", "", psc or "")
        if len(digits) != 5:
            return None
        if digits in self.by_psc:
            return self._center(digits, True)
        target = int(digits)
        nearest = min(self._codes, key=lambda code: abs(int(code) - target))
        if abs(int(nearest) - target) > 1000:
            return None
        return self._center(nearest, False)

    def by_place_name(self, name: str | None) -> Place | None:
        """Obec podle názvu, např. "Ostrava" nebo "Praha 7". Bez diakritiky i s ní.

        Velká města jsou v datech jen po částech ("Ostrava-Zábřeh"), proto se bere
        i předpona názvu a výsledkem je střed všech částí.
        """
        if not name:
            return None
        key = normalize(re.sub(r",.*$", "", name)).strip()
        if not key:
            return None
        candidates = self.by_name.get(key) or self._by_prefix(key)
        if not candidates and " " in key:
            first = key.split(" ")[0]
            candidates = self.by_name.get(first) or self._by_prefix(first)
        if not candidates:
            return None
        lat = sum(p.lat for p in candidates) / len(candidates)
        lon = sum(p.lon for p in candidates) / len(candidates)
        return Place(candidates[0].postal_code, name.split(",")[0].strip(), lat, lon, True)

    def _by_prefix(self, key: str) -> list[Place]:
        start = bisect.bisect_left(self._names, key)
        found: list[Place] = []
        for candidate in self._names[start:]:
            if not candidate.startswith(key):
                break
            if candidate[len(key):len(key) + 1] in ("-", " "):
                found.extend(self.by_name[candidate])
        return found

    def locate(self, postal_code: str | None, locality: str | None) -> Place | None:
        return self.by_postal_code(postal_code) or self.by_place_name(locality)


@lru_cache(maxsize=1)
def default_gazetteer() -> Gazetteer:
    text = resources.files("macwatch.data").joinpath("cz_postal.tsv").read_text("utf-8")
    return Gazetteer.from_text(text)


def haversine_km(a: Place, b: Place) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlambda = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimate_travel(settings: TravelSettings, home: Place | None, target: Place | None) -> Travel | None:
    if not settings.enabled or home is None or target is None:
        return None
    km = haversine_km(home, target) * settings.road_factor
    trips = 2 if settings.round_trip else 1
    cost = round(km * trips * settings.czk_per_km)
    within = settings.max_km is None or km <= settings.max_km
    return Travel(target, round(km, 1), cost, within)
