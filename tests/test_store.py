import unittest
from types import SimpleNamespace

from scrapbot.db import transaction
from scrapbot.engine.detector import evaluate_change
from scrapbot.engine.normalizer import NormalizedProduct
from scrapbot.engine.store import apply_products, resolve_discrepancy

from .helpers import AppTestCase, add_source


def prod(sku="A-1", final=10000, stock=5, ean=None, name="Producto"):
    net = round(final / 1.19)
    return NormalizedProduct(sku=sku, ean=ean, name=name, net_price=net, vat_amount=final - net,
                             final_price=final, stock=stock, available=stock > 0, content_hash="h")


class DetectorTests(unittest.TestCase):
    def test_umbral(self):
        self.assertEqual(evaluate_change(10000, 10000, 15).action, "unchanged")
        self.assertEqual(evaluate_change(10000, 11000, 15).action, "apply")
        self.assertEqual(evaluate_change(10000, 11500, 15).action, "apply")     # exactamente 15 %: pasa
        self.assertEqual(evaluate_change(10000, 11501, 15).action, "freeze")
        self.assertEqual(evaluate_change(10000, 8000, 15).action, "freeze")     # bajas también
        self.assertEqual(evaluate_change(10000, 8000, 15).pct, 20.0)

    def test_precio_publicado_cero(self):
        self.assertEqual(evaluate_change(0, 500, 15).action, "freeze")


class StoreTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sid = add_source(self.conn, "http://x/")
        self.run_id = self.conn.execute(
            "INSERT INTO runs (source_id, trigger, started_at, status) VALUES (?, 'cli', '2026-01-01 00:00:00', 'running')",
            (self.sid,)).lastrowid

    def apply(self, *products, threshold=15):
        with transaction(self.conn):
            return apply_products(self.conn, run_id=self.run_id, source_id=self.sid, products=list(products),
                                  threshold=threshold, vat_rate=19)

    def test_alta_actualizacion_y_sin_cambios(self):
        self.assertEqual(self.apply(prod()).created, 1)
        self.assertEqual(self.apply(prod()).unchanged, 1)
        res = self.apply(prod(final=10500))
        self.assertEqual(res.frozen, 1)
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 10000)
        self.assertEqual(self.scalar("SELECT new_final_price FROM discrepancies"), 10500)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history"), 2)   # alta + cambio

    def test_cambio_menor_al_umbral_tambien_requiere_aprobacion(self):
        self.apply(prod())
        res = self.apply(prod(final=10100))
        self.assertEqual(res.frozen, 1)
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 10000)
        self.assertEqual(self.scalar("SELECT new_final_price FROM discrepancies"), 10100)
        self.assertEqual(self.scalar("SELECT status FROM price_history ORDER BY id DESC LIMIT 1"), "held")

    def test_variacion_anomala_congela_y_mantiene_precio_publicado(self):
        self.apply(prod())
        res = self.apply(prod(final=20000, stock=9))
        self.assertEqual(res.frozen, 1)
        p = self.q("SELECT * FROM products")[0]
        self.assertEqual((p["final_price"], p["price_frozen"], p["stock"]), (10000, 1, 9))   # el stock sí se actualiza
        d = self.q("SELECT * FROM discrepancies")[0]
        self.assertEqual((d["status"], d["new_final_price"], d["old_final_price"]), ("pending", 20000, 10000))
        self.assertEqual(self.scalar("SELECT status FROM price_history ORDER BY id DESC LIMIT 1"), "held")

    def test_nueva_captura_anomala_actualiza_la_misma_alerta(self):
        self.apply(prod())
        self.apply(prod(final=20000))
        self.apply(prod(final=25000))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM discrepancies"), 1)
        self.assertEqual(self.scalar("SELECT new_final_price FROM discrepancies"), 25000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history WHERE status='superseded'"), 1)

    def test_volver_al_precio_publicado_cierra_la_alerta(self):
        self.apply(prod())
        self.apply(prod(final=20000))
        self.apply(prod())
        self.assertEqual(self.scalar("SELECT status FROM discrepancies"), "superseded")
        self.assertEqual(self.scalar("SELECT price_frozen FROM products"), 0)

    def test_aprobar_publica_el_precio_y_exige_justificacion(self):
        self.apply(prod())
        self.apply(prod(final=20000))
        did = self.scalar("SELECT id FROM discrepancies")
        for vacio in ("", "   ", "ok"):
            with self.subTest(vacio=vacio), self.assertRaises(ValueError):
                resolve_discrepancy(self.conn, did, approve=True, username="ana", justification=vacio)
        self.assertEqual(self.scalar("SELECT status FROM discrepancies"), "pending")
        resolve_discrepancy(self.conn, did, approve=True, username="ana", justification="Confirmado con el proveedor")
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 20000)
        self.assertEqual(self.scalar("SELECT price_frozen FROM products"), 0)
        d = self.q("SELECT * FROM discrepancies")[0]
        self.assertEqual((d["status"], d["resolved_by"], d["justification"]), ("approved", "ana", "Confirmado con el proveedor"))
        self.assertEqual(self.scalar("SELECT status FROM price_history ORDER BY id DESC LIMIT 1"), "approved")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='discrepancy.approved'"), 1)

    def test_rechazar_mantiene_el_precio(self):
        self.apply(prod())
        self.apply(prod(final=20000))
        did = self.scalar("SELECT id FROM discrepancies")
        resolve_discrepancy(self.conn, did, approve=False, username="ana", justification="Error tipográfico del proveedor")
        self.assertEqual(self.scalar("SELECT final_price FROM products"), 10000)
        self.assertEqual(self.scalar("SELECT status FROM discrepancies"), "rejected")
        with self.assertRaises(LookupError):
            resolve_discrepancy(self.conn, did, approve=True, username="ana", justification="segundo intento")

    def test_vinculacion_por_ean_y_conflicto_de_identificadores(self):
        ean = "4006381333931"
        self.apply(prod(sku="A-1", ean=ean))
        res = self.apply(prod(sku=None, ean=ean, final=10200))        # mismo producto identificado solo por EAN
        self.assertEqual((res.frozen, res.created), (1, 0))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 1)
        self.assertEqual(self.scalar("SELECT new_final_price FROM discrepancies"), 10200)
        self.apply(prod(sku="B-2", ean="96385074"))
        res = self.apply(prod(sku="A-1", ean="96385074"))              # SKU de uno y EAN de otro
        self.assertEqual(len(res.conflicts), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 2)

    def test_atomicidad_rollback_ante_falla_a_mitad(self):
        with self.assertRaises(AttributeError):
            with transaction(self.conn):
                apply_products(self.conn, run_id=self.run_id, source_id=self.sid, threshold=15, vat_rate=19,
                               products=[prod("A-1"), prod("A-2"), SimpleNamespace(sku="A-3", ean=None)])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM products"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM price_history"), 0)


if __name__ == "__main__":
    unittest.main()
