"""Capture the README screenshots from a demo database.

    .venv/bin/python scripts/readme_screenshots/shoot.py --db /tmp/demo_jobs.db \\
        [--runtime DIR] [--out docs/screenshots]

Starts its own web app on 127.0.0.1 against the demo database and takes each
screenshot as the owner at 1440x900, 2x. Three things keep the owner's real
data out of the images:

* the database is the demo built by build_demo_db.py plus mock_applications.py;
* ANTIGRAVITY_HOME points at an empty directory, because /applications reads
  the agent's conversation records from the machine it runs on;
* the autopilot worker is off, so mock workflows can never start an agent.

Technical stats and Grad schemes read `verify_state.json`, `tag_runs.jsonl` and
`campus_sweep/` from the repository root. `--runtime DIR` stages copies of them
there for the run. Only files that are absent are staged, and only staged files
are removed afterwards, so a machine's real runtime files are never touched.
Leftovers matter: scripts/export_public.sh scans the whole working tree.
"""
import argparse
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
RUNTIME = ("verify_state.json", "tag_runs.jsonl", "campus_sweep")


def first_role(db: str, where: str, params: tuple) -> str:
    """The newest role matching a view, skipping titles with a board prefix
    ("CAMPUS: ...") that read badly as a headline."""
    con = sqlite3.connect(db)
    try:
        for (job_id, title) in con.execute(
                f"SELECT id, title FROM seen_jobs WHERE {where} AND status = 'new' "
                "ORDER BY first_seen DESC", params):
            if ":" not in title.split(" ")[0] and len(title) <= 70:
                return job_id
    finally:
        con.close()
    return ""


def shots(db: str) -> list:
    apac = first_role(db, "job_type = 'graduate-programme' AND loc_region = 'APAC' "
                          "AND area IN ('quant', 'markets')", ())
    return [
        ("browse-internships", "/", {"area": "markets", "job_type": "internship"}, "light"),
        ("new-york-internships", "/", {"loc_city": "New York", "job_type": "internship"}, "light"),
        ("apac-graduate-dark", "/", {"job_type": "graduate-programme", "loc_region": "APAC",
                                     **({"sel": apac} if apac else {})}, "dark"),
        ("applications", "/applications", {}, "light"),
        ("application-stats", "/stats/applications", {}, "light"),
        ("limits", "/limits", {}, "light"),
        ("grad-schemes", "/campus", {"q": "trading"}, "light"),
        ("technical-stats", "/stats/technical", {}, "light"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="demo database, never the live one")
    ap.add_argument("--runtime", help="directory holding copies of the runtime files")
    ap.add_argument("--out", default=str(ROOT / "docs/screenshots"))
    args = ap.parse_args()
    if Path(args.db).resolve() == (ROOT / "jobs.db").resolve():
        sys.exit("refusing to screenshot the live database")

    staged = []
    empty_home = tempfile.mkdtemp(prefix="no-antigravity-")
    password = secrets.token_urlsafe(18)
    env = dict(os.environ, JOBS_DB=args.db, ANTIGRAVITY_HOME=empty_home,
               JOBFEED_AUTOPILOT_WORKER="0", WEB_USER="owner", WEB_PASSWORD=password,
               WEB_GUEST_PASSWORD="", WEB_ALLOW_NO_AUTH="")
    server = None
    try:
        if args.runtime:
            for name in RUNTIME:
                src, dst = Path(args.runtime) / name, ROOT / name
                if src.exists() and not dst.exists():
                    (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
                    staged.append(dst)
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1",
             "--port", str(PORT)], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{BASE}/login", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit("web app did not start")

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, path, params, scheme in shots(args.db):
                ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                          device_scale_factor=2, color_scheme=scheme)
                # The app keeps its theme in localStorage, not prefers-color-scheme.
                ctx.add_init_script(f"localStorage.setItem('jsui.theme', '{scheme}')")
                page = ctx.new_page()
                page.goto(f"{BASE}/login")
                page.fill("input[name=username]", "owner")
                page.fill("input[name=password]", password)
                page.click("button[type=submit], input[type=submit]")
                page.wait_for_load_state("networkidle")
                page.goto(f"{BASE}{path}" + (f"?{urlencode(params)}" if params else ""))
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(500)
                page.screenshot(path=str(out / f"{name}.png"))
                print(name)
                ctx.close()
            browser.close()
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
        for path in staged:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.rmtree(empty_home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
