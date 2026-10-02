"""Pruebas de aceptación: lote de 100 productos medido contra las métricas de los OE."""
import unittest

from .helpers import AppTestCase, FakeSite, add_source, catalog_page, ean13, fmt_clp, product_card


class BatchAcceptanceTests(AppTestCase):
    def test_lote_de_100_productos(self):
        site = FakeSite().start()
        self.addCleanup(site.stop)
        cards = [product_card(f"P-{i:03d}", ean13(f"7809000{i:05d}"), f"Producto {i}", fmt_clp(1000 + i * 137), f"Stock: {i % 17}")
                 for i in range(1, 101)]
        for page in range(5):
            chunk = cards[page * 20:(page + 1) * 20]
            nxt = f"/lote?page={page + 2}" if page < 4 else None
            site.routes["/lote" + (f"?page={page + 1}" if page else "")] = catalog_page(chunk, nxt)
        sid = add_source(self.conn, site.base + "/lote")

        run_id = self.runner.start(sid, "cli", "aceptacion")
        run = self.q("SELECT * FROM runs WHERE id=?", run_id)[0]
        self.assertEqual(run["status"], "success")
        self.assertEqual((run["pages_fetched"], run["items_found"], run["items_created"]), (5, 100, 100))
        self.assertEqual((run["items_invalid"], run["errors_count"]), (0, 0))                 # OE2: 0 % de error
        self.assertLess(run["duration_seconds"], 10 * 60)                                      # OE1: < 10 min por lote
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products WHERE net_price + vat_amount = final_price"), 100)

    def test_ejecuciones_repetidas_son_idempotentes(self):
        site = FakeSite().start()
        self.addCleanup(site.stop)
        site.routes["/c"] = catalog_page([product_card(f"P-{i}", ean13(f"7809000{i:05d}"), f"Prod {i}", "$9.990", "Stock: 2") for i in range(1, 11)])
        sid = add_source(self.conn, site.base + "/c")
        for _ in range(4):                                                                     # OE3: 4 ejecuciones
            self.runner.start(sid, "scheduled", "scheduler")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runs WHERE status='success'"), 4)


if __name__ == "__main__":
    unittest.main()
