-- Finance sub-ledger. Money is INTEGER paisa (minor units). No tax, no FBR, no chart of accounts:
-- those policies are not defined (BUSINESS_RULES.md). Finance records never change stock.
CREATE TABLE fin_entries (
    id           INTEGER PRIMARY KEY,
    entry_no     TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('bill','invoice','payment_out','payment_in','expense')),
    party_id     INTEGER REFERENCES parties(id),
    entry_date   TEXT NOT NULL,                       -- YYYY-MM-DD
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    currency     TEXT NOT NULL,
    category     TEXT,                                 -- expenses only; free text, no chart of accounts
    method       TEXT,                                 -- payments only: free text (cash/bank/cheque...)
    reference    TEXT,                                 -- external bill/cheque/transaction reference
    doc_ref_type TEXT CHECK (doc_ref_type IN ('goods_receipt','dispatch')),
    doc_ref_no   TEXT,
    description  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('draft','posted','voided','discarded')),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    posted_by    INTEGER REFERENCES users(id),
    posted_at    TEXT,
    voided_by    INTEGER REFERENCES users(id),
    voided_at    TEXT,
    void_reason  TEXT,
    CHECK ((kind IN ('bill','payment_out','invoice','payment_in') AND party_id IS NOT NULL) OR kind = 'expense')
);
CREATE INDEX ix_fin_party ON fin_entries(party_id, status);
CREATE INDEX ix_fin_date ON fin_entries(entry_date);
-- Once posted, the money facts can never change; only status (void) may.
CREATE TRIGGER trg_fin_immutable BEFORE UPDATE ON fin_entries
WHEN OLD.status <> 'draft' AND (NEW.amount_paise <> OLD.amount_paise OR NEW.kind <> OLD.kind
     OR IFNULL(NEW.party_id,0) <> IFNULL(OLD.party_id,0) OR NEW.entry_date <> OLD.entry_date OR NEW.currency <> OLD.currency)
BEGIN SELECT RAISE(ABORT, 'posted finance entries are immutable'); END;
CREATE TRIGGER trg_fin_no_delete BEFORE DELETE ON fin_entries BEGIN SELECT RAISE(ABORT, 'finance entries cannot be deleted'); END;

CREATE TABLE fin_allocations (
    id           INTEGER PRIMARY KEY,
    payment_id   INTEGER NOT NULL REFERENCES fin_entries(id),
    bill_id      INTEGER NOT NULL REFERENCES fin_entries(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    UNIQUE (payment_id, bill_id)
);
CREATE TRIGGER trg_finalloc_no_update BEFORE UPDATE ON fin_allocations BEGIN SELECT RAISE(ABORT, 'allocations are append-only'); END;
CREATE TRIGGER trg_finalloc_no_delete BEFORE DELETE ON fin_allocations BEGIN SELECT RAISE(ABORT, 'allocations are append-only'); END;
