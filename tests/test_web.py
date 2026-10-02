import re

from scrapbot.engine.normalizer import NormalizedProduct
from scrapbot.db import transaction
from scrapbot.engine.store import apply_products
from scrapbot.util import now_str, to_str, utcnow
from datetime import timedelta
from werkzeug.security import generate_password_hash

from .helpers import AppTestCase, FakeSite, FAST_HASH, add_source, catalog_page, ean13, fmt_clp, product_card

PASSWORD = "ClaveSegura123"


class WebTestCase(AppTestCase):
    def make_user(self, username, role="admin", password=PASSWORD):
        self.conn.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                          (username, generate_password_hash(password, method=FAST_HASH), role, now_str()))

    def token(self, client):
        """Como un navegador: el token existe una vez que se cargó una página con formulario."""
        for warmup in (None, "/", "/login"):
            if warmup:
                client.get(warmup)
            with client.session_transaction() as s:
                if s.get("_csrf"):
                    return s["_csrf"]
        return ""

    def post(self, client, url, data=None, **kw):
        return client.post(url, data={**(data or {}), "_csrf": self.token(client)}, **kw)

    def login(self, username="admin", password=PASSWORD, role="admin"):
        if not self.q("SELECT 1 FROM users WHERE username=?", username):
            self.make_user(username, role)
        client = self.app.test_client()
        client.get("/login")
        res = self.post(client, "/login", {"username": username, "password": password})
        self.assertEqual(res.status_code, 302, res.data[:300])
        return client


class AuthTests(WebTestCase):
    def test_acceso_denegado_sin_sesion(self):
        client = self.app.test_client()
        for url in ("/", "/products", "/sources", "/discrepancies", "/audit", "/export/catalog.csv"):
            with self.subTest(url=url):
                res = client.get(url)
                self.assertEqual(res.status_code, 302)
                self.assertIn("/login", res.headers["Location"])

    def test_login_correcto_y_registro_de_acceso(self):
        client = self.login()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM login_log WHERE success=1 AND username='admin'"), 1)

    def test_credenciales_incorrectas_no_revelan_cual_dato_fallo(self):
        self.make_user("admin")
        client = self.app.test_client()
        client.get("/login")
        r1 = self.post(client, "/login", {"username": "admin", "password": "mala"})
        r2 = self.post(client, "/login", {"username": "noexiste", "password": "mala"})
        self.assertEqual((r1.status_code, r2.status_code), (401, 401))
        self.assertEqual(re.findall(r"incorrectos", r1.get_data(as_text=True)), re.findall(r"incorrectos", r2.get_data(as_text=True)))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM login_log WHERE success=0"), 2)

    def test_bloqueo_tras_intentos_fallidos_y_desbloqueo(self):
        self.make_user("admin")
        client = self.app.test_client()
        client.get("/login")
        codes = [self.post(client, "/login", {"username": "admin", "password": "mala"}).status_code for _ in range(5)]
        self.assertEqual(codes, [401, 401, 401, 401, 423])
        self.assertEqual(self.post(client, "/login", {"username": "admin", "password": PASSWORD}).status_code, 423)  # ni con la clave correcta
        self.conn.execute("UPDATE users SET locked_until=? WHERE username='admin'", (to_str(utcnow() - timedelta(minutes=1)),))
        self.assertEqual(self.post(client, "/login", {"username": "admin", "password": PASSWORD}).status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='auth.lockout'"), 1)

    def test_sesion_expira_por_inactividad(self):
        client = self.login()
        with client.session_transaction() as s:
            s["last_seen"] -= self.app.config["SESSION_INACTIVITY_SECONDS"] + 5
        res = client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers["Location"])
        self.assertEqual(client.get("/products").status_code, 302)

    def test_csrf_obligatorio_en_post(self):
        client = self.login()
        self.assertEqual(client.post("/logout", data={}).status_code, 400)
        self.assertEqual(client.post("/logout", data={"_csrf": "falso"}).status_code, 400)
        self.assertEqual(self.post(client, "/logout").status_code, 302)

    def test_no_hay_redireccion_abierta(self):
        self.make_user("admin")
        client = self.app.test_client()
        client.get("/login")
        res = self.post(client, "/login?next=https://malo.example/", {"username": "admin", "password": PASSWORD})
        self.assertEqual(res.headers["Location"], "/")

    def test_contrasena_no_se_guarda_en_claro(self):
        self.make_user("admin")
        stored = self.scalar("SELECT password_hash FROM users")
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(stored.startswith("pbkdf2:"))

    def test_analista_no_accede_a_administracion(self):
        client = self.login("ana", role="analyst")
        for url in ("/settings", "/users", "/access-log", "/sources/new"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)
        self.assertEqual(self.post(client, "/sources/new", {"name": "x"}).status_code, 403)
        self.assertEqual(client.get("/discrepancies").status_code, 200)

    def test_cabeceras_de_seguridad(self):
        res = self.login().get("/")
        self.assertIn("default-src 'self'", res.headers["Content-Security-Policy"])
        self.assertEqual(res.headers["X-Frame-Options"], "DENY")


