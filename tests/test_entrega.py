"""Pruebas de la entrega 80-100 % (las correcciones del 50 % están en test_correcciones.py).

Funcionalidades
  * Login con selector Usuario / Administrador
  * Edición de precio y stock (solo acceso administrador)
  * Automatización: captura cada N minutos y alertas instantáneas sin intervención
  * Exportación de la auditoría de cambios de precio y de la bitácora
"""
import csv
import io
import sqlite3
from datetime import datetime


from scrapbot.db import connect, init_db, set_setting, transaction
from scrapbot.engine.normalizer import normalize_record
from scrapbot.engine.scheduler import IntradayScheduler
from scrapbot.engine.store import apply_products
from scrapbot.util import now_str

from .helpers import AppTestCase, FakeSite, add_source, catalog_page, ean13, fmt_clp, product_card
from .test_web import PASSWORD, WebTestCase


def prod(price, sku="A1", vat=19):
    return normalize_record({"sku": sku, "name": "Taladro", "price": str(price)}, configured={"sku", "name", "price"},
                            includes_vat=True, vat_rate=vat, content_hash="h")


class StoreBase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sid = add_source(self.conn, "http://x/a")
        self.run_id = self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) "
                                        "VALUES (?, 'cli', ?, 'running')", (self.sid, now_str())).lastrowid

    def apply(self, *products, vat=19, mode="auto", threshold=15):
        with transaction(self.conn):
            return apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=list(products),
                                  threshold=threshold, vat_rate=vat, mode=mode)

    def product(self, sku="A1"):
        return self.q("SELECT * FROM products WHERE sku=?", sku)[0]


# --- Alertas automáticas (publicar al instante y alertar) -------------------------------

