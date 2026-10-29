# WW Textile ERP & Factory Intelligence

Starlette + Jinja2 + SQLite (WAL) + vanilla JS. Single-node. Fictional demo data only.

Modules: Fabrics (receipts, lots, ledger) · Textile Processing (jobs, issues, results with reconciliation) · Dispatch + Gate passes ·
Gate Management (inward/outward vehicle entries) · Accessory Store · Multihead production · Finance (payables/receivables/expenses sub-ledger) ·
Reports (16, HTML/print/CSV) · Watchtower alerts · CEO dashboard · Document Reader (OCR → goods-receipt draft) · Jarvis assistant (text + voice) ·
Users/roles/audit/settings.

## Live client demo
See **DEMO.md** (rich fictional data, private passwords, public link via a tunnel, verify script, reset).

## Quick start (developer demo, minimal data)
```bash
pip install -r requirements.txt            # Python 3.11+ (tested 3.13)
WW_ENV=demo WW_DEMO_PROFILE=minimal python3 -m wwtextile.cli demo   # small DEMO-104 data set; without WW_DEMO_PROFILE you get the full showcase data
```
Demo logins (password `demo-only-change-me`, or `WW_DEMO_PASSWORD`): `admin`, `ceo`, `manager1`, `store1`, `prod1`, `gate1`, `fin1`.
The demo refuses to run when `WW_ENV=production`.

## Real installation
See **DEPLOYMENT.md** (init → create-admin → serve behind HTTPS → nightly backup). Feature status and limits: **FEATURE_STATUS.md**.

## Tests
```bash
pip install -r requirements-dev.txt && python3 -m pytest -q          # 339 tests
node tests/browser/browser_check.js; node tests/browser/browser_modules.js   # needs a demo server on :8000 and Playwright
```
See TESTING.md for what is and is not covered.
