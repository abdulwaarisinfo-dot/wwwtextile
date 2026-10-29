# Testing

Run: `python3 -m pytest -q` (see README for the test count; it is printed by pytest, not hard-coded here).

| Layer | Where | What it proves |
|---|---|---|
| Services/DB | `test_services.py`, `test_new_services.py` | ledgers, triggers, reversals, maker-checker, permissions, reconciliation, accessories, gate, multihead, finance, reports, watchtower |
| Web | `test_web.py`, `test_web_modules.py` | every route per role (200/303/403/422), CSRF, forced password change, form workflows end to end, CSV, no placeholder pages |
| AI | `test_ai.py` | rule engine, tool permission checks, follow-ups, drafts-only, **fake** HTTP LLM/STT/TTS servers (success, bad JSON, timeouts, fallback), document extraction incl. real tesseract OCR |
| CLI/ops | `test_cli.py` | init/create-admin/reset-password/check/backup/purge/scan, production start gate, `/healthz`, no inline styles |
| Concurrency | `test_concurrency.py` | 10 parallel posts on one lot from separate connections: exactly the affordable ones succeed, ledger stays consistent |
| Browser | `tests/browser/*.js` (Playwright/Chromium, run manually against a demo server) | rendering, nav, forms, assistant typed flow, mobile overflow, CSP console errors |

**Not covered, and why**
* Real speech: the browser tests inject a *fake* `SpeechRecognition`; no microphone or real STT/TTS vendor was available. Server STT/TTS and LLM providers are tested only against local fake servers.
* Docker image/compose: no Docker in the development environment; never built.
* Load/soak testing, multi-process deployments, restore rehearsal on a production host, accessibility audit with assistive technology, other browsers than Chromium.