class AutoAlertTests(StoreBase):
    def test_modo_automatico_publica_y_alerta_sin_intervencion(self):
        self.apply(prod(10000))
        res = self.apply(prod(20000))                              # +100 %
        self.assertEqual((res.updated, res.frozen, res.alerted), (1, 0, 1))
        p = self.product()
        self.assertEqual((p["final_price"], p["price_frozen"]), (20000, 0))   # publicado de inmediato
        a = self.q("SELECT * FROM price_alerts")[0]
        self.assertEqual((a["kind"], a["reference_price"], a["new_final_price"], a["variation_pct"]),
                         ("salto", 10000, 20000, 100.0))
        self.assertIsNone(a["seen_at"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM discrepancies"), 0)

    def test_subidas_graduales_generan_una_alerta_acumulada(self):
        self.apply(prod(10000))
        alerts = [self.apply(prod(p)).alerted for p in (11400, 12996, 14815)]   # +14 % cada vez
        self.assertEqual(alerts, [0, 1, 0])           # se alerta al cruzar el umbral y no se repite
        a = self.q("SELECT * FROM price_alerts")[0]
        self.assertEqual((a["kind"], a["reference_price"], a["new_final_price"]), ("acumulada", 10000, 12996))
        self.assertEqual(self.product()["final_price"], 14815)

    def test_ventana_cero_desactiva_la_deteccion_acumulada(self):
        self.apply(prod(10000))
        with transaction(self.conn):
            for p in (11400, 12996):
                apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(p)],
                               threshold=15, vat_rate=19, mode="auto", drift_window_days=0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_alerts"), 0)

    def test_modo_revision_sigue_disponible(self):
        self.apply(prod(10000), mode="review")
        res = self.apply(prod(20000), mode="review")
        self.assertEqual((res.frozen, res.alerted), (1, 0))
        self.assertEqual(self.product()["final_price"], 10000)


# --- Alerta automática en una captura real ----------------------------------------------

class PipelineAlertTests(AppTestCase):
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

    def test_alerta_automatica_en_una_captura_real(self):
        self.run_once()
        self.site.routes["/catalogo"] = self.site.routes["/catalogo"].replace("$10.000", "$15.000", 1)
        run = self.run_once()
        self.assertEqual((run["items_alerted"], run["items_frozen"]), (1, 0))
        self.assertEqual(self.scalar("SELECT final_price FROM products WHERE sku='A-1'"), 15000)


# --- Login con selector ------------------------------------------------------------

class AccessModeTests(WebTestCase):
    def login_as(self, username, access, role, expected=302):
        if not self.q("SELECT 1 FROM users WHERE username=?", username):
            self.make_user(username, role)
        client = self.app.test_client()
        client.get("/login")
        res = self.post(client, "/login", {"username": username, "password": PASSWORD, "access": access})
        self.assertEqual(res.status_code, expected, res.data[:300])
        return client, res

    def test_pantalla_de_login_tiene_ambos_accesos(self):
        page = self.app.test_client().get("/login").get_data(as_text=True)
        self.assertIn('value="user"', page)
        self.assertIn('value="admin"', page)

    def test_usuario_sin_rol_admin_no_entra_por_el_acceso_administrador(self):
        _, res = self.login_as("ana", "admin", "analyst", expected=403)
        self.assertIn("no tiene permisos de administrador", res.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT failed_attempts FROM users WHERE username='ana'"), 0)
        self.assertEqual(self.scalar("SELECT reason FROM login_log ORDER BY id DESC LIMIT 1"), "sin permiso de administrador")

    def test_admin_que_entra_como_usuario_no_puede_editar(self):
        client, _ = self.login_as("jefa", "user", "admin")
        self.assertEqual(client.get("/settings").status_code, 403)
        self.assertNotIn("Parámetros", client.get("/").get_data(as_text=True))

    def test_admin_por_acceso_administrador_ve_la_administracion(self):
        client, _ = self.login_as("jefa", "admin", "admin")
        self.assertEqual(client.get("/settings").status_code, 200)
        self.assertIn("Acceso administrador", client.get("/").get_data(as_text=True))


# --- Edición de precio y stock --------------------------------------------------------

class ProductEditTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.sid = add_source(self.conn, "http://x/a")
        self.run_id = self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) "
                                        "VALUES (?, 'cli', ?, 'running')", (self.sid, now_str())).lastrowid
        with transaction(self.conn):
            apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(11900)],
                           threshold=15, vat_rate=19, mode="auto")
        self.pid = self.scalar("SELECT id FROM products")

    def edit(self, client, **data):
        base = {"final_price": "$12.990", "stock": "8", "justification": "Precio de lista nuevo", "keep_manual": "on"}
        return self.post(client, f"/products/{self.pid}/edit", {**base, **data})

    def test_admin_edita_precio_y_stock_con_historial_y_auditoria(self):
        client = self.login()
        self.assertIn("Editar precio y stock", client.get(f"/products/{self.pid}").get_data(as_text=True))
        self.assertEqual(self.edit(client).status_code, 302)
        p = self.q("SELECT * FROM products")[0]
        self.assertEqual((p["final_price"], p["net_price"], p["vat_amount"], p["stock"], p["available"], p["manual_override"]),
                         (12990, 10916, 2074, 8, 1, 1))
        h = self.q("SELECT * FROM price_history ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((h["changed_by"], h["note"], h["run_id"]), ("admin", "Precio de lista nuevo", None))
        self.assertIn("11900->12990", self.scalar("SELECT detail FROM audit_log WHERE action='product.edit'"))

    def test_usuario_no_ve_ni_puede_usar_la_edicion(self):
        client = self.login("ana", role="analyst")
        self.assertNotIn("Editar precio y stock", client.get(f"/products/{self.pid}").get_data(as_text=True))
        self.assertEqual(self.edit(client).status_code, 403)
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 11900)

    def test_validaciones(self):
        client = self.login()
        for data, msg in (({"final_price": "gratis"}, "Precio"), ({"stock": "-3"}, "número entero"),
                          ({"justification": "x"}, "motivo")):
            with self.subTest(data=data):
                res = self.edit(client, **data)
                self.assertEqual(res.status_code, 400)
                self.assertIn(msg, res.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 11900)

    def test_precio_fijado_no_lo_sobrescribe_el_bot_hasta_liberarlo(self):
        client = self.login()
        self.edit(client)
        with transaction(self.conn):
            res = apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(9990)],
                                 threshold=15, vat_rate=19, mode="auto")
        self.assertEqual((res.locked, self.scalar("SELECT final_price FROM products")), (1, 12990))
        self.post(client, f"/products/{self.pid}/release")
        with transaction(self.conn):
            apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(9990)],
                           threshold=15, vat_rate=19, mode="auto")
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 9990)

    def test_sin_fijar_el_bot_vuelve_a_actualizarlo(self):
        client = self.login()
        self.post(client, f"/products/{self.pid}/edit",
                  {"final_price": "12990", "stock": "", "justification": "Promoción puntual"})
        self.assertEqual(self.scalar("SELECT manual_override FROM products"), 0)


# --- Exportaciones ---------------------------------------------------------------------

