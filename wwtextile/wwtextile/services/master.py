"""Master data the business must be able to maintain itself: parties and fabric items."""
from __future__ import annotations

import re
import sqlite3

from .. import audit
from ..db import transaction
from ..errors import ValidationFailed
from ..permissions import require

KINDS = {"supplier": "Supplier", "customer": "Customer", "job_work": "Job-work party (dye house etc.)"}
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")


def _c(v) -> str:
    return str(v if v is not None else "").strip()


def create_party(conn: sqlite3.Connection, user, form: dict) -> int:
    require(user, "master.manage")
    kind, name = _c(form.get("kind")), " ".join(_c(form.get("name")).split())
    errors = {}
    if kind not in KINDS:
        errors["kind"] = "Choose a party type."
    if not (2 <= len(name) <= 80):
        errors["name"] = "Name must be 2-80 characters."
    elif conn.execute("SELECT 1 FROM parties WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        errors["name"] = "A party with this name already exists."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO parties(kind, name, is_demo) VALUES (?,?,0)", (kind, name))
        audit.record(conn, user, "master.party_create", "party", name, detail={"kind": kind})
        return cur.lastrowid


def create_item(conn: sqlite3.Connection, user, form: dict) -> int:
    """Fabric item. The unit is fixed per item; there are no unit conversions."""
    require(user, "master.manage")
    code, name, unit = _c(form.get("code")), " ".join(_c(form.get("name")).split()), _c(form.get("unit"))
    errors = {}
    if not CODE_RE.match(code):
        errors["code"] = "Code: letters, digits, . _ - (max 30)."
    elif conn.execute("SELECT 1 FROM items WHERE code = ? COLLATE NOCASE", (code,)).fetchone():
        errors["code"] = "This code already exists."
    if not (2 <= len(name) <= 80):
        errors["name"] = "Name must be 2-80 characters."
    if not (1 <= len(unit) <= 10):
        errors["unit"] = "Unit is required (e.g. m, kg, yd), max 10 characters."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO items(code, name, unit, is_demo) VALUES (?,?,?,0)", (code, name, unit))
        audit.record(conn, user, "master.item_create", "item", code, detail={"unit": unit})
        return cur.lastrowid


def list_parties(conn):
    return conn.execute("SELECT * FROM parties ORDER BY kind, name").fetchall()


def list_items(conn):
    return conn.execute("SELECT * FROM items ORDER BY name").fetchall()
