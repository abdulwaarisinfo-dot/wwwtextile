# Feature status and limitations

## Complete (exercised by automated tests; see TESTING.md)
Fabrics · Textile Processing (reconciliation, finished lots) · Dispatch/reservations/gate passes · Gate Management · Accessory Store · Multihead ·
Finance sub-ledger · 16 Reports (HTML/print/CSV) · Watchtower (12 rules) · CEO/role dashboards · global search · Document Reader · Jarvis assistant
(rules + optional LLM) · voice (push-to-talk, optional wake-word, optional server STT/TTS) · users/roles/audit/settings · CLI operations · backup · health check.

## Limitations - read before relying on this in production
1. **Single node, SQLite.** No PostgreSQL port, no horizontal scaling, no HA.
2. **Finance is a sub-ledger, not accounting.** No general ledger, chart of accounts, tax/FBR invoicing, FX, bank reconciliation or period close.
3. **Gate entries do not move stock.** Stock moves via receipts, processing, dispatch (and accessory documents).
4. **Business rules are assumptions** until the owner answers BUSINESS_RULES.md section 3 (Multihead meaning/unit, dispatch deduction event, loss tolerances, unit conversions, role list, alert thresholds).
5. **Single unit per item, no conversions** (metre/yard/piece rules are unknown).
6. **AI/voice providers never tested live.** LLM, STT and TTS adapters were run only against local fake servers. Real human speech was not tested; browser tests use a fake recogniser. Browser recognition may send audio to the vendor. English only.
7. **The assistant cannot post, approve, pay or void** - by design; it drafts.
8. **OCR accuracy** depends on the scan; the reader proposes, a person verifies, and only a draft receipt is created.
9. **Docker files untested.** Restore from backup not rehearsed on a production host. Backups are not encrypted or shipped off-machine.
10. **No MFA/SSO**, no per-IP rate limiting (use the proxy), no external security audit.
11. **UI is English-only, desktop + mobile web**; no native app, no offline mode, no email/SMS/WhatsApp notifications (in-app bell only).
12. Drafts cannot be edited (discard and recreate).
13. Browser tests ran in Chromium only.
