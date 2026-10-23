"""Configuración de ScrapBot.

Los valores de negocio (IVA, umbral de variación, intervalo automático) son solo los
valores *iniciales*: una vez creada la base de datos se editan desde el panel
(Parámetros) y quedan guardados en la tabla `settings`.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


class Config:
    # --- Aplicación -------------------------------------------------------
    SECRET_KEY = os.environ.get("SCRAPBOT_SECRET_KEY")  # si falta, se genera en instance/secret.key
    DATABASE = os.environ.get("SCRAPBOT_DB", str(BASE_DIR / "instance" / "scrapbot.db"))
    TIMEZONE = os.environ.get("SCRAPBOT_TZ", "America/Santiago")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SCRAPBOT_COOKIE_SECURE", False)  # True detrás de HTTPS
    CSRF_ENABLED = True

    # --- Autenticación (Tabla 5, fila 1) ----------------------------------
    SESSION_INACTIVITY_SECONDS = 15 * 60   # expiración por inactividad
    MAX_FAILED_LOGINS = 5                  # bloqueo tras N intentos fallidos
    LOCKOUT_MINUTES = 15
    PASSWORD_MIN_LENGTH = 10
    PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"

    # --- Motor de extracción (Tabla 5 fila 3 / Tabla 9 R2 y R5) -----------
    USER_AGENT = os.environ.get(
        "SCRAPBOT_USER_AGENT",
        "ScrapBot/1.0 (bot de catalogo autorizado; contacto: operaciones@example.cl)",
    )
    HTTP_TIMEOUT = 10            # segundos (R2)
    HTTP_MAX_RETRIES = 3         # reintentos con espera exponencial (R2)
    HTTP_BACKOFF_BASE = 2.0      # 1 s, 2 s, 4 s ...
    HTTP_MAX_BYTES = 5 * 1024 * 1024
    REQUEST_DELAY_MIN = 2.0      # pausa aleatoria entre peticiones (R5)
    REQUEST_DELAY_MAX = 5.0
    STALE_RUN_MINUTES = 120      # una ejecución 'running' más antigua se considera caída

    # --- Valores iniciales de negocio ------------------------------------
    DEFAULT_VAT_RATE = 19                      # IVA Chile (%)
    DEFAULT_VARIATION_THRESHOLD = 15           # % (R3)
    DEFAULT_SCHEDULER_INTERVAL_MINUTES = 5
    SCHEDULER_ENABLED = _env_bool("SCRAPBOT_SCHEDULER", True)
    SCHEDULER_TICK_SECONDS = 20
