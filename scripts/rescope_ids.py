#!/usr/bin/env python3
"""Re-key one tenant's stored rows from bare `<ats>_<n>` ids to scoped ones.

Why: tal.net and iCIMS vacancy numbers are per tenant. A tenant wired before
scoping existed stores bare ids (`talnet_1427`), and when a second tenant
emits the same number the scan reports "cross-tenant job id collision" and
never stores the second role. The fix is `talnet_id_scope` / `icims.id_scope`
on the target, but that changes the ids the scraper emits, so every stored
row must move to the new id in the same change or the next scan stores the
roles again as new while status, stars, workflows and mail events stay on the
old keys.

Rows are selected by URL host, not company name, so a sibling source label
("Firm (Campus)") on the same tenant moves too, and nothing on another tenant
is touched. Every column that holds a job id is listed in JOB_ID_COLUMNS; the
script refuses to run if the schema has a `job_id` column it does not know,
so a new table cannot be skipped silently.

Guarded like split_descriptions.py: refuses while .deliver.lock exists,
writes and checks an online backup first, re-keys in one IMMEDIATE
transaction and verifies counts and foreign keys before committing.
Idempotent: a tenant with no bare rows left is reported and left alone.

Usage (on the M1, from the repo, after the targets.json scope is pulled):
    .venv/bin/python scripts/rescope_ids.py --ats talnet \\
        --host fidelityinternational.tal.net --scope fil
    add --dry-run to print the plan only.
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (table, column) pairs holding a seen_jobs id. seen_jobs.id itself first.
JOB_ID_COLUMNS = [
    ("seen_jobs", "id"),
    ("job_descriptions", "job_id"),
    ("description_attempts", "job_id"),
    ("tag_evaluations", "job_id"),
    ("applications", "job_id"),
    ("application_workflows", "job_id"),
    ("application_mail_events", "job_id"),
]


def unknown_job_id_columns(con) -> list[str]:
    known = set(JOB_ID_COLUMNS)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    return [f"{t}.job_id" for t in tables
            if (t, "job_id") not in known
            and any(r[1] == "job_id" for r in con.execute(f"PRAGMA table_info({t})"))]


def plan(con, ats: str, host: str, scope: str) -> dict[str, str]:
    """old id -> new id for every bare row stored under `host`."""
    bare = re.compile(rf"^{re.escape(ats)}_(\d+)$")
    mapping = {}
    for job_id, url in con.execute(
            "SELECT id, url FROM seen_jobs WHERE id LIKE ?", (f"{ats}_%",)):
        m = bare.match(job_id)
        if m and urlparse(url or "").hostname == host:
            mapping[job_id] = f"{ats}_{scope}_{m.group(1)}"
    return mapping


def present_columns(con) -> list[tuple[str, str]]:
    """JOB_ID_COLUMNS whose table exists (`applications` is legacy and only
    present on databases old enough to have had it)."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [(t, c) for t, c in JOB_ID_COLUMNS if t in tables]


def counts(con, ids) -> dict[str, int]:
    ids = list(ids)
    q = ",".join("?" * len(ids))
    return {f"{t}.{c}": con.execute(
        f"SELECT count(*) FROM {t} WHERE {c} IN ({q})", ids).fetchone()[0]
        for t, c in present_columns(con)}


def rescope(con, mapping: dict[str, str]) -> dict[str, int]:
    """Apply `mapping` in one transaction; returns per-column moved counts.
    Raises (and rolls back) on any collision, count mismatch or FK error."""
    before = counts(con, mapping)
    taken = counts(con, mapping.values())
    if any(taken.values()):
        raise RuntimeError(f"new ids already present: {taken}")
    # The FKs on seen_jobs(id) have no ON UPDATE CASCADE; re-key parent and
    # children together with enforcement off and check the result instead.
    con.execute("PRAGMA foreign_keys = OFF")
    # Judge only violations this change introduces: the live DB can carry
    # unrelated orphans (a workflow whose role was purged) that predate it.
    preexisting = set(con.execute("PRAGMA foreign_key_check").fetchall())
    con.execute("BEGIN IMMEDIATE")
    try:
        for t, c in present_columns(con):
            for old, new in mapping.items():
                con.execute(f"UPDATE {t} SET {c} = ? WHERE {c} = ?", (new, old))
        left = counts(con, mapping)
        after = counts(con, mapping.values())
        if any(left.values()) or after != before:
            raise RuntimeError(f"count mismatch: before {before}, after {after}, "
                               f"left {left}")
        bad = [v for v in con.execute("PRAGMA foreign_key_check").fetchall()
               if v not in preexisting]
        if bad:
            raise RuntimeError(f"foreign_key_check: {bad[:5]}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    ap.add_argument("--ats", required=True, choices=["talnet", "icims"])
    ap.add_argument("--host", required=True, help="tenant host, e.g. x.tal.net")
    ap.add_argument("--scope", required=True)
    ap.add_argument("--backup", help="default: jobs.pre-rescope-<scope>-<ts>.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9]+", args.scope):
        print("ABORT: scope must be lowercase alphanumeric", file=sys.stderr)
        return 2
    if os.path.isdir(os.path.join(ROOT, ".deliver.lock")):
        print("ABORT: .deliver.lock exists, a scan is active", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"ABORT: no database at {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    unknown = unknown_job_id_columns(con)
    if unknown:
        print(f"ABORT: unlisted job id columns {unknown}; add them to "
              "JOB_ID_COLUMNS", file=sys.stderr)
        return 2

    mapping = plan(con, args.ats, args.host, args.scope)
    if not mapping:
        print(f"nothing to do: no bare {args.ats} rows under {args.host}")
        return 0
    print(f"{len(mapping)} rows under {args.host} -> {args.ats}_{args.scope}_<n>")
    for col, n in counts(con, mapping).items():
        print(f"  {col}: {n}")
    if args.dry_run:
        return 0

    backup = args.backup or os.path.join(
        os.path.dirname(os.path.abspath(args.db)),
        f"jobs.pre-rescope-{args.scope}-{datetime.now():%Y%m%d-%H%M%S}.db")
    if os.path.exists(backup):
        print(f"ABORT: backup path exists: {backup}", file=sys.stderr)
        return 2
    dst = sqlite3.connect(backup)
    con.backup(dst)
    ok = dst.execute("PRAGMA quick_check").fetchone()[0]
    same = (dst.execute("SELECT count(*) FROM seen_jobs").fetchone()[0]
            == con.execute("SELECT count(*) FROM seen_jobs").fetchone()[0])
    dst.close()
    if ok != "ok" or not same:
        print(f"ABORT: backup check failed ({ok}, counts match: {same})",
              file=sys.stderr)
        return 2
    print(f"backup: {backup}")

    moved = rescope(con, mapping)
    print("re-keyed:")
    for col, n in moved.items():
        print(f"  {col}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
