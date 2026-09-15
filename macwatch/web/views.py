"""Stránky dashboardu."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from macwatch import fbconfig, pricemodel
from macwatch.config import AppConfig, ConfigError
from macwatch.db import Database
from macwatch.geo import default_gazetteer
from macwatch.inventory import COST_KINDS, Inventory, economics
from macwatch.market import market_overview, price_timeline, reference_book
from macwatch.models import Specs
from macwatch.settings import (
    FIELDS,
    FIELDS_BY_PATH,
    SettingsStore,
    coerce,
    effective_value,
    load_effective_config,
    validate_and_save,
)
from macwatch.web.html import (
    SOURCE_LABELS,
    cards,
    checkbox_field,
    czk,
    e,
    input_field,
    layout,
    line_chart,
    num,
    pct,
    safe_url,
    select_field,
    signed_class,
    source_label,
    table,
    url_with,
    verdict_badge,
)
from macwatch.web.server import Request, Response, html_response, redirect

INTERESTING = ("KOUPIT", "KOUPIT_S_RIZIKEM", "NEZNAMY_MODEL", "SLEDOVAT")
SSD_OPTIONS = [("", "?"), ("128", "128 GB"), ("256", "256 GB"), ("512", "512 GB"), ("1024", "1 TB"),
               ("2048", "2 TB"), ("4096", "4 TB"), ("8192", "8 TB")]
FAMILY_OPTIONS = [("", "?"), ("air", "Air"), ("pro", "Pro")]


@dataclass(slots=True)
class Ctx:
    app: Any
    db: Database
    req: Request

    @property
    def now(self) -> datetime:
        return self.app.clock()

    @property
    def cfg(self) -> AppConfig:
        return load_effective_config(self.app.config_path, self.db, self.now)

    def page(self, title: str, active: str, body: str) -> Response:
        return html_response(layout(title, active, body, self.req.arg("msg") or None,
                                    self.req.arg("kind", "good")))

    def back(self, default: str) -> str:
        target = self.req.form.get("next") or default
        return target if target.startswith("/") and not target.startswith("//") else default


# pomocné

def age(value: str | None, now: datetime) -> str:
    if not value:
        return "·"
    delta = now - datetime.fromisoformat(value)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"před {max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"před {minutes // 60} h"
    return f"před {minutes // 1440} dny"


def when(value: str | None) -> str:
    """ISO čas v UTC na místní čas, např. 14. 9. 15:40."""
    if not value:
        return "·"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return moment.strftime("%-d. %-m. %Y")
    return moment.astimezone().strftime("%-d. %-m. %H:%M")


def specs_from_row(row: sqlite3.Row) -> Specs:
    return Specs(family=row["family"], chip=row["chip"], ram_gb=row["ram_gb"], ssd_gb=row["ssd_gb"],
                 eligible=bool(row["eligible"]))


def origin_label(origin: str | None, samples: int | None) -> str:
    if not origin:
        return ""
    base, _, other = origin.partition(":")
    text = {"measured": f"z {samples} prodejů", "table": "tabulka z inzerátů", "manual": "ruční, zamčeno",
            "config": "starší ruční"}.get(base, base)
    if other:
        text += f", {SOURCE_LABELS.get(other, other)}"
    return text


def star_form(source: str, source_id: str, starred: bool, next_url: str) -> str:
    return (f'<form class=inline method=post action="/shortlist/prepnout">'
            f'<input type=hidden name=source value="{e(source)}"><input type=hidden name=source_id value="{e(source_id)}">'
            f'<input type=hidden name=next value="{e(next_url)}">'
            f'<button class="star{" on" if starred else ""}" title="Shortlist">{"★" if starred else "☆"}</button></form>')


def post_button(action: str, label: str, hidden: dict[str, object], cls: str = "ghost",
                confirm: str | None = None) -> str:
    fields = "".join(f'<input type=hidden name="{e(k)}" value="{e(v)}">' for k, v in hidden.items())
    onsubmit = f' onsubmit="return confirm(\'{e(confirm)}\')"' if confirm else ""
    return f'<form class=inline method=post action="{e(action)}"{onsubmit}>{fields}<button class="{cls}">{e(label)}</button></form>'


LISTING_SQL = """
SELECT l.source, l.source_id, l.url, l.title, l.price_czk, l.price_raw, l.locality, l.first_seen,
       l.last_seen, l.gone_at, l.alerted_at, l.body, l.postal_code, l.gone_reason, l.status,
       COALESCE(l.published_at, l.first_seen) AS listed_at,
       a.eligible, a.family, a.chip, a.ram_gb, a.ssd_gb, a.year, a.battery_pct, a.cycles, a.is_dealer,
       a.red_flags, a.reference_czk, a.reference_origin, a.reference_samples,
       a.adjusted_reference_czk, a.target_czk, a.est_profit_czk, a.roi, a.score, a.verdict,
       a.distance_km, a.travel_cost_czk,
       (SELECT price_czk FROM price_history ph WHERE ph.source = l.source AND ph.source_id = l.source_id
        ORDER BY id LIMIT 1) AS first_price,
       sl.note AS shortlist_note, sl.added_at AS shortlisted_at
