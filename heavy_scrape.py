#!/usr/bin/env python3
"""Single-company scrape entry point for the heavy-board path.

Giant / slow boards (JPM's 7k-role Oracle tenant, BNP's paced ~10min Akamai
sweep) can't run inside main.py's ThreadPoolExecutor: a worker thread that
overruns COMPANY_TIMEOUT can't be killed, so it wedges the whole scan. Running
each heavy board as its own *process* fixes that — a subprocess that overruns
its timeout is killed cleanly by the parent (see main.scrape_heavy_targets).

It reuses main.scrape_company (same dispatch as the light path), so a board only
needs `"heavy": true` in targets.json to move here — no scraper changes. Emits a
single JSON object on stdout: {"jobs": [...], "error": null|str}.

  python heavy_scrape.py --company "J.P. Morgan"
"""
import argparse
import json
import sys

from main import load_targets, scrape_company


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True, help="Exact target name")
    args = parser.parse_args()

    company = next(
        (c for c in load_targets() if c.get("name") == args.company), None
    )
    if company is None:
        json.dump({"jobs": [], "error": f"unknown company: {args.company}"},
                  sys.stdout)
        return 1

    jobs, error = scrape_company(company)
    json.dump({"jobs": jobs, "error": error}, sys.stdout)
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
