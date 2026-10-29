"""Operational settings (admin only). Every change is audited."""
from __future__ import annotations

import sqlite3

from .. import audit
from ..db import get_setting, now_iso, transaction
from ..errors import BusinessRuleViolation, ValidationFailed
from ..permissions import require
from . import dispatch


def current(conn: sqlite3.Connection) -> dict:
    return {
        "dispatch.deduction_event": dispatch.deduction_event(conn),
        "dispatch.require_separate_approver": get_setting(conn, "dispatch.require_separate_approver", "1"),
    }


def _put(conn, user, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, value, now_iso(), user["id"]))


def set_deduction_event(conn: sqlite3.Connection, user, value: str) -> None:
    require(user, "settings.edit")
    if value not in dispatch.DEDUCTION_EVENTS:
        raise ValidationFailed({"deduction_event": "Unknown event."})
    with transaction(conn):
        old = dispatch.deduction_event(conn)
        if value == old:
            return
        n = dispatch.in_flight_count(conn)
        if n:
            raise BusinessRuleViolation(
                f"{n} dispatch(es) are approved or awaiting exit. Finish or cancel them before changing the deduction event.")
        _put(conn, user, "dispatch.deduction_event", value)
        audit.record(conn, user, "settings.deduction_event", "setting", "dispatch.deduction_event",
                     detail={"from": old, "to": value})


def set_separate_approver(conn: sqlite3.Connection, user, enabled: bool) -> None:
    require(user, "settings.edit")
    with transaction(conn):
        old = get_setting(conn, "dispatch.require_separate_approver", "1")
        new = "1" if enabled else "0"
        if old == new:
            return
        _put(conn, user, "dispatch.require_separate_approver", new)
        audit.record(conn, user, "settings.separate_approver", "setting", "dispatch.require_separate_approver",
                     detail={"from": old, "to": new})


# ---------------------------------------------------------------- generic registry
# Every policy that the owner has NOT confirmed is an explicit, audited setting with a documented default.
REGISTRY: dict[str, dict] = {
    "dispatch.require_separate_approver": dict(group="Dispatch", kind="bool", default="1",
        label="Dispatch approver must differ from creator", help="Maker-checker on dispatch approval."),
    "gate.outward_requires_approval": dict(group="Gate", kind="bool", default="1",
        label="Outward gate entries need approval before exit", help="Applies to non-dispatch outward entries."),
    "gate.require_separate_approver": dict(group="Gate", kind="bool", default="1",
        label="Gate entry approver must differ from creator", help=""),
    "finance.require_separate_poster": dict(group="Finance", kind="bool", default="1",
        label="Finance entry poster must differ from creator", help="Maker-checker on posting bills, invoices, payments and expenses."),
    "multihead.units": dict(group="Multihead", kind="units", default="",
        label="Multihead units", help="Comma list of unit[:decimals], e.g. 'pcs:0, m:3'. Empty = orders cannot be created (unit is not assumed)."),
    "multihead.fabric_stock_effect": dict(group="Multihead", kind="choice", default="record_only", choices=["record_only", "consume"],
        label="Fabric consumed by a Multihead order", help="record_only: noted, no stock movement. consume: posts a negative ledger row."),
    "watchtower.pending_approval_hours": dict(group="Watchtower", kind="int", default="24", min=1, max=720,
        label="Flag approvals/postings pending longer than (hours)", help="Assumption: owner has not defined an SLA."),
    "watchtower.long_running_job_days": dict(group="Watchtower", kind="int", default="14", min=1, max=365,
        label="Flag open processing jobs older than (days)", help="Assumption: owner has not defined a normal duration."),
    "watchtower.vehicle_inside_hours": dict(group="Watchtower", kind="int", default="12", min=1, max=240,
        label="Flag vehicles inside longer than (hours)", help="Assumption."),
    "watchtower.draft_age_days": dict(group="Watchtower", kind="int", default="7", min=1, max=365,
        label="Flag draft documents older than (days)", help="Assumption."),
}


def get_value(conn, key: str) -> str:
    return get_setting(conn, key, REGISTRY[key]["default"])


def get_int(conn, key: str) -> int:
    try:
        return int(get_value(conn, key))
    except ValueError:
        return int(REGISTRY[key]["default"])


def _validate(spec: dict, value: str) -> str:
    value = (value or "").strip()
    k = spec["kind"]
    if k == "bool":
        if value not in ("0", "1"):
            raise ValidationFailed({"value": "Must be on or off."})
    elif k == "choice":
        if value not in spec["choices"]:
            raise ValidationFailed({"value": "Choose one of: " + ", ".join(spec["choices"])})
    elif k == "int":
        try:
            n = int(value)
        except ValueError:
            raise ValidationFailed({"value": "Enter a whole number."}) from None
        if not (spec["min"] <= n <= spec["max"]):
            raise ValidationFailed({"value": f"Enter a number from {spec['min']} to {spec['max']}."})
        value = str(n)
    elif k == "units":
        seen = set()
        parts = []
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, dec = part.partition(":")
            name, dec = name.strip(), dec.strip()
            if not (1 <= len(name) <= 10) or any(ch in name for ch in ":,;<>\"'"):
                raise ValidationFailed({"value": f"Invalid unit name '{name}'."})
            if dec not in ("", "0", "3"):
                raise ValidationFailed({"value": f"Decimals for '{name}' must be 0 or 3."})
            if name.lower() in seen:
                raise ValidationFailed({"value": f"Unit '{name}' is listed twice."})
            seen.add(name.lower())
            parts.append(f"{name}:{dec}" if dec else name)
        value = ",".join(parts)
    return value


def set_value(conn, user, key: str, value: str) -> bool:
    """Validate and store one registry setting. Returns True if it changed. Audited."""
    require(user, "settings.edit")
    spec = REGISTRY.get(key)
    if spec is None:
        raise ValidationFailed({"key": "Unknown setting."})
    value = _validate(spec, value)
    with transaction(conn):
        old = get_setting(conn, key, spec["default"])
        if old == value:
            return False
        _put(conn, user, key, value)
        audit.record(conn, user, "settings.change", "setting", key, detail={"from": old, "to": value})
        return True


def all_values(conn) -> list[dict]:
    return [{"key": k, **spec, "value": get_setting(conn, k, spec["default"])} for k, spec in REGISTRY.items()]
