-- Accessory Store. Separate item catalogue and ledger from fabric lots (accessories are not lot-tracked
-- in the confirmed requirements; lot tracking for accessories is an open question).
-- Quantities: integer thousandths. items.decimals = 0 forbids fractional quantities for count-type units.
CREATE TABLE acc_items (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    category      TEXT,
    unit          TEXT NOT NULL,
    decimals      INTEGER NOT NULL DEFAULT 0 CHECK (decimals IN (0,3)),
    reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),   -- 0 = no alert
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    is_demo       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE acc_locations (
    id     INTEGER PRIMARY KEY,
    code   TEXT NOT NULL UNIQUE,
    name   TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);
CREATE TABLE acc_docs (
    id               INTEGER PRIMARY KEY,
    doc_no           TEXT NOT NULL UNIQUE,
    doc_type         TEXT NOT NULL CHECK (doc_type IN ('receipt','issue','transfer')),
    supplier_id      INTEGER REFERENCES parties(id),
    from_location_id INTEGER REFERENCES acc_locations(id),
    to_location_id   INTEGER REFERENCES acc_locations(id),
    ref_type         TEXT CHECK (ref_type IN ('processing_job','multihead_order','other')),
    ref_no           TEXT,
    supplier_ref     TEXT,
    notes            TEXT,
    status           TEXT NOT NULL CHECK (status IN ('draft','posted','reversed','discarded')),
    created_by       INTEGER NOT NULL REFERENCES users(id),
    created_at       TEXT NOT NULL,
    posted_by        INTEGER REFERENCES users(id),
    posted_at        TEXT,
    reversed_by      INTEGER REFERENCES users(id),
    reversed_at      TEXT,
    reversal_reason  TEXT
);
CREATE INDEX ix_accdoc_ref ON acc_docs(ref_type, ref_no);
CREATE TABLE acc_doc_lines (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES acc_docs(id),
    item_id INTEGER NOT NULL REFERENCES acc_items(id),
    qty     INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (doc_id, item_id)
);
CREATE TABLE acc_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    posted_at   TEXT NOT NULL,
    item_id     INTEGER NOT NULL REFERENCES acc_items(id),
    location_id INTEGER NOT NULL REFERENCES acc_locations(id),
    qty         INTEGER NOT NULL CHECK (qty <> 0),
    movement    TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id   INTEGER NOT NULL,
    line_key    TEXT NOT NULL,
    reverses_id INTEGER REFERENCES acc_ledger(id),
    actor_id    INTEGER REFERENCES users(id),
    note        TEXT,
    UNIQUE (source_type, source_id, line_key)
);
CREATE INDEX ix_accledger_item_loc ON acc_ledger(item_id, location_id);
CREATE TRIGGER trg_accledger_no_update BEFORE UPDATE ON acc_ledger BEGIN SELECT RAISE(ABORT, 'acc_ledger is append-only'); END;
CREATE TRIGGER trg_accledger_no_delete BEFORE DELETE ON acc_ledger BEGIN SELECT RAISE(ABORT, 'acc_ledger is append-only'); END;
CREATE TRIGGER trg_accledger_no_negative BEFORE INSERT ON acc_ledger
WHEN (SELECT COALESCE(SUM(qty),0) FROM acc_ledger WHERE item_id = NEW.item_id AND location_id = NEW.location_id) + NEW.qty < 0
BEGIN SELECT RAISE(ABORT, 'negative stock is not allowed'); END;
