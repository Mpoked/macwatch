"""Nastavení Facebook Marketplace: úpravy configu fb-mp-rss z dashboardu.

fb-mp-rss (fork v ~/Projects/fb-mp-rss) si config načte znovu před každou kontrolou,
takže změna se projeví bez restartu kontejneru. Zápis je atomický (dočasný soubor
a přejmenování), adresa a port serveru se nemění.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MIN_INTERVAL = 5
SORT_NEWEST = "creation_time_descend"
DAY_CHOICES = (("", "Bez omezení"), ("1", "Posledních 24 hodin"), ("7", "Posledních 7 dní"),
               ("30", "Posledních 30 dní"))


class FbConfigError(ValueError):
    pass


@dataclass(slots=True)
class FbSettings:
    urls: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    refresh_minutes: int = MIN_INTERVAL
    days_since_listed: str = "1"
    sort_newest: bool = True


def parse_search_url(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), dict(parse_qsl(parts.query))


def normalize_url(url: str, days_since_listed: str, sort_newest: bool) -> str:
    """Doplní nebo odebere řazení a stáří inzerátů. Ostatní parametry (query, radius...) nechá."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc.endswith("facebook.com") \
            or not parts.path.startswith("/marketplace/"):
        raise FbConfigError(f"{url!r} není adresa hledání na Facebook Marketplace")
    base, params = parse_search_url(url)
    if days_since_listed:
        params["daysSinceListed"] = days_since_listed
    else:
        params.pop("daysSinceListed", None)
    if sort_newest:
        params["sortBy"] = SORT_NEWEST
    elif params.get("sortBy") == SORT_NEWEST:
        params.pop("sortBy")
    return base + ("?" + urlencode(params) if params else "")


def load(path: Path) -> tuple[dict, FbSettings]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FbConfigError(f"config fb-mp-rss {path} neexistuje") from exc
    except json.JSONDecodeError as exc:
        raise FbConfigError(f"config fb-mp-rss {path} není platný JSON: {exc}") from exc
    filters = data.get("url_filters") or {}
    urls = list(filters)
    keywords: list[str] = []
    for levels in filters.values():
        for word in (levels or {}).get("level1", []):
            if word not in keywords:
                keywords.append(word)
    first = parse_search_url(urls[0])[1] if urls else {}
    settings = FbSettings(
        urls=urls,
        keywords=keywords,
        refresh_minutes=int(data.get("refresh_interval_minutes", MIN_INTERVAL)),
        days_since_listed=first.get("daysSinceListed", ""),
        sort_newest=first.get("sortBy") == SORT_NEWEST if urls else True,
    )
    return data, settings


def save(path: Path, settings: FbSettings) -> FbSettings:
    """Ověří a uloží. Vrací uložené hodnoty (s upravenými adresami)."""
    if settings.refresh_minutes < MIN_INTERVAL:
        raise FbConfigError(f"interval kontroly musí být aspoň {MIN_INTERVAL} minut")
    if settings.days_since_listed not in {value for value, _ in DAY_CHOICES}:
        raise FbConfigError("neplatné stáří inzerátů")
    urls: list[str] = []
    for url in settings.urls:
        normalized = normalize_url(url, settings.days_since_listed, settings.sort_newest)
        if normalized not in urls:
            urls.append(normalized)
    if not urls:
        raise FbConfigError("zadej aspoň jednu adresu hledání")
    keywords = [k.strip().lower() for k in settings.keywords if k.strip()]
    data, _ = load(path)
    filters = {url: ({"level1": keywords} if keywords else {}) for url in urls}
    data.update(refresh_interval_minutes=settings.refresh_minutes, url_filters=filters)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.chmod(tmp, 0o644)  # kontejner čte jako jiný uživatel
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return FbSettings(urls, keywords, settings.refresh_minutes, settings.days_since_listed, settings.sort_newest)
