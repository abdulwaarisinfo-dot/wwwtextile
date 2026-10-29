"""Parallel posting against the same lot from separate connections: stock must never go negative or double-spend."""
import threading

from wwtextile import db, seed
from wwtextile.config import load
from wwtextile.errors import AppError
from wwtextile.services import inventory, processing


def test_parallel_issue_posts_never_overdraw(tmp_path, monkeypatch):
    path = str(tmp_path / "c.db")
    monkeypatch.setenv("WW_DB_PATH", path)
    monkeypatch.setenv("WW_ENV", "demo")
    cfg = load()
    c = db.connect(path)
    db.migrate(c)
    seed.seed_demo(c, cfg)
    store = c.execute("SELECT * FROM users WHERE username='store1'").fetchone()
    job = c.execute("SELECT j.id FROM processing_jobs j JOIN lots l ON l.id=j.lot_id WHERE l.lot_no='DEMO-105'").fetchone()["id"]
    # 250 m left in the raw store; ten workers each try to issue 100 m -> at most two may succeed
    drafts = [processing.create_issue_draft(c, store, job, dict(qty="100", unit="m")) for _ in range(10)]
    ok, fail, other = [], [], []

    def work(did):
        conn = db.connect(path)
        try:
            processing.post_issue(conn, dict(store), did)
            ok.append(did)
        except AppError:
            fail.append(did)
        except Exception as e:  # a lock timeout etc. must not be silently treated as success
            other.append(repr(e))
        finally:
            conn.close()

    ts = [threading.Thread(target=work, args=(d,)) for d in drafts]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not other, other
    assert len(ok) == 2 and len(fail) == 8
    assert inventory.reconcile_ledger(db.connect(path)) == []
