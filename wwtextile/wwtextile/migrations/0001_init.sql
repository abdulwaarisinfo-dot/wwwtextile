-- WW Textile schema, migration 0001.
-- Quantities are INTEGER thousandths of the item's unit.
-- Stock on hand is ALWAYS SUM(stock_ledger.qty); there is no balance column.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id)
);

CREATE TABLE sequences (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE parties (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL CHECK (kind IN ('supplier','customer','job_work')),
    name    TEXT NOT NULL UNIQUE,
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE items (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL,
    unit    TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE locations (
    id   INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('raw_store','wip','finished_store','reject_store'))
);

CREATE TABLE lots (
    id            INTEGER PRIMARY KEY,
    lot_no        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    supplier_id   INTEGER REFERENCES parties(id),
    parent_lot_id INTEGER REFERENCES lots(id),
    kind          TEXT NOT NULL CHECK (kind IN ('raw','finished')),
    created_at    TEXT NOT NULL,
    created_by    INTEGER REFERENCES users(id),
    is_demo       INTEGER NOT NULL DEFAULT 0
);

-- Append-only stock ledger. Corrections are new rows with reverses_id set.
CREATE TABLE stock_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    posted_at   TEXT NOT NULL,
    lot_id      INTEGER NOT NULL REFERENCES lots(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    qty         INTEGER NOT NULL CHECK (qty <> 0),
    movement    TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id   INTEGER NOT NULL,
    line_key    TEXT NOT NULL,
    reverses_id INTEGER REFERENCES stock_ledger(id),
    actor_id    INTEGER REFERENCES users(id),
    note        TEXT,
    -- one posting per source line: makes duplicate posting impossible at DB level
    UNIQUE (source_type, source_id, line_key)
);
CREATE INDEX ix_ledger_lot_loc ON stock_ledger(lot_id, location_id);
CREATE INDEX ix_ledger_source  ON stock_ledger(source_type, source_id);

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
-- DB-level backstop against negative stock (the service layer checks first).
CREATE TRIGGER trg_ledger_no_negative BEFORE INSERT ON stock_ledger
WHEN (SELECT COALESCE(SUM(qty),0) FROM stock_ledger
       WHERE lot_id = NEW.lot_id AND location_id = NEW.location_id) + NEW.qty < 0
BEGIN SELECT RAISE(ABORT, 'negative stock is not allowed'); END;

CREATE TABLE stock_reservations (
    id          INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL,
    lot_id      INTEGER NOT NULL REFERENCES lots(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    status      TEXT NOT NULL CHECK (status IN ('active','released','consumed')),
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);
CREATE INDEX ix_resv_active ON stock_reservations(lot_id, location_id, status);

CREATE TABLE goods_receipts (
    id              INTEGER PRIMARY KEY,
    doc_no          TEXT NOT NULL UNIQUE,
    supplier_id     INTEGER NOT NULL REFERENCES parties(id),
    item_id         INTEGER NOT NULL REFERENCES items(id),
    lot_no          TEXT NOT NULL COLLATE NOCASE,
    location_id     INTEGER NOT NULL REFERENCES locations(id),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    supplier_ref    TEXT,
    vehicle_no      TEXT,
    notes           TEXT,
    status          TEXT NOT NULL CHECK (status IN ('draft','posted','reversed','discarded')),
    lot_id          INTEGER REFERENCES lots(id),
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    posted_by       INTEGER REFERENCES users(id),
    posted_at       TEXT,
    reversed_by     INTEGER REFERENCES users(id),
    reversed_at     TEXT,
    reversal_reason TEXT
);

CREATE TABLE processing_jobs (
    id            INTEGER PRIMARY KEY,
    job_no        TEXT NOT NULL UNIQUE,
    lot_id        INTEGER NOT NULL REFERENCES lots(id),
    process_type  TEXT NOT NULL,
    party_id      INTEGER REFERENCES parties(id),
    expected_date TEXT,
    notes         TEXT,
    status        TEXT NOT NULL CHECK (status IN ('open','result_posted','cancelled')),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE processing_issues (
    id               INTEGER PRIMARY KEY,
    issue_no         TEXT NOT NULL UNIQUE,
    job_id           INTEGER NOT NULL REFERENCES processing_jobs(id),
    qty              INTEGER NOT NULL CHECK (qty > 0),
    from_location_id INTEGER NOT NULL REFERENCES locations(id),
    status           TEXT NOT NULL CHECK (status IN ('draft','posted','discarded')),
    created_by       INTEGER NOT NULL REFERENCES users(id),
    created_at       TEXT NOT NULL,
    posted_by        INTEGER REFERENCES users(id),
    posted_at        TEXT
);

CREATE TABLE processing_results (
    id                    INTEGER PRIMARY KEY,
    result_no             TEXT NOT NULL UNIQUE,
    issue_id              INTEGER NOT NULL REFERENCES processing_issues(id),
    job_id                INTEGER NOT NULL REFERENCES processing_jobs(id),
    accepted              INTEGER NOT NULL CHECK (accepted >= 0),
    rejected              INTEGER NOT NULL CHECK (rejected >= 0),
    wastage               INTEGER NOT NULL CHECK (wastage >= 0),
    shrinkage             INTEGER NOT NULL CHECK (shrinkage >= 0),
    remaining             INTEGER NOT NULL CHECK (remaining >= 0),
    variance              INTEGER NOT NULL,           -- computed, may be 0 or >0 (negative is rejected)
    reconciliation_status TEXT NOT NULL CHECK (reconciliation_status IN ('balanced','exception')),
    finished_lot_id       INTEGER REFERENCES lots(id),
    notes                 TEXT,
    status                TEXT NOT NULL CHECK (status IN ('draft','posted','discarded')),
    created_by            INTEGER NOT NULL REFERENCES users(id),
    created_at            TEXT NOT NULL,
    posted_by             INTEGER REFERENCES users(id),
    posted_at             TEXT
);

-- one live (draft or posted) result per issue; discarded rows stay as history
CREATE UNIQUE INDEX ux_result_issue ON processing_results(issue_id) WHERE status IN ('draft','posted');

CREATE TABLE dispatches (
    id           INTEGER PRIMARY KEY,
    doc_no       TEXT NOT NULL UNIQUE,
    customer_id  INTEGER NOT NULL REFERENCES parties(id),
    status       TEXT NOT NULL CHECK (status IN ('draft','approved','pass_issued','exited','cancelled')),
    notes        TEXT,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    approved_by  INTEGER REFERENCES users(id),
    approved_at  TEXT,
    deducted_at  TEXT,            -- set exactly once when stock leaves the ledger
    deducted_on  TEXT,            -- which lifecycle event caused the deduction
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE dispatch_lines (
    id          INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
    lot_id      INTEGER NOT NULL REFERENCES lots(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (dispatch_id, lot_id, location_id)
);

CREATE TABLE gate_passes (
    id          INTEGER PRIMARY KEY,
    pass_no     TEXT NOT NULL UNIQUE,
    dispatch_id INTEGER NOT NULL UNIQUE REFERENCES dispatches(id),
    status      TEXT NOT NULL CHECK (status IN ('issued','exited','void')),
    vehicle_no  TEXT NOT NULL,
    driver_name TEXT NOT NULL,
    issued_by   INTEGER NOT NULL REFERENCES users(id),
    issued_at   TEXT NOT NULL,
    exited_by   INTEGER REFERENCES users(id),
    exited_at   TEXT,
    exit_notes  TEXT
);

CREATE TABLE audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor_id    INTEGER,
    actor_name  TEXT NOT NULL,
    actor_role  TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_ref  TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('success','denied','failed')),
    detail      TEXT
);
CREATE INDEX ix_audit_ts ON audit_events(ts);
CREATE INDEX ix_audit_entity ON audit_events(entity_type, entity_ref);

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
