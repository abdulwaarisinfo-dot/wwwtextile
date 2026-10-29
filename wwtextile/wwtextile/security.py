"""Password hashing, sessions, lockout. Standard library only (scrypt)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit
from .config import Config
from .db import now_iso, transaction

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def login(conn: sqlite3.Connection, cfg: Config, username: str, password: str):
    """Returns (token, csrf, user) or raises ValueError with a deliberately generic message."""
    generic = ValueError("Invalid username or password.")
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if user is None:
        # equalise timing a little
        verify_password(password, "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + base64.b64encode(b"\0" * 32).decode())
        audit.record_standalone(conn, None, "auth.login", entity_type="user",
                                entity_ref=username.strip()[:60], outcome="failed", detail={"reason": "unknown_user"})
        raise generic
    now = datetime.now(timezone.utc)
    if user["locked_until"] and _parse(user["locked_until"]) > now:
        audit.record_standalone(conn, user, "auth.login", entity_type="user",
                                entity_ref=user["username"], outcome="denied", detail={"reason": "locked"})
        raise ValueError("Too many failed attempts. Try again later.")
    if not user["active"] or not verify_password(password, user["password_hash"]):
        with transaction(conn):
            fails = user["failed_logins"] + 1
            locked = None
            if fails >= cfg_limit(cfg):
                locked = (now + timedelta(minutes=cfg.lockout_minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                fails = 0
            conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (fails, locked, user["id"]))
            audit.record(conn, user, "auth.login", "user", user["username"], "failed",
                         {"reason": "bad_password_or_inactive", "locked": bool(locked)})
        raise generic
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    with transaction(conn):
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user["id"],))
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
            (_hash_token(token), user["id"], csrf, now_iso(),
             (now + timedelta(hours=cfg.session_hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
        )
        audit.record(conn, user, "auth.login", "user", user["username"])
    return token, csrf, user


def cfg_limit(cfg: Config) -> int:
    return cfg.max_failed_logins


def session_user(conn: sqlite3.Connection, token: str | None):
    """Returns (user_row, csrf) for a valid, unexpired session, else (None, None)."""
    if not token:
        return None, None
    row = conn.execute(
        "SELECT s.csrf, s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None or not row["active"]:
        return None, None
    if _parse(row["expires_at"]) <= datetime.now(timezone.utc):
        return None, None
    return row, row["csrf"]


def logout(conn: sqlite3.Connection, token: str | None, user) -> None:
    if not token:
        return
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
        if user is not None:
            audit.record(conn, user, "auth.logout", "user", user["username"])
