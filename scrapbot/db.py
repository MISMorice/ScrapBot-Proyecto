"""Capa de persistencia: conexiones SQLite, transacciones atómicas y ajustes.

* Modo WAL y `synchronous=FULL` para evitar corrupción ante cortes (R4).
* Conexiones en autocommit; las escrituras atómicas van dentro de
  `transaction()`, que hace ROLLBACK ante cualquier excepción.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .util import now_str

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Bloque atómico: COMMIT si termina bien, ROLLBACK si falla."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# Columnas agregadas después de la primera versión. `CREATE TABLE IF NOT EXISTS`
# no modifica tablas que ya existen, así que una base creada con la versión
# anterior se actualiza aquí sin perder datos.
MIGRATIONS = (
    ("products", "manual_override", "INTEGER NOT NULL DEFAULT 0"),
    ("products", "missing_since", "TEXT"),
    ("runs", "items_alerted", "INTEGER NOT NULL DEFAULT 0"),
    ("price_history", "changed_by", "TEXT"),
    ("price_history", "note", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(conn: sqlite3.Connection, config: dict | None = None) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    cfg = config or {}
    defaults = {
        "vat_rate": str(cfg.get("DEFAULT_VAT_RATE", 19)),
        "variation_threshold": str(cfg.get("DEFAULT_VARIATION_THRESHOLD", 15)),
        "schedule_times": cfg.get("DEFAULT_SCHEDULE", "00:00,06:00,12:00,18:00"),
        "scheduler_enabled": "1",
        "scheduler_last_slot": "",
        # Automatización (entrega 80-100 %)
        "schedule_mode": cfg.get("DEFAULT_SCHEDULE_MODE", "interval"),   # 'interval' | 'times'
        "interval_minutes": str(cfg.get("DEFAULT_INTERVAL_MINUTES", 15)),
        "alert_mode": cfg.get("DEFAULT_ALERT_MODE", "auto"),             # 'auto' | 'review'
        "drift_window_days": str(cfg.get("DEFAULT_DRIFT_WINDOW_DAYS", 7)),
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_str()),
        )


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str, user: str | None = None) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (key, str(value), now_str(), user),
    )


def load_business_settings(conn: sqlite3.Connection) -> dict:
    return {
        "vat_rate": float(get_setting(conn, "vat_rate", "19")),
        "variation_threshold": float(get_setting(conn, "variation_threshold", "15")),
        "alert_mode": get_setting(conn, "alert_mode", "auto"),
        "drift_window_days": int(float(get_setting(conn, "drift_window_days", "7"))),
    }
