-- ScrapBot · capa de persistencia (SQLite 3). Fechas en UTC 'YYYY-MM-DD HH:MM:SS'.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'analyst')),
    active          INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_log (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    success    INTEGER NOT NULL,
    reason     TEXT,
    ip         TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    start_urls     TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    respect_robots INTEGER NOT NULL DEFAULT 1,
    delay_min      REAL NOT NULL DEFAULT 2,
    delay_max      REAL NOT NULL DEFAULT 5,
    created_at     TEXT NOT NULL,
    created_by     TEXT
);

CREATE TABLE IF NOT EXISTS source_rules (
    id                 INTEGER PRIMARY KEY,
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    version            INTEGER NOT NULL,
    selector_type      TEXT NOT NULL CHECK (selector_type IN ('css', 'xpath')),
    item_selector      TEXT,
    sku_selector       TEXT,
    ean_selector       TEXT,
    name_selector      TEXT NOT NULL,
    price_selector     TEXT NOT NULL,
    stock_selector     TEXT,
    next_page_selector TEXT,
    max_pages          INTEGER NOT NULL DEFAULT 20,
    price_includes_vat INTEGER NOT NULL DEFAULT 1,
    is_current         INTEGER NOT NULL DEFAULT 1,
    updated_at         TEXT NOT NULL,
    updated_by         TEXT,
    UNIQUE (source_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rules_current ON source_rules(source_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    sku          TEXT,
    ean          TEXT,
    name         TEXT NOT NULL,
    net_price    INTEGER NOT NULL,
    vat_amount   INTEGER NOT NULL,
    final_price  INTEGER NOT NULL,
    stock        INTEGER,
    available    INTEGER,
    price_frozen INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    last_seen_at TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (sku IS NOT NULL OR ean IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_products_sku ON products(source_id, sku) WHERE sku IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_products_ean ON products(source_id, ean) WHERE ean IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_products_name ON products(name);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id),
    trigger          TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'cli')),
    triggered_by     TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    pages_fetched    INTEGER NOT NULL DEFAULT 0,
    items_found      INTEGER NOT NULL DEFAULT 0,
    items_created    INTEGER NOT NULL DEFAULT 0,
    items_updated    INTEGER NOT NULL DEFAULT 0,
    items_unchanged  INTEGER NOT NULL DEFAULT 0,
    items_invalid    INTEGER NOT NULL DEFAULT 0,
    items_duplicates INTEGER NOT NULL DEFAULT 0,
    items_frozen     INTEGER NOT NULL DEFAULT 0,
    errors_count     INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    message          TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_source ON runs(source_id, started_at);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    run_id       INTEGER REFERENCES runs(id),
    net_price    INTEGER NOT NULL,
    vat_amount   INTEGER NOT NULL,
    final_price  INTEGER NOT NULL,
    vat_rate     REAL NOT NULL,
    stock        INTEGER,
    status       TEXT NOT NULL CHECK (status IN ('applied', 'held', 'approved', 'rejected', 'superseded')),
    content_hash TEXT,
    captured_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history_product ON price_history(product_id, captured_at);

CREATE TABLE IF NOT EXISTS discrepancies (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    run_id          INTEGER REFERENCES runs(id),
    history_id      INTEGER REFERENCES price_history(id),
    old_final_price INTEGER NOT NULL,
    new_net_price   INTEGER NOT NULL,
    new_vat_amount  INTEGER NOT NULL,
    new_final_price INTEGER NOT NULL,
    variation_pct   REAL NOT NULL,
    threshold_pct   REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    resolved_by     TEXT,
    resolved_at     TEXT,
    justification   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_disc_pending ON discrepancies(product_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS http_log (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    url            TEXT NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 1,
    status_code    INTEGER,
    response_ms    INTEGER,
    content_sha256 TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_http_run ON http_log(run_id);

CREATE TABLE IF NOT EXISTS parse_errors (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    url        TEXT,
    item_index INTEGER,
    field      TEXT,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_parse_run ON parse_errors(run_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    action     TEXT NOT NULL,
    entity     TEXT,
    entity_id  INTEGER,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log(created_at);
