# WW Textile ERP — Implementation Plan

Status date: 2026-10-28. Written before any application code.

## 1. Workspace inspection (Milestone 0)

| Item | Finding |
|---|---|
| Existing WW Textile repository | **None.** Workspace was empty; the only input was the build prompt. Greenfield build explicitly authorised by the owner. |
| Existing tests / baseline | None to run. |
| Node | v22.23.4, npm 10.9.9 |
| Python | 3.13 |
| PostgreSQL | Server 16 binaries installed, cluster `16/main` present but stopped. **No Python driver** (`psycopg`, `asyncpg`, `pg8000` all absent). |
| Docker | CLI present, no daemon. |
| Package registries | **npm and PyPI are blocked by the sandbox egress policy (HTTP 403 on CONNECT).** Nothing can be installed. Not worked around. |
| Preinstalled Python | starlette 1.7, uvicorn, pydantic 2, Jinja2, PyJWT, itsdangerous, cryptography, pytest 9, python-multipart, sqlite3 (3.53) |
| Preinstalled Node | react, react-dom, typescript, tsx, playwright (+ Chromium in `/opt/pw-browsers`); **no** next, tailwindcss, vite, esbuild |

## 2. Stack decision

The requested stack (Next.js + Tailwind + FastAPI + PostgreSQL) **cannot be installed or run here**. Chosen practical alternative, using only what is verifiably present:

| Layer | Requested | Used | Consequence |
|---|---|---|---|
| Backend | FastAPI | **Starlette + Pydantic** (FastAPI is built on Starlette; same ASGI model) | No auto OpenAPI docs. Routing/validation written by hand. |
| Database | PostgreSQL | **SQLite 3.53, WAL, `BEGIN IMMEDIATE`**, FK enforcement, triggers | Real ACID transactions and writer serialisation, but single-node. Not a production multi-user database. SQL is kept portable; migration to PostgreSQL is future work and untested. |
| Frontend | Next.js + TS + Tailwind | **Server-rendered Jinja2 + hand-written CSS + small vanilla JS** | No SPA, no TypeScript frontend, no Tailwind. |
| Tests | — | pytest (service + HTTP layer), Playwright/Chromium for a browser check if it runs | |

If the owner later has registry access, moving to Next.js/FastAPI/PostgreSQL is a re-platform of the presentation and driver layers; the service layer (`wwtextile/services`) holds the business rules and is framework-independent.

## 3. Architecture

```
wwtextile/
  config.py        env-based configuration
  db.py            connections, transaction helper, migration runner
  migrations/      ordered .sql files (schema_migrations table)
  quantity.py      scaled-integer quantities (3 decimals), parse/format
  security.py      scrypt password hashing, session tokens, CSRF
  permissions.py   role -> permission map (PROPOSED, see BUSINESS_RULES.md)
  audit.py         append-only audit events
  services/        inventory ledger, receipts, processing, dispatch, gate, trace, ask
  web.py           Starlette routes, auth, CSRF, error handling
  templates/ static/   UI
  seed.py          fictional DEMO-104 data, built through the real services
  cli.py           migrate | seed | serve | demo
tests/
```

Rules enforced in the **service layer**, never in the UI:
- Stock on hand = `SUM(qty)` over `stock_ledger`. No stored balance column exists, so there is one source of truth.
- Ledger and audit tables are append-only (DB triggers reject UPDATE/DELETE). A DB trigger also rejects any ledger row that would drive a lot/location balance negative (backstop to the service check).
- Quantities are integers in thousandths of a unit. No floats.
- Every consequential action runs in one `BEGIN IMMEDIATE` transaction that re-reads current balances, so two concurrent issues cannot both spend the same stock.
- Corrections are reversal rows linked by `reverses_id`.
- Authorisation is checked server-side on every mutating route via the same service functions.

## 4. Dispatch / stock-deduction lifecycle (proposal, configurable)

`draft → approved (stock RESERVED) → pass_issued → exited`, plus `cancelled`.

- Reservation reduces **available** quantity, not on-hand.
- The single ledger deduction happens at one configurable event: `dispatch_approval`, `gate_pass_issue`, or `exit_confirm`. Default `exit_confirm` (stock leaves when the vehicle is verified out). **This default is a placeholder pending owner confirmation.**
- A unique ledger key per dispatch line makes a second deduction impossible; exit confirmation is a safety net that deducts only if no earlier event did.
- Cancelling after deduction posts reversal rows.
- The setting cannot be changed while dispatches are in flight.

## 5. Pass 1 scope

Delivered in this pass (details and verification status are in README/TESTING after the run):
1. Schema + migrations, auth (scrypt, sessions, CSRF, lockout), roles/permissions, audit.
2. Inventory ledger, goods receipt (draft/post/discard/reverse).
3. Processing job, issue, result, reconciliation (no silent balancing).
4. Dispatch with reservation, gate pass, exit confirmation, traceability.
5. Monochrome UI for the above, plus ledger and audit views.
6. Rule-based, evidence-backed lot query (explicitly **not** an LLM).
7. DEMO-104 seed built through the real services.
8. Tests.

Not in Pass 1 (shown as "not yet implemented" in the UI, never as working): Multihead, Accessory Store, Finance, inward gate register, reports/CSV export, global search, notifications, AI Command Center (LLM), voice, Watchtower, document reader, user management UI, draft editing (drafts are discarded and recreated).

## 6. Later passes

- **Pass 2:** accessory store, inward gate register, finance (rules pending), reports + CSV, dashboard hardening, global search, user admin, variance-resolution adjustments.
- **Pass 3:** AI Command Center: permission-aware read-only tools, provider boundary. Needs an LLM key for real use.
- **Pass 4:** push-to-talk voice. Needs a speech provider/browser support; testable only partially without hardware.
- **Pass 5:** Watchtower (deterministic rules first) and document reader (needs OCR/LLM provider).
- **Multihead:** blocked on owner decisions (unit, stock effect, workflow).

## 7. Business decisions needed

See `BUSINESS_RULES.md`. Highest impact: stock-deduction event, Multihead definition, units/conversions, loss tolerances, who may approve what.
