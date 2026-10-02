"""Registro de auditoría unificado (Tabla 5, fila 10)."""
from __future__ import annotations

import sqlite3

from .util import now_str


def log(conn: sqlite3.Connection, username: str | None, action: str,
        entity: str | None = None, entity_id: int | None = None, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (username, action, entity, entity_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, action, entity, entity_id, detail, now_str()),
    )
