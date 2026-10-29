-- Physical gate register for movements that are NOT dispatch gate passes.
-- A gate entry never changes stock: physical movement is recorded separately from inventory postings.
CREATE TABLE gate_entries (
    id             INTEGER PRIMARY KEY,
    entry_no       TEXT NOT NULL UNIQUE,
    direction      TEXT NOT NULL CHECK (direction IN ('inward','outward')),
    vehicle_no     TEXT NOT NULL,
    driver_name    TEXT NOT NULL,
    driver_phone   TEXT,
    party_id       INTEGER REFERENCES parties(id),
    party_text     TEXT,
    material       TEXT NOT NULL,
    declared_qty   INTEGER CHECK (declared_qty IS NULL OR declared_qty > 0),
    declared_unit  TEXT,
    ref_type       TEXT CHECK (ref_type IN ('goods_receipt','processing_issue','acc_doc','other')),
    ref_no         TEXT,
    notes          TEXT,
    status         TEXT NOT NULL CHECK (status IN ('awaiting_approval','inside','pending_exit','exited','cancelled')),
    entered_by     INTEGER NOT NULL REFERENCES users(id),
    entered_at     TEXT NOT NULL,
    approved_by    INTEGER REFERENCES users(id),
    approved_at    TEXT,
    exited_by      INTEGER REFERENCES users(id),
    exited_at      TEXT,
    exit_notes     TEXT,
    cancelled_by   INTEGER REFERENCES users(id),
    cancelled_at   TEXT,
    cancel_reason  TEXT
);
CREATE INDEX ix_gate_entries_status ON gate_entries(status);
