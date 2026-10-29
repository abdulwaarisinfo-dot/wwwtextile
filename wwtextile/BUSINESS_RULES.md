# WW Textile — Business Rules

Only rules in **Confirmed** come from the owner. Everything else is an assumption or an open question and is labelled as such.

## 1. Confirmed by owner

1. Stock-affecting actions need separate reservation, dispatch, gate-pass and exit-confirmation records where appropriate; the same stock must never be deducted twice.
2. The stock-posting event for dispatch must be configurable or documented until the owner confirms the real rule.
3. Multihead's unit, stock consumption and workflow must not be assumed.
4. Financial accounting policy, processing loss tolerances, unit conversions and role permissions must not be invented.
5. Demonstration data is fictional.

## 2. Implemented assumptions (all reversible, all configurable or isolated in one place)

| # | Assumption | Where | Impact if wrong |
|---|---|---|---|
| A1 | Demo unit is **metres**. Each item has one unit; quantity entry in another unit is rejected. **No conversions exist.** | `items.unit` | Yards/pieces unsupported until rules are given. |
| A2 | Quantities have 3 decimal places (integer thousandths). | `quantity.py` | Changing precision needs a data migration. |
| A3 | Stock-deduction event default = `exit_confirm`. Options: `dispatch_approval`, `gate_pass_issue`, `exit_confirm`. | setting `dispatch.deduction_event` | Changes when on-hand drops; available stock is unaffected because reservations apply from approval. |
| A4 | Dispatch approver must differ from the dispatch creator (maker-checker). | setting `dispatch.require_separate_approver` | Can be switched off. |
| A5 | Locations: Raw Store, Processing (WIP), Finished Store, Reject Store. | seed | Real warehouse layout unknown. |
| A6 | One live processing result per issue. Partial/multiple results per issue not supported. | partial unique index `ux_result_issue` | Partial results need a model change. |
| A7 | Reconciliation: `input = accepted + rejected + wastage + shrinkage + remaining_in_process + unexplained_variance`. Variance is computed, never typed. Status is `balanced` only when variance is exactly 0, otherwise `exception`. **No tolerance is applied.** | `services/processing.py` | A real tolerance would reclassify small variances. |
| A8 | If reported outputs exceed input (negative variance) the result is **rejected**, not posted. | same | A genuine over-yield (e.g. stretch) would be blocked until a rule exists. |
| A9 | Unexplained variance stays in the Processing (WIP) location as an open exception. It is not written off. | same | Resolution needs an authorised adjustment (Pass 2). |
| A10 | Accepted output becomes a new **finished lot** `<source>-F<n>` in Finished Store, linked to its parent lot. Rejected stays under the source lot in Reject Store. Wastage and shrinkage are written out of WIP as measured losses. | same | Naming/ownership rules unknown. |
| A11 | Goods receipt may be reversed only if the whole received quantity is still untouched at its location. | `services/receipts.py` | Otherwise an adjustment is needed (Pass 2). |
| A12 | Roles and permissions in `permissions.py` are **proposals**. | `permissions.py` | Must be reviewed. |
| A13 | Process types offered: dyeing, printing, embroidery. | `services/processing.py` | List is a placeholder. |
| A14 | Drafts cannot be edited, only discarded and recreated. | — | UX limitation. |
| A15 | Passwords for demo users are a fixed, documented demo value and only seeded when `WW_ENV=demo`. | `seed.py` | Never use in production. |

## 2b. Assumptions added with the Accessory, Gate, Multihead, Finance, Watchtower, Assistant modules

