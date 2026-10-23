import unittest
from datetime import datetime

from scrapbot.db import set_setting
from scrapbot.engine.scheduler import IntradayScheduler

from .helpers import AppTestCase, add_source


class FakeRunner:
    def __init__(self):
        self.calls = []

    def start(self, source_id, trigger="manual", user=None, background=False):
        self.calls.append((source_id, trigger, user))
        return len(self.calls)


class SchedulerTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()
        self.sched = IntradayScheduler(dict(self.app.config), self.db_path, self.runner)
        add_source(self.conn, "http://x/a", name="A")
        add_source(self.conn, "http://x/b", name="B")
        self.conn.execute("UPDATE sources SET active=0 WHERE name='B'")
        self.day = datetime(2026, 10, 1, tzinfo=self.sched.tz)

    def at(self, h, m):
        return self.day.replace(hour=h, minute=m)

    def test_captura_cada_cinco_minutos_sin_duplicar_el_mismo_intervalo(self):
        self.assertEqual(len(self.sched.check(self.at(8, 0))), 1)
        self.assertEqual(self.sched.check(self.at(8, 4)), [])
        self.assertEqual(len(self.sched.check(self.at(8, 5))), 1)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertTrue(all(c[1:] == ("scheduled", "scheduler") for c in self.runner.calls))

    def test_desactivado_no_dispara(self):
        set_setting(self.conn, "scheduler_enabled", "0")
        self.assertEqual(self.sched.check(self.at(6, 1)), [])

    def test_intervalo_personalizado(self):
        set_setting(self.conn, "scheduler_interval_minutes", "15")
        self.assertEqual(len(self.sched.check(self.at(6, 0))), 1)
        self.assertEqual(self.sched.check(self.at(6, 14)), [])
        self.assertEqual(len(self.sched.check(self.at(6, 15))), 1)

    def test_intervalo_invalido_se_reporta(self):
        set_setting(self.conn, "scheduler_interval_minutes", "7")
        with self.assertRaisesRegex(ValueError, "Intervalo de programación inválido"):
            self.sched.check(self.at(6, 0))

    def test_dos_procesos_no_duplican_el_disparo(self):
        otro = IntradayScheduler(dict(self.app.config), self.db_path, self.runner)
        self.assertEqual(len(self.sched.check(self.at(12, 2))), 1)
        self.assertEqual(otro.check(self.at(12, 2)), [])


if __name__ == "__main__":
    unittest.main()
