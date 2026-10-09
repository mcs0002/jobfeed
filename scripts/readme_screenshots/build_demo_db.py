"""Build a demo jobs.db for the README screenshots: public postings only.

Usage (on the machine that holds the live database):
    .venv/bin/python scripts/readme_screenshots/build_demo_db.py --dest /tmp/demo_jobs.db


Reads the live database read-only, copies a curated set of current entry-level
front-office roles across ten cities through JobDB's own API (so the web app
sees its real schema), and nothing personal: every row is status 'new', no
favourites, notes, limits, mail, workflows or runs.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobfeed.db import JobDB  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--source", default=str(ROOT / "jobs.db"))
ap.add_argument("--dest", required=True)
args = ap.parse_args()
SRC = Path(args.source)
DEST = Path(args.dest)
if SRC.resolve() == DEST.resolve():
    sys.exit("refusing to overwrite the source database")
CITIES = ["London", "New York", "Frankfurt", "Zurich", "Paris", "Amsterdam",
          "Singapore", "Hong Kong", "Chicago", "Dubai", "Milan", "Madrid", "Geneva",
          "Toronto", "Sydney", "Tokyo"]
AREAS = ("markets", "quant", "research", "economics", "ibd", "private-markets",
         "asset-management", "capital-markets", "consulting")
PER_CITY = 70
TAGS = ("area", "desk", "seniority", "job_type", "loc_city", "loc_country",
        "loc_region", "work_mode", "lang_req", "education", "start_date", "min_yoe")

src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
src.row_factory = sqlite3.Row
if DEST.exists():
    DEST.unlink()
db = JobDB(str(DEST))
kept = 0
for city in CITIES:
    rows = src.execute(f"""
        SELECT * FROM jobs_with_description
         WHERE delisted_at IS NULL AND loc_city = ?
           AND area IN ({",".join("?" * len(AREAS))})
           AND seniority IN ('intern', 'graduate', 'analyst')
           AND COALESCE(min_yoe, 0) <= 2
           AND last_seen >= datetime('now', '-2 days')
           AND length(COALESCE(description, '')) > 800
         ORDER BY (job_type IN ('internship', 'graduate-programme')) DESC,
                  first_seen DESC LIMIT ?""", (city, *AREAS, PER_CITY)).fetchall()
    for r in rows:
        db.mark_seen(r["id"], company=r["company"], title=r["title"], url=r["url"],
                     category=r["category"] or "", location=r["location"] or "",
                     posted=r["posted"] or "", description=r["description"] or "",
                     deadline=r["deadline"] or "")
        db.set_tags(r["id"], **{k: r[k] for k in TAGS if k in r.keys()},
                    provider=r["tag_provider"] or "", model=r["tag_model"] or "",
                    rubric_version=r["tag_rubric_version"] or "")
        db.conn.execute("UPDATE seen_jobs SET first_seen=?, last_seen=? WHERE id=?",
                        (r["first_seen"], r["last_seen"], r["id"]))
        kept += 1
    db.conn.commit()
    print(f"{city}: {len(rows)}")

# Prove the demo carries nothing personal before it leaves the M1.
assert db.conn.execute("SELECT COUNT(*) FROM seen_jobs WHERE status <> 'new' "
                       "OR COALESCE(favorite,0) <> 0 OR notes IS NOT NULL "
                       "OR applied_at IS NOT NULL").fetchone()[0] == 0
for table in ("application_workflows", "application_mail_events", "applications",
              "application_runs", "company_limits", "campus_state", "application_accounts"):
    try:
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    except sqlite3.OperationalError:
        pass
# Scan history for Technical stats: counts and timings, nothing personal.
cols = [r[1] for r in src.execute("PRAGMA table_info(runs)")]
runs = src.execute(f"SELECT {','.join(cols)} FROM runs ORDER BY rowid DESC LIMIT 400").fetchall()
db.conn.executemany(f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                    [tuple(r) for r in runs])
db.conn.commit()
print("runs", len(runs))
db.conn.execute("VACUUM")
db.conn.close()
print("kept", kept, "bytes", DEST.stat().st_size)
