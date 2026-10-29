# Deployment

Single node, SQLite. Do **not** run several app processes against the DB over a network filesystem. One uvicorn process is the supported shape
(SQLite serialises writers; `BEGIN IMMEDIATE` is used for every posting).

## 1. Install (Linux, systemd, nginx)
```bash
sudo useradd -r -m -d /opt/wwtextile wwtextile
sudo -u wwtextile python3 -m venv /opt/wwtextile/venv
# copy this repository to /opt/wwtextile, then:
sudo -u wwtextile /opt/wwtextile/venv/bin/pip install -r /opt/wwtextile/requirements.txt
sudo apt install tesseract-ocr poppler-utils      # only needed for Document Reader (OCR / PDF)
sudo install -d -o wwtextile -g wwtextile /var/lib/wwtextile /var/backups/wwtextile /etc/wwtextile
```

## 2. Configure
`cp .env.example /etc/wwtextile/env; chmod 600 /etc/wwtextile/env` and set `WW_SECRET_KEY` (`python3 -c "import secrets;print(secrets.token_hex(32))"`).
`serve`, `init`, `create-admin` refuse to run in production without a 32+ character secret and secure cookies.

## 3. Create the first administrator (no default accounts exist in production)
```bash
set -a; . /etc/wwtextile/env; set +a
cd /opt/wwtextile && venv/bin/python -m wwtextile.cli init
venv/bin/python -m wwtextile.cli create-admin owner --name "Your Name"        # prompts for the password
# or:  ... create-admin owner --generate     -> prints a one-time password; must be changed at first sign-in
```
Then sign in, open **Users** to create the other accounts, **Master data** for suppliers/customers/items, **Settings** for approvals, units
(`multihead.units`), and alert thresholds. Locked out? `cli reset-password USERNAME` from the server shell.

## 4. Run
```bash
sudo cp deploy/wwtextile.service deploy/wwtextile-maintenance.* /etc/systemd/system/
sudo systemctl enable --now wwtextile wwtextile-maintenance.timer
sudo cp deploy/nginx.conf /etc/nginx/conf.d/wwtextile.conf   # edit server_name + certificates, then: nginx -t && systemctl reload nginx
```
The app must only be reachable through the HTTPS proxy (it binds 127.0.0.1). `GET /healthz` returns `ok` when the database answers.

## 5. Backup / restore / checks
* Nightly timer: online backup (SQLite backup API, verified with `integrity_check`, mode 0600) + uploads copy, ledger/database `check`, Watchtower `scan`, retention `purge`; backups older than 30 days are deleted.
* Manual: `cli backup /path`, `cli check` (exit 1 on any ledger/FK/integrity problem).
* Restore: stop the service, copy the chosen `wwtextile-*.db` to `WW_DB_PATH` (remove stale `-wal`/`-shm` files), restore the matching `uploads-*` directory, start, run `cli check`.
* **Copy backups off the machine.** Nothing here does that for you. **Restore has not been rehearsed on a production host**; do it once before relying on it.

## 6. Upgrades
Stop → backup → replace code → `cli init` (applies new migrations; they are forward-only) → start → `cli check`.

## 7. Docker (UNTESTED)
`Dockerfile` and `docker-compose.yml` are provided but could not be built or run where this was developed (no Docker). Treat them as a starting point;
the systemd path above is the one that was exercised (via the same CLI commands).

## 8. PostgreSQL
**Not implemented.** The code uses SQLite features directly (WAL, triggers, `BEGIN IMMEDIATE`). Moving to PostgreSQL is a port, not a setting.

## 9. Optional AI / voice providers
Set `WW_AI_*`, `WW_STT_*`, `WW_TTS_*` (see `.env.example`). Only the rule-based assistant and browser speech work without them. Provider code was exercised against local fake servers only, never a live vendor.
