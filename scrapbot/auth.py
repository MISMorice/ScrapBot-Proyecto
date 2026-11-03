"""Autenticación y control de acceso (Tabla 5, fila 1).

* Contraseñas con hash PBKDF2-SHA256 (nunca en claro).
* Bloqueo temporal tras N intentos fallidos.
* Expiración de sesión por inactividad.
* Registro de todos los intentos en `login_log`.
* Protección CSRF para todos los POST.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import audit
from .util import now_str, parse_ts, to_str, utcnow

bp = Blueprint("auth", __name__)
_DUMMY_HASHES: dict[str, str] = {}
ACCESS_MODES = {"user": "Usuario", "admin": "Administrador"}


def _dummy_hash() -> str:
    """Hash ficticio con el MISMO costo que los reales.

    Si el hash ficticio fuera más barato, un usuario inexistente respondería
    cientos de veces más rápido y se podría saber qué cuentas existen midiendo
    el tiempo de respuesta.
    """
    method = current_app.config["PASSWORD_HASH_METHOD"]
    if method not in _DUMMY_HASHES:
        _DUMMY_HASHES[method] = generate_password_hash(secrets.token_hex(16), method=method)
    return _DUMMY_HASHES[method]


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=current_app.config["PASSWORD_HASH_METHOD"])


def validate_password(password: str) -> str | None:
    minimum = current_app.config["PASSWORD_MIN_LENGTH"]
    if len(password) < minimum:
        return f"La contraseña debe tener al menos {minimum} caracteres"
    if password.isdigit() or password.isalpha():
        return "La contraseña debe combinar letras y números u otros símbolos"
    return None


def _log_attempt(username, success: bool, reason: str) -> None:
    g.db.execute(
        "INSERT INTO login_log (username, success, reason, ip, created_at) VALUES (?,?,?,?,?)",
        (username, int(success), reason, request.remote_addr, now_str()),
    )


def safe_next(target: str | None) -> str:
    if target:
        parts = urlsplit(target)
        if not parts.scheme and not parts.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return url_for("web.dashboard")


# --- Decoradores -----------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Exige una cuenta administradora que haya ingresado por el acceso «Administrador»."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# --- CSRF ------------------------------------------------------------------

def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def init_auth(app) -> None:
    app.register_blueprint(bp)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def load_user_and_guard():
        g.user = None
        g.is_admin = False
        g.access_mode = None
        if request.endpoint == "static":
            return
        uid = session.get("uid")
        if uid is not None:
            last = session.get("last_seen", 0)
            now = utcnow().timestamp()
            if now - last > app.config["SESSION_INACTIVITY_SECONDS"]:
                session.clear()
                flash("Su sesión expiró por inactividad. Ingrese nuevamente.", "warning")
            else:
                row = g.db.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
                if row is None:
                    session.clear()
                else:
                    g.user = row
                    # El modo de acceso elegido en el login limita los permisos: una cuenta
                    # admin que entra como «Usuario» ve el panel sin poder editar.
                    mode = session.get("mode", "admin" if row["role"] == "admin" else "user")
                    g.access_mode = "admin" if (mode == "admin" and row["role"] == "admin") else "user"
                    g.is_admin = g.access_mode == "admin"
                    if request.endpoint != "web.alerts_summary":     # el sondeo de alertas no cuenta como actividad
                        session["last_seen"] = now
        if request.method == "POST" and app.config.get("CSRF_ENABLED", True):
            sent = request.form.get("_csrf", "")
            expected = session.get("_csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Token CSRF inválido o ausente")


# --- Rutas -----------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("web.dashboard"))
    if request.method == "POST":
        cfg = current_app.config
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Si el formulario no envía el selector (clientes antiguos), se usa el rol de la cuenta.
        access = request.form.get("access")
        if access is not None and access not in ACCESS_MODES:
            access = "user"
        user = g.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        generic = "Usuario o contraseña incorrectos"

        if user is None or not user["active"]:
            check_password_hash(_dummy_hash(), password)   # iguala tiempos de respuesta
            _log_attempt(username, False, "usuario inexistente o inactivo")
            flash(generic, "error")
            return render_template("login.html", access=access or "user", username=username), 401

        if user["locked_until"] and parse_ts(user["locked_until"]) > utcnow():
            _log_attempt(username, False, "cuenta bloqueada")
            minutes = max(1, int((parse_ts(user["locked_until"]) - utcnow()).total_seconds() // 60) + 1)
            flash(f"Cuenta bloqueada por intentos fallidos. Reintente en {minutes} min o pida a un administrador que la desbloquee.", "error")
            return render_template("login.html", access=access or "user", username=username), 423

        if not check_password_hash(user["password_hash"], password):
            attempts = user["failed_attempts"] + 1
            if attempts >= cfg["MAX_FAILED_LOGINS"]:
                locked = to_str(utcnow() + timedelta(minutes=cfg["LOCKOUT_MINUTES"]))
                g.db.execute("UPDATE users SET failed_attempts=0, locked_until=? WHERE id=?", (locked, user["id"]))
                _log_attempt(username, False, "contraseña incorrecta; cuenta bloqueada")
                audit.log(g.db, username, "auth.lockout", "user", user["id"], f"{attempts} intentos fallidos")
                flash("Demasiados intentos fallidos: la cuenta fue bloqueada temporalmente.", "error")
                return render_template("login.html", access=access or "user", username=username), 423
            g.db.execute("UPDATE users SET failed_attempts=? WHERE id=?", (attempts, user["id"]))
            _log_attempt(username, False, "contraseña incorrecta")
            flash(generic, "error")
            return render_template("login.html", access=access or "user", username=username), 401

        if access is None:
            access = "admin" if user["role"] == "admin" else "user"
        if access == "admin" and user["role"] != "admin":
            # La contraseña era correcta: no es un intento fallido, pero la cuenta no tiene ese permiso.
            g.db.execute("UPDATE users SET failed_attempts=0 WHERE id=?", (user["id"],))
            _log_attempt(username, False, "sin permiso de administrador")
            flash("Esta cuenta no tiene permisos de administrador. Ingrese con el acceso «Usuario».", "error")
            return render_template("login.html", access="user", username=username), 403

        g.db.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (user["id"],))
        _log_attempt(username, True, f"ok ({ACCESS_MODES[access].lower()})")
        audit.log(g.db, username, "auth.login", "user", user["id"], f"acceso={access}")
        session.clear()                                    # evita fijación de sesión
        session["uid"] = user["id"]
        session["mode"] = access
        session["last_seen"] = utcnow().timestamp()
        return redirect(safe_next(request.args.get("next")))
    return render_template("login.html", access=request.args.get("access", "user"), username="")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.log(g.db, g.user["username"], "auth.logout", "user", g.user["id"])
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("auth.login"))
