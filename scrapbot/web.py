"""Capa de presentación: rutas del panel de administración."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, url_for)

from . import audit
from .auth import admin_required, hash_password, login_required, validate_password
from .db import get_setting, load_business_settings, set_setting, transaction
from .engine.normalizer import InvalidRecord, parse_price
from .engine.pipeline import RunInProgress
from .engine.scheduler import upcoming_slots
from .engine.store import acknowledge_alerts, edit_product, release_product, resolve_discrepancy
from .forms import validate_settings_form, validate_source_form
from .util import get_tz, now_str, parse_ts, to_str, utcnow

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
    where, args = ["s.active=1", "p.missing_since IS NULL"], []   # no se muestran productos retirados
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
        "FROM sources s LEFT JOIN products p ON p.source_id=s.id AND p.missing_since IS NULL "
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
        "products": d.execute("SELECT COUNT(*) FROM products WHERE missing_since IS NULL").fetchone()[0],
        "sources": d.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0],
        "frozen": d.execute("SELECT COUNT(*) FROM discrepancies WHERE status='pending'").fetchone()[0],
        "alerts": d.execute("SELECT COUNT(*) FROM price_alerts WHERE seen_at IS NULL").fetchone()[0],
        "alerts_today": d.execute("SELECT COUNT(*) FROM price_alerts WHERE created_at >= ?",
                                  (to_str(utcnow() - timedelta(hours=24)),)).fetchone()[0],
    }
    automation = {
        "enabled": get_setting(d, "scheduler_enabled", "1") == "1",
        "mode": get_setting(d, "schedule_mode", "interval"),
        "interval": get_setting(d, "interval_minutes", "15"),
        "times": get_setting(d, "schedule_times", ""),
        "alert_mode": get_setting(d, "alert_mode", "auto"),
    }
    recent_alerts = d.execute(
        "SELECT a.*, p.name, s.name source_name FROM price_alerts a JOIN products p ON p.id=a.product_id "
        "JOIN sources s ON s.id=p.source_id ORDER BY a.id DESC LIMIT 5").fetchall()
    runs = d.execute("SELECT r.*, s.name source_name FROM runs r JOIN sources s ON s.id=r.source_id "
                     "ORDER BY r.id DESC LIMIT 8").fetchall()
    tz = get_tz(current_app.config["TIMEZONE"])
    now = datetime.now(tz)
    try:
        upcoming = [s.strftime("%d-%m %H:%M") for s in upcoming_slots(d, tz, now)]
    except ValueError:
        upcoming = []
    return render_template("dashboard.html", kpi=kpi, metrics=metrics, runs=runs, upcoming=upcoming,
                           automation=automation, recent_alerts=recent_alerts)


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
        if not g.is_admin:
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
    elif flt == "manual":
        where.append("p.manual_override=1")
    elif flt == "missing":
        where.append("p.missing_since IS NOT NULL")
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
    alerts = g.db.execute("SELECT * FROM price_alerts WHERE product_id=? ORDER BY id DESC LIMIT 50", (product_id,)).fetchall()
    form = {"final_price": p["final_price"], "stock": "" if p["stock"] is None else p["stock"],
            "keep_manual": True, "justification": ""}
    return render_template("product_detail.html", p=p, history=history, discs=discs, alerts=alerts,
                           form=form, errors={}, vat_rate=get_setting(g.db, "vat_rate", "19"))


@bp.route("/products/<int:product_id>/edit", methods=["POST"])
@admin_required
def product_edit(product_id):
    """Edición manual de precio y stock: solo para el acceso «Administrador»."""
    p = g.db.execute("SELECT p.*, s.name source_name FROM products p JOIN sources s ON s.id=p.source_id WHERE p.id=?",
                     (product_id,)).fetchone() or abort(404)
    form = {k: request.form.get(k, "").strip() for k in ("final_price", "stock", "justification")}
    form["keep_manual"] = bool(request.form.get("keep_manual"))
    errors = {}
    try:
        price = parse_price(form["final_price"])
    except InvalidRecord as exc:
        errors["final_price"] = exc.message
    stock = None
    if form["stock"] != "":
        try:
            stock = int(form["stock"].replace(".", ""))
            if stock < 0:
                raise ValueError
        except ValueError:
            errors["stock"] = "Ingrese un número entero de unidades (0 o más), o déjelo vacío"
    if len(" ".join(form["justification"].split())) < 5:
        errors["justification"] = "Indique el motivo del cambio (mínimo 5 caracteres)"
    if not errors:
        try:
            vat = load_business_settings(g.db)["vat_rate"]
            res = edit_product(g.db, product_id, final_price=price, stock=stock, keep_manual=form["keep_manual"],
                               username=g.user["username"], justification=form["justification"], vat_rate=vat)
            flash(f"Precio actualizado: {_clp(res['old_price'])} → {_clp(res['new_price'])}."
                  + (" El bot no lo sobrescribirá hasta que lo libere." if form["keep_manual"] else ""), "success")
            return redirect(url_for("web.product_detail", product_id=product_id))
        except ValueError as exc:
            errors["stock"] = str(exc)
    history = g.db.execute("SELECT * FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 100", (product_id,)).fetchall()
    discs = g.db.execute("SELECT * FROM discrepancies WHERE product_id=? ORDER BY id DESC", (product_id,)).fetchall()
    alerts = g.db.execute("SELECT * FROM price_alerts WHERE product_id=? ORDER BY id DESC LIMIT 50", (product_id,)).fetchall()
    return render_template("product_detail.html", p=p, history=history, discs=discs, alerts=alerts,
                           form=form, errors=errors, vat_rate=get_setting(g.db, "vat_rate", "19")), 400


@bp.route("/products/<int:product_id>/release", methods=["POST"])
@admin_required
def product_release(product_id):
    g.db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() or abort(404)
    release_product(g.db, product_id, g.user["username"])
    flash("El producto vuelve a seguir el precio y stock de la tienda desde la próxima captura.", "success")
    return redirect(url_for("web.product_detail", product_id=product_id))


def _clp(value) -> str:
    return "$" + f"{int(value):,}".replace(",", ".")


def _safe(v):
    """Neutraliza fórmulas al abrir el CSV en Excel (inyección CSV)."""
    text = "" if v is None else str(v)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv_response(header, rows, filename):
    """CSV con BOM y separador «;», que es el que usa Excel en configuración regional de Chile."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _date_range():
    """Filtros ?desde=AAAA-MM-DD&hasta=AAAA-MM-DD en hora de Chile -> límites UTC."""
    tz = get_tz(current_app.config["TIMEZONE"])
    bounds = []
    for key, add in (("desde", 0), ("hasta", 1)):
        raw = request.args.get(key, "").strip()
        if not raw:
            bounds.append(None)
            continue
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=add)
        except ValueError:
            abort(400, f"Fecha inválida en «{key}» (use AAAA-MM-DD)")
        bounds.append(to_str(day))
    return bounds


