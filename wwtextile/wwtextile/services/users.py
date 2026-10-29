"""User administration and password lifecycle."""
from __future__ import annotations

import re
import sqlite3

from .. import audit, security
from ..db import now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import ROLES, require

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD = 10
COMMON = {"password123", "1234567890", "qwertyuiop", "demo-only-change-me", "changeme123", "passwordpassword"}


def check_password_policy(password: str, username: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if password.lower() == username.lower() or username.lower() in password.lower():
        return "Password must not contain the username."
    if password.lower() in COMMON:
        return "That password is too common."
    if len(set(password)) < 5:
        return "Password is too repetitive."
    return None


def list_users(conn):
    return conn.execute("SELECT id, username, display_name, role, active, failed_logins, locked_until, must_change_password, created_at "
                        "FROM users ORDER BY username").fetchall()


def _active_admins(conn, excluding: int | None = None) -> int:
    sql, a = "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1", []
    if excluding is not None:
        sql += " AND id <> ?"
        a.append(excluding)
    return conn.execute(sql, a).fetchone()["c"]


def create_user(conn: sqlite3.Connection, actor, form: dict) -> int:
    require(actor, "user.manage")
    username = str(form.get("username") or "").strip().lower()
    name = " ".join(str(form.get("display_name") or "").split())
    role = str(form.get("role") or "")
    pw = str(form.get("password") or "")
    errors = {}
    if not USERNAME_RE.match(username):
        errors["username"] = "3-32 chars: lowercase letters, digits, . _ -"
    elif conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        errors["username"] = "Username already exists."
    if not (2 <= len(name) <= 60):
        errors["display_name"] = "Name must be 2-60 characters."
    if role not in ROLES:
        errors["role"] = "Choose a role."
    msg = check_password_policy(pw, username) if username else "Password is required."
    if msg:
        errors["password"] = msg
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role, active, must_change_password, created_at) VALUES (?,?,?,?,1,1,?)",
            (username, name, security.hash_password(pw), role, now_iso()))
        audit.record(conn, actor, "user.create", "user", username, detail={"role": role})
        return cur.lastrowid


def _get(conn, uid: int):
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u is None:
        raise NotFound("User not found.")
    return u


def set_role(conn, actor, uid: int, role: str) -> None:
    require(actor, "user.manage")
    if role not in ROLES:
        raise ValidationFailed({"role": "Unknown role."})
    with transaction(conn):
        u = _get(conn, uid)
        if u["role"] == "admin" and role != "admin" and u["active"] and _active_admins(conn, excluding=uid) == 0:
            raise BusinessRuleViolation("This is the last active administrator; it cannot be demoted.")
        if u["id"] == actor["id"] and role != "admin":
            raise BusinessRuleViolation("You cannot remove your own administrator role.")
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))   # role change takes effect immediately
        audit.record(conn, actor, "user.set_role", "user", u["username"], detail={"from": u["role"], "to": role})


def set_active(conn, actor, uid: int, active: bool) -> None:
    require(actor, "user.manage")
    with transaction(conn):
        u = _get(conn, uid)
        if not active:
            if u["id"] == actor["id"]:
                raise BusinessRuleViolation("You cannot deactivate your own account.")
            if u["role"] == "admin" and u["active"] and _active_admins(conn, excluding=uid) == 0:
                raise BusinessRuleViolation("This is the last active administrator; it cannot be deactivated.")
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit.record(conn, actor, "user.activate" if active else "user.deactivate", "user", u["username"])


def reset_password(conn, actor, uid: int, new_password: str) -> None:
    require(actor, "user.manage")
    with transaction(conn):
        u = _get(conn, uid)
        msg = check_password_policy(new_password or "", u["username"])
        if msg:
            raise ValidationFailed({"password": msg})
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL, "
                     "password_changed_at=? WHERE id=?", (security.hash_password(new_password), now_iso(), uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit.record(conn, actor, "user.reset_password", "user", u["username"])


def unlock(conn, actor, uid: int) -> None:
    require(actor, "user.manage")
    with transaction(conn):
        u = _get(conn, uid)
        conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (uid,))
        audit.record(conn, actor, "user.unlock", "user", u["username"])


def change_password(conn, user, current: str, new: str, confirm: str, keep_token: str | None) -> None:
    errors = {}
    row = _get(conn, user["id"])
    if not security.verify_password(current or "", row["password_hash"]):
        errors["current"] = "Current password is incorrect."
    if new != confirm:
        errors["confirm"] = "Passwords do not match."
    msg = check_password_policy(new or "", row["username"])
    if msg:
        errors["new"] = msg
    elif current and new == current:
        errors["new"] = "New password must differ from the current one."
    if errors:
        with transaction(conn):
            audit.record(conn, user, "user.change_password", "user", row["username"], outcome="failed")
        raise ValidationFailed(errors)
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed_at=? WHERE id=?",
                     (security.hash_password(new), now_iso(), user["id"]))
        # sign out every other session of this user
        if keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash <> ?", (user["id"], security._hash_token(keep_token)))
        audit.record(conn, user, "user.change_password", "user", row["username"])


# ---------------------------------------------------------------- operator (command line) actions: no logged-in actor
CLI_ACTOR = {"id": None, "username": "cli", "role": "system"}


def bootstrap_admin(conn: sqlite3.Connection, username: str, display_name: str, password: str, *, must_change: bool) -> int:
    """Create an administrator from the server command line. The password never appears in logs or audit detail."""
    username = (username or "").strip().lower()
    name = " ".join((display_name or "").split())
    errors = {}
    if not USERNAME_RE.match(username):
        errors["username"] = "3-32 chars: lowercase letters, digits, . _ -"
    elif conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        errors["username"] = "Username already exists."
    if not (2 <= len(name) <= 60):
        errors["display_name"] = "Name must be 2-60 characters."
    msg = check_password_policy(password or "", username) if username else "Password is required."
    if msg:
        errors["password"] = msg
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO users(username, display_name, password_hash, role, active, must_change_password, created_at) VALUES (?,?,?,'admin',1,?,?)",
                           (username, name, security.hash_password(password), 1 if must_change else 0, now_iso()))
        audit.record(conn, CLI_ACTOR, "user.bootstrap_admin", "user", username)
        return cur.lastrowid


def cli_reset_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    """Recovery when every administrator is locked out. Requires shell access to the server."""
    u = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),)).fetchone()
    if u is None:
        raise NotFound("No such user.")
    msg = check_password_policy(password or "", u["username"])
    if msg:
        raise ValidationFailed({"password": msg})
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL, active=1, password_changed_at=? WHERE id=?",
                     (security.hash_password(password), now_iso(), u["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        audit.record(conn, CLI_ACTOR, "user.cli_reset_password", "user", u["username"])
