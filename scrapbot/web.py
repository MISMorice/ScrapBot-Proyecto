"""Capa de presentación: rutas del panel de administración."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from . import audit
from .auth import admin_required, hash_password, login_required, validate_password
from .db import get_setting, set_setting, transaction
from .engine.pipeline import RunInProgress
from .engine.scheduler import parse_schedule
from .engine.store import resolve_discrepancy
from .forms import validate_settings_form, validate_source_form
from .util import get_tz, now_str, to_str, utcnow

bp = Blueprint("web", __name__)
PER_PAGE = 50
PUBLIC_CATALOG_PAGE_SIZE = 24
RULE_COLUMNS = ("selector_type", "item_selector", "sku_selector", "ean_selector", "name_selector",
                "price_selector", "stock_selector", "next_page_selector", "max_pages", "price_includes_vat")
FORM_TEXT = ("name", "start_urls", "delay_min", "delay_max", "selector_type", "item_selector", "sku_selector",
             "ean_selector", "name_selector", "price_selector", "stock_selector", "next_page_selector", "max_pages")


def _page() -> int:
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


def _paginate(count_sql, sql, args, page):
    total = g.db.execute(count_sql, args).fetchone()[0]
    rows = g.db.execute(f"{sql} LIMIT ? OFFSET ?", (*args, PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
    return rows, {"page": page, "pages": max(1, -(-total // PER_PAGE)), "total": total}


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@bp.route("/catalogo")
def public_catalog():
    where, args = ["s.active=1"], []
    q = request.args.get("q", "").strip()
    if q:
        where.append("(p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\' OR p.ean LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\')")
        args += [_like(q)] * 4
    source = request.args.get("source", "")
    if source.isdigit():
        where.append("p.source_id=?")
        args.append(int(source))
    availability = request.args.get("availability", "")
    if availability == "available":
        where.append("COALESCE(p.available, 1)=1")
    elif availability == "out":
        where.append("(p.available=0 OR p.stock=0)")

    order_by = {
        "name": "p.name COLLATE NOCASE, s.name COLLATE NOCASE",
        "price_low": "p.final_price ASC, p.name COLLATE NOCASE",
        "price_high": "p.final_price DESC, p.name COLLATE NOCASE",
        "recent": "p.updated_at DESC, p.name COLLATE NOCASE",
    }
    sort = request.args.get("sort", "name")
    if sort not in order_by:
        sort = "name"
    clause = " AND ".join(where)
    total = g.db.execute(
        f"SELECT COUNT(*) FROM products p JOIN sources s ON s.id=p.source_id WHERE {clause}", args
    ).fetchone()[0]
    pages = max(1, (total + PUBLIC_CATALOG_PAGE_SIZE - 1) // PUBLIC_CATALOG_PAGE_SIZE)
    page = min(_page(), pages)
    rows = g.db.execute(
        "SELECT p.*, s.name source_name, s.start_urls FROM products p "
        f"JOIN sources s ON s.id=p.source_id WHERE {clause} ORDER BY {order_by[sort]} LIMIT ? OFFSET ?",
        (*args, PUBLIC_CATALOG_PAGE_SIZE, (page - 1) * PUBLIC_CATALOG_PAGE_SIZE),
    ).fetchall()
    source_rows = g.db.execute(
        "SELECT s.id, s.name, s.start_urls, COUNT(p.id) product_count "
        "FROM sources s LEFT JOIN products p ON p.source_id=s.id "
        "WHERE s.active=1 GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
    ).fetchall()
    source_images = {
        "Ferretería Demo": "https://images.unsplash.com/photo-1504148455328-c376907d081c?auto=format&fit=crop&w=520&q=80",
        "Electro Demo": "https://images.unsplash.com/photo-1498049794561-7780e7231661?auto=format&fit=crop&w=520&q=80",
        "Jardín Demo": "https://images.unsplash.com/photo-1416879595882-3373a0480b5b?auto=format&fit=crop&w=520&q=80",
    }
    sources = []
    source_urls = {}
    for row in source_rows:
        candidate = next((line.strip() for line in row["start_urls"].splitlines() if line.strip()), "")
        external_url = ""
        try:
            parts = urlsplit(candidate)
            if parts.scheme in ("http", "https") and parts.netloc:
                external_url = candidate
        except ValueError:
            pass
        source = dict(row)
        source["external_url"] = external_url
        source["image"] = source_images.get(source["name"])
        source["initials"] = "".join(part[0] for part in source["name"].split()[:2]).upper()
        sources.append(source)
        source_urls[source["id"]] = external_url
    products = []
    for row in rows:
        product = dict(row)
        product["source_url"] = source_urls.get(product["source_id"], "")
        products.append(product)
    pg = {"page": page, "pages": pages, "total": total}
    return render_template(
        "public_catalog.html", products=products, sources=sources, pg=pg,
        q=q, source=source, availability=availability, sort=sort,
    )


# --- Panel -------------------------------------------------------------------

@bp.route("/")
@login_required
def dashboard():
    d = g.db
    since = to_str(utcnow() - timedelta(days=7))
    m = d.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(items_found),0) found, COALESCE(SUM(items_invalid),0) invalid, "
        "COALESCE(SUM(duration_seconds),0) secs FROM runs WHERE status IN ('success','partial') AND started_at >= ?",
        (since,)).fetchone()
    metrics = {
        "runs_per_day": round(m["n"] / 7, 1),
        "secs_per_100": round(m["secs"] / m["found"] * 100, 1) if m["found"] else None,
        "error_rate": round(m["invalid"] / m["found"] * 100, 2) if m["found"] else None,
    }
    kpi = {
        "products": d.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "sources": d.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0],
        "frozen": d.execute("SELECT COUNT(*) FROM discrepancies WHERE status='pending'").fetchone()[0],
    }
    runs = d.execute("SELECT r.*, s.name source_name FROM runs r JOIN sources s ON s.id=r.source_id "
                     "ORDER BY r.id DESC LIMIT 8").fetchall()
    tz = get_tz(current_app.config["TIMEZONE"])
    now = datetime.now(tz)
    upcoming = []
    if get_setting(d, "scheduler_enabled", "1") == "1":
        try:
            times = parse_schedule(get_setting(d, "schedule_times", ""))
            slots = sorted(datetime(day.year, day.month, day.day, int(t[:2]), int(t[3:]), tzinfo=tz)
                           for day in (now.date(), now.date() + timedelta(days=1)) for t in times)
            upcoming = [s.strftime("%d-%m %H:%M") for s in slots if s > now][:4]
        except ValueError:
            pass
    return render_template("dashboard.html", kpi=kpi, metrics=metrics, runs=runs, upcoming=upcoming)


# --- Fuentes -------------------------------------------------------------------

def _values_from_form(form) -> dict:
    values = {k: form.get(k, "") for k in FORM_TEXT}
    values["respect_robots"] = bool(form.get("respect_robots"))
    values["price_includes_vat"] = bool(form.get("price_includes_vat"))
    return values


def _values_from_db(source, rules) -> dict:
    values = {k: ("" if source[k] is None else source[k]) if k in source.keys() else
              ("" if rules[k] is None else rules[k]) for k in FORM_TEXT}
    values["respect_robots"] = bool(source["respect_robots"])
    values["price_includes_vat"] = bool(rules["price_includes_vat"])
    return values


DEFAULT_VALUES = {"name": "", "start_urls": "", "delay_min": 2, "delay_max": 5, "selector_type": "css",
                  "item_selector": "", "sku_selector": "", "ean_selector": "", "name_selector": "",
                  "price_selector": "", "stock_selector": "", "next_page_selector": "", "max_pages": 20,
                  "respect_robots": True, "price_includes_vat": True}


def _insert_rules(conn, source_id, version, data, user):
    conn.execute(
        "INSERT INTO source_rules (source_id, version, selector_type, item_selector, sku_selector, ean_selector, "
        "name_selector, price_selector, stock_selector, next_page_selector, max_pages, price_includes_vat, "
        "is_current, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (source_id, version, *[data[c] for c in RULE_COLUMNS], now_str(), user))


@bp.route("/sources")
@login_required
def sources():
    rows = g.db.execute(
        "SELECT s.*, r.version, "
        "(SELECT status FROM runs WHERE source_id=s.id ORDER BY id DESC LIMIT 1) last_status, "
        "(SELECT started_at FROM runs WHERE source_id=s.id ORDER BY id DESC LIMIT 1) last_run, "
        "(SELECT COUNT(*) FROM products WHERE source_id=s.id) product_count "
        "FROM sources s LEFT JOIN source_rules r ON r.source_id=s.id AND r.is_current=1 ORDER BY s.name").fetchall()
    return render_template("sources.html", sources=rows)


@bp.route("/sources/new", methods=["GET", "POST"])
@admin_required
def source_new():
    values, errors = dict(DEFAULT_VALUES), {}
    if request.method == "POST":
        values = _values_from_form(request.form)
        data, errors = validate_source_form(request.form)
        if not errors:
            try:
                with transaction(g.db):
                    cur = g.db.execute(
                        "INSERT INTO sources (name, start_urls, active, respect_robots, delay_min, delay_max, created_at, created_by) "
                        "VALUES (?,?,1,?,?,?,?,?)",
                        (data["name"], data["start_urls"], data["respect_robots"], data["delay_min"],
                         data["delay_max"], now_str(), g.user["username"]))
                    _insert_rules(g.db, cur.lastrowid, 1, data, g.user["username"])
                    audit.log(g.db, g.user["username"], "source.create", "source", cur.lastrowid, data["name"])
                flash("Fuente creada.", "success")
                return redirect(url_for("web.source_detail", source_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                errors["name"] = "Ya existe una fuente con ese nombre"
    return render_template("source_form.html", values=values, errors=errors, source=None, versions=[])


@bp.route("/sources/<int:source_id>", methods=["GET", "POST"])
@login_required
def source_detail(source_id):
    source = g.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone() or abort(404)
    rules = g.db.execute("SELECT * FROM source_rules WHERE source_id=? AND is_current=1", (source_id,)).fetchone()
    versions = g.db.execute("SELECT * FROM source_rules WHERE source_id=? ORDER BY version DESC", (source_id,)).fetchall()
    values, errors = _values_from_db(source, rules), {}
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        values = _values_from_form(request.form)
        data, errors = validate_source_form(request.form)
        if not errors:
            try:
                with transaction(g.db):
                    g.db.execute("UPDATE sources SET name=?, start_urls=?, respect_robots=?, delay_min=?, delay_max=? WHERE id=?",
                                 (data["name"], data["start_urls"], data["respect_robots"], data["delay_min"],
                                  data["delay_max"], source_id))
                    changed = any(data[c] != rules[c] for c in RULE_COLUMNS)
                    if changed:
                        g.db.execute("UPDATE source_rules SET is_current=0 WHERE source_id=? AND is_current=1", (source_id,))
                        _insert_rules(g.db, source_id, rules["version"] + 1, data, g.user["username"])
                    audit.log(g.db, g.user["username"], "source.update", "source", source_id,
                              f"reglas v{rules['version'] + 1}" if changed else "sin cambios en reglas")
                flash("Cambios guardados." + (" Se creó una nueva versión de las reglas." if changed else ""), "success")
                return redirect(url_for("web.source_detail", source_id=source_id))
            except sqlite3.IntegrityError:
                errors["name"] = "Ya existe una fuente con ese nombre"
    return render_template("source_form.html", values=values, errors=errors, source=source, versions=versions)


@bp.route("/sources/<int:source_id>/toggle", methods=["POST"])
@admin_required
def source_toggle(source_id):
    source = g.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone() or abort(404)
    new = 0 if source["active"] else 1
    g.db.execute("UPDATE sources SET active=? WHERE id=?", (new, source_id))
    audit.log(g.db, g.user["username"], "source.activate" if new else "source.deactivate", "source", source_id)
    flash("Fuente activada." if new else "Fuente desactivada.", "success")
    return redirect(url_for("web.sources"))


@bp.route("/sources/<int:source_id>/run", methods=["POST"])
@login_required
def source_run(source_id):
    source = g.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone() or abort(404)
    if not source["active"]:
        flash("La fuente está desactivada.", "warning")
        return redirect(url_for("web.sources"))
    runner = current_app.extensions["runner"]
    try:
        run_id = runner.start(source_id, "manual", g.user["username"],
                              background=not current_app.config.get("RUN_SYNCHRONOUSLY", False))
    except RunInProgress as exc:
        flash(str(exc), "warning")
        return redirect(url_for("web.sources"))
    return redirect(url_for("web.run_detail", run_id=run_id))


# --- Productos -----------------------------------------------------------------

@bp.route("/products")
@login_required
def products():
    where, args = ["1=1"], []
    q = request.args.get("q", "").strip()
    if q:
        where.append("(p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\' OR p.ean LIKE ? ESCAPE '\\')")
        args += [_like(q)] * 3
    source = request.args.get("source", "")
    if source.isdigit():
        where.append("p.source_id=?")
        args.append(int(source))
    flt = request.args.get("filter", "")
    if flt == "frozen":
        where.append("p.price_frozen=1")
    elif flt == "nostock":
        where.append("p.available=0")
    clause = " AND ".join(where)
    rows, pg = _paginate(f"SELECT COUNT(*) FROM products p WHERE {clause}",
                         f"SELECT p.*, s.name source_name FROM products p JOIN sources s ON s.id=p.source_id "
                         f"WHERE {clause} ORDER BY p.name", args, _page())
    srcs = g.db.execute("SELECT id, name FROM sources ORDER BY name").fetchall()
    return render_template("products.html", products=rows, pg=pg, sources=srcs, q=q, source=source, flt=flt)


@bp.route("/products/<int:product_id>")
@login_required
def product_detail(product_id):
    p = g.db.execute("SELECT p.*, s.name source_name FROM products p JOIN sources s ON s.id=p.source_id WHERE p.id=?",
                     (product_id,)).fetchone() or abort(404)
    history = g.db.execute("SELECT * FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 100", (product_id,)).fetchall()
    discs = g.db.execute("SELECT * FROM discrepancies WHERE product_id=? ORDER BY id DESC", (product_id,)).fetchall()
    return render_template("product_detail.html", p=p, history=history, discs=discs)


@bp.route("/export/catalog.csv")
@login_required
def export_catalog():
    def safe(v):
        text = "" if v is None else str(v)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fuente", "sku", "ean", "nombre", "precio_neto", "iva", "precio_final", "stock", "disponible",
                "precio_congelado", "actualizado_utc"])
    for r in g.db.execute("SELECT p.*, s.name source_name FROM products p JOIN sources s ON s.id=p.source_id ORDER BY s.name, p.name"):
        w.writerow([safe(r["source_name"]), safe(r["sku"]), safe(r["ean"]), safe(r["name"]), r["net_price"], r["vat_amount"],
                    r["final_price"], r["stock"] if r["stock"] is not None else "",
                    "" if r["available"] is None else r["available"], r["price_frozen"], r["updated_at"]])
    audit.log(g.db, g.user["username"], "catalog.export", "catalog", None, "CSV")
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=catalogo_scrapbot.csv"})


# --- Alertas de precio -------------------------------------------------------------

@bp.route("/discrepancies")
@login_required
def discrepancies():
    status = request.args.get("status", "pending")
    where, args = ("1=1", []) if status == "all" else ("d.status=?", [status if status in
                  ("pending", "approved", "rejected", "superseded") else "pending"])
    rows, pg = _paginate(f"SELECT COUNT(*) FROM discrepancies d WHERE {where}",
                         f"SELECT d.*, p.name, p.sku, p.ean, s.name source_name FROM discrepancies d "
                         f"JOIN products p ON p.id=d.product_id JOIN sources s ON s.id=p.source_id "
                         f"WHERE {where} ORDER BY d.id DESC", args, _page())
    return render_template("discrepancies.html", rows=rows, pg=pg, status=status)


@bp.route("/discrepancies/<int:disc_id>/resolve", methods=["POST"])
@login_required
def discrepancy_resolve(disc_id):
    decision = request.form.get("decision")
    if decision not in ("approve", "reject"):
        abort(400, "Decisión inválida")
    try:
        resolve_discrepancy(g.db, disc_id, approve=(decision == "approve"), username=g.user["username"],
                            justification=request.form.get("justification", ""))
        flash("Variación aprobada y publicada." if decision == "approve" else "Variación rechazada: se mantiene el precio publicado.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except LookupError as exc:
        flash(str(exc), "warning")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("web.discrepancies"))


# --- Ejecuciones y auditoría -----------------------------------------------------

@bp.route("/runs")
@login_required
def runs():
    rows, pg = _paginate("SELECT COUNT(*) FROM runs",
                         "SELECT r.*, s.name source_name FROM runs r JOIN sources s ON s.id=r.source_id ORDER BY r.id DESC",
                         [], _page())
    return render_template("runs.html", runs=rows, pg=pg)


@bp.route("/runs/<int:run_id>")
@login_required
def run_detail(run_id):
    run = g.db.execute("SELECT r.*, s.name source_name FROM runs r JOIN sources s ON s.id=r.source_id WHERE r.id=?",
                       (run_id,)).fetchone() or abort(404)
    http = g.db.execute("SELECT * FROM http_log WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    errs = g.db.execute("SELECT * FROM parse_errors WHERE run_id=? ORDER BY id LIMIT 200", (run_id,)).fetchall()
    return render_template("run_detail.html", run=run, http=http, errs=errs)


@bp.route("/audit")
@login_required
def audit_view():
    where, args = ["1=1"], []
    user, action = request.args.get("user", "").strip(), request.args.get("action", "").strip()
    if user:
        where.append("username = ?")
        args.append(user)
    if action:
        where.append("action LIKE ? ESCAPE '\\'")
        args.append(_like(action))
    clause = " AND ".join(where)
    rows, pg = _paginate(f"SELECT COUNT(*) FROM audit_log WHERE {clause}",
                         f"SELECT * FROM audit_log WHERE {clause} ORDER BY id DESC", args, _page())
    return render_template("audit.html", rows=rows, pg=pg, user=user, action=action)


@bp.route("/access-log")
@admin_required
def access_log():
    rows, pg = _paginate("SELECT COUNT(*) FROM login_log", "SELECT * FROM login_log ORDER BY id DESC", [], _page())
    return render_template("access_log.html", rows=rows, pg=pg)


# --- Administración ------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    d = g.db
    values = {k: get_setting(d, k) for k in ("vat_rate", "variation_threshold", "schedule_times", "scheduler_enabled")}
    errors = {}
    if request.method == "POST":
        data, errors = validate_settings_form(request.form)
        values = {k: request.form.get(k, "") for k in ("vat_rate", "variation_threshold", "schedule_times")}
        values["scheduler_enabled"] = "1" if request.form.get("scheduler_enabled") else "0"
        if not errors:
            with transaction(d):
                for k, v in data.items():
                    set_setting(d, k, v, g.user["username"])
                audit.log(d, g.user["username"], "settings.update", "settings", None,
                          ", ".join(f"{k}={v}" for k, v in data.items()))
            flash("Parámetros guardados.", "success")
            return redirect(url_for("web.settings"))
    return render_template("settings.html", values=values, errors=errors)


@bp.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    errors = {}
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        role = request.form.get("role", "analyst")
        password = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            errors["username"] = "Use 3 a 32 caracteres: letras, números, punto, guion o guion bajo"
        if role not in ("admin", "analyst"):
            errors["role"] = "Rol inválido"
        pw_error = validate_password(password)
        if pw_error:
            errors["password"] = pw_error
        if not errors:
            try:
                cur = g.db.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                                   (username, hash_password(password), role, now_str()))
                audit.log(g.db, g.user["username"], "user.create", "user", cur.lastrowid, f"{username} ({role})")
                flash("Usuario creado.", "success")
                return redirect(url_for("web.users"))
            except sqlite3.IntegrityError:
                errors["username"] = "Ese usuario ya existe"
    rows = g.db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("users.html", users=rows, errors=errors, now_utc=now_str())


@bp.route("/users/<int:user_id>/unlock", methods=["POST"])
@admin_required
def user_unlock(user_id):
    g.db.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (user_id,))
    audit.log(g.db, g.user["username"], "user.unlock", "user", user_id)
    flash("Cuenta desbloqueada.", "success")
    return redirect(url_for("web.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def user_toggle(user_id):
    if user_id == g.user["id"]:
        flash("No puede desactivar su propia cuenta.", "warning")
        return redirect(url_for("web.users"))
    row = g.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() or abort(404)
    g.db.execute("UPDATE users SET active=? WHERE id=?", (0 if row["active"] else 1, user_id))
    audit.log(g.db, g.user["username"], "user.deactivate" if row["active"] else "user.activate", "user", user_id)
    return redirect(url_for("web.users"))
