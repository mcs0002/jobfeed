#!/usr/bin/env python3
"""Populate filter facets offline for rows without model-generated tags."""
from __future__ import annotations

import argparse
import os

from db import JobDB
from fallback_tag import PROVENANCE, fallback_tag_jobs


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))


def backfill(apply: bool) -> int:
    db = JobDB(DB_PATH)
    columns = ("id", "company", "category", "title", "location", "description")
    rows = [dict(zip(columns, row)) for row in db.conn.execute(
        "SELECT id, company, category, title, location, description "
        "FROM seen_jobs WHERE COALESCE(tag_provider, '') IN ('', 'local')"
    ).fetchall()]
    fallback_tag_jobs(rows)
    print(f"local fallback: {len(rows)} roles prepared")
    if not apply:
        print("DRY RUN - re-run with --apply to write")
        return len(rows)

    with db.transaction():
        for job in rows:
            db.set_tags(
                job["id"], area=job["area"], desk=job["desk"],
                seniority=job["seniority"], job_type=job["job_type"],
                loc_city=job["loc_city"], loc_country=job["loc_country"],
                loc_region=job["loc_region"], work_mode=job["work_mode"],
                lang_req=job["lang_req"], education=job["education"],
                start_date=job["start_date"], min_yoe=job["min_yoe"],
                **PROVENANCE,
            )
    print(f"local fallback: {len(rows)} roles updated")
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes")
    backfill(parser.parse_args().apply)
