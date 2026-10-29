-- Watchtower alerts, assistant conversations (text transcripts only; never audio), uploaded documents.
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,
    rule            TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL,
    refs            TEXT NOT NULL DEFAULT '[]',       -- JSON [{label,url}]
    recommendation  TEXT,
    detected_at     TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','acknowledged','resolved','dismissed')),
    assigned_to     INTEGER REFERENCES users(id),
    resolved_by     INTEGER REFERENCES users(id),
    resolved_at     TEXT,
    resolution_note TEXT,
    auto_resolved   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_alerts_status ON alerts(status, severity);
CREATE TABLE alert_events (
    id       INTEGER PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    ts       TEXT NOT NULL,
    actor_id INTEGER REFERENCES users(id),
    action   TEXT NOT NULL,
    note     TEXT
);
CREATE TABLE ai_conversations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    context    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE ai_messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    ts              TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    channel         TEXT NOT NULL CHECK (channel IN ('text','voice')),
    content         TEXT NOT NULL,
    meta            TEXT
);
CREATE TABLE documents (
    id             INTEGER PRIMARY KEY,
    original_name  TEXT NOT NULL,
    stored_name    TEXT NOT NULL UNIQUE,
    mime           TEXT NOT NULL,
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    ocr_method     TEXT,
    ocr_text       TEXT,
    extracted      TEXT,                               -- JSON of candidate fields + confidences
    status         TEXT NOT NULL CHECK (status IN ('uploaded','extracted','draft_created','rejected')),
    receipt_id     INTEGER REFERENCES goods_receipts(id),
    uploaded_by    INTEGER NOT NULL REFERENCES users(id),
    uploaded_at    TEXT NOT NULL
);
