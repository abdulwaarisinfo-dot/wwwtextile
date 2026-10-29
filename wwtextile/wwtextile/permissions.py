"""Roles and permissions.

THE ROLE->PERMISSION MAP BELOW IS A PROPOSAL, NOT A CONFIRMED BUSINESS RULE
(see BUSINESS_RULES.md A12 and question 13). It exists so server-side
enforcement works end to end; the owner must review it.
"""
from __future__ import annotations

from .errors import PermissionDenied

ALL_PERMISSIONS = {
    "stock.view", "ledger.view",
    "receipt.create", "receipt.post", "receipt.reverse",
    "processing.view", "processing.create", "processing.issue", "processing.result",
    "dispatch.view", "dispatch.create", "dispatch.approve", "dispatch.cancel",
    "gate.view", "gate.pass.create", "gate.exit.confirm",
    "gate.entry.create", "gate.entry.approve", "gate.entry.exit", "gate.entry.cancel",
    "acc.view", "acc.create", "acc.post", "acc.reverse", "acc.items.manage",
    "fin.view", "fin.create", "fin.post", "fin.void", "fin.allocate",
    "mh.view", "mh.create", "mh.log", "mh.consume", "mh.close", "mh.machines.manage",
    "master.manage",
    "report.view",
    "alert.view", "alert.manage",
    "doc.upload", "doc.view",
    "audit.view", "settings.view", "settings.edit", "ask.query", "voice.use",
    "user.manage",
}

ROLES: dict[str, set[str]] = {
    "admin": set(ALL_PERMISSIONS),
    "ceo": {
        "stock.view", "ledger.view", "processing.view", "dispatch.view", "gate.view", "acc.view", "fin.view",
        "mh.view", "report.view", "alert.view", "alert.manage", "doc.view",
        "audit.view", "settings.view", "ask.query", "voice.use",
    },
    "manager": {
        "stock.view", "ledger.view",
        "receipt.create", "receipt.post", "receipt.reverse",
        "processing.view", "processing.create", "processing.issue", "processing.result",
        "dispatch.view", "dispatch.create", "dispatch.approve", "dispatch.cancel",
        "gate.view", "gate.entry.approve", "gate.entry.cancel",
        "acc.view", "acc.create", "acc.post", "acc.reverse", "acc.items.manage",
        "fin.view", "fin.post", "fin.void", "fin.allocate",
        "mh.view", "mh.create", "mh.log", "mh.consume", "mh.close", "mh.machines.manage",
        "master.manage", "report.view", "alert.view", "alert.manage", "doc.upload", "doc.view",
        "audit.view", "settings.view", "ask.query", "voice.use",
    },
    "storekeeper": {
        "stock.view", "ledger.view", "receipt.create", "receipt.post",
        "processing.view", "processing.create", "processing.issue",
        "dispatch.view", "dispatch.create", "gate.view",
        "acc.view", "acc.create", "acc.post", "mh.view",
        "report.view", "alert.view", "doc.upload", "doc.view", "ask.query", "voice.use",
    },
    "production": {
        "stock.view", "processing.view", "processing.create", "processing.result",
        "acc.view", "acc.create", "mh.view", "mh.create", "mh.log", "mh.consume", "mh.close",
        "report.view", "ask.query", "voice.use",
    },
    "gate": {
        "dispatch.view", "gate.view", "gate.pass.create", "gate.exit.confirm",
        "gate.entry.create", "gate.entry.exit",
    },
    "finance": {
        "stock.view", "dispatch.view", "fin.view", "fin.create", "fin.allocate", "report.view", "doc.view",
        "ask.query", "voice.use",
    },
}

ROLE_LABELS = {
    "admin": "Administrator", "ceo": "CEO", "manager": "Manager",
    "storekeeper": "Storekeeper", "production": "Production",
    "gate": "Gate personnel", "finance": "Finance",
}


def can(role: str, permission: str) -> bool:
    return permission in ROLES.get(role, set())


def require(user, permission: str) -> None:
    if user is None or not can(user["role"], permission):
        raise PermissionDenied(f"Your role does not have the '{permission}' permission.")


def roles_with(permission: str) -> list[str]:
    return sorted(r for r, perms in ROLES.items() if permission in perms)
