import unittest
from datetime import datetime, timedelta

from scrapbot.db import set_setting
from scrapbot.engine.scheduler import IntradayScheduler, parse_schedule

from .helpers import AppTestCase, add_source


class FakeRunner:
    def __init__(self):
        self.calls = []

    def start(self, source_id, trigger="manual", user=None, background=False):
        self.calls.append((source_id, trigger, user))
        return len(self.calls)


class ScheduleParsingTests(unittest.TestCase):
    def test_parseo(self):
        self.assertEqual(parse_schedule("18:00, 06:00,06:00,00:00"), ["00:00", "06:00", "18:00"])
        for bad in ("6:00", "25:00", "12:60", "mediodía"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_schedule(bad)


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

    def test_cuatro_ejecuciones_diarias_una_sola_vez_cada_una(self):
        total = 0
        for hh in (0, 6, 12, 18):
            self.assertEqual(len(self.sched.check(self.at(hh, 3))), 1)          # dispara (solo fuentes activas)
            self.assertEqual(self.sched.check(self.at(hh, 10)), [])             # no se repite en el mismo horario
            total += 1
        self.assertEqual(len(self.runner.calls), 4)
        self.assertTrue(all(c[1:] == ("scheduled", "scheduler") for c in self.runner.calls))

    def test_fuera_de_la_ventana_de_gracia_no_dispara(self):
        self.assertEqual(self.sched.check(self.at(9, 0)), [])                   # 3 h después de las 06:00
        self.assertEqual(self.runner.calls, [])

    def test_desactivado_no_dispara(self):
        set_setting(self.conn, "scheduler_enabled", "0")
        self.assertEqual(self.sched.check(self.at(6, 1)), [])

    def test_horarios_personalizados(self):
        set_setting(self.conn, "schedule_times", "08:30")
        self.assertEqual(self.sched.check(self.at(6, 1)), [])
        self.assertEqual(len(self.sched.check(self.at(8, 40))), 1)

    def test_dos_procesos_no_duplican_el_disparo(self):
        otro = IntradayScheduler(dict(self.app.config), self.db_path, self.runner)
        self.assertEqual(len(self.sched.check(self.at(12, 2))), 1)
        self.assertEqual(otro.check(self.at(12, 2)), [])


if __name__ == "__main__":
    unittest.main()
