#!/usr/bin/env python3
"""The manual-check list: firms we want but can't scrape reliably.

The scraper aims for ~95% coverage of the target list automatically. The
remainder are companies whose ATS can't be scraped dependably (anti-bot WAF,
JS-only boards, no stable endpoint) even after a real attempt. Rather than let
them rot as silent failures, we flag the target `"ats": "manual"` with a
`career_url` and `manual_reason`, and you check those few by hand.

Koch Supply & Trading is the canonical case: a firm the user likes, but its
Avature board sits behind an AWS WAF that needs a fresh token every week.

Usage:
    python manual_check.py            # print the list (markdown)
    python manual_check.py --md FILE  # also write it to FILE

Convention for routing a firm here: after a genuine attempt to wire/repair its
scraper fails, set its target to:
    {"name": "...", "category": "...", "ats": "manual",
     "career_url": "https://...", "manual_reason": "why it can't be automated"}
"""
import argparse
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(ROOT, "targets.json")


def manual_targets() -> list[dict]:
    arr = json.load(open(TARGETS))
    rows = [t for t in arr if t.get("ats") == "manual"]
    return sorted(rows, key=lambda t: (t.get("category", ""), t["name"]))


def render(rows: list[dict]) -> str:
    if not rows:
        return "# Manual check list\n\n_None — every wanted firm is scraped automatically._\n"
    out = ["# Manual check list",
           "",
           f"{len(rows)} firm(s) to review by hand (can't be scraped reliably).",
           ""]
    cat = None
    for t in rows:
        if t.get("category") != cat:
            cat = t.get("category")
            out.append(f"\n## {cat or 'Uncategorised'}\n")
        url = t.get("career_url", "")
        reason = t.get("manual_reason", "")
        link = f"[{url}]({url})" if url else "_no careers URL on file_"
        out.append(f"- **{t['name']}** — {link}")
        if reason:
            out.append(f"  - {reason}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", help="also write the markdown list to this path")
    args = ap.parse_args()
    md = render(manual_targets())
    print(md)
    if args.md:
        with open(args.md, "w") as fp:
            fp.write(md)
        print(f"[written to {args.md}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
