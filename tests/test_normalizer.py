import unittest

from scrapbot.engine.normalizer import (InvalidRecord, compute_breakdown, ean_checksum_ok, normalize_ean,
                                        normalize_record, normalize_sku, parse_price, parse_stock)


class PriceTests(unittest.TestCase):
    def test_formatos_chilenos(self):
        casos = {"$12.990": 12990, "12.990": 12990, "$ 1.299.990": 1299990, "CLP 7990": 7990,
                 "12 990": 12990, "12.990,50": 12991, "1,500": 1500, "$9.990 (IVA incl.)": 9990}
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(parse_price(texto), esperado)

    def test_rechaza_valores_no_confiables(self):
        for texto in [None, "", "   ", "Consultar", "$0", "$19.990 $24.990", "desde 5.000 hasta 9.000", "9" * 12]:
            with self.subTest(texto=texto):
                with self.assertRaises(InvalidRecord):
                    parse_price(texto)


class VatTests(unittest.TestCase):
    def test_desglose_iva_incluido(self):
        b = compute_breakdown(11900, True, 19)
        self.assertEqual((b.net, b.vat, b.final), (10000, 1900, 11900))

    def test_recargo_precio_neto(self):
        b = compute_breakdown(10000, False, 19)
        self.assertEqual((b.net, b.vat, b.final), (10000, 1900, 11900))

    def test_suma_siempre_cuadra(self):
        for precio in (1, 99, 1234, 14500, 99999, 1299990):
            for incluye in (True, False):
                with self.subTest(precio=precio, incluye=incluye):
                    b = compute_breakdown(precio, incluye, 19)
                    self.assertEqual(b.net + b.vat, b.final)


class IdentifierTests(unittest.TestCase):
    def test_ean_valido_e_invalido(self):
        self.assertEqual(normalize_ean("4006381333931"), "4006381333931")
        self.assertTrue(ean_checksum_ok("96385074"))        # EAN-8
        with self.assertRaises(InvalidRecord):
            normalize_ean("4006381333932")                   # dígito verificador malo
        with self.assertRaises(InvalidRecord):
            normalize_ean("12345")                           # largo inválido
        with self.assertRaises(InvalidRecord):
            normalize_ean("40063813339AB")

    def test_ean_con_espacios_y_guiones(self):
        self.assertEqual(normalize_ean("4006381-333931"), "4006381333931")

    def test_sku(self):
        self.assertEqual(normalize_sku("  abc-123 "), "ABC-123")
        self.assertIsNone(normalize_sku(""))
        with self.assertRaises(InvalidRecord):
            normalize_sku("<script>")


class StockTests(unittest.TestCase):
    def test_variantes(self):
        self.assertEqual(parse_stock("Stock: 12 unidades"), (12, True))
        self.assertEqual(parse_stock("Agotado"), (0, False))
        self.assertEqual(parse_stock("No disponible"), (0, False))
        self.assertEqual(parse_stock("Disponible"), (None, True))
        self.assertEqual(parse_stock("1.200"), (1200, True))
        self.assertEqual(parse_stock("0"), (0, False))

    def test_no_interpretable(self):
        with self.assertRaises(InvalidRecord):
            parse_stock("quizás")


class RecordTests(unittest.TestCase):
    CONF = {"sku", "ean", "name", "price", "stock"}

    def norm(self, **values):
        base = {"sku": "A-1", "ean": "4006381333931", "name": " Taladro  ", "price": "$11.900", "stock": "5"}
        base.update(values)
        return normalize_record(base, configured=self.CONF, includes_vat=True, vat_rate=19, content_hash="h")

    def test_registro_valido(self):
        p = self.norm()
        self.assertEqual((p.sku, p.ean, p.name, p.final_price, p.net_price, p.vat_amount, p.stock),
                         ("A-1", "4006381333931", "Taladro", 11900, 10000, 1900, 5))

    def test_sin_identificador_valido_se_descarta(self):
        with self.assertRaises(InvalidRecord) as ctx:
            self.norm(sku=None, ean="123")
        self.assertEqual(ctx.exception.field, "sku/ean")

    def test_un_identificador_malo_pero_otro_bueno_genera_advertencia(self):
        p = self.norm(ean="4006381333932")
        self.assertIsNone(p.ean)
        self.assertEqual(p.warnings[0][0], "ean")

    def test_precio_ausente_o_invalido(self):
        for valor in (None, "Consultar"):
            with self.subTest(valor=valor), self.assertRaises(InvalidRecord):
                self.norm(price=valor)

    def test_stock_invalido_no_descarta_el_producto(self):
        p = self.norm(stock="¿?")
        self.assertIsNone(p.stock)
        self.assertEqual(p.warnings[0][0], "stock")


if __name__ == "__main__":
    unittest.main()
