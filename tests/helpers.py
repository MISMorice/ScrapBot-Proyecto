"""Utilidades compartidas por las pruebas: sitio falso local, app temporal y fábrica de cliente rápido."""
import logging
import os
import shutil
import tempfile
import threading
import unittest

from flask import Flask, Response, request
from werkzeug.serving import make_server

from scrapbot import create_app
from scrapbot.db import connect
from scrapbot.engine.http_client import PoliteHttpClient
from scrapbot.engine.pipeline import SyncRunner
from scrapbot.util import now_str

FAST_HASH = "pbkdf2:sha256:1000"


def ean13(body12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


class FakeSite:
    """Servidor HTTP local con rutas configurables, para no depender de internet."""

    def __init__(self):
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.routes, self.hits = {}, []
        app = Flask("fake-site")

        @app.route("/", defaults={"p": ""})
        @app.route("/<path:p>")
        def serve(p):
            path = "/" + p
            qs = request.query_string.decode()
            self.hits.append((path, qs, dict(request.headers)))
            handler = self.routes.get(path + ("?" + qs if qs else ""), self.routes.get(path))
            if handler is None:
                return Response("no encontrado", 404)
            if callable(handler):
                return handler()
            ctype = "text/plain" if path == "/robots.txt" else "text/html; charset=utf-8"
            return Response(handler, content_type=ctype)

        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(2)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def count(self, path):
        return sum(1 for h in self.hits if h[0] == path)


def product_card(sku, ean, name, price, stock):
    return (f'<article class="producto" data-sku="{sku}"><h2 class="nombre">{name}</h2>'
            f'<span class="ean">{ean}</span><span class="precio">{price}</span><span class="stock">{stock}</span></article>')


def catalog_page(cards, next_href=None):
    nxt = f'<a class="siguiente" href="{next_href}">Siguiente</a>' if next_href else ""
    return f"<!doctype html><html><body>{''.join(cards)}{nxt}</body></html>"


def fmt_clp(n):
    return "$" + f"{n:,}".replace(",", ".")


CSS_RULES = dict(selector_type="css", item_selector="article.producto", sku_selector="@data-sku",
                 ean_selector="span.ean", name_selector="h2.nombre", price_selector="span.precio",
                 stock_selector="span.stock", next_page_selector="a.siguiente", max_pages=20, price_includes_vat=1)


def add_source(conn, start_urls, name="Fuente test", respect_robots=1, **overrides):
    rules = {**CSS_RULES, **overrides}
    cur = conn.execute(
        "INSERT INTO sources (name, start_urls, active, respect_robots, delay_min, delay_max, created_at) "
        "VALUES (?,?,1,?,1,2,?)", (name, start_urls, respect_robots, now_str()))
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO source_rules (source_id, version, selector_type, item_selector, sku_selector, ean_selector, "
        "name_selector, price_selector, stock_selector, next_page_selector, max_pages, price_includes_vat, is_current, updated_at) "
        "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,1,?)",
        (sid, rules["selector_type"], rules["item_selector"], rules["sku_selector"], rules["ean_selector"],
         rules["name_selector"], rules["price_selector"], rules["stock_selector"], rules["next_page_selector"],
         rules["max_pages"], rules["price_includes_vat"], now_str()))
    return sid


def fast_client_factory(max_retries=2):
    def factory(source, on_attempt):
        return PoliteHttpClient(user_agent="ScrapBot/test (pruebas)", timeout=2, max_retries=max_retries,
                                delay_range=(0, 0), sleep=lambda s: None, on_attempt=on_attempt,
                                respect_robots=bool(source["respect_robots"]))
    return factory


class AppTestCase(unittest.TestCase):
    """App Flask con base de datos temporal. `self.conn` es una conexión directa a esa base."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scrapbot-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.app = create_app({
            "TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "test-secret",
            "PASSWORD_HASH_METHOD": FAST_HASH, "DISABLE_SCHEDULER": True,
            "CLIENT_FACTORY": fast_client_factory(), "RUN_SYNCHRONOUSLY": True,
        })
        self.conn = connect(self.db_path)
        self.runner = SyncRunner(dict(self.app.config), self.db_path, client_factory=fast_client_factory())

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def scalar(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]
