"""Command line.

  init            migrate + create the structural records every installation needs (fabric stores, accessory store). No users, no demo data.
  create-admin    create the first administrator (interactive password prompt, or --generate for a one-time password)
  reset-password  recover a locked-out account from the server shell
  serve           start the web server (refuses to start in production with unsafe configuration)
  demo            migrate + seed fictional demo data + serve (WW_ENV=demo only)
  reset-demo      delete the demo database and regenerate fictional data (WW_ENV=demo, needs --yes)
  seed / migrate / check / scan / purge / backup
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

from . import config, db


def _password(args) -> tuple[str, bool]:
    """(password, must_change). --generate -> random one-time password. Otherwise prompt (tty) or WW_ADMIN_PASSWORD (scripts)."""
    if getattr(args, "generate", False):
        return secrets.token_urlsafe(14), True
    env_pw = os.environ.get("WW_ADMIN_PASSWORD")
    if env_pw:
        return env_pw, False
    if not sys.stdin.isatty():
        raise SystemExit("No terminal to prompt on. Use --generate, or set WW_ADMIN_PASSWORD for this one command and unset it afterwards.")
    a, b = getpass.getpass("Password: "), getpass.getpass("Repeat password: ")
    if a != b:
        raise SystemExit("Passwords do not match.")
    return a, False


def backup(conn: sqlite3.Connection, dest_dir: str, cfg) -> str:
    """Consistent online copy (SQLite backup API) verified with integrity_check; uploads are copied alongside."""
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(dest_dir, f"wwtextile-{stamp}.db")
    out = sqlite3.connect(target)
    try:
        conn.backup(out)
        res = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
    if res != "ok":
        os.remove(target)
        raise SystemExit(f"Backup failed integrity check: {res}")
    os.chmod(target, 0o600)
    if os.path.isdir(cfg.upload_dir) and os.listdir(cfg.upload_dir):
        import shutil
        up = os.path.join(dest_dir, f"uploads-{stamp}")
        shutil.copytree(cfg.upload_dir, up)
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wwtextile")
    ap.add_argument("command", choices=["init", "create-admin", "reset-password", "migrate", "seed", "serve", "demo", "check", "scan", "purge", "backup", "reset-demo"])
    ap.add_argument("target", nargs="?", help="username (create-admin / reset-password) or destination directory (backup)")
    ap.add_argument("--name", help="display name for create-admin")
    ap.add_argument("--generate", action="store_true", help="generate a one-time password (must be changed at first sign-in)")
    ap.add_argument("--yes", action="store_true", help="confirm reset-demo (deletes ALL data in the demo database)")
    ap.add_argument("--seed", type=int, help="random seed for the showcase data (default WW_DEMO_SEED)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    cfg = config.load()

    if args.command in ("serve", "demo", "init", "create-admin", "seed", "reset-demo"):
        problems = config.validate_for_start(cfg)
        if problems:
            print("Refusing to continue:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
    if args.command in ("seed", "demo", "reset-demo") and cfg.env != "demo":
        print("Demo data can only be created when WW_ENV=demo. Use 'init' and 'create-admin' for a real installation.", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)), exist_ok=True)
    if args.command == "reset-demo":
        if not args.yes:
            print(f"This DELETES the demo database ({cfg.db_path}) and uploads, then regenerates fictional data.\nRe-run with --yes to confirm.", file=sys.stderr)
            return 2
        from . import demo_data
        print("demo reset:", demo_data.reset(cfg, args.seed))
        return 0
    conn = db.connect(cfg.db_path)

    if args.command in ("reset-password", "check", "scan", "purge", "backup"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
            print(f"Database {cfg.db_path} is not initialised. Run 'init' first.", file=sys.stderr)
            return 2
    if args.command in ("init", "migrate", "seed", "serve", "demo", "create-admin"):
        applied = db.migrate(conn)
        print(f"migrations applied: {applied or 'none (up to date)'}")
    if args.command in ("init", "serve", "demo", "create-admin"):
        from . import seed
        seed.init_structure(conn)
    if args.command in ("seed", "demo"):
        from . import seed
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            if os.environ.get("WW_DEMO_PROFILE", "showcase") == "minimal":
                info = seed.seed_demo(conn, cfg)          # small DEMO-104 data set used by the automated tests
            else:
                from . import demo_data
                info = demo_data.build(conn, cfg, args.seed)
            print("seeded demo data:", info)
        else:
            print("database already seeded; leaving it unchanged")

    if args.command == "create-admin":
        from .errors import ValidationFailed
        from .services import users
        if not args.target:
            print("usage: create-admin USERNAME [--name 'Full Name'] [--generate]", file=sys.stderr)
            return 2
        pw, must = _password(args)
        try:
            users.bootstrap_admin(conn, args.target, args.name or args.target, pw, must_change=must)
        except ValidationFailed as e:
            print("Not created: " + "; ".join(e.errors.values()), file=sys.stderr)
            return 1
        print(f"administrator '{args.target.lower()}' created.")
        if args.generate:
            print(f"one-time password (shown once; change it at first sign-in): {pw}")
    elif args.command == "reset-password":
        from .errors import AppError
        from .services import users
        if not args.target:
            print("usage: reset-password USERNAME", file=sys.stderr)
            return 2
        pw, _ = _password(args)
        try:
            users.cli_reset_password(conn, args.target, pw)
        except AppError as e:
            print(f"Not changed: {e}", file=sys.stderr)
            return 1
        print("password reset; the user must change it at next sign-in.")
        if args.generate:
            print(f"one-time password: {pw}")
    elif args.command == "check":
        from .services import accessories, inventory
        problems = inventory.reconcile_ledger(conn) + accessories.reconcile(conn)
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if ic != "ok":
            problems.append(f"sqlite integrity_check: {ic}")
        problems += [f"foreign key violation in {r[0]}" for r in fk]
        print("ledgers and database consistent" if not problems else "PROBLEMS: " + "; ".join(problems))
        return 1 if problems else 0
    elif args.command == "scan":
        from .services import watchtower
        print("watchtower scan:", watchtower.scan(conn, None))
    elif args.command == "purge":
        from .ai import agent
        n = agent.purge_old(conn, cfg.transcript_retention_days)
        s = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now_iso(),)).rowcount
        print(f"purged {n} assistant message(s) older than {cfg.transcript_retention_days} days and {s} expired session(s)")
    elif args.command == "backup":
        print("backup written:", backup(conn, args.target or "backups", cfg))
    elif args.command in ("serve", "demo"):
        import uvicorn
        from .web import create_app
        print(f"serving on http://{args.host}:{args.port}  (env={cfg.env}, db={cfg.db_path})")
        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info", proxy_headers=True,
                    forwarded_allow_ips=os.environ.get("WW_FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
