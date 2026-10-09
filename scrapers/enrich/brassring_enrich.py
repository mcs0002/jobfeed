"""Fetch IBM/Kenexa BrassRing (TalentGateway) job descriptions via the AJAX API.

The BrassRing career site (jobs.ubs.com and client-hosted / *.brassring.com
TalentGateway tenants) is an Angular JS shell: the JobDetails page GET returns
~1.3 MB of chrome with the posting body NULL. The body is fetched client-side by
a single XHR:

    POST {host}/TgNewUI/Search/Ajax/JobDetails
        body (JSON): {partnerId, siteId, jobid, jobSiteId, configMode, turnOffHttps}
    -> {"ServiceResponse": {"Jobdetails": {"JobDetailQuestions": [...]}}}

`jobid` is the requisition id (== the `jobid` query param == listing `reqid`);
`jobSiteId` == the `siteid` query param. The endpoint is session-stateful: it
only returns a body for requisitions present in the session's current search
result set, so a plain cold POST returns Jobdetails=null. We prime the session
with one GET of the tenant's searchResults page (which populates the server-side
session XML) before posting. Cookie-only, no auth.

Closed/expired requisitions are NOT in the active search and legitimately return
null -> "" (this is the bulk of the ~650 stored brassring_ rows: the postings
were captured live, then the reqs closed, exactly as the listing scraper warns).

The posting body lives across several JobDetailQuestions, keyed by VerityZone.
`jobdescription` plus role-specific `formtext*` fields are the role itself; the
firm boilerplate ("Join us" / "About us" / disclaimers / contact) is dropped.
"""
import re
from urllib.parse import parse_qs, urlsplit

import requests

from .descriptions import _extract_text

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# Host must look like a BrassRing TalentGateway, and the URL must be a job
# detail page (PageType=JobDetails, case-insensitive) carrying partner/site/job.
_HOST_RE = re.compile(r"(^|\.)(jobs\.ubs\.com|sjobs\.brassring\.com|"
                      r"[a-z0-9-]+\.brassring\.com)$", re.I)

# VerityZones whose AnswerValue is firm boilerplate, not the role — dropped so
# the stored text is the posting, not an "about us" wall repeated across rows.
_BOILERPLATE_ZONES = {
    "formtext37",  # "Join us"
    "formtext60",  # "About us"
    "formtext42",  # "Disclaimer / Policy statements"
    "formtext43",  # "Contact Details"
    "formtext79",  # "Your Career Comeback" (returner boilerplate)
}
# Non-content metadata zones (ids/flags/coords) — never part of the body.
_META_ZONES = {
    "reqid", "hotjob", "siteid", "gqid", "jobreqlanguage", "latitude",
    "longitude", "lastupdated", "autoreq", "jobtitle",
}
_SKIP_ZONES = _BOILERPLATE_ZONES | _META_ZONES


def _host(url: str) -> str:
    return (urlsplit(url or "").netloc or "").split(":")[0].lower()


def is_brassring(url: str) -> bool:
    """True for a BrassRing TalentGateway job-detail URL we can enrich."""
    if not url:
        return False
    parts = urlsplit(url)
    if not _HOST_RE.search(_host(url)):
        return False
    q = parse_qs(parts.query)
    page_type = (q.get("PageType") or q.get("pagetype") or [""])[0].lower()
    if page_type != "jobdetails":
        return False
    return bool(_qparam(q, "jobid") and _qparam(q, "partnerid")
                and _qparam(q, "siteid"))


def _qparam(q: dict, name: str) -> str:
    """Case-insensitive query-param lookup (BrassRing mixes PageType casing)."""
    for k, v in q.items():
        if k.lower() == name.lower():
            return (v or [""])[0]
    return ""


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one BrassRing job URL, or "" on any failure
    (closed/expired requisition, network error, parse error).

    The endpoint is session-stateful, so we always use a private session primed
    with the tenant's search page — any caller-supplied session is ignored for
    correctness (a shared session may carry an unrelated/empty search state)."""
    parts = urlsplit(url or "")
    if not is_brassring(url):
        return ""
    q = parse_qs(parts.query)
    partner_id = _qparam(q, "partnerid")
    site_id = _qparam(q, "siteid")
    job_id = _qparam(q, "jobid")
    base = f"{parts.scheme}://{parts.netloc}"

    s = requests.Session()
    try:
        # Prime: the GET of the searchResults page populates the server-side
        # session XML the JobDetails endpoint reads from. Without it the POST
        # returns Jobdetails=null even for live reqs.
        s.get(
            f"{base}/TgNewUI/Search/Home/HomeWithPreLoad",
            params={"partnerid": partner_id, "siteid": site_id,
                    "PageType": "searchResults"},
            headers=_HEADERS, timeout=timeout,
        )
        r = s.post(
            f"{base}/TgNewUI/Search/Ajax/JobDetails",
            json={
                "partnerId": partner_id,
                "siteId": site_id,
                "jobid": job_id,
                "jobSiteId": site_id,
                "configMode": "",
                "turnOffHttps": False,
            },
            headers={**_HEADERS, "Referer": url},
            timeout=timeout,
        )
        if r.status_code >= 400:
            return ""
        jd = (r.json().get("ServiceResponse") or {}).get("Jobdetails")
    except (requests.RequestException, ValueError):
        return ""
    finally:
        s.close()

    if not jd:
        return ""  # closed/expired requisition, or not in active search
    questions = jd.get("JobDetailQuestions") or []
    parts_out: list[str] = []
    for item in questions:
        zone = (item.get("VerityZone") or "").lower()
        if zone in _SKIP_ZONES:
            continue
        val = (item.get("AnswerValue") or "").strip()
        if not val:
            continue
        label = (item.get("QuestionName") or "").strip()
        # Keep the field label (e.g. "Your role", "Your expertise") as a header
        # so the extracted text keeps the posting's section structure.
        parts_out.append(f"<h3>{label}</h3>{val}" if label else val)
    return _extract_text("\n".join(parts_out))


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        print("=" * 70)
        print(u)
        txt = description(u)
        print(f"[len={len(txt)}]")
        print(txt[:400] if txt else "(empty)")
