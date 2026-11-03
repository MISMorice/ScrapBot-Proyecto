import time
import unittest

from flask import Response

from scrapbot.engine.http_client import FetchError, PoliteHttpClient, RobotsDisallowed

from .helpers import FakeSite


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite().start()
        self.sleeps, self.events = [], []
        self.site.routes["/ok"] = "<html><body>hola</body></html>"

    def tearDown(self):
        self.site.stop()

    def client(self, **kw):
        params = dict(user_agent="ScrapBot/1.0 (+prueba)", timeout=1, max_retries=3, backoff_base=2.0,
                      delay_range=(0, 0), respect_robots=False, sleep=self.sleeps.append,
                      on_attempt=lambda **e: self.events.append(e))
        params.update(kw)
        return PoliteHttpClient(**params)

    def test_descarga_exitosa_con_hash_y_user_agent(self):
        res = self.client().get(self.site.base + "/ok")
        self.assertEqual(res.status_code, 200)
        self.assertIn("hola", res.text)
        self.assertEqual(len(res.sha256), 64)
        self.assertTrue(self.site.hits[-1][2]["User-Agent"].startswith("ScrapBot/1.0"))
        self.assertEqual(self.events[0]["status_code"], 200)

    def test_reintenta_con_espera_exponencial_y_se_recupera(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return Response("falla", 503) if calls["n"] < 3 else Response("<html>ok</html>", content_type="text/html")

        self.site.routes["/flaky"] = flaky
        res = self.client().get(self.site.base + "/flaky")
        self.assertEqual((res.attempts, calls["n"]), (3, 3))
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual([e["status_code"] for e in self.events], [503, 503, 200])

    def test_agota_reintentos(self):
        self.site.routes["/caido"] = lambda: Response("x", 500)
        with self.assertRaises(FetchError):
            self.client(max_retries=2).get(self.site.base + "/caido")
        self.assertEqual(self.site.count("/caido"), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_4xx_no_se_reintenta_ni_se_evade(self):
        self.site.routes["/prohibido"] = lambda: Response("no", 403)
        with self.assertRaises(FetchError) as ctx:
            self.client().get(self.site.base + "/prohibido")
        self.assertEqual((ctx.exception.status_code, self.site.count("/prohibido")), (403, 1))

    def test_timeout(self):
        def lento():
            time.sleep(0.8)
            return Response("tarde")

        self.site.routes["/lento"] = lento
        with self.assertRaises(FetchError) as ctx:
            self.client(timeout=0.2, max_retries=0).get(self.site.base + "/lento")
        self.assertIn("Timeout", str(ctx.exception))

    def test_pausa_aleatoria_entre_peticiones(self):
        seen = []
        client = self.client(delay_range=(2, 5), uniform=lambda a, b: (seen.append((a, b)), 3.0)[1])
        client.get(self.site.base + "/ok")
        self.assertEqual(self.sleeps, [])                 # la primera petición no espera
        client.get(self.site.base + "/ok")
        self.assertEqual(seen, [(2, 5)])
        self.assertTrue(2.5 < self.sleeps[0] <= 3.0)

    def test_robots_txt_bloquea_rutas_prohibidas(self):
        self.site.routes["/robots.txt"] = "User-agent: *\nDisallow: /privado/\n"
        self.site.routes["/privado/x"] = "<html>secreto</html>"
        client = self.client(respect_robots=True)
        self.assertEqual(client.get(self.site.base + "/ok").status_code, 200)
        with self.assertRaises(RobotsDisallowed):
            client.get(self.site.base + "/privado/x")
        self.assertEqual(self.site.count("/privado/x"), 0)

    def test_robots_txt_inexistente_permite_todo(self):
        self.assertEqual(self.client(respect_robots=True).get(self.site.base + "/ok").status_code, 200)

    def test_robots_txt_con_error_de_servidor_bloquea_por_precaucion(self):
        self.site.routes["/robots.txt"] = lambda: Response("x", 500)
        with self.assertRaises(RobotsDisallowed):
            self.client(respect_robots=True).get(self.site.base + "/ok")
        self.assertEqual(self.site.count("/ok"), 0)

    def test_no_sigue_redirecciones_a_otro_dominio(self):
        target = f"http://localhost:{self.site.port}/ok"
        self.site.routes["/salto"] = lambda: Response("", 302, headers={"Location": target})
        with self.assertRaises(FetchError) as ctx:
            self.client().get(self.site.base + "/salto")
        self.assertIn("dominio no autorizado", str(ctx.exception))

    def test_rechaza_esquemas_no_http(self):
        with self.assertRaises(FetchError):
            self.client().get("file:///etc/passwd")

    def test_rechaza_contenido_no_html(self):
        self.site.routes["/img"] = lambda: Response(b"\x89PNG", content_type="image/png")
        with self.assertRaises(FetchError):
            self.client().get(self.site.base + "/img")

    def test_limite_de_tamano(self):
        self.site.routes["/grande"] = lambda: Response("a" * 5000, content_type="text/html")
        with self.assertRaises(FetchError):
            self.client(max_bytes=1000, max_retries=0).get(self.site.base + "/grande")


if __name__ == "__main__":
    unittest.main()
