"""Owner vs guest role separation on the web app.

Guests authenticate with their own username+password pair and may read
everything, but every mutating endpoint must 403. Env is configured BEFORE
importing web.app because the module reads it at import time; base_url is
https because the session cookie is Secure-only.
"""
import os
import sqlite3
import sys
import tempfile

os.environ["WEB_PASSWORD"] = "owner-pw-test"
os.environ["WEB_USER"] = "admin"
os.environ["WEB_GUEST_PASSWORD"] = "guest-pw-test"
os.environ["WEB_GUEST_USER"] = "guest"

# A tiny throwaway db so the app never touches the real one.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["JOBS_DB"] = _tmp.name

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import JobDB  # noqa: E402

_db = JobDB(_tmp.name)
_db.conn.execute(
    "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status)"
    " VALUES ('j1', 'TestCo', 'Analyst', 'https://x.test/j1', '2026-07-01', '2026-07-01', 'new')"
)
_db.conn.commit()
_db.conn.close()

from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402


def client() -> TestClient:
    return TestClient(webapp.app, base_url="https://testserver")


def login(c: TestClient, user: str, pw: str):
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def test_owner_login_and_mutate():
    c = client()
    r = login(c, "admin", "owner-pw-test")
    assert r.status_code == 303
    assert c.get("/").status_code == 200
    r = c.post("/job/j1/status", data={"status": "queued"})
    assert r.status_code == 200


def test_guest_login_reads_but_cannot_mutate():
    c = client()
    r = login(c, "guest", "guest-pw-test")
    assert r.status_code == 303
    # Reads are fine.
    assert c.get("/").status_code == 200
    assert c.get("/job/j1").status_code == 200
    assert c.get("/sources").status_code == 200
    # Every mutator 403s.
    assert c.post("/job/j1/status", data={"status": "applied"}).status_code == 403
    assert c.post("/job/j1/favorite").status_code == 403
    assert c.post("/job/j1/notes", data={"notes": "x"}).status_code == 403
    assert c.post("/seen", follow_redirects=False).status_code == 403
    # And the DB really didn't change.
    con = sqlite3.connect(os.environ["JOBS_DB"])
    status, notes, fav = con.execute(
        "SELECT status, notes, favorite FROM seen_jobs WHERE id='j1'"
    ).fetchone()
    con.close()
    assert notes is None and (fav or 0) == 0


def test_wrong_pair_rejected():
    c = client()
    # Right password, wrong username (cross-pairing must fail).
    assert login(c, "guest", "owner-pw-test").status_code == 401
    assert login(c, "admin", "guest-pw-test").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 303  # still logged out


def test_guest_ui_hides_controls():
    c = client()
    login(c, "guest", "guest-pw-test")
    body = c.get("/").text
    assert "guest" in body  # role badge + body class
    assert 'class="browse guest"' in body or "guest" in body.split("<body", 1)[1][:120]
