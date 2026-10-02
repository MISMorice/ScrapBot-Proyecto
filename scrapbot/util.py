"""Utilidades de fecha/hora. En la base de datos todo se guarda en UTC."""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_str() -> str:
    return utcnow().strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def get_tz(name: str) -> tzinfo:
    """Zona horaria por nombre; si el sistema no tiene tzdata usa la hora local."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc
