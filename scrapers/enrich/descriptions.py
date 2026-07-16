#!/usr/bin/env python3
"""Fetch missing job descriptions for rows already in seen_jobs.

The daily scrape stays fast by skipping per-job detail fetches. This script
walks the DB later, picks rows where description IS NULL, GETs each job
URL once, extracts the visible text, and stores it. Throttled (1s default)
to be polite to ATS hosts. Runs idempotently — already-enriched rows are
skipped.

Default behavior:
  - cap at 200 rows per run (~3-4 minutes at 1 req/sec)
  - skip rows older than 60 days (rolling window — see --max-age-days)

Usage:
  ./enrich_descriptions.py                       # default: 200 rows, 60d window
  ./enrich_descriptions.py --limit 50            # smaller batch
  ./enrich_descriptions.py --max-age-days 30     # tighter window
  ./enrich_descriptions.py --prune-only          # only NULL out descriptions
                                                 #   on rows past the window
  ./enrich_descriptions.py --dry-run             # report what would happen
"""
import argparse
import html
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db import JobDB  # noqa: E402

DEFAULT_DB = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
# Browser-realistic header set. A bare UA + Accept gets a 403 "Access Denied"
# from Akamai-fronted sites (BNP Paribas group.bnpparibas), the same bot wall
# the paced scraper had to clear. A current UA plus Accept-Language/Sec-Fetch
# headers passes the fingerprint, so the generic GET path reaches those pages
# without needing headless rendering.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


# Regions whose text is page chrome, not the job description — dropped wholesale
# so the stored text isn't a wall of nav menus, cookie banners and footers.
_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer",
              "form", "aside", "svg", "button", "select"}
# Block-level tags whose close marks a paragraph/line boundary — we emit a
# newline so the extracted text keeps its structure instead of collapsing into
# one unreadable run-on line.
_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "tr", "table", "section",
               "article", "h1", "h2", "h3", "h4", "h5", "h6"}


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0  # depth counter for skipped (non-content) regions

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


_INLINE_WS_RE = re.compile(r"[ \t]+")
_PAD_NL_RE = re.compile(r" *\n *")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _extract_text(html: str, max_chars: int = 16000) -> str:
    """Best-effort plain-text from an HTML page. Skips page chrome (nav/footer/
    forms) and preserves paragraph breaks so the result reads like the posting,
    not a wall of run-on text. Truncated at max_chars so a pathological
    multi-megabyte page can't blow up the DB."""
    stripper = _Stripper()
    try:
        stripper.feed(html)
    except Exception:
        return ""
    text = " ".join(stripper.parts)
    text = _INLINE_WS_RE.sub(" ", text)      # collapse spaces/tabs, keep newlines
    text = _PAD_NL_RE.sub("\n", text)        # trim spaces around line breaks
    text = _MULTI_NL_RE.sub("\n\n", text)    # cap blank runs at one empty line
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " ..."
    return text


_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Client-side-rendering markers (Angular/React/Next/Vue mount points). Used by
# enrich_one's junk guard below.
_SPA_SHELL_RE = re.compile(
    r'ng-app=|data-reactroot|__NEXT_DATA__|<div[^>]+id=["\'](?:root|app)["\']',
    re.IGNORECASE,
)

# Outage/maintenance-banner phrases (case-insensitive). Confirmed 2026-07-08:
# UniCredit CIB's Avature board served a "the system is currently undergoing
# maintenance" page with a plain 200 status during the 04:00 scan window.
# Avature has no dedicated detail enricher, so those rows fall through to
# enrich_one same as any generic server-rendered ATS; the stripped banner text
# passed every guard that existed at the time (it isn't an SPA shell — it's
# real server-rendered HTML) and got stored as the job description. Since the
# nightly query is `description IS NULL`, that row is never revisited again —
# same permanent-junk failure class as the ~1815 Glencore rows before that ATS
# got its own JSON-LD enricher. Match the outage PHRASE, not the bare word
# "maintenance" — a real JD that mentions "scheduled maintenance" of some
# system in passing must still be stored.
OUTAGE_PHRASES_RE = re.compile(
    r"the system is currently undergoing"
    r"|currently undergoing maintenance"
    r"|scheduled maintenance"
    r"|temporarily unavailable"
    r"|service unavailable"
    r"|down for maintenance"
    r"|wartungsarbeiten",
    re.IGNORECASE,
)