def _local(ts):
    if not ts:
        return ""
    return parse_ts(ts).astimezone(get_tz(current_app.config["TIMEZONE"])).strftime("%d-%m-%Y %H:%M:%S")


@bp.route("/export/catalog.csv")
@login_required
def export_catalog():
    safe = _safe
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["fuente", "sku", "ean", "nombre", "precio_neto", "iva", "precio_final", "stock", "disponible",
                "precio_congelado", "precio_fijado_manual", "retirado_de_tienda", "actualizado_utc"])
    for r in g.db.execute("SELECT p.*, s.name source_name FROM products p JOIN sources s ON s.id=p.source_id ORDER BY s.name, p.name"):
        w.writerow([safe(r["source_name"]), safe(r["sku"]), safe(r["ean"]), safe(r["name"]), r["net_price"], r["vat_amount"],
                    r["final_price"], r["stock"] if r["stock"] is not None else "",
                    "" if r["available"] is None else r["available"], r["price_frozen"], r["manual_override"],
                    "si" if r["missing_since"] else "no", r["updated_at"]])
    audit.log(g.db, g.user["username"], "catalog.export", "catalog", None, "CSV")
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=catalogo_scrapbot.csv"})


@bp.route("/export/price-changes.csv")
@login_required
def export_price_changes():
    """Auditoría de cambios de precio: cada cambio publicado, hecho por el bot o por un administrador.

    Incluye el precio anterior, la variación, quién lo hizo, el motivo (si fue manual)
    y si generó una alerta automática. Filtros: ?desde=&hasta= (AAAA-MM-DD) y ?source=ID.
    """
    since, until = _date_range()
    where, args = ["1=1"], []
    if since:
        where.append("h.captured_at >= ?")
        args.append(since)
    if until:
        where.append("h.captured_at < ?")
        args.append(until)
    source = request.args.get("source", "")
    if source.isdigit():
        where.append("p.source_id = ?")
        args.append(int(source))
    sql = f"""
        WITH ordered AS (
            SELECT h.*, LAG(h.final_price) OVER (PARTITION BY h.product_id ORDER BY h.id) AS prev_price
            FROM price_history h WHERE h.status IN ('applied', 'approved')
        )
        SELECT h.*, p.sku, p.ean, p.name, s.name source_name,
               (SELECT a.kind FROM price_alerts a WHERE a.history_id = h.id LIMIT 1) alert_kind,
               (SELECT d.resolved_by FROM discrepancies d WHERE d.history_id = h.id LIMIT 1) approved_by,
               (SELECT d.justification FROM discrepancies d WHERE d.history_id = h.id LIMIT 1) approval_note
        FROM ordered h JOIN products p ON p.id = h.product_id JOIN sources s ON s.id = p.source_id
        WHERE {" AND ".join(where)} AND (h.prev_price IS NULL OR h.prev_price != h.final_price OR h.changed_by IS NOT NULL)
        ORDER BY h.captured_at, h.id"""
    rows = []
    for r in g.db.execute(sql, args):
        prev = r["prev_price"]
        pct = "" if not prev else f"{(r['final_price'] - prev) / prev * 100:.2f}".replace(".", ",")
        if r["changed_by"]:
            origin, who, note = "Edición manual", r["changed_by"], r["note"]
        elif r["status"] == "approved":
            origin, who, note = "Aprobación de revisión", r["approved_by"], r["approval_note"]
        else:
            origin, who, note = ("Alta (primera captura)" if prev is None else "Bot (captura automática)"), "sistema", ""
        rows.append([_local(r["captured_at"]), _safe(r["source_name"]), _safe(r["sku"]), _safe(r["ean"]),
                     _safe(r["name"]), "" if prev is None else prev, r["final_price"], pct, r["net_price"],
                     r["vat_amount"], f"{r['vat_rate']:g}", "" if r["stock"] is None else r["stock"],
                     origin, _safe(who), _safe(note), r["alert_kind"] or "", r["run_id"] or ""])
    audit.log(g.db, g.user["username"], "price_audit.export", "price_history", None,
              f"CSV filas={len(rows)} desde={request.args.get('desde', '')} hasta={request.args.get('hasta', '')}")
    header = ["fecha_hora_chile", "fuente", "sku", "ean", "producto", "precio_anterior", "precio_nuevo",
              "variacion_pct", "precio_neto", "iva", "tasa_iva_pct", "stock", "origen", "usuario", "motivo",
              "alerta", "ejecucion"]
    return _csv_response(header, rows, "auditoria_cambios_precio.csv")


