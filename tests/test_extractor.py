import unittest

from scrapbot.engine.extractor import PageParser, RuleSet, SelectorError, hash_values, validate_selector

HTML = """<html><body>
<article class="producto" data-sku="sk-1"><h2 class="nombre"> Taladro
 percutor </h2><span class="ean">4006381333931</span><span class="precio">$11.900</span><span class="stock">Stock: 4</span></article>
<article class="producto" data-sku="sk-2"><h2 class="nombre">Sierra</h2><span class="precio">$5.990</span></article>
<a class="siguiente" href="/catalogo?page=2">Siguiente</a></body></html>"""


def css_rules(**kw):
    base = dict(selector_type="css", item_selector="article.producto",
                field_selectors={"sku": "@data-sku", "ean": "span.ean", "name": "h2.nombre", "price": "span.precio", "stock": "span.stock"},
                next_page_selector="a.siguiente", max_pages=5, price_includes_vat=True)
    base.update(kw)
    return RuleSet(**base)


class CssTests(unittest.TestCase):
    def test_extrae_campos_atributos_y_siguiente(self):
        page = PageParser(css_rules()).parse(HTML, "https://tienda.cl/catalogo")
        self.assertEqual(len(page.items), 2)
        first = page.items[0].values
        self.assertEqual(first["sku"], "sk-1")
        self.assertEqual(first["name"], "Taladro percutor")          # espacios colapsados
        self.assertEqual(first["price"], "$11.900")
        self.assertEqual(page.next_url, "https://tienda.cl/catalogo?page=2")

    def test_campo_ausente_es_none(self):
        second = PageParser(css_rules()).parse(HTML, "https://t.cl/").items[1].values
        self.assertIsNone(second["ean"])
        self.assertIsNone(second["stock"])

    def test_hash_estable_y_sensible_al_contenido(self):
        a, b = {"x": "1", "y": "2"}, {"y": "2", "x": "1"}
        self.assertEqual(hash_values(a), hash_values(b))
        self.assertNotEqual(hash_values(a), hash_values({"x": "1", "y": "3"}))
        self.assertEqual(len(hash_values(a)), 64)

    def test_sin_contenedor_la_pagina_es_un_item(self):
        rules = css_rules(item_selector=None, field_selectors={"name": "h2.nombre", "price": "span.precio"}, next_page_selector=None)
        page = PageParser(rules).parse(HTML, "https://t.cl/")
        self.assertEqual(len(page.items), 1)

    def test_html_vacio(self):
        with self.assertRaises(ValueError):
            PageParser(css_rules()).parse("  ", "https://t.cl/")

    def test_enlace_javascript_no_se_sigue(self):
        html = HTML.replace("/catalogo?page=2", "javascript:void(0)")
        self.assertIsNone(PageParser(css_rules()).parse(html, "https://t.cl/").next_url)


class XPathTests(unittest.TestCase):
    def test_xpath(self):
        rules = RuleSet(selector_type="xpath", item_selector="//article[@class='producto']",
                        field_selectors={"sku": "./@data-sku", "name": ".//h2", "price": ".//span[@class='precio']",
                                         "ean": ".//span[@class='ean']"},
                        next_page_selector="//a[@class='siguiente']/@href", max_pages=3, price_includes_vat=True)
        page = PageParser(rules).parse(HTML, "https://tienda.cl/catalogo")
        self.assertEqual([i.values["sku"] for i in page.items], ["sk-1", "sk-2"])
        self.assertEqual(page.items[0].values["name"], "Taladro percutor")
        self.assertIsNone(page.items[1].values["ean"])
        self.assertEqual(page.next_url, "https://tienda.cl/catalogo?page=2")


class ValidationTests(unittest.TestCase):
    def test_selectores_validos(self):
        for kind, expr in [("css", "div.a > span"), ("css", "span.precio@data-price"), ("css", "@data-sku"),
                           ("xpath", "//div[@id='x']//span"), ("xpath", "./@data-sku")]:
            with self.subTest(expr=expr):
                validate_selector(kind, expr)

    def test_selectores_invalidos(self):
        for kind, expr in [("css", "div["), ("css", ""), ("css", "a >> b"), ("xpath", "//div[@"), ("xpath", ""), ("otro", "x")]:
            with self.subTest(expr=expr), self.assertRaises(SelectorError):
                validate_selector(kind, expr)

    def test_contenedor_no_admite_atributo(self):
        with self.assertRaises(SelectorError):
            validate_selector("css", "div@x", allow_attr=False)


if __name__ == "__main__":
    unittest.main()
