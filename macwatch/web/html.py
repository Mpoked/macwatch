"""Stavební kameny HTML: rozvržení, formátování, formuláře a SVG graf. Vše escapované."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
from urllib.parse import urlencode

NAV = (
    ("/", "Nálezy"),
    ("/shortlist", "Shortlist"),
    ("/trh", "Trh"),
    ("/ceny", "Ceny"),
    ("/sklad", "Sklad"),
    ("/nastaveni", "Nastavení"),
    ("/stav", "Stav"),
)

VERDICT_LABELS = {
    "KOUPIT": ("Koupit", "good"),
    "KOUPIT_S_RIZIKEM": ("Koupit s rizikem", "warn"),
    "NEZNAMY_MODEL": ("Model mimo tabulku", "info"),
    "SLEDOVAT": ("Sledovat", "muted"),
    "NEVYPLATI_SE": ("Nevyplatí se", "muted"),
    "BEZ_CENY": ("Bez ceny", "muted"),
    "MIMO": ("Mimo rozsah", "muted"),
}

SOURCE_LABELS = {"bazos": "Bazoš", "facebook": "Marketplace", "vlastni": "Vlastní prodej"}

CSS = """
:root{--bg:#f6f6f4;--panel:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e2e2e0;--accent:#0a66c2;
--good:#1a7f37;--good-bg:#e6f4ea;--warn:#9a5b00;--warn-bg:#fff3d6;--bad:#b42318;--bad-bg:#fde8e7;
--info:#0a58a8;--info-bg:#e5f0fb;--chart1:#0a66c2;--chart2:#d9480f;--chart3:#2f9e44}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1d1d20;--text:#ececec;--muted:#9a9aa0;
--line:#303035;--accent:#5aa9ff;--good:#57c878;--good-bg:#12301c;--warn:#f0b454;--warn-bg:#35280f;
--bad:#ff8a80;--bad-bg:#3a1714;--info:#7cb8ff;--info-bg:#132840;--chart1:#5aa9ff;--chart2:#ff8c4a;--chart3:#63d47a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:0 16px}
header .in{max-width:1280px;margin:0 auto;display:flex;gap:4px 18px;align-items:center;flex-wrap:wrap;min-height:52px}
header strong{font-size:17px;margin-right:8px}
header nav{display:flex;gap:2px;flex-wrap:wrap}
header nav a{padding:6px 10px;border-radius:6px;color:var(--text)}
header nav a.on{background:var(--bg);font-weight:600}
main{max-width:1280px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:28px 0 10px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .k{color:var(--muted);font-size:13px}.card .v{font-size:22px;font-weight:650;margin-top:2px}
.tablewrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
td.wrap{white-space:normal;min-width:220px}
th{font-size:12.5px;color:var(--muted);font-weight:600;background:var(--panel);position:sticky;top:0}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12.5px;font-weight:600}
.good{color:var(--good);background:var(--good-bg)}.warn{color:var(--warn);background:var(--warn-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}.info{color:var(--info);background:var(--info-bg)}
.muted{color:var(--muted)}.badge.muted{background:var(--bg)}
.pos{color:var(--good)}.neg{color:var(--bad)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:16px}
form.inline{display:inline}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:3px}
input,select,textarea{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;max-width:100%}
input[type=checkbox]{width:auto}
textarea{width:100%;min-height:70px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px 16px}
.grid input:not([type=checkbox]),.grid select{width:100%}
.help{font-size:12px;color:var(--muted);margin-top:2px}
button,.btn{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;display:inline-block}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line);padding:4px 10px}
button.danger{background:var(--bad)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:12px}
.star{background:none;border:0;padding:0 4px;font-size:18px;color:var(--muted);cursor:pointer}
.star.on{color:#e3a008}
pre.body{white-space:pre-wrap;font:inherit;margin:0}
svg.chart{width:100%;height:auto;max-width:760px}
svg.chart text{fill:var(--muted);font-size:11px}
.legend span{margin-right:14px;font-size:13px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
@media (max-width:640px){th,td{padding:7px 8px}.card .v{font-size:19px}}
"""


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def safe_url(url: str | None) -> str:
    """Do href jen http a https, nic jako javascript:."""
    text = (url or "").strip()
    return e(text) if text.lower().startswith(("http://", "https://")) else "#"


def czk(value: float | int | None, sign: bool = False) -> str:
    if value is None:
        return "·"
    text = f"{abs(round(value)):,}".replace(",", " ") + " Kč"
    if sign:
        return ("+" if value >= 0 else "-") + text
    return ("-" if value < 0 else "") + text


def pct(value: float | None, digits: int = 0) -> str:
    return "·" if value is None else f"{value * 100:.{digits}f} %".replace(".", ",")


def num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "·"
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def signed_class(value: float | int | None) -> str:
    return "" if value is None else ("pos" if value >= 0 else "neg")


def verdict_badge(verdict: str | None) -> str:
    label, cls = VERDICT_LABELS.get(verdict or "", (verdict or "·", "muted"))
    return f'<span class="badge {cls}">{e(label)}</span>'


def source_label(source: str | None) -> str:
    return e(SOURCE_LABELS.get(source or "", source or ""))


def url_with(path: str, **params: object) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


def layout(title: str, active: str, body: str, flash: str | None = None,
           flash_kind: str = "info") -> str:
    nav = "".join(f'<a href="{href}" class="{"on" if href == active else ""}">{e(label)}</a>'
                  for href, label in NAV)
    notice = f'<div class="flash {flash_kind}">{e(flash)}</div>' if flash else ""
    return (f"<!doctype html><html lang=cs><head><meta charset=utf-8>"
            f'<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{e(title)} · macwatch</title><style>{CSS}</style></head><body>"
            f'<header><div class=in><strong>macwatch</strong><nav>{nav}</nav></div></header>'
            f"<main>{notice}{body}</main></body></html>")


def table(headers: Iterable[tuple[str, str]], rows: Iterable[Iterable[str]], empty: str = "Zatím nic.") -> str:
    """headers: (text, css třída). Buňky řádků jsou už hotové HTML."""
    headers = list(headers)
    head = "".join(f'<th class="{cls}">{e(text)}</th>' for text, cls in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(row))
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td colspan="{len(headers)}" class="muted">{e(empty)}</td></tr>')
    return f'<div class=tablewrap><table><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'


def cards(items: Iterable[tuple[str, str, str]]) -> str:
    """(popisek, hodnota HTML, css třída hodnoty)."""
    return "<div class=cards>" + "".join(
        f'<div class=card><div class=k>{e(k)}</div><div class="v {cls}">{v}</div></div>'
        for k, v, cls in items) + "</div>"


def input_field(name: str, label: str, value: object = "", kind: str = "text", help_text: str = "",
                required: bool = False, attrs: str = "") -> str:
    req = " required" if required else ""
    return (f'<div><label for="{e(name)}">{e(label)}</label>'
            f'<input id="{e(name)}" name="{e(name)}" type="{kind}" value="{e(value)}"{req} {attrs}>'
            + (f'<div class=help>{e(help_text)}</div>' if help_text else "") + "</div>")


def select_field(name: str, label: str, options: Iterable[tuple[str, str]], selected: object = "",
                 help_text: str = "") -> str:
    opts = "".join(f'<option value="{e(v)}"{" selected" if str(v) == str(selected) else ""}>{e(t)}</option>'
                   for v, t in options)
    return (f'<div><label for="{e(name)}">{e(label)}</label><select id="{e(name)}" name="{e(name)}">{opts}</select>'
            + (f'<div class=help>{e(help_text)}</div>' if help_text else "") + "</div>")


def checkbox_field(name: str, label: str, checked: bool, help_text: str = "") -> str:
    return (f'<div><label><input type=checkbox name="{e(name)}"{" checked" if checked else ""}> {e(label)}</label>'
            + (f'<div class=help>{e(help_text)}</div>' if help_text else "") + "</div>")


def line_chart(series: dict[str, list[tuple[str, float]]], width: int = 720, height: int = 240) -> str:
    """Jednoduchý spojnicový graf. series: {název: [(popisek osy x, hodnota)]}."""
    labels = sorted({label for points in series.values() for label, _ in points})
    values = [v for points in series.values() for _, v in points]
    if not labels or not values:
        return '<p class=muted>Na graf zatím nejsou data.</p>'
    left, right, top, bottom = 64, 16, 14, 30
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo * 0.95, hi * 1.05 or 1
    span_x = max(len(labels) - 1, 1)

    def x(label: str) -> float:
        return left + labels.index(label) / span_x * (width - left - right)

    def y(value: float) -> float:
        return top + (hi - value) / (hi - lo) * (height - top - bottom)

    parts = [f'<svg class=chart viewBox="0 0 {width} {height}" role="img">']
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        yy = y(value)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="var(--line)"/>')
        parts.append(f'<text x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{e(num(value))}</text>')
    step = max(1, len(labels) // 8)
    for label in labels[::step]:
        parts.append(f'<text x="{x(label):.1f}" y="{height - 10}" text-anchor="middle">{e(label)}</text>')
    legend = []
    for index, (name, points) in enumerate(series.items()):
        color = f"var(--chart{index % 3 + 1})"
        coords = " ".join(f"{x(label):.1f},{y(value):.1f}" for label, value in sorted(points))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>')
        for label, value in points:
            parts.append(f'<circle cx="{x(label):.1f}" cy="{y(value):.1f}" r="3" fill="{color}">'
                         f'<title>{e(name)} {e(label)}: {e(czk(value))}</title></circle>')
        legend.append(f'<span><i style="background:{color}"></i>{e(name)}</span>')
    parts.append("</svg>")
    return "".join(parts) + f'<div class=legend>{"".join(legend)}</div>'