FROM listings l
JOIN assessments a ON a.source = l.source AND a.source_id = l.source_id
LEFT JOIN shortlist sl ON sl.source = l.source AND sl.source_id = l.source_id
"""


def listing_rows(rows: list[sqlite3.Row], now: datetime, next_url: str) -> list[list[str]]:
    out = []
    for r in rows:
        dropped = r["first_price"] and r["price_czk"] and r["price_czk"] < r["first_price"]
        price = czk(r["price_czk"]) if r["price_czk"] is not None else e(r["price_raw"] or "·")
        if dropped:
            price += f'<br><span class="badge warn">zlevněno z {e(czk(r["first_price"]))}</span>'
        flags = json.loads(r["red_flags"] or "[]")
        title = f'<a href="/inzerat/{e(r["source"])}/{e(r["source_id"])}">{e(r["title"])}</a>'
        extra = []
        if flags:
            extra.append(f'<span class="badge bad">{e(", ".join(flags).replace("_", " "))}</span>')
        if r["is_dealer"]:
            extra.append('<span class="badge muted">obchodník</span>')
        if r["gone_at"]:
            extra.append(f'<span class="badge muted">{e(r["gone_reason"] or "zmizel")}</span>')
        if r["status"] == "reserved":
            extra.append('<span class="badge warn">rezervováno</span>')
        distance = "·"
        if r["distance_km"] is not None:
            distance = f'{num(r["distance_km"])} km<br><span class=muted>{e(czk(r["travel_cost_czk"]))}</span>'
        out.append([
            star_form(r["source"], r["source_id"], r["shortlisted_at"] is not None, next_url),
            verdict_badge(r["verdict"]),
            f'<span class="{signed_class(r["roi"])}">{e(pct(r["roi"]))}</span>',
            e(specs_from_row(r).model_label()),
            title + ("<br>" + " ".join(extra) if extra else ""),
            price,
            (e(czk(r["adjusted_reference_czk"])) + f'<br><span class=muted>{e(origin_label(r["reference_origin"], r["reference_samples"]))}</span>'
             if r["adjusted_reference_czk"] else "·"),
            f'<span class="{signed_class(r["est_profit_czk"])}">{e(czk(r["est_profit_czk"], sign=True)) if r["est_profit_czk"] is not None else "·"}</span>',
            distance,
            e(r["locality"] or "·"),
            source_label(r["source"]),
            e(age(r["listed_at"], now)),
        ])
    return out


LISTING_HEADERS = [("", ""), ("Verdikt", ""), ("ROI", "num"), ("Model", ""), ("Inzerát", "wrap"),
                   ("Cena", "num"), ("Reference", "num"), ("Zisk", "num"), ("Dojezd", "num"),
                   ("Místo", ""), ("Zdroj", ""), ("Vloženo", "")]


def recompute(ctx: Ctx) -> int:
    """Hned přepočítá posouzení aktivních inzerátů podle nového nastavení.

    Upozornění se odtud neposílají, pošle je služba při svém dalším průchodu.
    """
    from macwatch.pipeline import Pipeline

    pipeline = Pipeline(ctx.cfg, ctx.db, None, clock=ctx.app.clock, gazetteer=default_gazetteer())
    pipeline.reassess_active({"bazos"})
    return ctx.db.conn.execute("SELECT COUNT(*) FROM listings WHERE gone_at IS NULL").fetchone()[0]


# nálezy

def findings(ctx: Ctx) -> Response:
    req = ctx.req
    show = req.arg("show", "vyhodne")
    source = req.arg("source")
    max_km = req.arg("km")
    text = req.arg("q").strip()
    where = ["l.gone_at IS NULL", "a.eligible = 1"]
    params: list[Any] = []
    if show == "vyhodne":
        where.append(f"a.verdict IN ({','.join('?' for _ in INTERESTING)})")
        params.extend(INTERESTING)
    if source in ("bazos", "facebook"):
        where.append("l.source = ?")
        params.append(source)
    if max_km:
        try:
            params.append(float(max_km.replace(",", ".")))
            where.append("(a.distance_km IS NULL OR a.distance_km <= ?)")
        except ValueError:
            pass
    if text:
        where.append("l.title LIKE ?")
        params.append(f"%{text}%")
    rows = ctx.db.conn.execute(
        LISTING_SQL + " WHERE " + " AND ".join(where) +
        " ORDER BY a.score IS NULL, a.score DESC, listed_at DESC LIMIT 300", params).fetchall()
    here = url_with("/", show=show, source=source, km=max_km, q=text)
    filters = (
        '<form class=filters method=get action="/">'
        + select_field("show", "Zobrazit", [("vyhodne", "Zajímavé"), ("vse", "Všechny MacBooky")], show)
        + select_field("source", "Zdroj", [("", "Všechny"), ("bazos", "Bazoš"), ("facebook", "Marketplace")], source)
        + input_field("km", "Max. km", max_km, "number", attrs='min=0 step=1 style="width:90px"')
        + input_field("q", "Hledat v titulku", text)
        + "<div><button>Filtrovat</button></div></form>")
    counts = ctx.db.conn.execute(
        """SELECT a.verdict, COUNT(*) AS n FROM listings l JOIN assessments a USING (source, source_id)
           WHERE l.gone_at IS NULL AND a.eligible = 1 GROUP BY a.verdict""").fetchall()
    by_verdict = {row["verdict"]: row["n"] for row in counts}
    summary = cards([
        ("Koupit", str(by_verdict.get("KOUPIT", 0)), "pos"),
        ("Koupit s rizikem", str(by_verdict.get("KOUPIT_S_RIZIKEM", 0)), ""),
        ("Model mimo tabulku", str(by_verdict.get("NEZNAMY_MODEL", 0)), ""),
        ("Aktivních MacBooků", str(sum(by_verdict.values())), ""),
    ])
    body = (f"<h1>Nálezy</h1>{summary}{filters}"
            + table(LISTING_HEADERS, listing_rows(rows, ctx.now, here), "Žádné nálezy pro tyto filtry.")
            + '<p class=muted>Seřazeno podle skóre (ROI po započtení cesty a srážek).</p>')
    return ctx.page("Nálezy", "/", body)


def listing_detail(ctx: Ctx, source: str, source_id: str) -> Response:
    row = ctx.db.conn.execute(LISTING_SQL + " WHERE l.source = ? AND l.source_id = ?",
                              (source, source_id)).fetchone()
    if row is None:
        return html_response(layout("Nenalezeno", "/", "<h1>Inzerát nenalezen</h1>"), 404)
    history = ctx.db.price_history((source, source_id))
    here = f"/inzerat/{source}/{source_id}"
    specs = specs_from_row(row)
    facts = [
        ("Verdikt", verdict_badge(row["verdict"]), ""),
        ("Cena", e(czk(row["price_czk"])) if row["price_czk"] is not None else e(row["price_raw"]), ""),
        ("Reference", e(czk(row["adjusted_reference_czk"])), ""),
        ("Odhad zisku", e(czk(row["est_profit_czk"], sign=True)) if row["est_profit_czk"] is not None else "·",
         signed_class(row["est_profit_czk"])),
        ("ROI", e(pct(row["roi"])), signed_class(row["roi"])),
        ("Dojezd", f'{e(num(row["distance_km"]))} km, {e(czk(row["travel_cost_czk"]))}'
         if row["distance_km"] is not None else "·", ""),
    ]
    spec_rows = [[e(k), e(v)] for k, v in (
        ("Model", specs.model_label()), ("Rok", row["year"] or "·"),
        ("Baterie", f'{row["battery_pct"]} %' if row["battery_pct"] else "·"),
        ("Cykly", row["cycles"] or "·"), ("Obchodník", "ano" if row["is_dealer"] else "ne"),
        ("Rizika", ", ".join(json.loads(row["red_flags"] or "[]")) or "žádná"),
        ("Místo", f'{row["locality"] or "·"} {row["postal_code"] or ""}'),
        ("Zdroj", SOURCE_LABELS.get(source, source)),
        ("Vloženo na zdroji", when(row["listed_at"]) if row["listed_at"] != row["first_seen"] else "neznámo"),
        ("Poprvé viděn", when(row["first_seen"])), ("Naposled viděn", when(row["last_seen"])),
        ("Zmizel", f'{when(row["gone_at"])} ({row["gone_reason"]})' if row["gone_at"] else "ne"),
        ("Upozornění odesláno", when(row["alerted_at"]) if row["alerted_at"] else "ne"))]
    chart = line_chart({"cena": [(h["observed_at"][:16], h["price_czk"])
                                 for h in history if h["price_czk"]]}) if len(history) > 1 else ""
    shortlist = ""
    if row["shortlisted_at"]:
        shortlist = (f'<form method=post action="/shortlist/poznamka" class=panel>'
                     f'<input type=hidden name=source value="{e(source)}"><input type=hidden name=source_id value="{e(source_id)}">'
                     f'<input type=hidden name=next value="{e(here)}"><label>Poznámka ve shortlistu</label>'
                     f'<textarea name=note>{e(row["shortlist_note"] or "")}</textarea><p><button>Uložit poznámku</button></p></form>')
    body = (f'<h1>{star_form(source, source_id, row["shortlisted_at"] is not None, here)} {e(row["title"])}</h1>'
            f'<p><a class=btn href="{safe_url(row["url"])}" target=_blank rel="noopener noreferrer">Otevřít inzerát</a> '
            f'<a class=btn href="{e(url_with("/sklad/novy", source=source, source_id=source_id))}">Koupil jsem, přidat do skladu</a></p>'
            + cards(facts) + shortlist
            + "<h2>Konfigurace</h2>" + table([("Údaj", ""), ("Hodnota", "wrap")], spec_rows)
            + "<h2>Historie ceny</h2>" + chart
            + table([("Kdy", ""), ("Cena", "num"), ("Text", "")],
                    [[e(when(h["observed_at"])), e(czk(h["price_czk"])), e(h["price_raw"])] for h in history])
            + (f'<h2>Text inzerátu</h2><div class=panel><pre class=body>{e(row["body"])}</pre></div>' if row["body"] else ""))
    return ctx.page(row["title"], "/", body)


# shortlist

def shortlist_page(ctx: Ctx) -> Response:
    rows = ctx.db.conn.execute(LISTING_SQL + " WHERE sl.added_at IS NOT NULL ORDER BY sl.added_at DESC").fetchall()
    table_rows = listing_rows(rows, ctx.now, "/shortlist")
    for row, cells in zip(rows, table_rows):
        if row["shortlist_note"]:
            cells[4] += f'<br><span class=muted>{e(row["shortlist_note"])}</span>'
    body = ("<h1>Shortlist</h1><p class=muted>Kusy, které sleduješ. Hvězdičkou je přidáš nebo odebereš, "
            "zůstanou tu i po zmizení inzerátu.</p>" + table(LISTING_HEADERS, table_rows, "Shortlist je prázdný."))
    return ctx.page("Shortlist", "/shortlist", body)


def shortlist_toggle(ctx: Ctx) -> Response:
    key = (ctx.req.form.get("source", ""), ctx.req.form.get("source_id", ""))
    conn = ctx.db.conn
    with conn:
        if conn.execute("SELECT 1 FROM shortlist WHERE source = ? AND source_id = ?", key).fetchone():
            conn.execute("DELETE FROM shortlist WHERE source = ? AND source_id = ?", key)
            msg = "Odebráno ze shortlistu."
        elif ctx.db.get_listing(key):
            conn.execute("INSERT INTO shortlist (source, source_id, added_at) VALUES (?, ?, ?)",
                         (*key, ctx.now.isoformat(timespec="seconds")))
            msg = "Přidáno do shortlistu."
        else:
            return redirect(ctx.back("/"), "Inzerát neexistuje.", "bad")
    return redirect(ctx.back("/"), msg)


def shortlist_note(ctx: Ctx) -> Response:
    form = ctx.req.form
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE shortlist SET note = ? WHERE source = ? AND source_id = ?",
                            (form.get("note", "").strip() or None, form.get("source"), form.get("source_id")))
    return redirect(ctx.back("/shortlist"), "Poznámka uložena.")


# trh

def market_page(ctx: Ctx) -> Response:
    cfg = ctx.cfg
    stats = market_overview(ctx.db.conn, cfg.market, ctx.now)
    rows = []
    for s in stats:
        family, chip, *ssd = s.group.split("|")
        link = url_with("/trh/skupina", family=family, chip=chip, ssd=ssd[0] if ssd else "")
        label = specs_label(family, chip, int(ssd[0]) if ssd else None)
        rows.append([f'<a href="{e(link)}">{e(label)}</a>', source_label(s.source), str(s.sold),
                     e(czk(s.median_price)), e(num(s.median_days, 1)), e(num(s.mean_days, 1)),
                     str(s.active), e(czk(s.median_asking))])
    by_group: dict[str, dict[str, Any]] = {}
    for s in stats:
        by_group.setdefault(s.group, {})[s.source] = s
    compare = []
    for group, per_source in sorted(by_group.items()):
        if len(per_source) < 2:
            continue
        b, f = per_source.get("bazos"), per_source.get("facebook")
        family, chip, *ssd = group.split("|")
        diff = None
        if b and f and b.median_asking and f.median_asking:
            diff = (f.median_asking - b.median_asking) / b.median_asking
        compare.append([e(specs_label(family, chip, int(ssd[0]) if ssd else None)),
                        e(czk(b.median_asking if b else None)), e(czk(f.median_asking if f else None)),
                        f'<span class="{signed_class(-(diff or 0))}">{e(pct(diff))}</span>',
                        e(czk(b.median_price if b else None)), e(num(b.median_days, 1) if b else "·")])
    headers = [("Skupina", ""), ("Zdroj", ""), ("Prodáno", "num"), ("Medián prodejní ceny", "num"),
               ("Medián dní do prodeje", "num"), ("Průměr dní", "num"), ("Aktivních", "num"),
               ("Medián inzertní ceny", "num")]
    body = ("<h1>Trh</h1>"
            f'<p class=muted>Prodaný kus: inzerát zmizel do {cfg.market.sold_within_days} dní od prvního spatření. '
            f'Počítá se posledních {cfg.market.lookback_days} dní, bez obchodníků a kusů s rizikem. '
            "Zmizení sleduje kontrola životního cyklu jen na Bazoši, Facebook bez přihlášení zmizení nepozná.</p>"
            + table(headers, rows, "Zatím nejsou data. Počkej, až služba pár dní sbírá.")
            + "<h2>Bazoš proti Marketplace</h2>"
            + table([("Skupina", ""), ("Bazoš inzertní", "num"), ("Marketplace inzertní", "num"),
                     ("Rozdíl FB proti Bazoši", "num"), ("Bazoš prodejní", "num"), ("Bazoš dní", "num")],
                    compare, "Zatím žádná skupina není na obou zdrojích."))
    return ctx.page("Trh", "/trh", body)


def specs_label(family: str | None, chip: str | None, ssd: int | None) -> str:
    label = Specs(family=family, chip=chip, ssd_gb=ssd).model_label()
    return label.replace(" ?/", " ").replace("/?", "").rstrip()


def market_group(ctx: Ctx) -> Response:
    family, chip = ctx.req.arg("family"), ctx.req.arg("chip")
    ssd = int(ctx.req.arg("ssd")) if ctx.req.arg("ssd").isdigit() else None
    if not family or not chip:
        return redirect("/trh", "Chybí skupina.", "bad")
    series = price_timeline(ctx.db.conn, family, chip, ssd, ctx.now)
    chart = line_chart({SOURCE_LABELS.get(src, src): [(week, price) for week, price, _ in points]
                        for src, points in series.items()})
    params: list[Any] = [family, chip]
    ssd_clause = ""
    if ssd:
        ssd_clause = " AND a.ssd_gb = ?"
        params.append(ssd)
    rows = ctx.db.conn.execute(
        LISTING_SQL + " WHERE a.family = ? AND a.chip = ?" + ssd_clause +
        " ORDER BY l.gone_at IS NULL, l.gone_at DESC, l.first_seen DESC LIMIT 200", params).fetchall()
    here = url_with("/trh/skupina", family=family, chip=chip, ssd=ssd)
    body = (f"<h1>{e(specs_label(family, chip, ssd))}</h1><h2>Týdenní medián inzertní ceny</h2>{chart}"
            "<h2>Inzeráty ve skupině</h2>" + table(LISTING_HEADERS, listing_rows(rows, ctx.now, here)))
    return ctx.page("Trh", "/trh", body)


# ceny

RAM_OPTIONS = [(str(v), f"{v} GB") for v in (8, 16, 18, 24, 32, 36, 48, 64, 96, 128)]
SSD_BASE_OPTIONS = [(v, t) for v, t in SSD_OPTIONS if v]


def model_name(family: str, chip: str) -> str:
    return Specs(family=family, chip=chip).model_label().replace(" ?/?", "")


def model_sort_key(key: str) -> tuple:
    family, chip = key.split("|", 1)
    generation = chip.split()[0]
    variant = chip[len(generation):].strip()
    return (family != "air", generation, {"": 0, "pro": 1, "max": 2}.get(variant, 3))


def prices_page(ctx: Ctx) -> Response:
    cfg = ctx.cfg
    conn = ctx.db.conn
    book = reference_book(conn, cfg.market, cfg.pricing, ctx.now)
    fit_info = pricemodel.last_fit(conn)
    fresh = conn.execute(
        "SELECT COUNT(*) FROM asking_snapshots WHERE last_seen >= ? AND eligible = 1",
        ((ctx.now - timedelta(days=pricemodel.FIT_WINDOW_DAYS)).isoformat(timespec="seconds"),)).fetchone()[0]
    next_fit = "hned při dalším průchodu" if fit_info is None else when(
        (datetime.fromisoformat(fit_info["at"]) + timedelta(days=cfg.market.refit_interval_days)).isoformat())
    top = cards([
        ("Přirážka za 2× RAM", e(f"+{num(cfg.market.ram_pct_per_doubling, 1)} %"), ""),
        ("Přirážka za 2× SSD", e(f"+{num(cfg.market.ssd_pct_per_doubling, 1)} %"), ""),
        ("Poslední přepočet", e(when(fit_info["at"])) if fit_info else "zatím nebyl", ""),
        ("Inzerátů pro přepočet", str(fresh), ""),
    ])
    fit_note = ""
    if fit_info:
        fit_note = (f'<p class=muted>Poslední přepočet: {fit_info["models"]} modelů z {fit_info["samples"]} inzerátů, '
                    f'cena podle tabulky se od inzerátu liší v mediánu o {e(num(fit_info["median_error_pct"], 1))} %. '
                    f'Další přepočet: {e(next_fit)}.</p>')
    controls = (
        '<div class=panel><form method=post action="/ceny/prirazky"><div class=grid>'
        + input_field("ram_pct", "Přirážka za zdvojnásobení RAM (%)", cfg.market.ram_pct_per_doubling, "number",
                      attrs='step="0.1" min=0 max=100')
        + input_field("ssd_pct", "Přirážka za zdvojnásobení SSD (%)", cfg.market.ssd_pct_per_doubling, "number",
                      attrs='step="0.1" min=0 max=100')
        + checkbox_field("auto_percentages", "Přepočet upraví i přirážky", cfg.market.auto_percentages)
        + '</div><p><button>Uložit přirážky</button></p></form>'
        + post_button("/ceny/prepocitat", "Přepočítat tabulku z inzerátů teď", {}, "")
        + f'{fit_note}</div>')

    rows = []
    for key in sorted(book.models, key=model_sort_key):
        model = book.models[key]
        family, chip = model.family, model.chip
        used = book.lookup("bazos", family, chip, model.base_ssd_gb, model.base_ram_gb)
        measured = book.measured.get("bazos", {}).get(key)
        double_ram = book.lookup("bazos", family, chip, model.base_ssd_gb, model.base_ram_gb * 2)
        double_ssd = book.lookup("bazos", family, chip, model.base_ssd_gb * 2, model.base_ram_gb)
        edit = (f'<form class=inline method=post action="/ceny/model">'
                f'<input type=hidden name=family value="{e(family)}"><input type=hidden name=chip value="{e(chip)}">'
                + _select("base_ram_gb", RAM_OPTIONS, model.base_ram_gb) + " / "
                + _select("base_ssd_gb", SSD_BASE_OPTIONS, model.base_ssd_gb)
                + f' <input name=base_price_czk type=number min=1 step=100 value="{model.base_price_czk}" style="width:100px">'
                  ' <button class=ghost title="Uloží a zamkne, týdenní přepočet ho pak nepřepíše">Uložit</button></form>')
        origin = ('<span class="badge warn">zamčeno ručně</span>' if model.locked
                  else f'<span class=muted>z {model.samples} inzerátů, {e(when(model.updated_at))}</span>')
        actions = (post_button("/ceny/model/odemknout", "Odemknout", {"family": family, "chip": chip})
                   if model.locked else "") + " " + post_button(
            "/ceny/model/smazat", "Smazat", {"family": family, "chip": chip}, "ghost", "Smazat řádek tabulky?")
        rows.append([
            e(model_name(family, chip)), edit, origin,
            (e(czk(measured.price_czk)) + f' <span class=muted>({measured.samples})</span>') if measured else "·",
            (e(czk(used[0])) + f'<br><span class=muted>{e(origin_label(used[2], used[3]))}</span>') if used else "·",
            f'{e(czk(double_ram[0]))} <span class=muted>({model.base_ram_gb * 2} GB)</span>' if double_ram else "·",
            f'{e(czk(double_ssd[0]))} <span class=muted>({ssd_label(model.base_ssd_gb * 2)})</span>' if double_ssd else "·",
            actions,
        ])
    model_table = table(
        [("Model", ""), ("Základní konfigurace a cena", ""), ("Původ", ""), ("Naměřeno z prodejů", "num"),
         ("Používá se", "num"), ("2× RAM", "num"), ("2× SSD", "num"), ("", "")],
        rows, "Tabulka je prázdná. Spusť přepočet z inzerátů, nebo přidej model ručně.")

    add_form = (
        '<form method=post action="/ceny/model" class=panel><div class=grid>'
        + select_field("family", "Rodina", FAMILY_OPTIONS[1:], ctx.req.arg("family", "air"))
        + input_field("chip", "Čip", ctx.req.arg("chip"), help_text="např. m2, m3 pro, m4 max", required=True)
        + select_field("base_ram_gb", "Základní RAM", RAM_OPTIONS, ctx.req.arg("ram", "8"))
        + select_field("base_ssd_gb", "Základní SSD", SSD_BASE_OPTIONS, ctx.req.arg("ssd", "256"))
        + input_field("base_price_czk", "Cena základní konfigurace (Kč)", "", "number", required=True,
                      attrs="min=1 step=100")
        + "</div><p><button>Přidat nebo přepsat a zamknout</button></p></form>")

    missing = conn.execute(
        """SELECT a.family, a.chip, COUNT(*) AS n, CAST(AVG(l.price_czk) AS INTEGER) AS avg_price
           FROM listings l JOIN assessments a USING (source, source_id)
           WHERE a.verdict = 'NEZNAMY_MODEL' AND l.gone_at IS NULL AND a.chip IS NOT NULL
           GROUP BY a.family, a.chip ORDER BY n DESC LIMIT 30""").fetchall()
    missing_rows = [[e(model_name(r["family"], r["chip"])), str(r["n"]), e(czk(r["avg_price"])),
                     f'<a href="{e(url_with("/ceny", family=r["family"], chip=r["chip"]))}#pridat">přidat</a>']
                    for r in missing]

    manual = SettingsStore(ctx.db).references()
    manual_rows = [[
        e(key),
        (f'<form class=inline method=post action="/ceny/ulozit"><input type=hidden name=key value="{e(key)}">'
         f'<input name=price_czk type=number min=1 step=100 value="{price}" style="width:110px"> '
         f'<button class=ghost>Uložit</button></form>'),
        post_button("/ceny/smazat", "Smazat", {"key": key}, "ghost", "Smazat referenční cenu?"),
    ] for key, price in manual.items()]

    body = ("<h1>Ceny</h1>"
            "<p class=muted>Reference kusu = cena základní konfigurace modelu, upravená přirážkami za RAM a SSD. "
            "Odkud se bere: 1. řádek, který jsi uložil ručně (zamčený), 2. naměřené prodeje, když jich je aspoň "
            f"{cfg.market.min_samples}, 3. tabulka z inzertních cen, přepočítaná každých "
            f"{e(num(cfg.market.refit_interval_days))} dní, 4. starší ruční ceny s klíčem.</p>"
            + top + controls
            + "<h2>Cenová tabulka</h2>" + model_table
            + '<h2 id=pridat>Přidat model</h2>' + add_form
            + "<h2>Chybí v tabulce</h2>"
            + table([("Model", ""), ("Aktivních inzerátů", "num"), ("Průměrná cena", "num"), ("", "")], missing_rows,
                    "Všechny aktivní MacBooky mají referenci.")
            + "<h2>Starší ruční ceny</h2><p class=muted>Klíč zdroj|rodina|čip|ssd, platí jen tam, kde tabulka "
              "model nemá. Nové ceny zadávej raději do tabulky výš.</p>"
            + table([("Klíč", ""), ("Cena", ""), ("", "")], manual_rows, "Žádné.")
            + '<form method=post action="/ceny/ulozit" class=panel><div class=grid>'
            + input_field("key", "Klíč", "", help_text="např. bazos|pro|m2 pro|512", required=True)
            + input_field("price_czk", "Cena (Kč)", "", "number", required=True, attrs="min=1 step=100")
            + "</div><p><button class=ghost>Přidat starší ruční cenu</button></p></form>")
    return ctx.page("Ceny", "/ceny", body)


def _select(name: str, options: list[tuple[str, str]], selected: object) -> str:
    return (f'<select name="{e(name)}">' + "".join(
        f'<option value="{e(v)}"{" selected" if str(v) == str(selected) else ""}>{e(t)}</option>'
        for v, t in options) + "</select>")


def ssd_label(gb: int) -> str:
    return f"{gb // 1024} TB" if gb >= 1024 else f"{gb} GB"


def prices_model_save(ctx: Ctx) -> Response:
    form = ctx.req.form
    try:
        family = form.get("family", "")
        chip = " ".join(form.get("chip", "").lower().split())
        if family not in ("air", "pro") or not chip:
            raise ValueError("rodina musí být air nebo pro a čip nesmí být prázdný")
        model = pricemodel.PriceModel(family, chip, int(form.get("base_ram_gb", "")), int(form.get("base_ssd_gb", "")),
                                      int(form.get("base_price_czk", "").replace(" ", "")), locked=True)
        if model.base_price_czk <= 0:
            raise ValueError("cena musí být kladná")
        pricemodel.save_model(ctx.db.conn, model, ctx.now)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return redirect("/ceny", f"Neuloženo: {exc}", "bad")
    recompute(ctx)
    return redirect("/ceny", f"{model_name(family, chip)} uložen a zamčen, posouzení přepočítáno.")


def prices_model_unlock(ctx: Ctx) -> Response:
    pricemodel.set_locked(ctx.db.conn, ctx.req.form.get("family", ""), ctx.req.form.get("chip", ""), False)
    return redirect("/ceny", "Řádek odemčen, při dalším přepočtu ho nahradí cena z inzerátů.")


def prices_model_delete(ctx: Ctx) -> Response:
    pricemodel.delete_model(ctx.db.conn, ctx.req.form.get("family", ""), ctx.req.form.get("chip", ""))
    recompute(ctx)
    return redirect("/ceny", "Řádek tabulky smazán, posouzení přepočítáno.")


def prices_percentages_save(ctx: Ctx) -> Response:
    form = ctx.req.form
    try:
        values = {
            "market.ram_pct_per_doubling": coerce(FIELDS_BY_PATH["market.ram_pct_per_doubling"], form.get("ram_pct")),
            "market.ssd_pct_per_doubling": coerce(FIELDS_BY_PATH["market.ssd_pct_per_doubling"], form.get("ssd_pct")),
            "market.auto_percentages": bool(form.get("auto_percentages")),
        }
        validate_and_save(ctx.app.config_path, ctx.db, values, ctx.now)
    except ConfigError as exc:
        return redirect("/ceny", f"Neuloženo: {exc}", "bad")
    recompute(ctx)
    return redirect("/ceny", "Přirážky uloženy, posouzení přepočítáno.")


def prices_refit(ctx: Ctx) -> Response:
    result = pricemodel.refit(ctx.db.conn, ctx.now, update_percentages=ctx.cfg.market.auto_percentages)
    if result is None:
        return redirect("/ceny", "Na přepočet je málo inzerátů. Počkej na kontrolu životního cyklu, "
                                 "běží každé 3 hodiny.", "bad")
    recompute(ctx)
    return redirect("/ceny", f"Tabulka přepočítána z {result.samples} inzerátů, posouzení přepočítáno.")


def prices_save(ctx: Ctx) -> Response:
    try:
        price = int(ctx.req.form.get("price_czk", "").replace(" ", ""))
        SettingsStore(ctx.db).set_reference(ctx.req.form.get("key", ""), price, ctx.now)
    except (ValueError, ConfigError) as exc:
        return redirect("/ceny", f"Neuloženo: {exc}", "bad")
    recompute(ctx)
    return redirect("/ceny", "Referenční cena uložena a posouzení přepočítáno.")


def prices_delete(ctx: Ctx) -> Response:
    SettingsStore(ctx.db).delete_reference(ctx.req.form.get("key", ""))
    recompute(ctx)
    return redirect("/ceny", "Referenční cena smazána a posouzení přepočítáno.")


# sklad

def inventory_page(ctx: Ctx) -> Response:
    cfg = ctx.cfg
    inv = Inventory(ctx.db.conn)
    today = ctx.now.date()
    summary = inv.summary(cfg.travel, cfg.inventory.capital_cap_czk, today)
    cap = cfg.inventory.capital_cap_czk
    warning = ""
    if summary.over_cap:
        warning = (f'<div class="flash bad">Vázaný kapitál {e(czk(summary.capital_tied_czk))} '
                   f'překračuje strop {e(czk(cap))}. Než koupíš další kus, něco prodej.</div>')
    top = cards([
        ("Vázaný kapitál", e(czk(summary.capital_tied_czk)) + (f' <span class=muted>/ {e(czk(cap))}</span>' if cap else ""),
         "neg" if summary.over_cap else ""),
        ("Skladem", str(summary.in_stock), ""),
        ("Prodáno", str(summary.sold), ""),
        ("Čistý zisk", e(czk(summary.net_profit_czk, sign=True)), signed_class(summary.net_profit_czk)),
        ("Celkové ROI", e(pct(summary.total_roi, 1)), signed_class(summary.total_roi)),
        ("Průměrně dní do prodeje", e(num(summary.avg_days_to_sell, 1)), ""),
    ])
    rows = []
    for item in inv.items():
        econ = economics(item, item["costs_czk"], cfg.travel, today)
        status = {"prodano": ("prodáno", "good"), "vystaveno": ("vystaveno", "info"),
                  "skladem": ("skladem", "muted")}[econ.status]
        rows.append([
            f'<a href="/sklad/{item["id"]}">{e(item["title"])}</a>',
            f'<span class="badge {status[1]}">{status[0]}</span>',
            e(item["bought_at"]), e(czk(item["buy_price_czk"])), e(czk(econ.costs_czk + econ.travel_czk)),
            e(czk(item["sale_price_czk"])),
            f'<span class="{signed_class(econ.gross_margin_czk)}">{e(czk(econ.gross_margin_czk, sign=True)) if econ.gross_margin_czk is not None else "·"}</span>',
            f'<span class="{signed_class(econ.net_margin_czk)}">{e(czk(econ.net_margin_czk, sign=True)) if econ.net_margin_czk is not None else "·"}</span>',
            str(econ.days_held),
            f'<span class="{signed_class(econ.roi)}">{e(pct(econ.roi, 1))}</span>',
            f'<span class="{signed_class(econ.annualized_roi)}">{e(pct(econ.annualized_roi))}</span>',
            e(czk(econ.capital_tied_czk)) if econ.capital_tied_czk else "·",
        ])
    headers = [("Kus", "wrap"), ("Stav", ""), ("Koupeno", ""), ("Nákup", "num"), ("Náklady a cesta", "num"),
               ("Prodej", "num"), ("Hrubá marže", "num"), ("Čistá marže", "num"), ("Dní", "num"),
               ("ROI", "num"), ("ROI ročně", "num"), ("Vázáno", "num")]
    body = (f'<h1>Sklad a ekonomika</h1>{warning}{top}<p><a class=btn href="/sklad/novy">Přidat kus</a></p>'
            + table(headers, rows, "Sklad je prázdný. Kus přidáš tlačítkem výš nebo z detailu inzerátu.")
            + '<p class=muted>Čistá marže = prodej, minus nákup, díly, opravy, cesta a poplatky. '
            "ROI = čistá marže / (nákup + náklady + cesta). Roční ROI přepočítá výnos na dobu držení, "
            "rychlá obrátka s menší marží tak může vyjít lépe než pomalá s velkou. "
            f'Cesta: {e(num(cfg.travel.czk_per_km, 2))} Kč/km podle <a href="/nastaveni">spotřeby a ceny paliva</a>.</p>')
    return ctx.page("Sklad", "/sklad", body)


def _item_values_from_listing(ctx: Ctx) -> dict[str, Any]:
    source, source_id = ctx.req.arg("source"), ctx.req.arg("source_id")
    if not source or not source_id:
        return {"bought_at": ctx.now.date().isoformat()}
    row = ctx.db.conn.execute(LISTING_SQL + " WHERE l.source = ? AND l.source_id = ?",
                              (source, source_id)).fetchone()
    if row is None:
        return {"bought_at": ctx.now.date().isoformat()}
    trips = 2 if ctx.cfg.travel.round_trip else 1
    return {
        "title": row["title"], "family": row["family"], "chip": row["chip"], "ram_gb": row["ram_gb"],
        "ssd_gb": row["ssd_gb"], "bought_at": ctx.now.date().isoformat(), "buy_price_czk": row["price_czk"],
        "listing_url": row["url"], "source": source, "source_id": source_id,
        "travel_km": round(row["distance_km"] * trips) if row["distance_km"] else None,
    }


def inventory_form_html(values: dict[str, Any], action: str, item_id: int | None = None) -> str:
    v = lambda k: "" if values.get(k) is None else values.get(k)  # noqa: E731
    return (
        f'<form method=post action="{e(action)}" class=panel>'
        '<h2 style="margin-top:0">Nákup</h2><div class=grid>'
        + input_field("title", "Název", v("title"), required=True)
        + select_field("family", "Rodina", FAMILY_OPTIONS, v("family"))
        + input_field("chip", "Čip", v("chip"), help_text="např. m1, m2 pro")
        + input_field("ram_gb", "RAM (GB)", v("ram_gb"), "number", attrs="min=0")
        + select_field("ssd_gb", "SSD", SSD_OPTIONS, v("ssd_gb"))
        + input_field("bought_at", "Datum nákupu", v("bought_at"), "date", required=True)
        + input_field("buy_price_czk", "Nákupní cena (Kč)", v("buy_price_czk"), "number", required=True, attrs="min=0")
        + input_field("bought_from", "Od koho", v("bought_from"), help_text="jen pro tebe, třeba Bazoš Brno")
        + input_field("listing_url", "Odkaz na inzerát", v("listing_url"), "url")
        + input_field("travel_km", "Ujeto km celkem", v("travel_km"), "number", attrs="min=0 step=1",
                      help_text="celá cesta, cena se spočítá ze spotřeby")
        + '</div><h2>Prodej</h2><div class=grid>'
        + input_field("listed_at", "Vystaveno", v("listed_at"), "date")
        + input_field("list_price_czk", "Inzertní cena (Kč)", v("list_price_czk"), "number", attrs="min=0")
        + input_field("sold_at", "Prodáno", v("sold_at"), "date")
        + input_field("sale_price_czk", "Prodejní cena (Kč)", v("sale_price_czk"), "number", attrs="min=0")
        + input_field("sale_fees_czk", "Poplatky, doprava (Kč)", v("sale_fees_czk"), "number", attrs="min=0")
        + '</div><div style="margin-top:12px"><label for=note>Poznámka</label>'
        f'<textarea id=note name=note>{e(v("note"))}</textarea></div>'
        f'<input type=hidden name=source value="{e(v("source"))}"><input type=hidden name=source_id value="{e(v("source_id"))}">'
        f'<p><button>{"Uložit změny" if item_id else "Přidat do skladu"}</button></p></form>')


class FormError(ValueError):
    pass


def parse_item_form(form: dict[str, str]) -> dict[str, Any]:
    def opt_int(name: str) -> int | None:
        raw = form.get(name, "").replace(" ", "")
        if not raw:
            return None
        try:
            return int(float(raw.replace(",", ".")))
        except ValueError as exc:
            raise FormError(f"{name}: neplatné číslo") from exc

    def opt_date(name: str) -> str | None:
        raw = form.get(name, "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise FormError(f"{name}: datum ve tvaru RRRR-MM-DD") from exc

    values = {
        "title": form.get("title", "").strip(),
        "family": form.get("family") or None,
        "chip": form.get("chip", "").strip().lower() or None,
        "ram_gb": opt_int("ram_gb"), "ssd_gb": opt_int("ssd_gb"),
        "bought_at": opt_date("bought_at"), "buy_price_czk": opt_int("buy_price_czk"),
        "bought_from": form.get("bought_from", "").strip() or None,
        "listing_url": form.get("listing_url", "").strip() or None,
        "source": form.get("source") or None, "source_id": form.get("source_id") or None,
        "travel_km": opt_int("travel_km"),
        "listed_at": opt_date("listed_at"), "list_price_czk": opt_int("list_price_czk"),
        "sold_at": opt_date("sold_at"), "sale_price_czk": opt_int("sale_price_czk"),
        "sale_fees_czk": opt_int("sale_fees_czk") or 0,
        "note": form.get("note", "").strip() or None,
    }
    if not values["title"] or not values["bought_at"] or values["buy_price_czk"] is None:
        raise FormError("název, datum nákupu a nákupní cena jsou povinné")
    if values["sold_at"] and values["sale_price_czk"] is None:
        raise FormError("u prodaného kusu vyplň prodejní cenu")
    if values["sold_at"] and values["sold_at"] < values["bought_at"]:
        raise FormError("datum prodeje je před datem nákupu")
    return values


def inventory_new(ctx: Ctx) -> Response:
    body = "<h1>Nový kus do skladu</h1>" + inventory_form_html(_item_values_from_listing(ctx), "/sklad/novy")
    return ctx.page("Nový kus", "/sklad", body)


def inventory_create(ctx: Ctx) -> Response:
    try:
        values = parse_item_form(ctx.req.form)
    except FormError as exc:
        body = (f'<h1>Nový kus do skladu</h1><div class="flash bad">{e(exc)}</div>'
                + inventory_form_html(dict(ctx.req.form), "/sklad/novy"))
        return html_response(layout("Nový kus", "/sklad", body), 400)
    item_id = Inventory(ctx.db.conn).add(values, ctx.now)
    return redirect(f"/sklad/{item_id}", "Kus přidán do skladu.")


def inventory_item(ctx: Ctx, item_id: str) -> Response:
    inv = Inventory(ctx.db.conn)
    item = inv.get(int(item_id))
    if item is None:
        return html_response(layout("Nenalezeno", "/sklad", "<h1>Kus nenalezen</h1>"), 404)
    cfg = ctx.cfg
    econ = economics(item, item["costs_czk"], cfg.travel, ctx.now.date())
    costs = inv.costs(item["id"])
    kinds = dict(COST_KINDS)
    cost_rows = [[e(c["spent_at"]), e(kinds.get(c["kind"], c["kind"])), e(c["description"] or ""), e(czk(c["amount_czk"])),
                  post_button(f"/sklad/{item['id']}/naklad/smazat", "Smazat", {"cost_id": c["id"]}, "ghost")]
                 for c in costs]
    summary = cards([
        ("Investováno", e(czk(econ.invested_czk)), ""),
        ("Cesta", e(czk(econ.travel_czk)), ""),
        ("Hrubá marže", e(czk(econ.gross_margin_czk, sign=True)) if econ.gross_margin_czk is not None else "·",
         signed_class(econ.gross_margin_czk)),
        ("Čistá marže", e(czk(econ.net_margin_czk, sign=True)) if econ.net_margin_czk is not None else "·",
         signed_class(econ.net_margin_czk)),
        ("Dní držení", str(econ.days_held), ""),
        ("ROI / ročně", f"{e(pct(econ.roi, 1))} / {e(pct(econ.annualized_roi))}", signed_class(econ.roi)),
    ])
    cost_form = (
        f'<form method=post action="/sklad/{item["id"]}/naklad" class=panel><div class=grid>'
        + input_field("spent_at", "Datum", ctx.now.date().isoformat(), "date", required=True)
        + select_field("kind", "Druh", COST_KINDS, "dily")
        + input_field("description", "Popis")
        + input_field("amount_czk", "Částka (Kč)", "", "number", required=True)
        + "</div><p><button>Přidat náklad</button></p></form>")
    body = (f'<h1>{e(item["title"])}</h1>{summary}'
            + (f'<p><a href="{safe_url(item["listing_url"])}" target=_blank rel="noopener noreferrer">Původní inzerát</a></p>'
               if item["listing_url"] else "")
            + "<h2>Náklady na díly a opravy</h2>"
            + table([("Datum", ""), ("Druh", ""), ("Popis", "wrap"), ("Částka", "num"), ("", "")], cost_rows, "Bez nákladů.")
            + cost_form
            + "<h2>Údaje</h2>" + inventory_form_html(dict(item), f"/sklad/{item['id']}", item["id"])
            + post_button(f"/sklad/{item['id']}/smazat", "Smazat kus ze skladu", {}, "danger",
                          "Opravdu smazat kus i s náklady?"))
    return ctx.page(item["title"], "/sklad", body)


def inventory_update(ctx: Ctx, item_id: str) -> Response:
    inv = Inventory(ctx.db.conn)
    if inv.get(int(item_id)) is None:
        return redirect("/sklad", "Kus nenalezen.", "bad")
    try:
        values = parse_item_form(ctx.req.form)
    except FormError as exc:
        return redirect(f"/sklad/{item_id}", f"Neuloženo: {exc}", "bad")
    inv.update(int(item_id), values)
    return redirect(f"/sklad/{item_id}", "Uloženo.")


def inventory_delete(ctx: Ctx, item_id: str) -> Response:
    Inventory(ctx.db.conn).delete(int(item_id))
    return redirect("/sklad", "Kus smazán.")


def inventory_add_cost(ctx: Ctx, item_id: str) -> Response:
    form = ctx.req.form
    try:
        amount = int(form.get("amount_czk", "").replace(" ", ""))
        spent = date.fromisoformat(form.get("spent_at", "")).isoformat()
        Inventory(ctx.db.conn).add_cost(int(item_id), spent, form.get("kind", ""),
                                        form.get("description", "").strip() or None, amount)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return redirect(f"/sklad/{item_id}", f"Náklad neuložen: {exc}", "bad")
    return redirect(f"/sklad/{item_id}", "Náklad přidán.")


def inventory_delete_cost(ctx: Ctx, item_id: str) -> Response:
    try:
        Inventory(ctx.db.conn).delete_cost(int(ctx.req.form.get("cost_id", "")))
    except ValueError:
        return redirect(f"/sklad/{item_id}", "Neplatný náklad.", "bad")
    return redirect(f"/sklad/{item_id}", "Náklad smazán.")


# nastavení

def mask(url: str) -> str:
    return f"nastaveno, končí …{url[-6:]}" if url else "nenastaveno"


def settings_page(ctx: Ctx, submitted: dict[str, Any] | None = None, error: str | None = None) -> Response:
    cfg = ctx.cfg
    groups: dict[str, list[str]] = {}
    for field in FIELDS:
        current = effective_value(cfg, field.path)
        value = submitted.get(field.path, current) if submitted else current
        if field.path == "travel.max_km" and value in (None, 0):
            value = ""
        if field.kind == "bool":
            html_field = checkbox_field(field.path, field.label, bool(value), field.help)
        elif field.kind == "lines":
            html_field = (f'<div style="grid-column:1/-1"><label for="{e(field.path)}">{e(field.label)}</label>'
                          f'<textarea id="{e(field.path)}" name="{e(field.path)}">{e(chr(10).join(value or []))}</textarea>'
                          f'<div class=help>{e(field.help)}</div></div>')
        elif field.kind == "choice":
            html_field = select_field(field.path, field.label, field.choices, value, field.help)
        elif field.kind == "secret":
            html_field = input_field(field.path, field.label, "", "password",
                                     f"{mask(value or '')}. Prázdné pole nechá současnou hodnotu. {field.help}",
                                     attrs='autocomplete=off placeholder="https://discord.com/api/webhooks/…"')
            html_field += checkbox_field("clear_webhook", "Smazat webhook", False)
        else:
            kind = "number" if field.kind in ("int", "float") else "text"
            step = ' step="any"' if field.kind == "float" else ""
            html_field = input_field(field.path, field.label, "" if value is None else value, kind, field.help, attrs=step)
        groups.setdefault(field.group, []).append(html_field)
    home = default_gazetteer().by_postal_code(cfg.travel.home_psc) if cfg.travel.home_psc else None
    home_info = ("Domov: " + (f"{home.name} ({home.lat:.3f}, {home.lon:.3f})"
                              + ("" if home.exact else ", PSČ není v datech, použito nejbližší") if home
                              else "PSČ nenalezeno, vzdálenost se nepočítá")
                 + f". Cesta vychází na {num(cfg.travel.czk_per_km, 2)} Kč/km"
                 + (" tam i zpět." if cfg.travel.round_trip else " jedním směrem."))
    sections = "".join(
        f'<div class=panel><h2 style="margin-top:0">{e(group)}</h2><div class=grid>{"".join(items)}</div>'
        + (f'<p class=muted>{e(home_info)}</p>' if group == "Dojezd" else "") + "</div>"
        for group, items in groups.items())
    notice = f'<div class="flash bad">{e(error)}</div>' if error else ""
    facebook = facebook_panel(ctx, cfg)
    test = ('<form method=post action="/nastaveni/test-webhook" class=panel><h2 style="margin-top:0">Test upozornění</h2>'
            '<p class=muted>Pošle na uložený webhook zkušební zprávu.</p><button>Odeslat testovací zprávu</button></form>')
    body = (f"<h1>Nastavení</h1>{notice}<p class=muted>Změny se uloží do databáze a služba je použije při "
            "dalším průchodu, restart není potřeba. Regulární výrazy parseru zůstávají v config.json.</p>"
            f'<form method=post action="/nastaveni">{sections}<p><button>Uložit nastavení</button></p></form>'
            f'{facebook}{test}')
    return ctx.page("Nastavení", "/nastaveni", body) if not error else \
        html_response(layout("Nastavení", "/nastaveni", body), 400)


def settings_save(ctx: Ctx) -> Response:
    form = ctx.req.form
    values: dict[str, Any] = {}
    try:
        for field in FIELDS:
            raw = form.get(field.path)
            if field.kind == "secret":
                if form.get("clear_webhook"):
                    values[field.path] = ""
                elif raw and raw.strip():
                    url = raw.strip()
                    if not url.startswith("https://"):
                        raise ConfigError("webhook musí začínat https://")
                    values[field.path] = url
                continue
            if field.path == "travel.max_km" and not (raw or "").strip():
                values[field.path] = None
                continue
            values[field.path] = coerce(field, raw)
        validate_and_save(ctx.app.config_path, ctx.db, values, ctx.now)
    except ConfigError as exc:
        return settings_page(ctx, values, f"Neuloženo: {exc}")
    count = recompute(ctx)
    return redirect("/nastaveni", f"Nastavení uloženo, posouzení {count} inzerátů přepočítáno.")


def facebook_panel(ctx: Ctx, cfg: AppConfig, submitted: fbconfig.FbSettings | None = None,
                   error: str | None = None) -> str:
    path = cfg.facebook.config_path
    head = '<div class=panel id=facebook><h2 style="margin-top:0">Facebook Marketplace</h2>'
    if path is None:
        return (head + '<p class=muted>Cesta ke configu fb-mp-rss není nastavená. Doplň do config.json '
                '<code>sources.facebook.config_path</code>, třeba <code>../fb-mp-rss/data/config.json</code>.</p></div>')
    try:
        _, current = fbconfig.load(path)
    except fbconfig.FbConfigError as exc:
        return head + f'<div class="flash bad">{e(exc)}</div></div>'
    values = submitted or current
    run = ctx.db.conn.execute(
        "SELECT started_at, items_seen, items_new, errors FROM poll_runs WHERE source = 'facebook' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    count = ctx.db.conn.execute("SELECT COUNT(*) FROM listings WHERE source = 'facebook'").fetchone()[0]
    status = (f"Naposled načteno {when(run['started_at'])}: {run['items_seen']} inzerátů, {run['items_new']} nových"
              + (", s chybou, běží kontejner fb-mp-rss?" if run["errors"] else ".") if run else "Feed zatím nebyl načten.")
    return (
        head + (f'<div class="flash bad">{e(error)}</div>' if error else "")
        + f'<p class=muted>{e(status)} Celkem {count} inzerátů z Marketplace. Změny zapíše do {e(path)}, '
        "fb-mp-rss je použije při další kontrole bez restartu.</p>"
        '<form method=post action="/nastaveni/facebook"><div class=grid>'
        '<div style="grid-column:1/-1"><label for=fb_urls>Adresy hledání</label>'
        f'<textarea id=fb_urls name=urls>{e(chr(10).join(values.urls))}</textarea>'
        '<div class=help>Jedna na řádek. Na Facebooku nastav lokalitu, okruh a hledání a zkopíruj adresu z prohlížeče. '
        'Řazení a stáří se doplní samo podle voleb níže.</div></div>'
        + input_field("keywords", "Klíčová slova v titulku", ", ".join(values.keywords),
                      help_text="Oddělená čárkou, stačí jedno z nich. Prázdné = bez filtru.")
        + input_field("refresh_minutes", "Kontrola každých (min)", values.refresh_minutes, "number",
                      help_text=f"Nejméně {fbconfig.MIN_INTERVAL} minut, častěji hrozí blokace.",
                      attrs=f"min={fbconfig.MIN_INTERVAL} step=1")
        + select_field("days_since_listed", "Stáří inzerátů", fbconfig.DAY_CHOICES, values.days_since_listed,
                       "Bez přihlášení je vidět jen první várka kolem 24 inzerátů, kratší okno pomáhá.")
        + checkbox_field("sort_newest", "Řadit od nejnovějších", values.sort_newest,
                         "Doporučeno, jinak Facebook řadí podle vlastního doporučení.")
        + "</div><p><button>Uložit nastavení Facebooku</button></p></form></div>")


def settings_facebook_save(ctx: Ctx) -> Response:
    cfg = ctx.cfg
    form = ctx.req.form
    try:
        minutes = int((form.get("refresh_minutes") or "0").strip())
    except ValueError:
        minutes = 0
    submitted = fbconfig.FbSettings(
        urls=[line.strip() for line in form.get("urls", "").splitlines() if line.strip()],
        keywords=[word.strip() for word in form.get("keywords", "").split(",") if word.strip()],
        refresh_minutes=minutes,
        days_since_listed=form.get("days_since_listed", ""),
        sort_newest=bool(form.get("sort_newest")),
    )
    if cfg.facebook.config_path is None:
        return redirect("/nastaveni#facebook", "Cesta ke configu fb-mp-rss není nastavená.", "bad")
    try:
        fbconfig.save(cfg.facebook.config_path, submitted)
    except (fbconfig.FbConfigError, OSError) as exc:
        body = (f"<h1>Nastavení</h1>" + facebook_panel(ctx, cfg, submitted, f"Neuloženo: {exc}")
                + '<p><a href="/nastaveni">Zpět na celé nastavení</a></p>')
        return html_response(layout("Nastavení", "/nastaveni", body), 400)
    return redirect("/nastaveni#facebook", "Nastavení Facebooku uloženo.")


def settings_test_webhook(ctx: Ctx) -> Response:
    url = ctx.cfg.discord_webhook_url
    if not url:
        return redirect("/nastaveni", "Webhook není nastavený.", "bad")
    ok = ctx.app.notifier_factory(url).send({
        "title": "macwatch: testovací zpráva", "description": "Webhook funguje, upozornění budou chodit sem.",
        "color": 0x2ECC71})
    return redirect("/nastaveni", "Testovací zpráva odeslána." if ok else "Discord zprávu nepřijal, zkontroluj URL.",
                    "good" if ok else "bad")


# stav

def status_page(ctx: Ctx) -> Response:
    runs = ctx.db.conn.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 40").fetchall()
    counts = ctx.db.conn.execute(
        """SELECT source, COUNT(*) AS total, SUM(gone_at IS NULL) AS active, SUM(gone_at IS NOT NULL) AS gone,
                  SUM(alerted_at IS NOT NULL) AS alerted FROM listings GROUP BY source""").fetchall()
    last_run = {r["source"]: r for r in reversed(runs)}
    stale = [src for src, r in last_run.items() if not src.endswith(":lifecycle") and r["started_at"]
             and ctx.now - datetime.fromisoformat(r["started_at"]) > timedelta(minutes=30)]
    warn = (f'<div class="flash warn">Zdroj {e(", ".join(stale))} neběžel přes 30 minut. Běží služba?</div>'
            if stale else "")
    body = ("<h1>Stav</h1>" + warn
            + table([("Zdroj", ""), ("Inzerátů", "num"), ("Aktivních", "num"), ("Zmizelých", "num"), ("Upozornění", "num")],
                    [[source_label(c["source"]), str(c["total"]), str(c["active"]), str(c["gone"]), str(c["alerted"])]
                     for c in counts], "Databáze je prázdná.")
            + "<h2>Poslední průchody</h2>"
            + table([("Začátek", ""), ("Zdroj", ""), ("Položek", "num"), ("Nových", "num"), ("Detailů", "num"),
                     ("Upozornění", "num"), ("Chyby", "num"), ("Popis chyby", "wrap")],
                    [[e(when(r["started_at"])), e(r["source"]), str(r["items_seen"]), str(r["items_new"]),
                      str(r["details_fetched"]), str(r["alerts_sent"]),
                      f'<span class="{"neg" if r["errors"] else ""}">{r["errors"]}</span>',
                      e((r["error_text"] or "")[:300])] for r in runs]))
    return ctx.page("Stav", "/stav", body)


def health(ctx: Ctx) -> Response:
    ctx.db.conn.execute("SELECT 1").fetchone()
    return Response(200, b"ok", [("Content-Type", "text/plain; charset=utf-8")])


ROUTES = [
    ("GET", "/", findings),
    ("GET", r"/inzerat/(?P<source>[a-z]+)/(?P<source_id>\d+)", listing_detail),
    ("GET", "/shortlist", shortlist_page),
    ("POST", "/shortlist/prepnout", shortlist_toggle),
    ("POST", "/shortlist/poznamka", shortlist_note),
    ("GET", "/trh", market_page),
    ("GET", "/trh/skupina", market_group),
    ("GET", "/ceny", prices_page),
    ("POST", "/ceny/ulozit", prices_save),
    ("POST", "/ceny/smazat", prices_delete),
    ("POST", "/ceny/model", prices_model_save),
    ("POST", "/ceny/model/odemknout", prices_model_unlock),
    ("POST", "/ceny/model/smazat", prices_model_delete),
    ("POST", "/ceny/prirazky", prices_percentages_save),
    ("POST", "/ceny/prepocitat", prices_refit),
    ("GET", "/sklad", inventory_page),
    ("GET", "/sklad/novy", inventory_new),
    ("POST", "/sklad/novy", inventory_create),
    ("GET", r"/sklad/(?P<item_id>\d+)", inventory_item),
    ("POST", r"/sklad/(?P<item_id>\d+)", inventory_update),
    ("POST", r"/sklad/(?P<item_id>\d+)/smazat", inventory_delete),
    ("POST", r"/sklad/(?P<item_id>\d+)/naklad", inventory_add_cost),
    ("POST", r"/sklad/(?P<item_id>\d+)/naklad/smazat", inventory_delete_cost),
    ("GET", "/nastaveni", settings_page),
    ("POST", "/nastaveni", settings_save),
    ("POST", "/nastaveni/test-webhook", settings_test_webhook),
    ("POST", "/nastaveni/facebook", settings_facebook_save),
    ("GET", "/stav", status_page),
    ("GET", "/zdravi", health),
]