class ExportTests(ProductEditTests):
    def rows(self, client, url):
        text = client.get(url).get_data(as_text=True).lstrip("﻿")
        return list(csv.DictReader(io.StringIO(text), delimiter=";"))

    def test_auditoria_de_cambios_de_precio(self):
        client = self.login()
        self.edit(client)
        with transaction(self.conn):
            apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(11900, sku="B2")],
                           threshold=15, vat_rate=19, mode="auto")
        rows = self.rows(client, "/export/price-changes.csv")
        manual = [r for r in rows if r["origen"] == "Edición manual"][0]
        self.assertEqual((manual["precio_anterior"], manual["precio_nuevo"], manual["usuario"], manual["motivo"]),
                         ("11900", "12990", "admin", "Precio de lista nuevo"))
        self.assertEqual(manual["variacion_pct"], "9,16")
        self.assertTrue(any(r["origen"].startswith("Alta") for r in rows))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='price_audit.export'"), 1)

    def test_alerta_aparece_en_la_auditoria(self):
        with transaction(self.conn):
            apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=[prod(30000)],
                           threshold=15, vat_rate=19, mode="auto")
        rows = self.rows(self.login(), "/export/price-changes.csv")
        self.assertEqual(rows[-1]["alerta"], "salto")
        self.assertEqual(rows[-1]["origen"], "Bot (captura automática)")

    def test_filtro_de_fechas(self):
        client = self.login()
        self.assertEqual(self.rows(client, "/export/price-changes.csv?desde=2000-01-01&hasta=2000-01-02"), [])
        self.assertEqual(client.get("/export/price-changes.csv?desde=ayer").status_code, 400)

    def test_bitacora_completa(self):
        client = self.login()
        self.edit(client)
        rows = self.rows(client, "/export/audit.csv?action=product")
        self.assertEqual([r["accion"] for r in rows], ["product.edit"])


# --- Automatización ------------------------------------------------------------------

class AutomationTests(WebTestCase):
    def test_programador_por_intervalo_dispara_una_vez_por_bloque(self):
        class Runner:
            calls = []

            def start(self, sid, trigger="manual", user=None, background=False):
                self.calls.append(sid)
                return len(self.calls)

        add_source(self.conn, "http://x/a")
        set_setting(self.conn, "interval_minutes", "15")
        sched = IntradayScheduler(dict(self.app.config), self.db_path, Runner())
        t = datetime(2026, 10, 23, 10, 1, tzinfo=sched.tz)
        self.assertEqual(len(sched.check(t)), 1)
        self.assertEqual(sched.check(t.replace(minute=14)), [])       # mismo bloque 10:00-10:15
        self.assertEqual(len(sched.check(t.replace(minute=16))), 1)   # bloque siguiente

    def test_aviso_en_vivo_y_marcar_revisada(self):
        sid = add_source(self.conn, "http://x/a")
        run_id = self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) "
                                   "VALUES (?, 'cli', ?, 'running')", (sid, now_str())).lastrowid
        with transaction(self.conn):
            for price in (10000, 20000):
                apply_products(self.conn, run_id=run_id, source_id=sid, products=[prod(price)],
                               threshold=15, vat_rate=19, mode="auto")
        client = self.login("ana", role="analyst")
        data = client.get("/api/alerts/summary?after=0").get_json()
        self.assertEqual((data["unseen"], len(data["new"]), data["new"][0]["price"]), (1, 1, "$20.000"))
        self.assertEqual(client.get(f"/api/alerts/summary?after={data['last_id']}").get_json()["new"], [])
        self.assertIn("Cambio brusco", client.get("/alerts").get_data(as_text=True))
        aid = self.scalar("SELECT id FROM price_alerts")
        self.post(client, f"/alerts/{aid}/ack")
        self.assertEqual(self.scalar("SELECT seen_by FROM price_alerts"), "ana")

    def test_parametros_de_automatizacion(self):
        client = self.login()
        base = {"vat_rate": "19", "variation_threshold": "15", "schedule_times": "08:00", "scheduler_enabled": "on",
                "schedule_mode": "interval", "interval_minutes": "3", "alert_mode": "auto", "drift_window_days": "7"}
        self.assertIn("entre 5 y 1440", self.post(client, "/settings", base).get_data(as_text=True))
        self.post(client, "/settings", {**base, "interval_minutes": "10", "alert_mode": "review"})
        self.assertEqual(self.scalar("SELECT value FROM settings WHERE key='interval_minutes'"), "10")
        self.assertEqual(self.scalar("SELECT value FROM settings WHERE key='alert_mode'"), "review")


# --- Migración de bases existentes ----------------------------------------------------------

class MigrationTests(AppTestCase):
    def test_base_del_50_por_ciento_recibe_las_columnas_nuevas(self):
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
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        self.assertTrue({"manual_override", "missing_since"} <= cols)
        self.assertEqual(conn.execute("SELECT name, manual_override FROM products").fetchone()[:], ("Viejo", 0))
        conn.close()
