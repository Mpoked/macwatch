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
    "PODEZRELE": ("Podezřele levné", "bad"),
    "MIMO": ("Mimo rozsah", "muted"),
}

SOURCE_LABELS = {"bazos": "Bazoš", "facebook": "Marketplace", "vlastni": "Vlastní prodej"}

CSS = """
:root{--background:oklch(1 0 0);--foreground:oklch(0.145 0 0);--card:oklch(1 0 0);--card-foreground:oklch(0.145 0 0);
--popover:oklch(1 0 0);--popover-foreground:oklch(0.145 0 0);--primary:oklch(0.205 0 0);--primary-foreground:oklch(0.985 0 0);
--secondary:oklch(0.97 0 0);--secondary-foreground:oklch(0.205 0 0);--muted:oklch(0.97 0 0);--muted-foreground:oklch(0.556 0 0);
--accent:oklch(0.97 0 0);--accent-foreground:oklch(0.205 0 0);--destructive:oklch(0.577 0.245 27.325);
--border:oklch(0.922 0 0);--input:oklch(0.922 0 0);--ring:oklch(0.708 0 0);
--chart-1:oklch(0.87 0 0);--chart-2:oklch(0.556 0 0);--chart-3:oklch(0.439 0 0);--chart-4:oklch(0.371 0 0);--chart-5:oklch(0.269 0 0);
--radius:0.625rem;--sidebar:oklch(0.985 0 0);--sidebar-foreground:oklch(0.145 0 0);--sidebar-primary:oklch(0.205 0 0);
--sidebar-primary-foreground:oklch(0.985 0 0);--sidebar-accent:oklch(0.97 0 0);--sidebar-accent-foreground:oklch(0.205 0 0);
--sidebar-border:oklch(0.922 0 0);--sidebar-ring:oklch(0.708 0 0);
--success:oklch(0.527 0.137 150.07);--success-bg:oklch(0.962 0.044 156.74);
--warning:oklch(0.555 0.135 66.44);--warning-bg:oklch(0.973 0.071 103.19);
--info:oklch(0.488 0.217 264.38);--info-bg:oklch(0.951 0.026 236.82);--danger-bg:oklch(0.971 0.013 17.38);
--series-1:oklch(0.205 0 0);--series-2:oklch(0.488 0.243 264.376);--series-3:oklch(0.556 0 0)}
.dark{--background:oklch(0.145 0 0);--foreground:oklch(0.985 0 0);--card:oklch(0.205 0 0);--card-foreground:oklch(0.985 0 0);
--popover:oklch(0.205 0 0);--popover-foreground:oklch(0.985 0 0);--primary:oklch(0.922 0 0);--primary-foreground:oklch(0.205 0 0);
--secondary:oklch(0.269 0 0);--secondary-foreground:oklch(0.985 0 0);--muted:oklch(0.269 0 0);--muted-foreground:oklch(0.708 0 0);
--accent:oklch(0.269 0 0);--accent-foreground:oklch(0.985 0 0);--destructive:oklch(0.704 0.191 22.216);
--border:oklch(1 0 0 / 10%);--input:oklch(1 0 0 / 15%);--ring:oklch(0.556 0 0);
--sidebar:oklch(0.205 0 0);--sidebar-foreground:oklch(0.985 0 0);--sidebar-primary:oklch(0.488 0.243 264.376);
--sidebar-primary-foreground:oklch(0.985 0 0);--sidebar-accent:oklch(0.269 0 0);--sidebar-accent-foreground:oklch(0.985 0 0);
--sidebar-border:oklch(1 0 0 / 10%);--sidebar-ring:oklch(0.556 0 0);
--success:oklch(0.792 0.209 151.71);--success-bg:oklch(0.792 0.209 151.71 / 12%);
--warning:oklch(0.828 0.189 84.43);--warning-bg:oklch(0.828 0.189 84.43 / 12%);
--info:oklch(0.707 0.165 254.62);--info-bg:oklch(0.707 0.165 254.62 / 14%);--danger-bg:oklch(0.704 0.191 22.216 / 14%);
--series-1:oklch(0.922 0 0);--series-2:oklch(0.707 0.165 254.62);--series-3:oklch(0.556 0 0)}
@media (prefers-color-scheme:dark){:root:not(.light){--background:oklch(0.145 0 0);--foreground:oklch(0.985 0 0);
--card:oklch(0.205 0 0);--card-foreground:oklch(0.985 0 0);--popover:oklch(0.205 0 0);--popover-foreground:oklch(0.985 0 0);
--primary:oklch(0.922 0 0);--primary-foreground:oklch(0.205 0 0);--secondary:oklch(0.269 0 0);--secondary-foreground:oklch(0.985 0 0);
--muted:oklch(0.269 0 0);--muted-foreground:oklch(0.708 0 0);--accent:oklch(0.269 0 0);--accent-foreground:oklch(0.985 0 0);
--destructive:oklch(0.704 0.191 22.216);--border:oklch(1 0 0 / 10%);--input:oklch(1 0 0 / 15%);--ring:oklch(0.556 0 0);
--sidebar:oklch(0.205 0 0);--sidebar-foreground:oklch(0.985 0 0);--sidebar-primary:oklch(0.488 0.243 264.376);
--sidebar-primary-foreground:oklch(0.985 0 0);--sidebar-accent:oklch(0.269 0 0);--sidebar-accent-foreground:oklch(0.985 0 0);
--sidebar-border:oklch(1 0 0 / 10%);--sidebar-ring:oklch(0.556 0 0);
--success:oklch(0.792 0.209 151.71);--success-bg:oklch(0.792 0.209 151.71 / 12%);
--warning:oklch(0.828 0.189 84.43);--warning-bg:oklch(0.828 0.189 84.43 / 12%);
--info:oklch(0.707 0.165 254.62);--info-bg:oklch(0.707 0.165 254.62 / 14%);--danger-bg:oklch(0.704 0.191 22.216 / 14%);
--series-1:oklch(0.922 0 0);--series-2:oklch(0.707 0.165 254.62);--series-3:oklch(0.556 0 0)}}
/* staré názvy proměnných, používají je grafy a pár inline stylů */
:root{--bg:var(--background);--panel:var(--card);--text:var(--foreground);--line:var(--border);
--chart1:var(--series-1);--chart2:var(--series-2);--chart3:var(--series-3)}
*{box-sizing:border-box;border-color:var(--border)}
html{color-scheme:light dark}
body{margin:0;background:var(--background);color:var(--foreground);
font:14px/1.5 "Geist","Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--foreground);text-decoration:underline;text-decoration-color:var(--border);text-underline-offset:3px}
a:hover{text-decoration-color:var(--foreground)}
header{background:var(--background);border-bottom:1px solid var(--border);padding:0 16px;position:sticky;top:0;z-index:10;
backdrop-filter:saturate(1.2) blur(6px)}
header .in{max-width:1280px;margin:0 auto;display:flex;gap:4px 20px;align-items:center;flex-wrap:wrap;min-height:56px}
header strong{font-size:15px;font-weight:600;letter-spacing:-0.01em;margin-right:4px}
header nav{display:flex;gap:2px;flex-wrap:wrap}
header nav a{padding:6px 12px;border-radius:calc(var(--radius) - 2px);color:var(--muted-foreground);text-decoration:none;
font-weight:500;transition:color .15s,background .15s}
header nav a:hover{color:var(--foreground);background:var(--accent)}
header nav a.on{color:var(--accent-foreground);background:var(--accent)}
main{max-width:1280px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:24px;font-weight:600;letter-spacing:-0.025em;margin:0 0 20px}
h2{font-size:16px;font-weight:600;letter-spacing:-0.01em;margin:32px 0 12px}
p{margin:0 0 12px}
.panel{background:var(--card);color:var(--card-foreground);border:1px solid var(--border);border-radius:var(--radius);
padding:20px;margin-bottom:16px;box-shadow:0 1px 2px oklch(0 0 0 / 5%)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);color:var(--card-foreground);border:1px solid var(--border);border-radius:var(--radius);
padding:16px 18px;box-shadow:0 1px 2px oklch(0 0 0 / 5%)}
.card .k{color:var(--muted-foreground);font-size:13px;font-weight:500}
.card .v{font-size:24px;font-weight:600;letter-spacing:-0.02em;margin-top:4px;font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
box-shadow:0 1px 2px oklch(0 0 0 / 5%)}
table{border-collapse:collapse;width:100%}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top;white-space:nowrap}
td.wrap{white-space:normal;min-width:220px}
th{font-size:13px;color:var(--muted-foreground);font-weight:500;background:var(--card);position:sticky;top:0;height:40px;
vertical-align:middle}
tbody tr{transition:background .15s}
tbody tr:hover{background:color-mix(in oklch,var(--muted) 50%,transparent)}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:calc(var(--radius) - 4px);font-size:12px;
font-weight:500;line-height:1.4;border:1px solid transparent;white-space:nowrap}
.good{color:var(--success);background:var(--success-bg)}
.warn{color:var(--warning);background:var(--warning-bg)}
.bad{color:var(--destructive);background:var(--danger-bg)}
.info{color:var(--info);background:var(--info-bg)}
.muted{color:var(--muted-foreground)}
.badge.muted{background:var(--secondary);color:var(--secondary-foreground);border-color:var(--border)}
.pos{color:var(--success)}.neg{color:var(--destructive)}
.flash{padding:12px 16px;border-radius:var(--radius);margin-bottom:16px;border:1px solid var(--border);font-weight:500}
form.inline{display:inline}
.panel > form.inline{display:block;margin:12px 0 4px}
.panel form p{margin:16px 0 0}
label{display:block;font-size:13px;font-weight:500;color:var(--foreground);margin-bottom:6px}
input,select,textarea{font:inherit;color:var(--foreground);background:transparent;border:1px solid var(--input);
border-radius:calc(var(--radius) - 2px);padding:6px 10px;min-height:36px;max-width:100%;
box-shadow:0 1px 2px oklch(0 0 0 / 5%);transition:border-color .15s,box-shadow .15s}
select{background:var(--background)}
input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible,.btn:focus-visible{outline:none;
border-color:var(--ring);box-shadow:0 0 0 3px color-mix(in oklch,var(--ring) 50%,transparent)}
input[type=checkbox]{width:16px;height:16px;min-height:0;padding:0;accent-color:var(--primary);vertical-align:-3px;box-shadow:none}
label:has(input[type=checkbox]){display:flex;gap:8px;align-items:center}
textarea{width:100%;min-height:80px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px 20px}
.grid input:not([type=checkbox]),.grid select{width:100%}
.help{font-size:12px;color:var(--muted-foreground);margin-top:6px}
button,.btn{font:inherit;font-weight:500;background:var(--primary);color:var(--primary-foreground);border:1px solid transparent;
border-radius:calc(var(--radius) - 2px);padding:0 16px;height:36px;display:inline-flex;align-items:center;gap:6px;
cursor:pointer;text-decoration:none;box-shadow:0 1px 2px oklch(0 0 0 / 5%);transition:background .15s,opacity .15s}
button:hover,.btn:hover{background:color-mix(in oklch,var(--primary) 90%,transparent);text-decoration:none}
button.ghost{background:var(--background);color:var(--foreground);border-color:var(--input);height:32px;padding:0 12px}
button.ghost:hover{background:var(--accent);color:var(--accent-foreground)}
button.danger{background:var(--destructive);color:oklch(0.985 0 0)}
button.danger:hover{background:color-mix(in oklch,var(--destructive) 90%,transparent)}
.filters{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-bottom:16px}
.filters label{margin-bottom:6px}
.star{background:none;border:0;box-shadow:none;height:auto;padding:0 4px;font-size:18px;color:var(--muted-foreground);cursor:pointer}
.star:hover{background:none;color:var(--foreground)}
.star.on{color:oklch(0.795 0.184 86.047)}
pre.body{white-space:pre-wrap;font:inherit;margin:0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:var(--muted);padding:1px 5px;
border-radius:4px}
svg.chart{width:100%;height:auto;max-width:760px}
svg.chart text{fill:var(--muted-foreground);font-size:11px}
.legend{margin-top:6px}.legend span{margin-right:16px;font-size:13px;color:var(--muted-foreground)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
@media (max-width:640px){th,td{padding:8px 10px}.card .v{font-size:20px}h1{font-size:21px}main{padding-top:20px}}
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
