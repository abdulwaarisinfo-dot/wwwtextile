-- Multihead: a GENERIC, unit-explicit production-order module. What Multihead physically is, its unit and its
-- stock effect are open business questions; nothing here assumes them (see BUSINESS_RULES.md M1-M6).
CREATE TABLE mh_machines (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name    TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE mh_orders (
    id            INTEGER PRIMARY KEY,
    order_no      TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    unit          TEXT NOT NULL,              -- chosen per order from the admin-configured list
    planned_qty   INTEGER CHECK (planned_qty IS NULL OR planned_qty > 0),
    machine_id    INTEGER REFERENCES mh_machines(id),
    operator      TEXT,
    due_date      TEXT,
    notes         TEXT,
    status        TEXT NOT NULL CHECK (status IN ('open','completed','cancelled')),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    closed_by     INTEGER REFERENCES users(id),
    closed_at     TEXT,
    close_note    TEXT
);
CREATE TABLE mh_logs (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES mh_orders(id),
    log_date     TEXT NOT NULL,
    machine_id   INTEGER REFERENCES mh_machines(id),
    operator     TEXT,
    good_qty     INTEGER NOT NULL CHECK (good_qty >= 0),
    rejected_qty INTEGER NOT NULL CHECK (rejected_qty >= 0),
    notes        TEXT,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    voided_by    INTEGER REFERENCES users(id),
    voided_at    TEXT,
    void_reason  TEXT,
    CHECK (good_qty + rejected_qty > 0)
);
CREATE TRIGGER trg_mhlog_immutable BEFORE UPDATE ON mh_logs
WHEN NEW.good_qty <> OLD.good_qty OR NEW.rejected_qty <> OLD.rejected_qty OR NEW.order_id <> OLD.order_id OR NEW.log_date <> OLD.log_date
BEGIN SELECT RAISE(ABORT, 'production log quantities are immutable; void and re-enter'); END;
CREATE TRIGGER trg_mhlog_no_delete BEFORE DELETE ON mh_logs BEGIN SELECT RAISE(ABORT, 'production logs cannot be deleted'); END;
-- Fabric consumed by an order. stock_effect says whether a ledger row was posted (owner-configurable policy).
CREATE TABLE mh_consumptions (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES mh_orders(id),
    lot_id       INTEGER NOT NULL REFERENCES lots(id),
    location_id  INTEGER NOT NULL REFERENCES locations(id),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    waste_qty    INTEGER NOT NULL DEFAULT 0 CHECK (waste_qty >= 0 AND waste_qty <= qty),
    stock_effect TEXT NOT NULL CHECK (stock_effect IN ('posted','record_only')),
    notes        TEXT,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    voided_by    INTEGER REFERENCES users(id),
    voided_at    TEXT,
    void_reason  TEXT
);
