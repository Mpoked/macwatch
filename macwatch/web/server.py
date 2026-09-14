"""Webový dashboard: malý router nad http.server, vykreslování na serveru.

Poslouchá výchozí na 127.0.0.1. Ven se vystavuje jen přes Cloudflare Tunnel za
Cloudflare Access (viz README). POST požadavky musí mít Origin nebo Referer ze
stejného hostitele, jinak se odmítnou (ochrana proti CSRF).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from macwatch.config import AppConfig
from macwatch.db import Database
from macwatch.notify import DiscordNotifier

log = logging.getLogger(__name__)

MAX_BODY = 256 * 1024


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def arg(self, name: str, default: str = "") -> str:
        return self.query.get(name, default)


@dataclass(slots=True)
class Response:
    status: int
    body: bytes
    headers: list[tuple[str, str]] = field(default_factory=list)


def html_response(body: str, status: int = 200) -> Response:
    return Response(status, body.encode("utf-8"), [("Content-Type", "text/html; charset=utf-8")])


def redirect(location: str, msg: str | None = None, kind: str = "good") -> Response:
    if msg:
        path, hash_mark, fragment = location.partition("#")
        sep = "&" if "?" in path else "?"
        location = f"{path}{sep}msg={quote(msg)}&kind={kind}{hash_mark}{fragment}"
    return Response(303, b"", [("Location", location)])


def _single(values: dict[str, list[str]]) -> dict[str, str]:
    return {key: items[-1] for key, items in values.items()}


def same_origin(req: Request) -> bool:
    """Origin (nebo Referer) musí sedět na Host. Za tunelem může přijít X-Forwarded-Host."""
    hosts = {req.headers.get("host", ""), req.headers.get("x-forwarded-host", "")} - {""}
    origin = req.headers.get("origin") or req.headers.get("referer") or ""
    return bool(hosts) and urlsplit(origin).netloc in hosts


View = Callable[..., Response]


class App:
    def __init__(self, config_path: Path, database: Path | str, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 notifier_factory: Callable[[str], Any] = DiscordNotifier,
                 check_origin: bool = True) -> None:
        from macwatch.web import views

        self.config_path = config_path
        self.database = database
        self.clock = clock
        self.notifier_factory = notifier_factory
        self.check_origin = check_origin
        self._shared_db: Database | None = None
        if str(database) == ":memory:":
            self._shared_db = Database.open(":memory:")
        self.routes: list[tuple[str, re.Pattern[str], View]] = [
            (method, re.compile(f"^{pattern}$"), view) for method, pattern, view in views.ROUTES]

    def open_db(self) -> Database:
        return self._shared_db or Database.open(self.database)

    def handle(self, req: Request) -> Response:
        if req.method == "POST" and self.check_origin and not same_origin(req):
            return html_response("Požadavek z cizí stránky byl odmítnut.", 403)
        allowed = False
        for method, pattern, view in self.routes:
            match = pattern.match(req.path)
            if not match:
                continue
            allowed = True
            if method != req.method:
                continue
            db = self.open_db()
            try:
                from macwatch.web.views import Ctx
                return view(Ctx(self, db, req), **match.groupdict())
            except Exception:
                log.exception("chyba při obsluze %s %s", req.method, req.path)
                return html_response("Interní chyba, podrobnosti jsou v logu.", 500)
            finally:
                if db is not self._shared_db:
                    db.close()
        if allowed:
            return html_response("Metoda není povolená.", 405)
        return html_response("Stránka nenalezena.", 404)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "macwatch"

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            form: dict[str, str] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(Response(413, "Příliš velký požadavek.".encode("utf-8")))
                    return
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                form = _single(parse_qs(raw, keep_blank_values=True))
            req = Request(method, parts.path, _single(parse_qs(parts.query, keep_blank_values=True)),
                          form, {k.lower(): v for k, v in self.headers.items()})
            self._send(app.handle(req))

        def _send(self, resp: Response) -> None:
            self.send_response(resp.status)
            headers = dict(resp.headers)
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "same-origin", "Cache-Control": "no-store"})
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            self.wfile.write(resp.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(config_path: Path, cfg: AppConfig, host: str, port: int) -> int:
    Database.open(cfg.database).close()  # migrace před startem
    app = App(config_path, cfg.database)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    log.info("dashboard běží na http://%s:%d", host, port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("dashboard poslouchá na %s, bez Cloudflare Access ho uvidí každý v síti", host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