@bp.route("/export/audit.csv")
@login_required
def export_audit():
    """Bitácora completa (todas las acciones del sistema) con los mismos filtros de la pantalla Auditoría."""
    since, until = _date_range()
    where, args = ["1=1"], []
    user, action = request.args.get("user", "").strip(), request.args.get("action", "").strip()
    if user:
        where.append("username = ?")
        args.append(user)
    if action:
        where.append("action LIKE ? ESCAPE '\\'")
        args.append(_like(action))
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if until:
        where.append("created_at < ?")
        args.append(until)
    rows = [[_local(r["created_at"]), _safe(r["username"]), r["action"], r["entity"] or "",
             r["entity_id"] if r["entity_id"] is not None else "", _safe(r["detail"])]
            for r in g.db.execute(f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id", args)]
    audit.log(g.db, g.user["username"], "audit.export", "audit_log", None, f"CSV filas={len(rows)}")
    return _csv_response(["fecha_hora_chile", "usuario", "accion", "entidad", "id_entidad", "detalle"],
                         rows, "bitacora_auditoria.csv")


# --- Alertas automáticas -------------------------------------------------------

@bp.route("/alerts")
@login_required
def alerts():
    show = request.args.get("show", "unseen")
    where = "a.seen_at IS NULL" if show == "unseen" else "1=1"
    rows, pg = _paginate(f"SELECT COUNT(*) FROM price_alerts a WHERE {where}",
                         "SELECT a.*, p.name, p.sku, p.ean, p.final_price current_price, s.name source_name "
                         "FROM price_alerts a JOIN products p ON p.id=a.product_id JOIN sources s ON s.id=p.source_id "
                         f"WHERE {where} ORDER BY a.id DESC", [], _page())
    pending_reviews = g.db.execute("SELECT COUNT(*) FROM discrepancies WHERE status='pending'").fetchone()[0]
    return render_template("alerts.html", rows=rows, pg=pg, show=show, pending_reviews=pending_reviews,
                           alert_mode=get_setting(g.db, "alert_mode", "auto"))


@bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@login_required
def alert_ack(alert_id):
    acknowledge_alerts(g.db, g.user["username"], alert_id)
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("web.alerts"))


@bp.route("/alerts/ack-all", methods=["POST"])
@login_required
def alerts_ack_all():
    n = acknowledge_alerts(g.db, g.user["username"])
    flash(f"{n} alerta(s) marcadas como revisadas." if n else "No había alertas sin revisar.", "info")
    return redirect(url_for("web.alerts"))


@bp.route("/api/alerts/summary")
@login_required
def alerts_summary():
    """Consultado cada pocos segundos por static/alerts.js para avisar en pantalla sin recargar."""
    unseen = g.db.execute("SELECT COUNT(*) FROM price_alerts WHERE seen_at IS NULL").fetchone()[0]
    pending = g.db.execute("SELECT COUNT(*) FROM discrepancies WHERE status='pending'").fetchone()[0]
    try:
        after = int(request.args.get("after", "0"))
    except ValueError:
        after = 0
    latest = g.db.execute(
        "SELECT a.id, a.kind, a.reference_price, a.new_final_price, a.variation_pct, p.id product_id, p.name, "
        "s.name source_name FROM price_alerts a JOIN products p ON p.id=a.product_id JOIN sources s ON s.id=p.source_id "
        "WHERE a.id > ? ORDER BY a.id DESC LIMIT 5", (after,)).fetchall()
    last_id = g.db.execute("SELECT COALESCE(MAX(id), 0) FROM price_alerts").fetchone()[0]
    return jsonify({
        "unseen": unseen, "pending_reviews": pending, "last_id": last_id,
        "new": [{**dict(r), "url": url_for("web.product_detail", product_id=r["product_id"]),
                 "reference": _clp(r["reference_price"]), "price": _clp(r["new_final_price"])} for r in latest],
    })


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
    sources = g.db.execute("SELECT id, name FROM sources ORDER BY name").fetchall()
    return render_template("audit.html", rows=rows, pg=pg, user=user, action=action, sources=sources)


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
    keys = ("vat_rate", "variation_threshold", "schedule_times", "scheduler_enabled", "schedule_mode",
            "interval_minutes", "alert_mode", "drift_window_days")
    values = {k: get_setting(d, k) for k in keys}
    errors = {}
    if request.method == "POST":
        data, errors = validate_settings_form(request.form)
        values = {k: request.form.get(k, "") for k in keys if k != "scheduler_enabled"}
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
