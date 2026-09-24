#!/usr/bin/env python3
"""Export a shareable subset of jobs.db.

The live database mixes two very different things: facts about jobs, which are
expensive to collect and tag and are worth sharing, and the user's own job
search, which is not. This produces a derived database containing only the
former, so the file itself can be handed to someone else.

Deliberately excluded:

  * the entire `applications` table (application documents, ATS answers, screenshots)
  * `seen_jobs.score`, `score_reason`, `scored_at` - personal fit scoring, not
    a property of the job
  * `seen_jobs.applied_at`, `notes`, `favorite` - personal state

The safety property that matters is the UNKNOWN-COLUMN ABORT below. A column
added to `seen_jobs` later is neither shared nor personal until someone
decides, so this refuses to run rather than guessing. Guessing would silently
publish a new personal field the first night after a schema change.

Usage:
    export_shared.py --source ~/projects/job_scraper/jobs.db \\
                     --dest   /tmp/jobs_shared.db
    export_shared.py --with-descriptions      # +234 MB as of 2026-09-01
    export_shared.py --gzip                   # also write <dest>.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# Columns that describe the job. Safe to share.
SHARED_COLUMNS = [
    "id",
    "company",
    "title",
    "url",
    "first_seen",
    "last_seen",
    "category",
    "location",
    "posted",
    "delisted_at",
    # Why the listing went away (aged out vs gone from the board) — a property
    # of the posting's lifecycle, same category as delisted_at above.
    "delisted_by_age",
    "description_fetched_at",
    # tagging output - the expensive part, and the reason to share at all
    "function",
    "seniority",
    "job_type",
    "loc_city",
    "loc_country",
    "loc_region",
    "work_mode",
    "area",
    "desk",
    "min_yoe",
    "lang_req",
    "education",
    "start_date",
    "deadline",
    "tagged_at",
    # tag provenance, so a consumer can tell what produced a label
    "tag_provider",
    "tag_model",
    "tag_rubric_version",
]

# Included only with --with-descriptions.
OPTIONAL_COLUMNS = ["description"]

# Personal. Never exported. Listed explicitly so the intent is auditable and so
# the unknown-column check below can tell "known personal" from "new".
PERSONAL_COLUMNS = [
    # `status` is the application CRM stage (queued / applied / rejected /
    # interview). It was shared until 2026-09-08, which contradicted this
    # file's own purpose: it names the roles he applied to and was rejected
    # from, which is his job search, not a property of the job. the user's
    # call — status stays on the private site, which reads the live DB.
    # Nothing is lost to a consumer: it is 'new' for all but four of ~53,000
    # rows.
    "status",
    "score",
    "score_reason",
    "scored_at",
    "applied_at",
    "notes",
    "favorite",
]

# 2 (2026-09-08): `status` removed from the shared schema — see PERSONAL_COLUMNS.
# Dropping a column breaks a consumer that selects it, so this is a bump, not a
# silent edit.
SCHEMA_VERSION = 2


def classify_columns(live: list[str], with_descriptions: bool) -> list[str]:
    """Return the columns to export, or abort if the schema has drifted."""
    known = set(SHARED_COLUMNS) | set(OPTIONAL_COLUMNS) | set(PERSONAL_COLUMNS)
    unknown = [c for c in live if c not in known]
    if unknown:
        sys.exit(
            "ABORT: seen_jobs has columns this exporter has never seen: "
            + ", ".join(unknown)
            + "\nAdd each to SHARED_COLUMNS or PERSONAL_COLUMNS in "
            + os.path.basename(__file__)
            + " and re-run. Refusing to guess, because guessing publishes "
            "personal data."
        )

    missing = [c for c in SHARED_COLUMNS if c not in live]
    if missing:
        sys.exit(
            "ABORT: seen_jobs is missing columns this exporter expects: "
            + ", ".join(missing)
            + "\nThe schema changed; update the exporter."
        )

    cols = list(SHARED_COLUMNS)
    if with_descriptions:
        for c in OPTIONAL_COLUMNS:
            if c in live:
                cols.append(c)
    return cols


def export(source: str, dest: str, with_descriptions: bool) -> dict:
    if not os.path.exists(source):
        sys.exit(f"ABORT: source database not found: {source}")
    if os.path.exists(dest):
        os.remove(dest)

    # Read-only URI so a running scan can never be disturbed by this.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # The view restores the pre-split shape (description and
        # description_fetched_at live in job_descriptions since 2026-09-23),
        # so the column guard and the exported file are unchanged.
        live = [r[1] for r in src.execute("PRAGMA table_info(jobs_with_description)")]
        if not live:
            sys.exit("ABORT: no jobs_with_description view in source database "
                     "(pre-split schema? run scripts/split_descriptions.py)")
        cols = classify_columns(live, with_descriptions)

        out = sqlite3.connect(dest)
        try:
            collist = ", ".join(cols)
            out.execute(f"CREATE TABLE jobs ({', '.join(c + ' TEXT' for c in cols)})")
            placeholders = ", ".join("?" for _ in cols)

            rows = 0
            cur = src.execute(f"SELECT {collist} FROM jobs_with_description")
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                out.executemany(
                    f"INSERT INTO jobs ({collist}) VALUES ({placeholders})", batch
                )
                rows += len(batch)

            out.execute("CREATE INDEX idx_jobs_company ON jobs(company)")
            out.execute("CREATE INDEX idx_jobs_first_seen ON jobs(first_seen)")
            out.execute("CREATE INDEX idx_jobs_function ON jobs(function)")

            manifest = {
                "schema_version": SCHEMA_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "rows": rows,
                "columns": cols,
                "includes_descriptions": with_descriptions,
                "excluded_personal_columns": PERSONAL_COLUMNS,
                "excluded_tables": ["applications", "runs", "tag_evaluations", "meta"],
            }
            out.execute("CREATE TABLE export_meta (key TEXT PRIMARY KEY, value TEXT)")
            out.executemany(
                "INSERT INTO export_meta (key, value) VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in manifest.items()],
            )
            out.commit()
        finally:
            out.close()
    finally:
        src.close()

    # VACUUM in a separate connection so the file is compact on disk.
    vac = sqlite3.connect(dest)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()

    manifest["bytes"] = os.path.getsize(dest)
    return manifest


def verify(dest: str) -> None:
    """Prove the personal columns really are absent before anything ships."""
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        tables = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for banned in ("applications",):
            if banned in tables:
                sys.exit(f"ABORT: export contains table {banned!r}")

        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
        leaked = [c for c in PERSONAL_COLUMNS if c in cols]
        if leaked:
            sys.exit(f"ABORT: export contains personal columns: {', '.join(leaked)}")
    finally:
        con.close()


def main() -> None:
    home = os.path.expanduser("~")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=os.path.join(home, "projects/job_scraper/jobs.db"))
    ap.add_argument("--dest", default=os.path.join(home, "projects/job_scraper/jobs_shared.db"))
    ap.add_argument("--with-descriptions", action="store_true",
                    help="include the description column (much larger file)")
    ap.add_argument("--gzip", action="store_true", help="also write <dest>.gz")
    args = ap.parse_args()

    manifest = export(args.source, args.dest, args.with_descriptions)
    verify(args.dest)

    print(f"wrote {args.dest}")
    print(f"  rows          {manifest['rows']}")
    print(f"  columns       {len(manifest['columns'])}")
    print(f"  descriptions  {manifest['includes_descriptions']}")
    print(f"  size          {manifest['bytes'] / 1048576:.1f} MB")

    if args.gzip:
        gz = args.dest + ".gz"
        with open(args.dest, "rb") as fh, gzip.open(gz, "wb", compresslevel=6) as out:
            shutil.copyfileobj(fh, out)
        print(f"  gzipped       {os.path.getsize(gz) / 1048576:.1f} MB -> {gz}")


if __name__ == "__main__":
    main()
