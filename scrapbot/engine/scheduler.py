"""Programador de capturas automáticas por intervalo configurable."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from ..db import connect, get_setting, set_setting, transaction
from ..util import get_tz

log = logging.getLogger("scrapbot.scheduler")
VALID_INTERVALS = (5, 15, 60)


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

    @staticmethod
    def latest_slot(now: datetime, interval_minutes: int) -> datetime:
        minute = now.minute - now.minute % interval_minutes
        return now.replace(minute=minute, second=0, microsecond=0)

    def check(self, now: datetime | None = None) -> list[int]:
        """Dispara las fuentes activas si corresponde. Devuelve los ids de ejecución creados."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        conn = connect(self.db_path)
        try:
            if get_setting(conn, "scheduler_enabled", "1") != "1":
                return []
            interval = int(get_setting(
                conn, "scheduler_interval_minutes",
                str(self.config.get("DEFAULT_SCHEDULER_INTERVAL_MINUTES", 5)),
            ))
            if interval not in VALID_INTERVALS:
                raise ValueError(f"Intervalo de programación inválido: {interval}")
            slot = self.latest_slot(now, interval)
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
