# Security notes

**Implemented**
* Passwords: scrypt (stdlib), per-user salt; policy (min length, not the username, not common, not repetitive). Failed-login counter with timed lockout; admin unlock; forced change after admin/CLI reset or generated password.
* Sessions: server-side rows, random IDs, HttpOnly + SameSite cookies, `Secure` enforced in production; sessions deleted on password reset/deactivation; expired sessions purged by `cli purge`.
* CSRF token on every state-changing form/API call; login form has its own pre-session token. Rejections are audited.
* Authorisation is checked in the **service layer and the routes** (role → permission map in `permissions.py`); denied attempts are written to the audit trail even though the action's transaction rolled back.
* Append-only ledgers and audit trail enforced by DB triggers; negative stock blocked by trigger; money/quantity are integers.
* Headers: CSP (`default-src 'self'`, no inline style/script - covered by a test that rejects `style=` attributes), X-Frame-Options DENY, nosniff, same-origin referrer, Permissions-Policy (microphone only for self).
* Uploads: size cap, magic-byte type check, stored under random names outside the static path, text extraction via fixed argument lists (no shell).
* CSV exports neutralise formula-injection cells. Jinja autoescaping is on.
* AI: tool allow-list with schema validation, per-call permission re-check, LLM-proposed document values must appear in the source text, write-like actions create drafts only. Provider keys are read from the environment and never rendered or logged. Raw audio is not stored; transcripts are purged after the retention period.
* Production start refuses a missing/short `WW_SECRET_KEY` or insecure cookies. Demo seeding refuses to run in production. No default accounts exist in production (`cli create-admin`).

**Not implemented / known gaps**
* No MFA, no SSO, no password-expiry policy, no IP allow-listing (use the reverse proxy).
* Login rate limiting is per account (lockout), not per IP; put rate limiting on the proxy.
* No encryption at rest (use disk encryption); backups are not encrypted.
* No external penetration test or dependency audit has been done. Browser speech recognition may send audio to the browser vendor; server STT/TTS send audio/text to whichever provider is configured.