# An outage banner is a sentence or two; even a terse real JD runs into the
# low thousands of characters once title/location/body text is stripped and
# joined. 1500 chars is the same order of magnitude as the SPA-shell
# threshold above and gives real postings — including ones that happen to
# namedrop "scheduled maintenance" — comfortable headroom, while a bare outage
# banner (typically well under 500 chars) never clears it.
_OUTAGE_MAX_CHARS = 1500


def _reject_if_outage(text: str, url: str) -> bool:
    """True if text looks like an outage/maintenance banner rather than a real
    job posting (short body + an outage phrase), in which case it prints a
    named line so repeated maintenance windows on the same source are visible
    in the log. Callers must treat a True return exactly like a failed
    fetch — return "" so the row's description stays NULL and is retried the
    next run, instead of being permanently marked enriched."""
    if not text or len(text) >= _OUTAGE_MAX_CHARS or not OUTAGE_PHRASES_RE.search(text):
        return False
    print(f"  MAINTENANCE guard: {urlsplit(url).netloc} served an outage/"
          f"maintenance page instead of a job description — treating as a "
          f"failed fetch")
    return True


# ATS id-prefixes the nightly backstop must SKIP: their public pages are JS
# shells with no detail enricher yet, so every attempt returns "" and the rows
# re-enter the queue each night. Observed 2026-07-02: ~800 beesite (Deutsche
# Bank) rows starved the 400-row budget down to 10 productive fetches. Remove
# a prefix from this list when its enricher lands.
UNENRICHABLE_PREFIXES = ("beesite_",)


def _jobposting_description(page_html: str) -> str:
    """Extract a JobPosting's `description` field from schema.org JSON-LD, if
    present. Several ATS platforms (Radancy confirmed 2026-07-01, likely
    others) inject the full job text here for SEO while the visible DOM only
    fills in after client-side JS runs — the generic DOM stripper below skips
    <script> tags entirely (by design, to avoid pulling in JS code) and
    silently returns nothing for these pages even on a clean 200.

    This is the ONE JSON-LD parser — talentbrew/glencore/jibe delegate here.
    It carries every quirk-fix that used to live in a per-enricher copy:
    top-level arrays, ``@graph`` wrappers (Radancy/TalentBrew), ``@type`` as a
    list (TalentBrew), and ``strict=False`` for raw control chars in the
    string (Glencore)."""
    for block in _JSONLD_RE.findall(page_html):
        try:
            data = json.loads(block, strict=False)
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            for node in graph if isinstance(graph, list) else [item]:
                if not isinstance(node, dict) or not node.get("description"):
                    continue
                t = node.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return _extract_text(html.unescape(node["description"]))
    return ""


def enrich_one(url: str, session: requests.Session, timeout: int = 15) -> str:
    """GET one URL and extract its text. Returns "" on any failure — we want
    the enrichment pass to keep going even if individual URLs error.

    Note: Workday URLs are NOT enrichable this way (the public page is an
    empty JS shell); they go through workday_enrich.WorkdayEnricher, which has
    the tenant/board config and a primed session. Callers route Workday rows
    there separately."""
    if not url:
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        text = _jobposting_description(r.text)
        if text:
            if _reject_if_outage(text, url):
                return ""
            return text
        text = _extract_text(r.text)
        # JS-shell guard: when the page is a client-rendered shell and JSON-LD
        # yielded nothing, the stripped DOM is nav/cookie chrome, not the
        # posting. Persisting it would permanently mark the row enriched (the
        # nightly query is `description IS NULL`) — the mechanism behind the
        # ~1815 junk Glencore rows before that ATS got its own enricher. Short
        # text + shell marker => report unenrichable instead; genuinely
        # server-rendered pages produce long text and pass through.
        if text and len(text) < 1500 and _SPA_SHELL_RE.search(r.text):
            return ""
        # Maintenance/outage guard: same idea, different signature — the page
        # is server-rendered (not an SPA shell) but its body is an outage
        # banner, not a posting. See OUTAGE_PHRASES_RE above for the incident.
        if _reject_if_outage(text, url):
            return ""
        return text
    except requests.RequestException:
        return ""


