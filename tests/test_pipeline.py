import unittest
from unittest import mock

from scrapbot.engine.pipeline import RunInProgress
from scrapbot.engine.store import apply_products as real_apply
from scrapbot.util import now_str

from .helpers import (AppTestCase, FakeSite, add_source, catalog_page, ean13, fmt_clp, product_card)


class PipelineTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.site = FakeSite().start()
        self.addCleanup(self.site.stop)
        self.prices = {"A-1": 11900, "A-2": 23800, "A-3": 5950}
        self.stock = {"A-1": 5, "A-2": 0, "A-3": 12}
        self.publish()
        self.sid = add_source(self.conn, self.site.base + "/catalogo")

    def cards(self, skus):
        return [product_card(s, self.ean(s), f"Producto {s}", fmt_clp(self.prices[s]),
                             "Agotado" if self.stock[s] == 0 else f"Stock: {self.stock[s]}")
                for s in skus]

    @staticmethod
    def ean(sku):
        return ean13("78012345" + f"{int(sku.split('-')[1]):04d}")

    def publish(self):
        self.site.routes["/catalogo"] = catalog_page(self.cards(["A-1", "A-2"]), "/catalogo?page=2")
        self.site.routes["/catalogo?page=2"] = catalog_page(self.cards(["A-3"]))

    def run_once(self):
        run_id = self.runner.start(self.sid, "cli", "tester")
        return self.q("SELECT * FROM runs WHERE id=?", run_id)[0]

    def test_primera_ejecucion_crea_el_catalogo(self):
        run = self.run_once()
        self.assertEqual((run["status"], run["pages_fetched"], run["items_found"], run["items_created"]), ("success", 2, 3, 3))
        p = self.q("SELECT * FROM products WHERE sku='A-1'")[0]
        self.assertEqual((p["net_price"], p["vat_amount"], p["final_price"], p["stock"]), (10000, 1900, 11900, 5))
        self.assertEqual(len(p["content_hash"]), 64)
        self.assertEqual(self.q("SELECT available FROM products WHERE sku='A-2'")[0][0], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM http_log WHERE content_sha256 IS NOT NULL"), 3)  # robots + 2 páginas
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='catalog.commit'"), 1)
        self.assertIsNotNone(run["duration_seconds"])

    def test_segunda_ejecucion_sin_cambios_no_toca_el_historial(self):
        self.run_once()
        run = self.run_once()
        self.assertEqual((run["items_unchanged"], run["items_updated"], run["items_created"]), (3, 0, 0))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history"), 3)

    def test_cambio_menor_se_publica_y_cambio_mayor_se_congela(self):
        self.run_once()
        self.prices["A-1"] = 12500      # +5,0 %
        self.prices["A-3"] = 11900      # +100 %
        self.publish()
        run = self.run_once()
        self.assertEqual((run["items_updated"], run["items_frozen"]), (1, 1))
        self.assertEqual(self.q("SELECT final_price FROM products WHERE sku='A-1'")[0][0], 12500)
        a3 = self.q("SELECT * FROM products WHERE sku='A-3'")[0]
        self.assertEqual((a3["final_price"], a3["price_frozen"]), (5950, 1))
        d = self.q("SELECT * FROM discrepancies")[0]
        self.assertEqual((d["status"], d["new_final_price"], d["variation_pct"]), ("pending", 11900, 100.0))

    def test_registros_invalidos_y_duplicados_dejan_la_ejecucion_con_observaciones(self):
        cards = self.cards(["A-1", "A-2"]) + [
            product_card("B-1", ean13("780999999001"), "Sin precio", "Consultar", "Stock: 1"),
            product_card("A-1", self.ean("A-1"), "Duplicado", "$11.900", "Stock: 5"),
            product_card("B-2", "1234567890123", "EAN malo", "$7.000", "Stock: 2"),
        ]
        self.site.routes["/catalogo"] = catalog_page(cards)
        run = self.run_once()
        self.assertEqual(run["status"], "partial")
        self.assertEqual((run["items_found"], run["items_invalid"], run["items_duplicates"], run["items_created"]), (5, 1, 1, 3))
        fields = {r["field"] for r in self.q("SELECT field FROM parse_errors")}
        self.assertEqual(fields, {"price", "ean"})
        self.assertIsNone(self.q("SELECT ean FROM products WHERE sku='B-2'")[0][0])

    def test_cambio_de_dom_se_detecta_y_no_corrompe_el_catalogo(self):          # riesgo R1
        self.run_once()
        self.site.routes["/catalogo"] = "<html><body><div class='nuevo-diseno'>otra estructura</div></body></html>"
        run = self.run_once()
        self.assertEqual(run["status"], "failed")
        self.assertIn("selectores", run["message"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM parse_errors WHERE field='item_selector'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 3)

    def test_fuente_caida_registra_reintentos(self):                             # riesgo R2
        self.site.routes["/catalogo"] = lambda: __import__("flask").Response("caído", 503)
        run = self.run_once()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM http_log WHERE url LIKE '%/catalogo' AND status_code=503"), 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 0)

    def test_robots_txt_impide_la_captura(self):
        self.site.routes["/robots.txt"] = "User-agent: *\nDisallow: /catalogo\n"
        run = self.run_once()
        self.assertEqual((run["status"], run["pages_fetched"]), ("failed", 0))
        self.assertEqual(self.site.count("/catalogo"), 0)

    def test_paginacion_circular_y_dominios_externos(self):
        self.site.routes["/catalogo?page=2"] = catalog_page(self.cards(["A-3"]), "/catalogo")          # vuelve a la 1
        run = self.run_once()
        self.assertEqual(run["pages_fetched"], 2)
        self.site.routes["/catalogo"] = catalog_page(self.cards(["A-1"]), "http://otro-sitio.example/catalogo")
        run = self.run_once()
        self.assertEqual(run["pages_fetched"], 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM parse_errors WHERE field='next_page'"), 1)

    def test_rollback_total_si_falla_la_escritura(self):                          # riesgo R4
        def failing(conn, **kw):
            real_apply(conn, **{**kw, "products": kw["products"][:2]})
            raise RuntimeError("corte simulado")

        with mock.patch("scrapbot.engine.pipeline.apply_products", side_effect=failing):
            run = self.run_once()
        self.assertEqual(run["status"], "failed")
        self.assertIn("revirtieron", run["message"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history"), 0)
        self.assertGreater(self.scalar("SELECT COUNT(*) FROM http_log"), 0)     # la bitácora sí persiste

    def test_no_permite_dos_ejecuciones_simultaneas_de_la_misma_fuente(self):
        self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) VALUES (?, 'manual', ?, 'running')",
                          (self.sid, now_str()))
        with self.assertRaises(RunInProgress):
            self.runner.create_run(self.sid, "manual", "x")

    def test_ejecucion_colgada_antigua_se_marca_como_fallida(self):
        self.conn.execute("INSERT INTO runs (source_id, trigger, started_at, status) VALUES (?, 'manual', '2020-01-01 00:00:00', 'running')",
                          (self.sid,))
        run = self.run_once()
        self.assertEqual(run["status"], "success")
        self.assertEqual(self.scalar("SELECT status FROM runs WHERE id=1"), "failed")

    def test_precio_sin_iva_se_recarga(self):
        self.conn.execute("UPDATE source_rules SET price_includes_vat=0 WHERE source_id=?", (self.sid,))
        self.prices = {"A-1": 10000, "A-2": 20000, "A-3": 5000}
        self.publish()
        self.run_once()
        p = self.q("SELECT * FROM products WHERE sku='A-1'")[0]
        self.assertEqual((p["net_price"], p["vat_amount"], p["final_price"]), (10000, 1900, 11900))


if __name__ == "__main__":
    unittest.main()
