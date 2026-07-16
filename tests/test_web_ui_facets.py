"""Web-layer tests for the facets update: the Start filter end-to-end, the
"English only" language option, the always-on 8-cell detail meta grid, and the
Description section label.

Env is configured BEFORE importing web.app (module reads it at import time);
the values mirror tests/test_web_roles.py so import order between the two
modules doesn't matter. Each test points webapp.DB_FILE at its own throwaway
db (get_db reads the global per request) and restores it afterwards.
"""
import os
import sys
import tempfile

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import JobDB  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402


ROWS = [
    # id, title, area, start_date, education, lang_req, min_yoe, work_mode, loc_city
    ("r1", "Markets Analyst 2026", "markets", "2026-09", "bachelor", "de,fr", 0, "onsite", "Frankfurt"),
    ("r2", "Quant Grad", "quant", "2027", "master", "", None, "hybrid", "Paris"),
    ("r3", "Trading Analyst ASAP", "markets", "asap", "phd", None, 3, "", "London"),
    ("r4", "Untagged Role", "", None, None, None, None, "", ""),
]


def make_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db = JobDB(tmp.name)
    for jid, title, area, start, edu, lang, yoe, mode, city in ROWS:
        db.conn.execute(
            "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen,"
            " status, area, start_date, education, lang_req, min_yoe, work_mode,"
            " loc_city, description)"
            " VALUES (?, 'TestCo', ?, ?, '2026-07-01', '2026-07-01', 'new', ?, ?, ?,"
            " ?, ?, ?, ?, 'Support the desk in pricing and execution.')",
            (jid, title, f"https://x.test/{jid}", area, start, edu, lang, yoe, mode, city),
        )
    db.conn.commit()
    db.conn.close()
    return tmp.name


def run(fn):
    """Run one test body against a fresh db, restoring webapp.DB_FILE after."""
    path = make_db()
    old = webapp.DB_FILE
    webapp.DB_FILE = path
    try:
        c = TestClient(webapp.app, base_url="https://testserver")
        r = c.post("/login", data={"username": os.environ["WEB_USER"],
                                   "password": os.environ["WEB_PASSWORD"]},
                   follow_redirects=False)
        assert r.status_code == 303
        fn(c)
    finally:
        webapp.DB_FILE = old
        os.unlink(path)


def _titles(c, url):
    import json
    data = json.loads(c.get(url).text)
    return {j["title"] for j in data["jobs"]}


# ── Start filter end-to-end ──────────────────────────────────────────────────
def test_start_filter_year_prefix_and_asap():
    def body(c):
        assert _titles(c, "/api/jobs?start=2026") == {"Markets Analyst 2026"}
        assert _titles(c, "/api/jobs?start=2027") == {"Quant Grad"}
        # r3 carries min_yoe=3, hidden by the default senior gate — reveal it.
        assert _titles(c, "/api/jobs?start=asap&show_senior=1") == {"Trading Analyst ASAP"}
        # No start filter: untagged row is present.
        assert "Untagged Role" in _titles(c, "/api/jobs")
    run(body)


def test_start_select_options_and_chip():
    def body(c):
        page = c.get("/?start=asap").text
        # Panel group renamed + cols-3, dropdown has ASAP + one option per year.
        assert "Recency &amp; start" in page
        assert "Recency &amp; status" not in page
        assert '<option value="asap" selected>ASAP</option>' in page
        assert '<option value="2026"' in page and '<option value="2027"' in page
        # Chip renders and its removal link drops only `start`.
        assert "Start: ASAP" in page
        page = c.get("/?start=2026").text
        assert "Start: 2026" in page
        # Panel group order: Role → Location → Requirements → Recency & start → Show.
        order = ["Role", "Location", "Requirements", "Recency &amp; start", "Show"]
        idx = [page.find(f'<div class="fp-label">{lbl}</div>') for lbl in order]
        assert all(i >= 0 for i in idx) and idx == sorted(idx)
    run(body)


# ── English only ─────────────────────────────────────────────────────────────
def test_lang_none_english_only():
    def body(c):
        # '' matches (r2), NULL (r3/r4) and 'de,fr' (r1) excluded.
        assert _titles(c, "/api/jobs?lang_req=none") == {"Quant Grad"}
        page = c.get("/?lang_req=none").text
        assert '<option value="none" selected>English only</option>' in page
        assert ">English only <span" in page  # chip label
    run(body)


# ── Detail pane: 8 always-on meta cells + Description label ─────────────────
def test_detail_meta_grid_tagged_row():
    def body(c):
        pane = c.get("/job/r1?pane=1").text
        for k in ("Area", "Location", "Type", "First seen", "Start",
                  "Education", "Languages", "Experience"):
            assert f'<div class="k">{k}</div>' in pane
        assert "2026-09" in pane
        assert "bachelor+" in pane
        assert "English + German, French" in pane
        assert "entry level" in pane            # min_yoe 0
        assert "Frankfurt · onsite" in pane     # work mode always appended
        assert '<div class="dp-section-label">Description</div>' in pane
    run(body)


def test_detail_meta_grid_untagged_row_dashes():
    def body(c):
        pane = c.get("/job/r4?pane=1").text
        for k in ("Start", "Education", "Languages", "Experience"):
            assert f'<div class="k">{k}</div>' in pane
        assert pane.count('<div class="v">—</div>') >= 2  # Start + Education unknown
        assert ">English<" in pane.replace("\n", "")      # baseline, no + suffix
        assert "entry level" in pane                      # min_yoe NULL
    run(body)


def test_detail_meta_asap_phd_and_yoe():
    def body(c):
        pane = c.get("/job/r3?pane=1").text
        assert '<div class="v">ASAP</div>' in pane
        assert "phd" in pane and "phd+" not in pane   # no + suffix on phd
        assert "3+ years" in pane
        assert "English" in pane                      # lang_req NULL → baseline
    run(body)