class PublicCatalogTests(WebTestCase):
    def test_publico_muestra_oferta_y_enlace_a_tienda_sin_abrir_inventario(self):
        site = FakeSite().start()
        self.addCleanup(site.stop)
        site.routes["/catalogo"] = catalog_page([
            product_card("CAT-1", ean13("780123456789"), "Lámpara de prueba", "$12.990", "Stock: 4")
        ])
        source_id = add_source(self.conn, site.base + "/catalogo", name="Tienda Externa")
        self.runner.start(source_id, "cli", "public-test")

        client = self.app.test_client()
        response = client.get("/catalogo")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Lámpara de prueba", html)
        self.assertIn(site.base + "/catalogo", html)
        self.assertIn("Ver tienda", html)
        self.assertEqual(client.get("/products").status_code, 302)


class SourceTests(WebTestCase):
    FORM = {"name": "Proveedor Uno", "start_urls": "https://proveedor.example/catalogo", "delay_min": "2", "delay_max": "5",
            "respect_robots": "on", "selector_type": "css", "item_selector": "div.item", "sku_selector": "@data-sku",
            "name_selector": "h2", "price_selector": ".precio", "stock_selector": ".stock", "next_page_selector": "a.next",
            "max_pages": "10", "price_includes_vat": "on"}

    def test_crear_fuente_valida(self):
        client = self.login()
        res = self.post(client, "/sources/new", self.FORM)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 1)
        self.assertEqual(self.scalar("SELECT version FROM source_rules WHERE is_current=1"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='source.create'"), 1)

    def test_rechaza_selector_con_sintaxis_invalida_y_urls_malas(self):
        client = self.login()
        for cambio, campo in [({"price_selector": "div["}, "Selector CSS inválido"),
                              ({"start_urls": "ftp://servidor/archivo"}, "URL inválida"),
                              ({"start_urls": "no es una url"}, "URL inválida"),
                              ({"sku_selector": ""}, "al menos un selector de identificador"),
                              ({"delay_min": "0"}, "pausa entre peticiones")]:
            with self.subTest(cambio=cambio):
                res = self.post(client, "/sources/new", {**self.FORM, **cambio})
                self.assertEqual(res.status_code, 200)
                self.assertIn(campo, res.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 0)

    def test_editar_reglas_crea_nueva_version_y_deja_una_vigente(self):
        client = self.login()
        self.post(client, "/sources/new", self.FORM)
        self.post(client, "/sources/1", {**self.FORM, "price_selector": ".nuevo-precio"})
        rows = self.q("SELECT version, is_current, price_selector, updated_by FROM source_rules ORDER BY version")
        self.assertEqual([(r["version"], r["is_current"]) for r in rows], [(1, 0), (2, 1)])
        self.assertEqual((rows[1]["price_selector"], rows[1]["updated_by"]), (".nuevo-precio", "admin"))
        self.post(client, "/sources/1", {**self.FORM, "price_selector": ".nuevo-precio"})      # sin cambios de reglas
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM source_rules"), 2)

    def test_nombre_duplicado(self):
        client = self.login()
        self.post(client, "/sources/new", self.FORM)
        res = self.post(client, "/sources/new", self.FORM)
        self.assertIn("Ya existe una fuente", res.get_data(as_text=True))

    def test_ejecutar_desde_el_panel_y_ver_resultados(self):
        site = FakeSite().start()
        self.addCleanup(site.stop)
        site.routes["/c"] = catalog_page([product_card("Z-1", ean13("780000000001"), "Martillo &lt;script&gt;alert(1)&lt;/script&gt;", "$11.900", "Stock: 3")])
        sid = add_source(self.conn, site.base + "/c", respect_robots=0)
        client = self.login()
        res = self.post(client, f"/sources/{sid}/run")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/runs/", res.headers["Location"])
        self.assertEqual(self.scalar("SELECT status FROM runs"), "success")
        page = client.get("/products").get_data(as_text=True)
        self.assertIn("$11.900", page)
        self.assertIn("&lt;script&gt;alert(1)", page)      # el texto capturado se escapa: no hay XSS almacenado
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("Ejecución #1", client.get(res.headers["Location"]).get_data(as_text=True))


class DiscrepancyWebTests(WebTestCase):
    def setUp(self):
        super().setUp()
        sid = add_source(self.conn, "http://x/")
        run_id = self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) VALUES (?, 'cli', ?, 'success')",
                                   (sid, now_str())).lastrowid

        def prod(final):
            net = round(final / 1.19)
            return NormalizedProduct("Q-1", None, "=HYPERLINK(\"http://malo\")", net, final - net, final, 3, True, "h")

        for final in (10000, 30000):
            with transaction(self.conn):
                apply_products(self.conn, run_id=run_id, source_id=sid, products=[prod(final)], threshold=15, vat_rate=19)
        self.did = self.scalar("SELECT id FROM discrepancies")

    def test_flujo_de_revision_con_justificacion_obligatoria(self):
        client = self.login("ana", role="analyst")
        page = client.get("/discrepancies").get_data(as_text=True)
        self.assertIn("+200.0 %", page)
        res = self.post(client, f"/discrepancies/{self.did}/resolve", {"decision": "approve", "justification": ""}, follow_redirects=True)
        self.assertIn("justificación es obligatoria", res.get_data(as_text=True))
        self.assertEqual(self.scalar("SELECT status FROM discrepancies"), "pending")
        self.post(client, f"/discrepancies/{self.did}/resolve", {"decision": "approve", "justification": "Nuevo precio de lista confirmado"})
        self.assertEqual(self.scalar("SELECT status FROM discrepancies"), "approved")
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 30000)
        auditoria = client.get("/audit?action=discrepancy").get_data(as_text=True)
        self.assertIn("ana", auditoria)
        self.assertIn("Nuevo precio de lista confirmado", auditoria)

    def test_decision_invalida(self):
        client = self.login()
        self.assertEqual(self.post(client, f"/discrepancies/{self.did}/resolve", {"decision": "borrar", "justification": "xxxxxx"}).status_code, 400)

    def test_panel_muestra_alerta_pendiente(self):
        client = self.login()
        self.assertIn("variaciones esperando revisión", client.get("/").get_data(as_text=True))

    def test_exportacion_csv_neutraliza_formulas(self):
        csv_text = self.login().get("/export/catalog.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertNotIn(",=HYPERLINK", csv_text)


class AdminTests(WebTestCase):
    def test_parametros_validados_y_auditados(self):
        client = self.login()
        bad = self.post(client, "/settings", {"vat_rate": "19", "variation_threshold": "0", "schedule_times": "25:00"})
        self.assertIn("umbral debe estar", bad.get_data(as_text=True))
        self.assertIn("Hora inválida", bad.get_data(as_text=True))
        ok = self.post(client, "/settings", {"vat_rate": "19", "variation_threshold": "10", "schedule_times": "07:00, 19:00", "scheduler_enabled": "on"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(self.scalar("SELECT value FROM settings WHERE key='variation_threshold'"), "10.0")
        self.assertEqual(self.scalar("SELECT value FROM settings WHERE key='schedule_times'"), "07:00,19:00")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='settings.update'"), 1)

    def test_crear_usuario_valida_contrasena(self):
        client = self.login()
        res = self.post(client, "/users", {"username": "nuevo", "role": "analyst", "password": "corta1"})
        self.assertIn("al menos 10 caracteres", res.get_data(as_text=True))
        self.post(client, "/users", {"username": "nuevo", "role": "analyst", "password": "UnaClaveLarga99"})
        self.assertEqual(self.scalar("SELECT role FROM users WHERE username='nuevo'"), "analyst")
        self.assertEqual(self.login("nuevo", "UnaClaveLarga99", "analyst").get("/").status_code, 200)

    def test_registro_de_accesos_solo_admin(self):
        self.assertEqual(self.login().get("/access-log").status_code, 200)