def _load_workday_cfgs() -> dict:
    """Map company name -> its Workday config (tenant/board/applied_facets),
    read from targets.json, so the backfill can route Workday rows to the cxs
    API. Empty dict if targets.json is missing/unreadable."""
    import json
    path = os.path.join(ROOT, "targets.json")
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return {}
    arr = data if isinstance(data, list) else data.get("targets", [])
    return {c["name"]: c["workday"] for c in arr
            if c.get("ats") == "workday" and c.get("workday")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="max rows to enrich this run (default 200)")
    parser.add_argument("--max-age-days", type=int, default=60,
                        help="rolling window: only enrich rows newer than this "
                             "(default 60). Use 0 to disable the window.")
    parser.add_argument("--throttle", type=float, default=1.0,
                        help="seconds between requests (default 1.0)")
    parser.add_argument("--prune-only", action="store_true",
                        help="NULL out descriptions on rows past the window "
                             "and exit (no new fetches)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without writing")
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    db = JobDB(args.db)
    window = None if args.max_age_days == 0 else args.max_age_days

    if window:
        if args.dry_run:
            print(f"[dry-run] would prune descriptions older than {window} days")
        else:
            pruned = db.prune_old_descriptions(window)
            print(f"pruned {pruned} aged-out descriptions (>{window}d old)")

    if args.prune_only:
        return 0

    rows = db.jobs_missing_description(limit=args.limit, max_age_days=window,
                                       exclude_prefixes=UNENRICHABLE_PREFIXES)
    if not rows:
        print("no rows need enrichment")
        return 0

    print(f"enriching up to {len(rows)} rows (throttle={args.throttle}s)...")
    session = requests.Session()
    from .workday_enrich import WorkdayEnricher, is_workday
    from . import oracle_enrich
    from . import workable_enrich
    from . import goldman_enrich
    from . import csod_enrich
    from . import balyasny_enrich
    from . import talentbrew_enrich
    # THE detail-enricher registry lives in the package __init__ (single source
    # of truth; the old copy in main.py had drifted once). Lazy import to avoid
    # a package-init cycle.
    from . import DETAIL_ENRICHERS as detail_enrichers
    wd_cfgs = _load_workday_cfgs()
    wd_enricher = WorkdayEnricher()
    csod_enricher = csod_enrich.CsodEnricher()
    balyasny_enricher = balyasny_enrich.BalyasnyEnricher()
    enriched = 0
    skipped = 0
    def _route(row) -> str:
        if is_workday(row["url"]):
            cfg = wd_cfgs.get(row["company"]) or {}
            # .get, not [] — one malformed workday entry in targets.json must
            # not abort the batch.
            if not (cfg.get("tenant") and cfg.get("board")):
                return ""
            return wd_enricher.description(
                row["url"], cfg["tenant"], cfg["board"], cfg.get("applied_facets"))
        if oracle_enrich.is_oracle(row["url"]):
            return oracle_enrich.description(row["url"], session)
        if workable_enrich.is_workable(row["url"]):
            return workable_enrich.description(row["url"], session)
        if goldman_enrich.is_goldman(row["url"]):
            return goldman_enrich.description(row["url"], session)
        if csod_enrich.is_csod(row["url"]):
            return csod_enricher.description(row["url"])
        if balyasny_enrich.is_balyasny(row["url"]):
            return balyasny_enricher.description(row["url"])
        if talentbrew_enrich.is_talentbrew(row["id"]):
            # By id prefix, BEFORE the detail matchers — TalentBrew URLs collide
            # with the SuccessFactors matcher (see talentbrew_enrich docstring).
            return talentbrew_enrich.description(row["url"], session)
        if (detail_fn := next((d for is_fn, d in detail_enrichers
                               if is_fn(row["url"])), None)):
            return detail_fn(row["url"], session)
        return enrich_one(row["url"], session)

    for i, row in enumerate(rows, 1):
        try:
            text = _route(row)
        except Exception as exc:
            # Per-row guard, mirroring main.py's enrichment loops: enricher
            # internals only catch (RequestException, ValueError), so an
            # unexpected payload shape (WAF error array, gateway JSON) raises
            # — that must cost one row, not the rest of the nightly batch.
            text = ""
            print(f"  [{i}/{len(rows)}] ERR  {row['company']}: "
                  f"{type(exc).__name__}: {exc}")
        if not text:
            skipped += 1
            print(f"  [{i}/{len(rows)}] SKIP {row['company']}: {row['title'][:50]}")
        else:
            if not args.dry_run:
                db.set_description(row["id"], text)
            enriched += 1
            preview = text[:60].replace("\n", " ")
            print(f"  [{i}/{len(rows)}] OK   {row['company']}: {row['title'][:40]} — {preview}...")
        if i < len(rows):
            time.sleep(args.throttle)

    print(f"\nenriched {enriched} | skipped {skipped} | total {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
