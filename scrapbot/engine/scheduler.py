"""Programador de ejecuciones intradiarias (OE3): por defecto 00:00, 06:00, 12:00 y 18:00."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta

from ..db import connect, get_setting, set_setting, transaction
from ..util import get_tz

log = logging.getLogger("scrapbot.scheduler")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_schedule(value: str) -> list[str]:
    times = sorted({t.strip() for t in (value or "").split(",") if t.strip()})
    for t in times:
        if not _TIME_RE.match(t):
            raise ValueError(f"Hora inválida: {t!r} (use HH:MM)")
    return times


class IntradayScheduler(threading.Thread):
    def __init__(self, config: dict, db_path: str, runner, tick: float = 20, tzname: str = "America/Santiago"):
        super().__init__(daemon=True, name="scrapbot-scheduler")
        self.config, self.db_path, self.runner = config, db_path, runner
        self.tick = tick
        self.tz = get_tz(tzname)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.tick):
            try:
                self.check()
            except Exception:
                log.exception("Error en el ciclo del programador")

    def latest_slot(self, now: datetime, times: list[str]) -> datetime | None:
        """Último horario programado que ya ocurrió (hoy o ayer)."""
        candidates = []
        for day in (now.date() - timedelta(days=1), now.date()):
            for t in times:
                hh, mm = map(int, t.split(":"))
                candidates.append(datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz))
        past = [c for c in candidates if c <= now]
        return max(past) if past else None

    def check(self, now: datetime | None = None) -> list[int]:
        """Dispara las fuentes activas si corresponde. Devuelve los ids de ejecución creados."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        conn = connect(self.db_path)
        try:
            if get_setting(conn, "scheduler_enabled", "1") != "1":
                return []
            times = parse_schedule(get_setting(conn, "schedule_times", self.config["DEFAULT_SCHEDULE"]))
            slot = self.latest_slot(now, times)
            if slot is None:
                return []
            grace = timedelta(minutes=self.config.get("SCHEDULER_GRACE_MINUTES", 30))
            if now - slot > grace:
                return []
            slot_key = slot.strftime("%Y-%m-%d %H:%M")
            # Reclamo atómico del horario: evita doble disparo con varios procesos.
            with transaction(conn):
                if get_setting(conn, "scheduler_last_slot", "") >= slot_key:
                    return []
                set_setting(conn, "scheduler_last_slot", slot_key, "scheduler")
            source_ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE active=1 ORDER BY id")]
        finally:
            conn.close()
        run_ids = []
        for sid in source_ids:
            try:
                run_ids.append(self.runner.start(sid, trigger="scheduled", user="scheduler", background=False))
            except Exception as exc:
                log.warning("Fuente %s omitida en el horario %s: %s", sid, slot_key, exc)
        return run_ids