| # | Assumption | Where | Impact if wrong |
|---|---|---|---|
| M1 | **Multihead = an embroidery-style production order run on a named machine.** The unit is NOT assumed: orders cannot be created until an administrator sets `multihead.units` (e.g. `pcs:0, m:3`). The demo sets `pcs:0,m:3` explicitly. | setting `multihead.units` | Order model may need to change. |
| M2 | Fabric consumed by a Multihead order is **recorded only** by default; set `multihead.fabric_stock_effect=consume` to post a negative fabric-ledger row. | setting | Stock overstated while `record_only`. |
| M3 | Production logs and consumptions are voided (reversing row), never edited or deleted. | `services/multihead.py` | - |
| M4 | An order closes as completed / short / cancelled with a mandatory note; no further logging after close. | same | - |
| M5 | Multihead consumes accessories only through Accessory Store *issue* documents that reference the order number. | `services/accessories.py` | - |
| M6 | Operators/machines are free text / a machine list; no shift or wage logic. | - | - |
| AC1 | Accessory quantities are integers by default; an item may be defined with 3 decimals. No unit conversion. | `acc_items.decimals` | - |
| AC2 | Accessory stock is a separate append-only ledger per item+location. Negative stock is blocked by a DB trigger. Posted documents are reversed by linked rows, never edited. | `acc_ledger` | - |
| AC3 | Item/location master data needs `acc.items.manage` (manager/admin); storekeepers create and post documents. | `permissions.py` | - |
| G1 | Gate entries record vehicle movements (inward/outward) and **do not move stock**. Dispatch gate passes (Pass 2) remain the stock-linked outward path. | `services/gate_entries.py` | Receipts are still posted in Fabrics/Accessories. |
| G2 | Outward entries need approval before exit (setting, default on); approver must differ from the creator (setting, default on). | settings | - |
| F1 | Finance is **sub-ledger bookkeeping only**: supplier bills, customer invoices, payments in/out, expenses, allocation of payments to bills/invoices, party statements and balances. **No general ledger, chart of accounts, tax (GST/FBR), FX, depreciation or period close.** Currency is a single configured code (default PKR); money is integer paisa. | `services/finance.py` | A real accounting policy is still owed by the owner. |
| F2 | The poster of a finance entry must differ from its creator (setting, default on). Posted entries are voided with a reason, never edited. Allocation cannot exceed the payment's unallocated amount or the bill's outstanding amount. | settings | - |
| W1 | Watchtower alerts come only from deterministic rules; thresholds (24 h pending, 14 d job, 12 h vehicle inside, 7 d draft) are **adjustable assumptions**, not owner standards. Alerts are fingerprinted, re-opened if the condition returns, and auto-resolved when it clears. | `services/watchtower.py` | No ML / anomaly detection exists. |
| R1 | Reports: one function per report feeds HTML, print and CSV so the three always agree. Totals are per unit (never summed across units). Day ranges use the configured display time zone converted to UTC. CSV cells starting `= + - @` are neutralised. | `services/reports.py` | - |
| AI1 | The assistant only calls allow-listed, schema-validated tools, re-checked against the signed-in user's permissions on every call. It can **draft** documents; it never posts, approves, voids or pays. | `ai/` | - |
| AI2 | Without an LLM key the rule-based assistant answers; with one, an LLM tool-loop is used and falls back to rules (with a visible notice) on any provider error. | `ai/agent.py` | Provider paths were tested only against fake servers. |
| AI3 | Transcripts are kept `WW_TRANSCRIPT_RETENTION_DAYS` (default 30) then purged by `cli purge`. Raw audio is never stored by the app. | `cli.py` | - |
| V1 | Voice is push-to-talk by default; wake-word is opt-in, browser-only and visibly indicated. Write-like utterances are placed in the input box for review, never auto-sent. Browser speech recognition may send audio to the browser vendor. English only. | `static/assistant.js` | - |
| D1 | Document Reader extracts text (PDF text layer, else OCR) and **proposes** fields; a human reviews and creates a goods-receipt DRAFT. LLM-proposed values are accepted only if they literally appear in the extracted text. | `ai/documents.py` | OCR quality depends on scan quality. |

## 3. Unresolved questions (owner input needed)

### Stock and dispatch
1. At which event does stock leave the books: dispatch approval, gate-pass issue, or verified vehicle exit?
2. Can a dispatch be partly loaded / partly exited? Can one vehicle carry several dispatches?
3. Is stock ever held at a third party after dispatch (consignment, job-work return)?

### Multihead (built on configurable assumptions M1-M6 below; the owner's answers may change them)
4. What does Multihead mean operationally (embroidery machine heads? a department? a product line)?
5. What unit does it count (metres, pieces, stitches, panels)?
6. Does it consume fabric/accessory stock? From which store, and does output re-enter stock?
7. What is the workflow and who operates it?

### Processing
8. Acceptable loss tolerances per process (wastage/shrinkage), and what happens beyond them?
9. Can results be reported in parts? Can one job have several issues?
10. Is shrinkage measured or derived? Who records it?
11. Who may resolve an unexplained variance, and how is it booked?

### Units
12. Which units exist per process, and what are the conversion rules (metre/yard, piece/metre)?

### Roles and approvals
13. The real list of roles and what each may do. Who approves adjustments, reversals and dispatches? May a user approve their own work?

### Finance
14. Is the Finance sub-ledger (F1) enough, or is a full general ledger / tax invoicing / bank reconciliation required? Which expense categories are real?

### Documents and language
15. Which documents must be printed (gate pass, receipt, dispatch note) and in what format?
16. Is Urdu / Roman Urdu required in the UI or voice?

### Fabrics
17. Does "Fabrics" cover greige only, or also finished fabric, weaving and trading?
