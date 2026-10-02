"""ScrapBot · automatización de catálogos e-commerce desde fuentes autorizadas.

Arquitectura en tres capas:
  * Presentación: Flask + Jinja2 (`web.py`, `auth.py`, `templates/`)
  * Lógica/motor: `engine/` (HTTP, extractor, normalizador, detector, pipeline, scheduler)
  * Persistencia: SQLite 3 en modo WAL (`db.py`, `schema.sql`)
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, g, render_template, request

from config import Config

from .db import connect, init_db
from .util import get_tz, parse_ts


def _secret_key(app: Flask) -> str:
    if app.config.get("SECRET_KEY"):
        return app.config["SECRET_KEY"]
    path = Path(app.config["DATABASE"]).parent / "secret.key"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    app.config["SECRET_KEY"] = _secret_key(app)

    conn = connect(app.config["DATABASE"])
    try:
        init_db(conn, app.config)
    finally:
        conn.close()

    # Conexión por petición (debe registrarse antes que los hooks de autenticación).
    @app.before_request
    def open_db():
        g.db = connect(app.config["DATABASE"])

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    from .auth import init_auth
    from .engine.pipeline import SyncRunner
    from .web import bp as web_bp

    init_auth(app)
    app.register_blueprint(web_bp)
    app.extensions["runner"] = SyncRunner(dict(app.config), app.config["DATABASE"],
                                          client_factory=app.config.get("CLIENT_FACTORY"))

    tz = get_tz(app.config["TIMEZONE"])

    @app.template_filter("clp")
    def clp(value):
        if value is None:
            return "—"
        return "$" + f"{int(value):,}".replace(",", ".")

    @app.template_filter("dictupdate")
    def dictupdate(d, key, value):
        from urllib.parse import urlencode

        return urlencode({**d, key: value})

    @app.template_filter("dt")
    def dt(value):
        if not value:
            return "—"
        return parse_ts(value).astimezone(tz).strftime("%d-%m-%Y %H:%M")

    @app.context_processor
    def inject_globals():
        pending = 0
        if getattr(g, "user", None) is not None:
            pending = g.db.execute("SELECT COUNT(*) FROM discrepancies WHERE status='pending'").fetchone()[0]
        return {"pending_count": pending, "current_path": request.path}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://images.unsplash.com; style-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    for code, title in ((400, "Solicitud inválida"), (403, "Acceso denegado"), (404, "No encontrado")):
        def handler(err, code=code, title=title):
            detail = getattr(err, "description", "")
            return render_template("error.html", code=code, title=title, detail=detail), code
        app.register_error_handler(code, handler)

    if (app.config["SCHEDULER_ENABLED"] and not app.testing and not app.config.get("DISABLE_SCHEDULER")
            and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true")):
        from .engine.scheduler import IntradayScheduler

        IntradayScheduler(dict(app.config), app.config["DATABASE"], app.extensions["runner"],
                          tick=app.config["SCHEDULER_TICK_SECONDS"], tzname=app.config["TIMEZONE"]).start()
        logging.getLogger("scrapbot").info("Programador intradiario iniciado")
    return app
