"""Duplicate postings: one role advertised more than once.

Two shapes reach the list (measured 2026-10-09 over the live rows outside
`other`): the same requisition posted on a firm's campus AND lateral board
(Workday adds a `-1`/`-2` suffix to the copy), and several requisitions with
the same title in the same city (one bank's "Client Associate" x10 in one
city, another's "Investment Advisor" x17). Both are grouped for
DISPLAY only: every row keeps its own status, applications and newness, and
nothing is deleted at scrape time, so a wrong grouping costs a click, never
state. Reposts (a role delisted and put back up under a new id) are linked at
insert time instead, by `JobDB.mark_seen` through `posting_key`.
"""
import re

from .db import plain_company

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text) -> str:
    return _NON_ALNUM.sub(" ", str(text or "").lower()).strip()


def posting_key(job: dict) -> tuple[str, str, str]:
    """Firm (board label stripped: "Bank (Students)" is Bank), title and
    place. The tagger's city wins over the raw location string, which two
    boards of one firm format differently."""
    return (plain_company(job.get("company")).lower(), _norm(job.get("title")),
            _norm(job.get("loc_city") or job.get("location")))


def _marked(job: dict) -> bool:
    return (job.get("status") or "new") != "new" or bool(job.get("favorite"))


def group_postings(jobs: list[dict]) -> list[dict]:
    """Collapse duplicates into one card at the position of the group's first
    (newest) member. The card shown is the one he has already marked, if any,
    so a status never hides behind an unmarked twin; the others ride along in
    `also`."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for job in jobs:
        key = posting_key(job)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(job)
    out = []
    for key in order:
        members = groups[key]
        lead = next((j for j in members if _marked(j)), members[0])
        lead = dict(lead)
        lead["also"] = [j for j in members if j["id"] != lead["id"]]
        out.append(lead)
    return out
