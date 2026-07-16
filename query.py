#!/usr/bin/env python3
"""
Browse, export, and update the roles database.

Usage:
  python3 query.py                          # list all stored roles
  python3 query.py --status new             # filter by status
  python3 query.py --category Bank          # filter by category (substring)
  python3 query.py --company "Jane Street"  # filter by company (substring)
  python3 query.py --export roles.csv       # export matching roles to CSV
  python3 query.py --mark <job_id> applied  # set a role's status

Set JOBS_DB to point at an alternative database file.
"""
import argparse
import csv
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))

sys.path.insert(0, ROOT)

from db import JobDB

COLUMNS = [
    "id", "company", "category", "title", "location",
    "posted", "status", "first_seen", "last_seen", "url",
]


def fetch_roles(db, status=None, category=None, company=None):
    query = f"SELECT {', '.join(COLUMNS)} FROM seen_jobs"
    clauses = []
    params = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("category LIKE ?")
        params.append(f"%{category}%")
    if company:
        clauses.append("company LIKE ?")
        params.append(f"%{company}%")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY company, title"
    cur = db.conn.execute(query, params)
    return [dict(zip(COLUMNS, row)) for row in cur.fetchall()]


def print_table(roles):
    if not roles:
        print("No roles found.")
        return
    headers = ["company", "title", "location", "category", "status", "first_seen", "id"]
    rows = [
        [str(role.get(header) or "")[:60] for header in headers]
        for role in roles
    ]
    widths = [
        max(len(header), *(len(row[i]) for row in rows))
        for i, header in enumerate(headers)
    ]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print(f"\n{len(roles)} role(s)")


def export_csv(roles, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(roles)
    print(f"Exported {len(roles)} role(s) to {path}")


def main():
    parser = argparse.ArgumentParser(description="Browse and export the roles database")
    parser.add_argument("--status", help="Filter by status (e.g. new, applied, ignored)")
    parser.add_argument("--category", help="Filter by category (substring match)")
    parser.add_argument("--company", help="Filter by company (substring match)")
    parser.add_argument("--export", metavar="PATH", help="Export matching roles to CSV")
    parser.add_argument(
        "--mark", nargs=2, metavar=("JOB_ID", "STATUS"),
        help="Set a role's status (e.g. applied, ignored)",
    )
    args = parser.parse_args()

    db = JobDB(DB_FILE)

    if args.mark:
        job_id, status = args.mark
        if db.set_status(job_id, status):
            print(f"Marked {job_id} as {status}")
        else:
            print(f"No role with id {job_id}", file=sys.stderr)
            sys.exit(1)
        return

    roles = fetch_roles(
        db,
        status=args.status,
        category=args.category,
        company=args.company,
    )

    if args.export:
        export_csv(roles, args.export)
    else:
        print_table(roles)


if __name__ == "__main__":
    main()
