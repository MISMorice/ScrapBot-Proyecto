"""Orquestación de una ejecución: captura -> parsing -> normalización -> escritura atómica."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, fields
from datetime import timedelta
from urllib.parse import urlsplit

from .. import audit
from ..db import connect, load_business_settings, transaction
from ..util import now_str, parse_ts, utcnow, to_str
from .extractor import PageParser, RuleSet
from .http_client import FetchError, PoliteHttpClient
from .normalizer import InvalidRecord, normalize_record
from .store import apply_products

log = logging.getLogger("scrapbot.pipeline")


class RunInProgress(Exception):
    pass


@dataclass
class RunStats:
    pages_fetched: int = 0
    items_found: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    items_invalid: int = 0
    items_duplicates: int = 0
    items_frozen: int = 0
    errors_count: int = 0


def _same_site(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host

    return norm(a) == norm(b)


class SyncRunner:
    """Crea y ejecuta ejecuciones de scraping. `client_factory` permite inyectar un cliente en pruebas."""

    def __init__(self, config: dict, db_path: str, client_factory=None):
        self.config = config
        self.db_path = db_path
        self.client_factory = client_factory or self._default_client

    # --- Fábrica de cliente --------------------------------------------
    def _default_client(self, source, on_attempt):
        c = self.config
        return PoliteHttpClient(
            user_agent=c["USER_AGENT"], timeout=c["HTTP_TIMEOUT"], max_retries=c["HTTP_MAX_RETRIES"],
            backoff_base=c["HTTP_BACKOFF_BASE"], max_bytes=c["HTTP_MAX_BYTES"],
            delay_range=(source["delay_min"], source["delay_max"]),
            respect_robots=bool(source["respect_robots"]), on_attempt=on_attempt,
        )

    # --- Creación de la ejecución (atómica entre procesos) ---------------
    def create_run(self, source_id: int, trigger: str, user: str | None) -> int:
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                stale = to_str(utcnow() - timedelta(minutes=self.config.get("STALE_RUN_MINUTES", 120)))
                conn.execute(
                    "UPDATE runs SET status='failed', finished_at=?, message='Ejecución interrumpida (sin finalizar)' "
                    "WHERE status='running' AND started_at < ?", (now_str(), stale))
                if conn.execute("SELECT 1 FROM runs WHERE source_id=? AND status='running'", (source_id,)).fetchone():
                    raise RunInProgress("Ya hay una ejecución en curso para esta fuente")
                cur = conn.execute(
                    "INSERT INTO runs (source_id, trigger, triggered_by, started_at, status) VALUES (?,?,?,?, 'running')",
                    (source_id, trigger, user, now_str()))
                audit.log(conn, user or trigger, "run.start", "run", cur.lastrowid, f"fuente={source_id} origen={trigger}")
                return cur.lastrowid
        finally:
            conn.close()

    def start(self, source_id: int, trigger: str = "manual", user: str | None = None, background: bool = False) -> int:
        run_id = self.create_run(source_id, trigger, user)
        if background:
            threading.Thread(target=self.execute, args=(run_id,), daemon=True, name=f"run-{run_id}").start()
        else:
            self.execute(run_id)
        return run_id

    # --- Ejecución -------------------------------------------------------
    def execute(self, run_id: int) -> None:
        started = time.monotonic()
        conn = connect(self.db_path)
        stats = RunStats()
        status, message = "failed", None
        try:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            source = conn.execute("SELECT * FROM sources WHERE id=?", (run["source_id"],)).fetchone()
            rules_row = conn.execute("SELECT * FROM source_rules WHERE source_id=? AND is_current=1", (source["id"],)).fetchone()
            if rules_row is None:
                raise RuntimeError("La fuente no tiene reglas de extracción vigentes")
            rules = RuleSet.from_row(rules_row)
            biz = load_business_settings(conn)

            def on_attempt(**kw):
                conn.execute(
                    "INSERT INTO http_log (run_id, url, attempt, status_code, response_ms, content_sha256, error, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, kw["url"], kw["attempt"], kw["status_code"], kw["response_ms"], kw["sha256"], kw["error"], now_str()))
                if kw["error"]:
                    stats.errors_count += 1

            client = self.client_factory(source, on_attempt)

            def parse_error(url, index, field, msg):
                conn.execute("INSERT INTO parse_errors (run_id, url, item_index, field, message, created_at) VALUES (?,?,?,?,?,?)",
                             (run_id, url, index, field, msg, now_str()))

            collected = self._crawl(client, source, rules, stats, parse_error)
            products = self._normalize(collected, rules, biz, stats, parse_error)

            try:
                with transaction(conn):
                    res = apply_products(conn, run_id=run_id, source_id=source["id"], products=products,
                                         threshold=biz["variation_threshold"], vat_rate=biz["vat_rate"])
                    audit.log(conn, "sistema", "catalog.commit", "run", run_id,
                              f"creados={res.created} actualizados={res.updated} sin_cambios={res.unchanged} congelados={res.frozen}")
            except Exception as exc:
                log.exception("Rollback de la ejecución %s", run_id)
                stats.errors_count += 1
                message = f"Error al escribir en la base de datos; se revirtieron todos los cambios ({exc})"
                raise _Handled(message)

            stats.items_created, stats.items_updated = res.created, res.updated
            stats.items_unchanged, stats.items_frozen = res.unchanged, res.frozen
            for ident, msg in res.conflicts:
                stats.items_invalid += 1
                stats.errors_count += 1
                parse_error(None, None, "sku/ean", f"{ident}: {msg}")

            if stats.pages_fetched == 0:
                status, message = "failed", "No se pudo descargar ninguna página de la fuente"
            elif stats.items_found == 0:
                status, message = "failed", "Las páginas no contienen productos: revise los selectores (¿cambió el DOM?)"
            elif stats.errors_count > 0:
                status = "partial"
            else:
                status = "success"
        except _Handled as exc:
            status, message = "failed", str(exc)
        except Exception as exc:
            log.exception("Fallo inesperado en la ejecución %s", run_id)
            status, message = "failed", f"Error inesperado: {exc}"
        finally:
            try:
                sets = ", ".join(f"{f.name}=?" for f in fields(stats))
                conn.execute(
                    f"UPDATE runs SET {sets}, status=?, message=?, finished_at=?, duration_seconds=? WHERE id=?",
                    (*[getattr(stats, f.name) for f in fields(stats)], status, message, now_str(),
                     round(time.monotonic() - started, 2), run_id))
                audit.log(conn, "sistema", "run.finish", "run", run_id,
                          f"estado={status} productos={stats.items_found} errores={stats.errors_count}")
            finally:
                conn.close()

    # --- Fases -----------------------------------------------------------
    def _crawl(self, client, source, rules, stats, parse_error):
        parser = PageParser(rules)
        collected, visited = [], set()
        for start in [u.strip() for u in source["start_urls"].splitlines() if u.strip()]:
            url, pages = start, 0
            while url and pages < rules.max_pages and url not in visited:
                if not _same_site(url, start):
                    parse_error(url, None, "next_page", "Enlace de paginación fuera del dominio autorizado")
                    stats.errors_count += 1
                    break
                visited.add(url)
                try:
                    result = client.get(url)
                except FetchError:
                    break          # el detalle ya quedó en http_log
                stats.pages_fetched += 1
                pages += 1
                try:
                    page = parser.parse(result.text, result.final_url)
                except Exception as exc:
                    stats.errors_count += 1
                    parse_error(url, None, "page", f"No se pudo procesar el HTML: {exc}")
                    break
                if rules.item_selector and not page.items:
                    stats.errors_count += 1
                    parse_error(url, None, "item_selector", "El selector de ítems no devolvió resultados (posible cambio del DOM)")
                    break
                collected.extend((url, item) for item in page.items)
                url = page.next_url
        return collected

    def _normalize(self, collected, rules, biz, stats, parse_error):
        products, seen_sku, seen_ean = [], set(), set()
        for url, item in collected:
            stats.items_found += 1
            try:
                p = normalize_record(item.values, configured=rules.configured,
                                     includes_vat=rules.price_includes_vat, vat_rate=biz["vat_rate"],
                                     content_hash=item.content_hash)
            except InvalidRecord as exc:
                stats.items_invalid += 1
                stats.errors_count += 1
                parse_error(url, item.index, exc.field, exc.message)
                continue
            for fld, msg in p.warnings:
                stats.errors_count += 1
                parse_error(url, item.index, fld, msg)
            if (p.sku and p.sku in seen_sku) or (p.ean and p.ean in seen_ean):
                stats.items_duplicates += 1
                continue
            if p.sku:
                seen_sku.add(p.sku)
            if p.ean:
                seen_ean.add(p.ean)
            products.append(p)
        return products


class _Handled(Exception):
    pass
