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
from pathlib import Path

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("JOBS_DB", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from jobfeed.db import JobDB  # noqa: E402

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
            " loc_city)"
            " VALUES (?, 'TestCo', ?, ?, '2026-07-01', '2026-07-01', 'new', ?, ?, ?,"
            " ?, ?, ?, ?)",
            (jid, title, f"https://x.test/{jid}", area, start, edu, lang, yoe, mode, city),
        )
        db.set_description(jid, "Support the desk in pricing and execution.")
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


def test_card_opening_does_not_compete_with_star_toggle():
    """A list-star click must not also inherit a detail-pane GET from its card."""
    root = Path(__file__).resolve().parents[1]
    role_list = (root / "web/templates/_role_list.html").read_text()
    index = (root / "web/templates/index.html").read_text()
    assert "hx-get=" not in role_list
    assert "if (star)" in index and "toggleStar" in index
    assert "open(card);" in index


def test_browse_view_is_persisted_across_navigation():
    root = Path(__file__).resolve().parents[1]
    base = (root / "web/templates/base.html").read_text()
    index = (root / "web/templates/index.html").read_text()
    assert base.count("data-browse-link") >= 2
    assert 'localStorage.getItem("jsui.lastBrowse")' in base
    assert 'localStorage.setItem("jsui.lastBrowse", browseHref)' in index


def test_desktop_filters_use_wide_sectioned_card():
    root = Path(__file__).resolve().parents[1]
    css = (root / "web/static/style.css").read_text()
    index = (root / "web/templates/index.html").read_text()
    assert "width: min(760px, calc(100vw - 28px))" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert '<span class="sh-title">Filters</span>' in index
    assert "Narrow the feed without losing your place" not in index


def test_filter_reset_is_single_toolbar_control_and_tracks_active_state():
    root = Path(__file__).resolve().parents[1]
    index = (root / "web/templates/index.html").read_text()
    role_list = (root / "web/templates/_role_list.html").read_text()
    assert index.count(">Reset all</a>") == 1
    assert 'id="browse-reset"' in index
    assert 'reset.hidden = n === 0' in index
    assert 'class="sh-reset"' not in index
    assert 'class="btn-reset-filters"' not in index
    assert 'class="le-reset"' not in role_list

    def body(c):
        clean = c.get("/").text
        assert 'id="browse-reset" href="/"\n        hidden' in clean

        filtered = c.get("/?loc_city=Frankfurt").text
        assert 'id="browse-reset" href="/"\n        hidden' not in filtered
        assert filtered.count(">Reset all</a>") == 1

        empty = c.get("/?area=other").text
        assert empty.count(">Reset all</a>") == 1
        assert "Reset filters" not in empty
    run(body)


def test_filter_card_dismissal_and_sticky_seam_guards():
    root = Path(__file__).resolve().parents[1]
    index = (root / "web/templates/index.html").read_text()
    css = (root / "web/static/style.css").read_text()
    assert 'id="filter-scrim" hidden onclick="window.__toggleFilters(false)"' in index
    assert 'class="sh-close" onclick="window.__toggleFilters(false)"' in index
    assert "position: fixed; inset: 0; z-index: 7" in css
    assert "position: absolute; top: 8px; right: 8px" in css
    assert "background: var(--bg);" in css
    assert "flex: 1; overflow-y: auto; padding: 0 14px 16px;" in css


def test_browse_search_is_removed_and_legacy_query_is_stripped():
    root = Path(__file__).resolve().parents[1]
    index = (root / "web/templates/index.html").read_text()
    assert 'id="head-search"' not in index
    assert 'e.key === "/"' not in index

    def body(c):
        r = c.get("/?q=Munich&loc_city=Frankfurt", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/?loc_city=Frankfurt"
    run(body)


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
        # Start sits in the Timing group; ASAP plus one option per year.
        assert '<option value="asap" selected>ASAP</option>' in page
        assert '<option value="2026"' in page and '<option value="2027"' in page
        # Chip renders and its removal link drops only `start`.
        assert "Start: ASAP" in page
        page = c.get("/?start=2026").text
        assert "Start: 2026" in page
        # Panel group order since the 2026-08-19 restructure (6929518).
        order = ["Role", "Location", "Timing", "Show", "Saved"]
        idx = [page.find(f'<div class="fp-gutter">{lbl}</div>') for lbl in order]
        assert all(i >= 0 for i in idx) and idx == sorted(idx)
    run(body)


# ── English only ─────────────────────────────────────────────────────────────
def test_lang_none_english_only():
    def body(c):
        # '' matches (r2), NULL (r3/r4) and 'de,fr' (r1) excluded. The panel's
        # language dropdown was removed 2026-08-19 (6929518); the filter itself
        # survives on the query string and the JSON API.
        assert _titles(c, "/api/jobs?lang_req=none") == {"Quant Grad"}
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


def load_tests(loader, tests, pattern):
    from _function_tests import function_suite
    return function_suite(globals())
