import sqlite3

import pytest

from wwtextile import cli, config, db
from wwtextile.services import users


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WW_UPLOAD_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("WW_ENV", "production")
    monkeypatch.setenv("WW_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("WW_COOKIE_SECURE", "1")
    monkeypatch.delenv("WW_ADMIN_PASSWORD", raising=False)
    return tmp_path


def conn_of(tmp):
    return db.connect(str(tmp / "t.db"))


def test_init_creates_structure_but_no_users(env):
    assert cli.main(["init"]) == 0
    c = conn_of(env)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 4
    assert c.execute("SELECT COUNT(*) FROM acc_locations").fetchone()[0] == 1


def test_create_admin_generate_forces_change_and_audits(env, capsys):
    cli.main(["init"])
    assert cli.main(["create-admin", "Owner", "--name", "Owner One", "--generate"]) == 0
    out = capsys.readouterr().out
    pw = out.split("first sign-in): ")[1].strip()
    c = conn_of(env)
    u = c.execute("SELECT * FROM users WHERE username='owner'").fetchone()
    assert u["role"] == "admin" and u["must_change_password"] == 1
    from wwtextile import security
    assert security.verify_password(pw, u["password_hash"])
    a = c.execute("SELECT * FROM audit_events WHERE action='user.bootstrap_admin'").fetchone()
    assert a is not None and pw not in str(dict(a))


def test_create_admin_rejects_weak_password_and_duplicates(env, monkeypatch):
    cli.main(["init"])
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "short")
    assert cli.main(["create-admin", "bob"]) == 1
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "a-long-enough-pass-1")
    assert cli.main(["create-admin", "bob"]) == 0
    assert cli.main(["create-admin", "bob"]) == 1


def test_no_tty_no_password_exits(env, monkeypatch):
    cli.main(["init"])
    monkeypatch.setattr("sys.stdin", open("/dev/null"))
    with pytest.raises(SystemExit):
        cli.main(["create-admin", "carol"])


def test_reset_password_unlocks_and_kills_sessions(env, monkeypatch):
    cli.main(["init"])
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "a-long-enough-pass-1")
    cli.main(["create-admin", "dan"])
    c = conn_of(env)
    c.execute("UPDATE users SET failed_logins=9, locked_until='2999-01-01T00:00:00Z' WHERE username='dan'")
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "another-long-pass-2")
    assert cli.main(["reset-password", "dan"]) == 0
    u = conn_of(env).execute("SELECT * FROM users WHERE username='dan'").fetchone()
    assert u["failed_logins"] == 0 and u["locked_until"] is None and u["must_change_password"] == 1
    assert cli.main(["reset-password", "nobody"]) == 1


def test_production_refuses_demo_and_weak_config(env, monkeypatch):
    assert cli.main(["seed"]) == 2
    monkeypatch.setenv("WW_SECRET_KEY", "short")
    assert cli.main(["serve"]) == 2


def test_commands_need_initialised_db(env):
    for c in ("check", "scan", "purge", "reset-password"):
        assert cli.main([c, "x"] if c == "reset-password" else [c]) == 2


def test_check_detects_corruption_free_db_and_backup_is_valid(env, capsys):
    cli.main(["init"])
    assert cli.main(["check"]) == 0
    assert cli.main(["backup", str(env / "bk")]) == 0
    files = list((env / "bk").glob("wwtextile-*.db"))
    assert len(files) == 1
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert sqlite3.connect(files[0]).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_scan_and_purge_run(env, capsys):
    cli.main(["init"])
    assert cli.main(["scan"]) == 0
    assert cli.main(["purge"]) == 0


def test_healthz_is_public_and_minimal(env):
    from starlette.testclient import TestClient
    from wwtextile.web import create_app
    cli.main(["init"])
    with TestClient(create_app(config.load())) as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.text == "ok"


def test_no_inline_style_attributes_in_templates():
    """The CSP forbids style attributes; they would be silently dropped in the browser."""
    import pathlib, re
    bad = [p.name for p in pathlib.Path("wwtextile/templates").glob("*.html") if re.search(r'\sstyle\s*=', p.read_text())]
    assert not bad, bad


def test_cli_reset_password_ends_existing_sessions(env, monkeypatch):
    cli.main(["init"])
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "a-long-enough-pass-1")
    cli.main(["create-admin", "erin"])
    c = conn_of(env)
    uid = c.execute("SELECT id FROM users WHERE username='erin'").fetchone()[0]
    c.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES ('h', ?, 'c', '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z')", (uid,))
    monkeypatch.setenv("WW_ADMIN_PASSWORD", "another-long-pass-2")
    cli.main(["reset-password", "erin"])
    assert conn_of(env).execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 0
