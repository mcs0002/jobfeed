"""SAP SuccessFactors RCM career site (DWR variant).

Some SF career-site tenants (e.g. Pictet `banquepict`, Sumitomo EMEA
`S004690996D`) run the modern **RCM job search** widget instead of the legacy
server-rendered listing. The job list is NOT in the page HTML and NOT a clean
REST API — it loads via a **DWR** (Direct Web Remoting) RPC:

    POST {base}/xi/ajax/remoting/call/plaincall/
         careerJobSearchControllerProxy.getInitialJobSearchData.dwr

The classic (`successfactors_classic`) and RMK-API (`successfactors_api`)
handlers both get 0 on these tenants. This handler replicates the DWR call over
plain HTTP: it pulls the per-session CSRF token (`_s.crb`) off the landing page,
sends it as the `x-csrf-token` / `x-ajax-token` headers (the piece a naive fetch
misses), and parses the DWR-encoded reply.

The reply is JavaScript assignments — `sN.title="…"; sN.id=124654;
sN.postingDate="03/07/2026";` plus a `detailURLPrefix`. We group assignments by
object and keep the ones that carry a title.

Pagination: the widget's `search` DWR method takes a pagination object
(`{currentPage, startRow, endRow, pageSize, totalCount, ...}`) plus sort options,
so one `search` call with a large `pageSize` returns the whole board in a single
request (verified: Pictet's 42 come back at once). `getInitialJobSearchData`
(first page only, 10 rows) is not used — `search` supersedes it. We still assert
we got every posting vs the reply's `postingCount`, and raise if short so a
partial pull can never masquerade as a complete board.
"""
import re
from datetime import datetime

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
DWR_PATH = ("/xi/ajax/remoting/call/plaincall/"
            "careerJobSearchControllerProxy.search.dwr")
# One call with a big page size returns the whole board; larger than any real
# finance careers site. If a board ever exceeds this we detect it (count <
# postingCount) and raise rather than silently truncate.
PAGE_SIZE = 500


def _token(session, base_url, company):
    """Pull the per-session _s.crb CSRF token off the landing page."""
    land = (f"{base_url}/career?company={company}"
            "&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH")
    resp = session.get(land, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    # The page carries the token both singly (%2f/%3d) and doubly (%252f)
    # encoded; we want the singly-encoded form for the header + body.
    toks = [t for t in re.findall(r'_s\.crb=([^&"\'\\ ]+)', resp.text)
            if "%252f" not in t]
    if not toks:
        raise RuntimeError(f"successfactors_dwr: no _s.crb token for {company}")
    return toks[-1], land


def _posted(value):
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _parse(text, base_url):
    """Turn the DWR reply into job dicts.

    Assignments look like ``s31.title="…";`` / ``s31.id=124654;``. Group by the
    ``sN`` object, keep those with a title.
    """
    prefix_m = re.search(r'detailURLPrefix="([^"]+)"', text)
    prefix = prefix_m.group(1).replace("\\/", "/") if prefix_m else ""
    prefix = prefix.replace("%5f", "_").replace("%5F", "_")

    objs = {}
    for obj, field, _quote, val in re.findall(
            r'(s\d+)\.([A-Za-z0-9_]+)=("?)([^";]*)\3;', text):
        objs.setdefault(obj, {})[field] = val

    jobs = {}
    for obj, fields in objs.items():
        title = fields.get("title")
        job_id = fields.get("id")
        if not title or not job_id or not job_id.isdigit():
            continue
        title = title.replace("\\/", "/").strip()
        url = f"{base_url}{prefix}{job_id}" if prefix else (
            f"{base_url}/career?career_ns=job_listing&career_job_req_id={job_id}")
        jobs[job_id] = {
            "id": f"sfdwr_{job_id}",
            "title": title,
            "url": url,
            "location": "",   # not present in the list payload; tagger infers
            "posted": _posted(fields.get("postingDate", "").replace("\\/", "/")),
        }
    return jobs, prefix_m is not None


def scrape(config):
    """
    config = {"base_url": "https://career012.successfactors.eu",
              "company": "banquepict"}
    """
    base_url = config["base_url"].rstrip("/")
    company = config["company"]
    session = make_session()
    token, referer = _token(session, base_url, company)

    # `search` with a big page size = whole board in one call. The pagination
    # object field names (currentPage/endRow/pageSize/startRow/totalCount) and
    # the {pagination, sortByColumn, sortOrder} param0 shape mirror the widget's
    # own request.
    body = "\n".join([
        "callCount=1",
        f"page=/career?company={company}"
        f"&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH&_s.crb={token}",
        "httpSessionId=",
        "scriptSessionId=80A8BD291A8E635A37D57F13E5D1F423590",
        "c0-scriptName=careerJobSearchControllerProxy",
        "c0-methodName=search",
        "c0-id=0",
        "c0-e2=number:1",                       # currentPage
        f"c0-e3=number:{PAGE_SIZE}",            # endRow
        "c0-e4=boolean:false",                  # increaseCandSummaryPagination
        f"c0-e5=number:{PAGE_SIZE}",            # pageSize
        "c0-e6=number:1",                       # startRow
        "c0-e7=number:0",                       # totalCount (server recomputes)
        "c0-e1=Object_Object:{currentPage:reference:c0-e2, "
        "endRow:reference:c0-e3, increaseCandSummaryPagination:reference:c0-e4, "
        "pageSize:reference:c0-e5, startRow:reference:c0-e6, "
        "totalCount:reference:c0-e7}",
        "c0-e8=string:JOB_POSTING_DATE",        # sortByColumn
        "c0-e9=string:DESC",                    # sortOrder
        "c0-param0=Object_Object:{pagination:reference:c0-e1, "
        "sortByColumn:reference:c0-e8, sortOrder:reference:c0-e9}",
        "batchId=0",
        "",
    ])
    headers = {
        **HEADERS,
        "Content-Type": "text/plain",
        "Origin": base_url,
        "Referer": referer,
        "x-csrf-token": token,
        "x-ajax-token": token,
        "x-subaction": "1",
        "viewid": "/ui/rcmcareer/pages/careersite/career.jsp.xhtml",
        "x-sap-page-info": f"companyId={company}",
    }
    resp = session.post(base_url + DWR_PATH, data=body, headers=headers,
                        timeout=40)
    resp.raise_for_status()
    if "DWR" not in resp.text and "handleCallback" not in resp.text:
        raise RuntimeError(
            f"successfactors_dwr: unexpected reply for {company} "
            f"({len(resp.text)} chars, no DWR payload)")

    jobs, ok = _parse(resp.text, base_url)
    if not ok:
        raise RuntimeError(
            f"successfactors_dwr: no detailURLPrefix in reply for {company} "
            "(DWR shape changed?)")

    total_m = re.search(r'postingCount="(\d+)"', resp.text)
    total = int(total_m.group(1)) if total_m else len(jobs)
    if total > len(jobs):
        # Only possible if a board exceeds PAGE_SIZE. Fail loud rather than ship
        # a silently-truncated board (the delist/purge logic trusts the count).
        raise RuntimeError(
            f"successfactors_dwr: {company} returned {len(jobs)} of {total} "
            f"postings — board exceeds PAGE_SIZE={PAGE_SIZE}, add real paging")
    return list(jobs.values())
