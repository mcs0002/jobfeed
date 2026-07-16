"""Fetch UKG Pro Recruiting (UltiPro) job descriptions from the bot-rendered
OpportunityDetail page.

The listing scraper (`scrapers/ukg.py`) stores the search API's
``BriefDescription`` — a genuine teaser (~150-650 chars). The full posting body
is NOT exposed by any candidate-facing JSON endpoint: the JobBoardView search
returns only ``BriefDescription``, and the OpportunityDetail page is a React SPA
whose detail-load call lives in a runtime-only webpack chunk (no static route to
probe, ``LoadOpportunity`` and friends all 404).

The one plain-HTTP path: request the SAME OpportunityDetail URL with a Googlebot
User-Agent. UltiPro server-renders a crawler variant that inlines the full
record as a JS object literal::

    var opportunity = new US.Opportunity.CandidateOpportunityDetail({... ,
        "Description": "<html body>", ...});

We slice out that object literal, JSON-parse it, and extract the ``Description``
HTML. Public, no auth, no token — the requisition guid is already in the stored
URL. Confirmed 2026-07-16: Stephens 3071 chars, VanEck 7473 chars, vs. ~400 for
the stored teaser.
"""
import html
import json
import re

import requests

from .descriptions import _extract_text

# Googlebot UA is the gate: a normal browser UA gets the empty React shell;
# this triggers UltiPro's server-rendered crawler variant with the inlined
# opportunity record.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                   "+http://www.google.com/bot.html)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# UKG Pro recruiting hosts: <tenant>.rec.pro.ukg.net and recruiting*.ultipro.com,
# on an .../OpportunityDetail?opportunityId=... path.
_URL_RE = re.compile(
    r"^https?://[^/]+\.(?:rec\.pro\.ukg\.net|ultipro\.com)/.*OpportunityDetail",
    re.I)

# The inlined record: `new US.Opportunity.CandidateOpportunityDetail({ ... })`.
_OBJ_RE = re.compile(r"CandidateOpportunityDetail\(\s*(\{)")


def is_ukg(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _slice_object(text: str, brace_start: int) -> str:
    """Return the balanced {...} JSON object beginning at brace_start,
    respecting string literals so a `}` inside the HTML body doesn't end it."""
    depth = 0
    in_str = False
    esc = False
    for i in range(brace_start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start:i + 1]
    return ""


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one UKG OpportunityDetail URL, or "" on any
    failure (expired requisition, network error, parse error)."""
    if not is_ukg(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    m = _OBJ_RE.search(r.text)
    if not m:
        return ""
    obj = _slice_object(r.text, m.start(1))
    if not obj:
        return ""
    try:
        data = json.loads(obj)
    except ValueError:
        return ""
    body = data.get("Description") or ""
    return _extract_text(html.unescape(body))
