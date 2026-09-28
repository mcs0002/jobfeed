"""ADP Workforce Now — public Career Center REST API scraper.

ADP WFN exposes each client's public board as open JSON (no auth, no JS):

    GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/
        staffing/v1/job-requisitions?cid={cid}&timeStamp={ms}
    -> {"jobRequisitions": [...], "meta": {"totalNumber": N}}

The only per-firm parameter is the ``cid`` (a GUID). It's derivable from a
client's public short-code: GET
``…/jobs/apply/posting.html?client={code}`` redirects to
``intermediateRedirect.html?cid={cid}``. The pattern generalises to any ADP WFN
client, so a single handler covers all of them.

Config (in targets.json under ``adp_careercenter``):
    cid    the client GUID
"""
import time

from ._http import make_session

BASE = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/"
        "events/staffing/v1/job-requisitions")
APPLY = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
         "recruitment.html?cid={cid}&jobId={job}")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _location(req: dict) -> str:
    parts = []
    for loc in req.get("requisitionLocations") or []:
        addr = loc.get("address") or {}
        city = (addr.get("cityName") or "").strip()
        if city:
            parts.append(city)
        else:
            short = (loc.get("nameCode") or {}).get("shortName", "").strip()
            if short:
                parts.append(short)
    return " / ".join(dict.fromkeys(parts))


def scrape(config: dict) -> list[dict]:
    cid = config["cid"]
    r = make_session().get(
        BASE, params={"cid": cid, "timeStamp": int(time.time() * 1000)},
        headers=HEADERS, timeout=30)
    r.raise_for_status()

    jobs = []
    for req in r.json().get("jobRequisitions") or []:
        item_id = str(req.get("itemID") or "")
        title = req.get("requisitionTitle") or ""
        if not item_id or not title:
            continue
        client_req = req.get("clientRequisitionID") or item_id
        jobs.append({
            "id": f"adp_{item_id}",
            "title": title,
            "url": APPLY.format(cid=cid, job=client_req),
            "location": _location(req),
            "posted": str(req.get("postDate") or "")[:10],
        })
    return jobs
