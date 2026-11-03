"""Pruebas de las correcciones de la entrega del 50 %.

  1. Timing del login: el hash ficticio tenía otro costo y permitía enumerar usuarios.
  2. Cambio de IVA: no recalculaba neto ni IVA si el precio final era el mismo.
  3. Reintentos HTTP recuperados contaban como errores de la ejecución.
  4. Productos retirados de la tienda seguían en el catálogo público.
  5. Subidas graduales (bajo el umbral cada una) nunca se congelaban.
"""
import sqlite3

from flask import Response

from scrapbot import create_app
from scrapbot.db import connect, init_db, transaction
from scrapbot.engine.normalizer import normalize_record
from scrapbot.engine.store import apply_products
from scrapbot.util import now_str

from .helpers import AppTestCase, FakeSite, add_source, catalog_page, ean13, fmt_clp, product_card


def prod(price, sku="A1", vat=19):
    return normalize_record({"sku": sku, "name": "Taladro", "price": str(price)}, configured={"sku", "name", "price"},
                            includes_vat=True, vat_rate=vat, content_hash="h")


class StoreBase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sid = add_source(self.conn, "http://x/a")
        self.run_id = self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) "
                                        "VALUES (?, 'cli', ?, 'running')", (self.sid, now_str())).lastrowid

    def apply(self, *products, vat=19, **kw):
        with transaction(self.conn):
            return apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=list(products),
                                  threshold=15, vat_rate=vat, **kw)

    def product(self):
        return self.q("SELECT * FROM products WHERE sku='A1'")[0]


class LoginTimingTests(AppTestCase):
    """Bug 1."""

    def test_hash_ficticio_tiene_el_mismo_costo_que_los_reales(self):
        from scrapbot.auth import _dummy_hash
        with self.app.app_context():
            self.assertTrue(_dummy_hash().startswith(self.app.config["PASSWORD_HASH_METHOD"]))
        app = create_app({"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "x", "DISABLE_SCHEDULER": True})
        with app.app_context():
            self.assertTrue(_dummy_hash().startswith("pbkdf2:sha256:600000"))


class VatTests(StoreBase):
    """Bug 2."""

    def test_cambio_de_iva_recalcula_neto_e_iva(self):
        self.apply(prod(11900, vat=19))
        self.assertEqual((self.product()["net_price"], self.product()["vat_amount"]), (10000, 1900))
        res = self.apply(prod(11900, vat=10), vat=10)
        p = self.product()
        self.assertEqual((p["net_price"], p["vat_amount"], p["final_price"]), (10818, 1082, 11900))
        self.assertEqual(res.updated, 1)
        self.assertEqual(self.scalar("SELECT vat_rate FROM price_history ORDER BY id DESC LIMIT 1"), 10)


class DriftTests(StoreBase):
    """Bug 5: la variación se mide también contra el historial, no solo contra el último precio."""

    def test_subidas_graduales_se_congelan_para_revision(self):
        self.apply(prod(10000))
        frozen = [self.apply(prod(p)).frozen for p in (11400, 12996)]      # +14 % cada vez
        self.assertEqual(frozen, [0, 1])
        p = self.product()
        self.assertEqual((p["final_price"], p["price_frozen"]), (11400, 1))    # 12.996 queda retenido
        d = self.q("SELECT * FROM discrepancies")[0]
        self.assertEqual((d["status"], d["new_final_price"], d["variation_pct"]), ("pending", 12996, 29.96))

    def test_tras_aprobar_se_reinicia_la_comparacion(self):
        from scrapbot.engine.store import resolve_discrepancy
        self.apply(prod(10000))
        self.apply(prod(11400))
        self.apply(prod(12996))
        resolve_discrepancy(self.conn, self.scalar("SELECT id FROM discrepancies"), approve=True,
                            username="ana", justification="Alza de lista confirmada")
        self.assertEqual(self.apply(prod(13500)).frozen, 0)                   # +3,9 % desde lo aprobado

    def test_ventana_cero_desactiva_la_deteccion(self):
        self.apply(prod(10000))
        for p in (11400, 12996):
            self.apply(prod(p), drift_window_days=0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM discrepancies"), 0)


class PipelineFixTests(AppTestCase):
    """Bugs 3 y 4, con capturas reales contra un sitio local."""

    def setUp(self):
        super().setUp()
        self.site = FakeSite().start()
        self.addCleanup(self.site.stop)
        self.skus = ["A-1", "A-2", "A-3"]
        self.publish()
        self.sid = add_source(self.conn, self.site.base + "/catalogo")

    def publish(self):
        cards = [product_card(s, ean13("78012345" + f"{int(s[2:]):04d}"), f"Producto {s}", fmt_clp(10000), "Stock: 5")
                 for s in self.skus]
        self.site.routes["/catalogo"] = catalog_page(cards)

    def run_once(self):
        run_id = self.runner.start(self.sid, "cli", "tester")
        return self.q("SELECT * FROM runs WHERE id=?", run_id)[0]

    def test_reintento_recuperado_no_cuenta_como_error(self):
        calls = {"n": 0}
        page = self.site.routes["/catalogo"]

        def flaky():
            calls["n"] += 1
            return Response("caído", 503) if calls["n"] == 1 else Response(page, content_type="text/html")

        self.site.routes["/catalogo"] = flaky
        run = self.run_once()
        self.assertEqual((run["status"], run["errors_count"], run["items_found"]), ("success", 0, 3))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM http_log WHERE error IS NOT NULL"), 1)   # queda registrado

    def test_pagina_que_nunca_responde_si_cuenta_un_error(self):
        self.site.routes["/catalogo"] = lambda: Response("caído", 503)
        run = self.run_once()
        self.assertEqual((run["status"], run["errors_count"]), ("failed", 1))

    def test_producto_retirado_desaparece_del_catalogo_publico_y_vuelve_si_reaparece(self):
        self.run_once()
        self.skus = ["A-1", "A-2"]
        self.publish()
        self.run_once()
        self.assertIsNotNone(self.scalar("SELECT missing_since FROM products WHERE sku='A-3'"))
        page = self.app.test_client().get("/catalogo").get_data(as_text=True)
        self.assertIn("Producto A-1", page)
        self.assertNotIn("Producto A-3", page)
        self.skus = ["A-1", "A-2", "A-3"]
        self.publish()
        self.run_once()
        self.assertIsNone(self.scalar("SELECT missing_since FROM products WHERE sku='A-3'"))

    def test_captura_incompleta_no_retira_productos(self):
        self.run_once()
        self.site.routes["/catalogo"] = lambda: Response("caído", 503)
        self.run_once()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products WHERE missing_since IS NOT NULL"), 0)


class MigrationTests(AppTestCase):
    def test_base_de_la_version_anterior_se_actualiza_sin_perder_datos(self):
        path = self.db_path + ".old"
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE products (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, sku TEXT, ean TEXT, name TEXT NOT NULL,
              net_price INTEGER NOT NULL, vat_amount INTEGER NOT NULL, final_price INTEGER NOT NULL, stock INTEGER,
              available INTEGER, price_frozen INTEGER NOT NULL DEFAULT 0, content_hash TEXT, last_seen_at TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO products VALUES (1, 1, 'X', NULL, 'Viejo', 1, 1, 2, NULL, NULL, 0, NULL, 't', 't', 't');""")
        old.close()
        conn = connect(path)
        init_db(conn, {})
        self.assertIn("missing_since", {r["name"] for r in conn.execute("PRAGMA table_info(products)")})
        self.assertEqual(conn.execute("SELECT name FROM products").fetchone()[0], "Viejo")
        conn.close()
