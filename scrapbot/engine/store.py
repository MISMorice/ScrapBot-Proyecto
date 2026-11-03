"""Escritura transaccional del catálogo (Tabla 5, filas 6-9).

Todas las funciones asumen que el llamador ya abrió `transaction(conn)`,
salvo `resolve_discrepancy`, que abre la suya.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import json
from datetime import timedelta

from .. import audit
from ..db import transaction
from ..util import now_str, parse_ts, to_str
from .detector import evaluate_change, variation_pct
from .normalizer import MAX_STOCK, NormalizedProduct, compute_breakdown


class IdentifierConflict(Exception):
    pass


@dataclass
class ApplyResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    frozen: int = 0
    alerted: int = 0
    locked: int = 0                                   # productos con precio fijado por un administrador
    conflicts: list = field(default_factory=list)   # [(identificador, mensaje)]
    seen_ids: set = field(default_factory=set)      # productos presentes en esta captura


def find_product(conn, source_id: int, sku: str | None, ean: str | None):
    by_ean = conn.execute("SELECT * FROM products WHERE source_id=? AND ean=?", (source_id, ean)).fetchone() if ean else None
    by_sku = conn.execute("SELECT * FROM products WHERE source_id=? AND sku=?", (source_id, sku)).fetchone() if sku else None
    if by_ean and by_sku and by_ean["id"] != by_sku["id"]:
        raise IdentifierConflict(f"SKU {sku} y EAN {ean} pertenecen a productos distintos")
    return by_ean or by_sku


def _history(conn, product_id, run_id, net, vat, final, vat_rate, stock, status, chash, now,
             changed_by=None, note=None) -> int:
    cur = conn.execute(
        "INSERT INTO price_history (product_id, run_id, net_price, vat_amount, final_price, vat_rate, "
        "stock, status, content_hash, captured_at, changed_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (product_id, run_id, net, vat, final, vat_rate, stock, status, chash, now, changed_by, note),
    )
    return cur.lastrowid


def _close_pending(conn, pending, status: str, who: str, note: str, now: str) -> None:
    conn.execute(
        "UPDATE discrepancies SET status=?, resolved_by=?, resolved_at=?, justification=?, updated_at=? WHERE id=?",
        (status, who, now, note, now, pending["id"]),
    )
    if pending["history_id"]:
        hist = {"superseded": "superseded", "approved": "approved", "rejected": "rejected"}[status]
        conn.execute("UPDATE price_history SET status=? WHERE id=?", (hist, pending["history_id"]))


def drift_reference(conn, product_id: int, window_days: int, now: str) -> int | None:
    """Precio de referencia para detectar subidas o bajadas graduales.

    Es el precio publicado más antiguo dentro de la ventana (por defecto 7 días),
    contado desde el último evento que "reinicia" la comparación: una alerta,
    una revisión o una edición manual. Así, una serie de cambios pequeños que
    sumados superan el umbral genera una sola alerta, no una por captura.
    """
    if window_days <= 0:
        return None
    since = to_str(parse_ts(now) - timedelta(days=window_days))
    # El reinicio se ubica por id del historial (no por hora, que tiene resolución de segundos).
    last_event = conn.execute(
        "SELECT MAX(h) FROM ("
        " SELECT MAX(history_id) h FROM price_alerts WHERE product_id=:p"
        " UNION ALL SELECT MAX(history_id) FROM discrepancies WHERE product_id=:p"
        " UNION ALL SELECT MAX(id) FROM price_history WHERE product_id=:p AND changed_by IS NOT NULL)",
        {"p": product_id}).fetchone()[0] or 0
    row = conn.execute(
        "SELECT final_price FROM price_history WHERE product_id=? AND status IN ('applied','approved') "
        "AND captured_at >= ? AND id >= ? ORDER BY id LIMIT 1", (product_id, since, last_event)).fetchone()
    return row["final_price"] if row else None


def _alert_kind(conn, existing, new_final: int, threshold: float, window_days: int, now: str):
    """Devuelve (tipo, precio_referencia, variación) o (None, None, None)."""
    decision = evaluate_change(existing["final_price"], new_final, threshold)
    if decision.action == "freeze":
        return "salto", existing["final_price"], decision.pct
    if decision.action == "apply":
        ref = drift_reference(conn, existing["id"], window_days, now)
        if ref is not None and ref != new_final:
            pct = round(variation_pct(ref, new_final), 2)
            if pct > threshold:
                return "acumulada", ref, pct
    return None, None, None


def apply_products(conn: sqlite3.Connection, *, run_id: int, source_id: int,
                   products: list[NormalizedProduct], threshold: float, vat_rate: float,
                   mode: str = "review", drift_window_days: int = 7) -> ApplyResult:
    """Escribe los productos capturados.

    `mode='auto'`: toda variación se publica de inmediato; si supera el umbral
    (de una vez o acumulada en la ventana) se registra una alerta en
    `price_alerts`, sin esperar a ninguna persona.
    `mode='review'`: comportamiento original; la variación queda retenida en
    `discrepancies` hasta que alguien la apruebe o rechace.
    """
    result = ApplyResult()
    for p in products:
        now = now_str()
        try:
            existing = find_product(conn, source_id, p.sku, p.ean)
        except IdentifierConflict as exc:
            result.conflicts.append((p.sku or p.ean, str(exc)))
            continue

        if existing is None:
            cur = conn.execute(
                "INSERT INTO products (source_id, sku, ean, name, net_price, vat_amount, final_price, stock, "
                "available, price_frozen, content_hash, last_seen_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (source_id, p.sku, p.ean, p.name, p.net_price, p.vat_amount, p.final_price, p.stock,
                 None if p.available is None else int(p.available), p.content_hash, now, now, now),
            )
            _history(conn, cur.lastrowid, run_id, p.net_price, p.vat_amount, p.final_price, vat_rate,
                     p.stock, "applied", p.content_hash, now)
            result.created += 1
            result.seen_ids.add(cur.lastrowid)
            continue

        pid = existing["id"]
        result.seen_ids.add(pid)
        # completar identificadores faltantes (la unicidad ya fue verificada)
        sku = existing["sku"] or p.sku
        ean = existing["ean"] or p.ean

        if existing["manual_override"]:
            # Un administrador fijó precio y stock: el bot solo confirma que el producto sigue publicado.
            conn.execute("UPDATE products SET sku=?, ean=?, name=?, last_seen_at=? WHERE id=?",
                         (sku, ean, p.name, now, pid))
            result.locked += 1
            continue

        kind, ref_price, pct = _alert_kind(conn, existing, p.final_price, threshold, drift_window_days, now)
        pending = conn.execute("SELECT * FROM discrepancies WHERE product_id=? AND status='pending'", (pid,)).fetchone()
        new_stock = p.stock if p.stock is not None else existing["stock"]
        new_avail = existing["available"] if p.available is None else int(p.available)
        stock_changed = (new_stock != existing["stock"]) or (new_avail != existing["available"])
        name_changed = p.name != existing["name"]
        price_changed = p.final_price != existing["final_price"]
        # Mismo precio final, pero otro desglose: cambió la tasa de IVA en Parámetros.
        breakdown_changed = (p.net_price, p.vat_amount) != (existing["net_price"], existing["vat_amount"])

        if kind and mode == "review":
            hist_id = _history(conn, pid, run_id, p.net_price, p.vat_amount, p.final_price, vat_rate,
                               new_stock, "held", p.content_hash, now)
            if pending:
                if pending["history_id"]:
                    conn.execute("UPDATE price_history SET status='superseded' WHERE id=?", (pending["history_id"],))
                conn.execute(
                    "UPDATE discrepancies SET run_id=?, history_id=?, new_net_price=?, new_vat_amount=?, "
                    "new_final_price=?, variation_pct=?, threshold_pct=?, updated_at=? WHERE id=?",
                    (run_id, hist_id, p.net_price, p.vat_amount, p.final_price, pct, threshold, now, pending["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO discrepancies (product_id, run_id, history_id, old_final_price, new_net_price, "
                    "new_vat_amount, new_final_price, variation_pct, threshold_pct, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (pid, run_id, hist_id, existing["final_price"], p.net_price, p.vat_amount, p.final_price,
                     pct, threshold, now, now),
                )
            conn.execute(
                "UPDATE products SET sku=?, ean=?, name=?, stock=?, available=?, price_frozen=1, last_seen_at=?, updated_at=? WHERE id=?",
                (sku, ean, p.name, new_stock, new_avail, now, now, pid),
            )
            result.frozen += 1
            continue

        # Se publica (o no cambia).
        if pending:
            note = ("Publicado automáticamente: el modo de alertas es automático" if kind
                    else "La variación dejó de superar el umbral en una captura posterior")
            _close_pending(conn, pending, "superseded", "sistema", note, now)
        changed = (price_changed or breakdown_changed or stock_changed or name_changed
                   or bool(pending) or existing["price_frozen"])
        if not changed:
            conn.execute("UPDATE products SET last_seen_at=? WHERE id=?", (now, pid))
            result.unchanged += 1
            continue
        conn.execute(
            "UPDATE products SET sku=?, ean=?, name=?, net_price=?, vat_amount=?, final_price=?, stock=?, "
            "available=?, price_frozen=0, content_hash=?, last_seen_at=?, updated_at=? WHERE id=?",
            (sku, ean, p.name, p.net_price, p.vat_amount, p.final_price, new_stock, new_avail,
             p.content_hash, now, now, pid),
        )
        hist_id = None
        if price_changed or breakdown_changed or stock_changed:
            hist_id = _history(conn, pid, run_id, p.net_price, p.vat_amount, p.final_price, vat_rate, new_stock,
                               "applied", p.content_hash, now)
        if kind:      # modo automático: ya se publicó; se deja la alerta
            conn.execute(
                "INSERT INTO price_alerts (product_id, run_id, history_id, kind, reference_price, new_final_price, "
                "variation_pct, threshold_pct, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, run_id, hist_id, kind, ref_price, p.final_price, pct, threshold, now))
            result.alerted += 1
        result.updated += 1
    return result


def mark_missing(conn, source_id: int, seen_ids: set, complete: bool) -> int:
    """Actualiza qué productos sigue publicando la tienda.

    Los vistos en esta captura vuelven a ser visibles. Si la captura fue completa
    (todas las páginas, sin cortes), los que no aparecieron se marcan como retirados
    y dejan de mostrarse en el catálogo público. Con una captura incompleta no se
    retira nada: que falte un producto puede deberse solo a la página que falló.
    Devuelve cuántos productos pasaron a «retirado».
    """
    ids = json.dumps(sorted(seen_ids))
    conn.execute("UPDATE products SET missing_since=NULL WHERE source_id=? AND missing_since IS NOT NULL "
                 "AND id IN (SELECT value FROM json_each(?))", (source_id, ids))
    if not complete:
        return 0
    cur = conn.execute("UPDATE products SET missing_since=? WHERE source_id=? AND missing_since IS NULL "
                       "AND id NOT IN (SELECT value FROM json_each(?))", (now_str(), source_id, ids))
    return cur.rowcount


def edit_product(conn: sqlite3.Connection, product_id: int, *, final_price: int, stock: int | None,
                 keep_manual: bool, username: str, justification: str, vat_rate: float) -> dict:
    """Edición manual de precio y stock por un administrador (queda en historial y auditoría).

    El precio ingresado es el precio final con IVA; el neto y el IVA se recalculan.
    Con `keep_manual`, las capturas siguientes no lo sobrescriben hasta que se libere.
    """
    justification = " ".join((justification or "").split())
    if len(justification) < MIN_JUSTIFICATION:
        raise ValueError(f"El motivo es obligatorio (mínimo {MIN_JUSTIFICATION} caracteres)")
    if stock is not None and not 0 <= stock <= MAX_STOCK:
        raise ValueError("El stock debe estar entre 0 y 10.000.000")
    b = compute_breakdown(final_price, True, vat_rate)
    with transaction(conn):
        old = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if old is None:
            raise LookupError("El producto no existe")
        now = now_str()
        available = (stock > 0) if stock is not None else old["available"]
        conn.execute(
            "UPDATE products SET net_price=?, vat_amount=?, final_price=?, stock=?, available=?, price_frozen=0, "
            "manual_override=?, updated_at=? WHERE id=?",
            (b.net, b.vat, b.final, stock, None if available is None else int(available), int(keep_manual), now, product_id))
        pending = conn.execute("SELECT * FROM discrepancies WHERE product_id=? AND status='pending'", (product_id,)).fetchone()
        if pending:
            _close_pending(conn, pending, "superseded", username, "Reemplazada por una edición manual del precio", now)
        _history(conn, product_id, None, b.net, b.vat, b.final, vat_rate, stock, "applied",
                 old["content_hash"], now, changed_by=username, note=justification)
        detail = (f"precio {old['final_price']}->{b.final}; stock {old['stock']}->{stock}; "
                  f"{'fijado' if keep_manual else 'sigue a la tienda'}; motivo={justification}")
        audit.log(conn, username, "product.edit", "product", product_id, detail)
    return {"old_price": old["final_price"], "new_price": b.final}


def release_product(conn: sqlite3.Connection, product_id: int, username: str) -> None:
    """Vuelve a dejar que el bot actualice precio y stock desde la tienda."""
    with transaction(conn):
        cur = conn.execute("UPDATE products SET manual_override=0, updated_at=? WHERE id=? AND manual_override=1",
                           (now_str(), product_id))
        if cur.rowcount:
            audit.log(conn, username, "product.release", "product", product_id, "vuelve a seguir el precio de la tienda")


def acknowledge_alerts(conn: sqlite3.Connection, username: str, alert_id: int | None = None) -> int:
    now = now_str()
    if alert_id is None:
        cur = conn.execute("UPDATE price_alerts SET seen_by=?, seen_at=? WHERE seen_at IS NULL", (username, now))
    else:
        cur = conn.execute("UPDATE price_alerts SET seen_by=?, seen_at=? WHERE id=? AND seen_at IS NULL",
                           (username, now, alert_id))
    if cur.rowcount:
        audit.log(conn, username, "alert.ack", "price_alert", alert_id, f"{cur.rowcount} alerta(s) revisada(s)")
    return cur.rowcount


MIN_JUSTIFICATION = 5


def resolve_discrepancy(conn: sqlite3.Connection, discrepancy_id: int, *, approve: bool,
                        username: str, justification: str) -> None:
    """Aprobar/rechazar una variación retenida. La justificación es obligatoria."""
    justification = " ".join((justification or "").split())
    if len(justification) < MIN_JUSTIFICATION:
        raise ValueError(f"La justificación es obligatoria (mínimo {MIN_JUSTIFICATION} caracteres)")
    with transaction(conn):
        disc = conn.execute("SELECT * FROM discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
        if disc is None or disc["status"] != "pending":
            raise LookupError("La alerta no existe o ya fue resuelta")
        now = now_str()
        if approve:
            conn.execute(
                "UPDATE products SET net_price=?, vat_amount=?, final_price=?, price_frozen=0, updated_at=? WHERE id=?",
                (disc["new_net_price"], disc["new_vat_amount"], disc["new_final_price"], now, disc["product_id"]),
            )
        else:
            conn.execute("UPDATE products SET price_frozen=0, updated_at=? WHERE id=?", (now, disc["product_id"]))
        status = "approved" if approve else "rejected"
        _close_pending(conn, disc, status, username, justification, now)
        audit.log(conn, username, f"discrepancy.{status}", "discrepancy", discrepancy_id,
                  f"producto={disc['product_id']} {disc['old_final_price']}->{disc['new_final_price']} "
                  f"({disc['variation_pct']}%) motivo={justification}")
