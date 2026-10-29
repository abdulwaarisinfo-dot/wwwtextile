-- Platform: password lifecycle, notifications.
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN password_changed_at TEXT;

CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY,
    role       TEXT,                       -- delivered to everyone with this role, or
    user_id    INTEGER REFERENCES users(id), -- to one user
    title      TEXT NOT NULL,
    body       TEXT,
    url        TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (role IS NOT NULL OR user_id IS NOT NULL)
);
CREATE TABLE notification_reads (
    notification_id INTEGER NOT NULL REFERENCES notifications(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    read_at         TEXT NOT NULL,
    PRIMARY KEY (notification_id, user_id)
);
