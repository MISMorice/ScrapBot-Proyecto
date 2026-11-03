#!/usr/bin/env python3
"""Línea de comandos de ScrapBot.

  python manage.py init-db
  python manage.py create-user martin --role admin
  python manage.py seed-demo            # fuente de ejemplo (requiere: python -m demo_site.app)
  python manage.py run --all            # una ejecución por fuente activa (útil para cron / Programador de tareas)
  python manage.py serve                # panel web + ejecuciones intradiarias
"""
import argparse
import getpass
import os
import sys

from scrapbot import audit, create_app
from scrapbot.db import connect, transaction
from scrapbot.engine.pipeline import RunInProgress
from scrapbot.util import now_str


def _app(scheduler: bool = False):
    return create_app({"DISABLE_SCHEDULER": not scheduler})


def cmd_init_db(args):
    app = _app()
    print(f"Base de datos lista en {app.config['DATABASE']}")


def cmd_create_user(args):
    app = _app()
    password = args.password or os.environ.get("SCRAPBOT_NEW_PASSWORD") or getpass.getpass("Contraseña: ")
    with app.test_request_context():
        from scrapbot.auth import hash_password, validate_password

        problem = validate_password(password)
        if problem:
            sys.exit(problem)
        pw_hash = hash_password(password)
    conn = connect(app.config["DATABASE"])
    try:
        conn.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                     (args.username, pw_hash, args.role, now_str()))
        audit.log(conn, "cli", "user.create", "user", None, f"{args.username} ({args.role})")
    except Exception as exc:
        sys.exit(f"No se pudo crear el usuario: {exc}")
    finally:
        conn.close()
    print(f"Usuario {args.username} creado ({args.role}).")


def cmd_seed_demo(args):
    app = _app()
    demo_sources = (
        ("Ferretería Demo", "/catalogo"),
        ("Electro Demo", "/electronica"),
        ("Jardín Demo", "/jardin"),
    )
    created = []
    conn = connect(app.config["DATABASE"])
    try:
        with transaction(conn):
            for name, path in demo_sources:
                if conn.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
                    continue
                cur = conn.execute(
                    "INSERT INTO sources (name, start_urls, active, respect_robots, delay_min, delay_max, created_at, created_by) "
                    "VALUES (?,?,1,1,1,2,?,'cli')", (name, args.url.rstrip("/") + path, now_str()))
                conn.execute(
                    "INSERT INTO source_rules (source_id, version, selector_type, item_selector, sku_selector, ean_selector, "
                    "name_selector, price_selector, stock_selector, next_page_selector, max_pages, price_includes_vat, is_current, "
                    "updated_at, updated_by) VALUES (?,1,'css','article.producto','@data-sku','span.ean','h2.nombre','span.precio',"
                    "'span.stock','a.siguiente',10,1,1,?, 'cli')", (cur.lastrowid, now_str()))
                audit.log(conn, "cli", "source.create", "source", cur.lastrowid, name)
                created.append(name)
    finally:
        conn.close()
    if created:
        print("Fuentes de demostración listas: " + ", ".join(created))
    else:
        print("Las fuentes de demostración ya estaban registradas.")
    print("Ejecute las fuentes desde el panel o con: python manage.py run --all")


def cmd_run(args):
    app = _app()
    runner = app.extensions["runner"]
    conn = connect(app.config["DATABASE"])
    try:
        if args.source:
            ids = [args.source]
        else:
            ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE active=1 ORDER BY id")]
    finally:
        conn.close()
    if not ids:
        sys.exit("No hay fuentes activas.")
    exit_code = 0
    for sid in ids:
        try:
            run_id = runner.start(sid, trigger="cli", user="cli", background=False)
        except RunInProgress as exc:
            print(f"Fuente {sid}: {exc}")
            exit_code = 1
            continue
        c = connect(app.config["DATABASE"])
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        c.close()
        print(f"Fuente {sid} · ejecución #{run_id}: {r['status']} · {r['items_found']} productos · "
              f"{r['items_frozen']} congelados · {r['errors_count']} errores · {r['duration_seconds']} s"
              + (f" · {r['message']}" if r["message"] else ""))
        if r["status"] == "failed":
            exit_code = 1
    sys.exit(exit_code)


def cmd_serve(args):
    app = _app(scheduler=True)
    try:
        from waitress import serve
        print(f"ScrapBot en http://{args.host}:{args.port} (waitress)")
        serve(app, host=args.host, port=args.port)
    except ImportError:
        print(f"ScrapBot en http://{args.host}:{args.port} (servidor de desarrollo; use waitress/gunicorn en producción)")
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    p = sub.add_parser("create-user")
    p.add_argument("username")
    p.add_argument("--role", choices=["admin", "analyst"], default="analyst")
    p.add_argument("--password", help="si se omite, se solicita por consola")
    p.set_defaults(fn=cmd_create_user)
    p = sub.add_parser("seed-demo")
    p.add_argument("--url", default="http://127.0.0.1:5001")
    p.set_defaults(fn=cmd_seed_demo)
    p = sub.add_parser("run")
    p.add_argument("--source", type=int)
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.set_defaults(fn=cmd_serve)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
