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
from jobfeed.db import JobDB  # noqa: E402

_db = JobDB(_tmp.name)
_db.conn.execute(
    "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status)"
    " VALUES ('j1', 'TestCo', 'Analyst', 'https://x.test/j1', '2026-07-01', '2026-07-01', 'new')"
)
_db.conn.execute(
    "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status, favorite)"
    " VALUES ('j2', 'PrivateCo', 'Private Role', 'https://x.test/j2',"
    " '2026-07-02', '2026-07-02', 'applied', 1)"
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

    topnav = body.split('<nav class="topnav">', 1)[1].split("</nav>", 1)[0]
    assert 'href="/"' in topnav
    assert 'href="/stats/technical"' in topnav
    assert 'href="/sources"' in topnav
    for href in ("/applications", "/stats/applications", "/review", "/limits", "/campus"):
        assert f'href="{href}"' not in topnav

    # Owner-state controls and values are absent from guest HTML, not merely
    # hidden with CSS.
    assert 'aria-label="Status"' not in body
    assert "★ Favorites" not in body
    assert 'class="star' not in body
    assert 'class="rc-status' not in body
    assert "data-status=" not in body
    assert "is-new" not in body
    assert "newdot" not in body
    assert "mark seen" not in body


def test_guest_routes_are_default_deny_outside_shared_surface():
    c = client()
    login(c, "guest", "guest-pw-test")
    for path in ("/applications", "/stats", "/stats/applications", "/review",
                 "/limits", "/campus", "/preview/why-this-is-here", "/api/jobs"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 403, path
    for path in ("/", "/partials/jobs", "/job/j1", "/sources", "/stats/technical"):
        assert c.get(path).status_code == 200, path


def test_guest_cannot_restore_owner_status_or_favorites_through_query_params():
    c = client()
    login(c, "guest", "guest-pw-test")
    r = c.get("/?status=applied&fav=1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    # HTMX partials are sanitized too, even without the full-page redirect.
    body = c.get("/partials/jobs?status=applied&fav=1").text
    assert "Analyst" in body
    assert "Private Role" in body
    assert "Status: applied" not in body
    assert "Favorites" not in body
    assert 'class="star' not in body
    assert 'class="rc-status' not in body

    detail = c.get("/job/j2").text
    assert "badge-applied" not in detail
    assert "<dt>Status</dt>" not in detail
    assert "<dt>Applied</dt>" not in detail


def test_owner_keeps_full_navigation_and_personal_browse_state():
    db = JobDB(os.environ["JOBS_DB"])
    db.conn.execute("UPDATE seen_jobs SET status='new', area='markets' WHERE id='j1'")
    db.set_meta("last_seen_ts", "1970-01-01T00:00:00")
    db.conn.close()
    c = client()
    login(c, "admin", "owner-pw-test")
    body = c.get("/").text
    topnav = body.split('<nav class="topnav">', 1)[1].split("</nav>", 1)[0]
    for href in ("/", "/applications", "/stats/applications", "/stats/technical",
                 "/sources", "/review", "/limits", "/campus"):
        assert f'href="{href}"' in topnav
    assert 'aria-label="Status"' in body
    assert "★ Favorites" in body
    assert 'class="star' in body
    assert 'class="rc-status' in body
    assert "is-new" in body
    assert "mark seen" in body


# Under `unittest discover` every web test module shares one imported web.app,
# so the import-time JOBS_DB above may not be the one the app read. Pin this
# module's DB and credentials around each test, as the unittest-style web
# tests do, and put back whatever was there.
_SAVED = {}
_PINNED = {
    "DB_FILE": _tmp.name,
    "WEB_USER": "admin", "WEB_PASSWORD": "owner-pw-test",
    "WEB_GUEST_USER": "guest", "WEB_GUEST_PASSWORD": "guest-pw-test",
}


def _pin():
    for name, value in _PINNED.items():
        _SAVED[name] = getattr(webapp, name)
        setattr(webapp, name, value)


def _unpin():
    for name, value in _SAVED.items():
        setattr(webapp, name, value)


def load_tests(loader, tests, pattern):
    from _function_tests import function_suite
    return function_suite(globals(), setup=_pin, teardown=_unpin)
