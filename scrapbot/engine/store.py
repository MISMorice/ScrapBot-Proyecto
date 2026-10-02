"""Escritura transaccional del catálogo (Tabla 5, filas 6-9).

Todas las funciones asumen que el llamador ya abrió `transaction(conn)`,
salvo `resolve_discrepancy`, que abre la suya.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import audit
from ..db import transaction
from ..util import now_str
from .detector import evaluate_change
from .normalizer import NormalizedProduct


class IdentifierConflict(Exception):
    pass


@dataclass
class ApplyResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    frozen: int = 0
    conflicts: list = field(default_factory=list)   # [(identificador, mensaje)]


def find_product(conn, source_id: int, sku: str | None, ean: str | None):
    by_ean = conn.execute("SELECT * FROM products WHERE source_id=? AND ean=?", (source_id, ean)).fetchone() if ean else None
    by_sku = conn.execute("SELECT * FROM products WHERE source_id=? AND sku=?", (source_id, sku)).fetchone() if sku else None
    if by_ean and by_sku and by_ean["id"] != by_sku["id"]:
        raise IdentifierConflict(f"SKU {sku} y EAN {ean} pertenecen a productos distintos")
    return by_ean or by_sku


def _history(conn, product_id, run_id, net, vat, final, vat_rate, stock, status, chash, now) -> int:
    cur = conn.execute(
        "INSERT INTO price_history (product_id, run_id, net_price, vat_amount, final_price, vat_rate, "
        "stock, status, content_hash, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (product_id, run_id, net, vat, final, vat_rate, stock, status, chash, now),
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


def apply_products(conn: sqlite3.Connection, *, run_id: int, source_id: int,
                   products: list[NormalizedProduct], threshold: float, vat_rate: float) -> ApplyResult:
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
            continue

        pid = existing["id"]
        decision = evaluate_change(existing["final_price"], p.final_price, threshold)
        pending = conn.execute("SELECT * FROM discrepancies WHERE product_id=? AND status='pending'", (pid,)).fetchone()
        new_stock = p.stock if p.stock is not None else existing["stock"]
        new_avail = existing["available"] if p.available is None else int(p.available)
        stock_changed = (new_stock != existing["stock"]) or (new_avail != existing["available"])
        name_changed = p.name != existing["name"]
        # completar identificadores faltantes (la unicidad ya fue verificada)
        sku = existing["sku"] or p.sku
        ean = existing["ean"] or p.ean

        if decision.action == "freeze":
            hist_id = _history(conn, pid, run_id, p.net_price, p.vat_amount, p.final_price, vat_rate,
                               new_stock, "held", p.content_hash, now)
            if pending:
                if pending["history_id"]:
                    conn.execute("UPDATE price_history SET status='superseded' WHERE id=?", (pending["history_id"],))
                conn.execute(
                    "UPDATE discrepancies SET run_id=?, history_id=?, new_net_price=?, new_vat_amount=?, "
                    "new_final_price=?, variation_pct=?, threshold_pct=?, updated_at=? WHERE id=?",
                    (run_id, hist_id, p.net_price, p.vat_amount, p.final_price, decision.pct, threshold, now, pending["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO discrepancies (product_id, run_id, history_id, old_final_price, new_net_price, "
                    "new_vat_amount, new_final_price, variation_pct, threshold_pct, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (pid, run_id, hist_id, existing["final_price"], p.net_price, p.vat_amount, p.final_price,
                     decision.pct, threshold, now, now),
                )
            conn.execute(
                "UPDATE products SET sku=?, ean=?, name=?, stock=?, available=?, price_frozen=1, last_seen_at=?, updated_at=? WHERE id=?",
                (sku, ean, p.name, new_stock, new_avail, now, now, pid),
            )
            result.frozen += 1
            continue

        # Sin anomalía: se publica (o no cambia).
        if pending:
            _close_pending(conn, pending, "superseded", "sistema",
                           "La variación dejó de superar el umbral en una captura posterior", now)
        changed = decision.action == "apply" or stock_changed or name_changed or bool(pending) or existing["price_frozen"]
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
        if decision.action == "apply" or stock_changed:
            _history(conn, pid, run_id, p.net_price, p.vat_amount, p.final_price, vat_rate, new_stock,
                     "applied", p.content_hash, now)
        result.updated += 1
    return result


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
