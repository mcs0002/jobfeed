"""Erste Group careers scraper (custom SAP BTP app, NOT SuccessFactors).

erstegroup.com/en/career/positions-offered is an AEM page whose job list is a
custom SAP BTP (Cloud Foundry) service. The listing endpoint + its Basic-auth
credential are published in the page itself, inside a `data-cid="joblist"`
gem-json config block (`apiConfiguration.url` + `.headers.Authorization`). We
read them from the page each run so a rotated credential doesn't strand us,
then GET the list (plain JSON, plain requests). ~151 roles, mostly Austrian
Sparkasse retail — leans on the tagger/filters downstream.
"""
import html
import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "job"


def _posted(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _read_api_config(page_html: str) -> dict:
    """Pull the joblist apiConfiguration (url + Authorization header) out of the
    page's gem-json config blocks."""
    soup = BeautifulSoup(page_html, "html.parser")
    for el in soup.find_all(attrs={"data-cid": True}):
        raw = el.string or el.get_text()
        if not raw or "apiConfiguration" not in raw:
            continue
        try:
            cfg = json.loads(html.unescape(raw))
        except ValueError:
            continue
        api = cfg.get("apiConfiguration")
        if isinstance(api, dict) and "list" in (api.get("url") or ""):
            return api
    raise RuntimeError("Erste: joblist apiConfiguration not found on config page")


def _detail_url(list_url: str, job_id: str) -> str:
    """The per-job detail endpoint sits on the same SAP-BTP host as the list,
    at /jobdetail. Same Basic-auth credential works (read from the page each
    run, so rotation-safe)."""
    parts = urlsplit(list_url)
    path = re.sub(r"/list\b", "/jobdetail", parts.path) or "/jobdetail"
    return urlunsplit((parts.scheme, parts.netloc, path,
                       f"language=en_US&jobID={job_id}", ""))


def _sections_to_text(sections) -> str:
    """Flatten the detail API's `sections` list ({name, data}) into plain text.
    `data` is either an HTML string or a list of bullet strings."""
    out = []
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        name = (sec.get("name") or "").strip()
        data = sec.get("data")
        if isinstance(data, list):
            text = "\n".join(str(x).strip() for x in data if x)
        else:
            text = BeautifulSoup(str(data or ""), "html.parser").get_text(
                "\n", strip=True)
        if text:
            out.append(f"{name}\n{text}" if name else text)
    return "\n\n".join(out)


def _fetch_description(session, headers, list_url: str, job_id: str) -> str:
    """Body for one Erste job, or "" on any failure (one bad job never sinks
    the scan; leaving it unset lets the nightly backstop retry)."""
    try:
        r = session.get(_detail_url(list_url, job_id), headers=headers, timeout=40)
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
    except Exception as e:
        print(f"ERSTE_DETAIL_FAIL {job_id} {type(e).__name__}: {e}")
        return ""
    if not rows or not isinstance(rows[0], dict):
        return ""
    return _sections_to_text(rows[0].get("sections"))


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "config_page": "https://www.erstegroup.com/en/career/positions-offered",
        "detail_base": "https://www.erstegroup.com/en/career/positions-offered/job-detail",
    }
    """
    config_page = config["config_page"]
    detail_base = config["detail_base"].rstrip("/")
    session = make_session()

    page = session.get(config_page, headers=HEADERS, timeout=40)
    page.raise_for_status()
    api = _read_api_config(page.text)

    headers = {**HEADERS, "Referer": "https://www.erstegroup.com/"}
    auth = (api.get("headers") or {}).get("Authorization")
    if auth:
        headers["Authorization"] = auth

    resp = session.get(api["url"], headers=headers, timeout=40)
    resp.raise_for_status()
    data = (resp.json() or {}).get("data", []) or []

    jobs = {}
    for rec in data:
        job_id = str(rec.get("id", "")).strip()
        title = (rec.get("external_title") or rec.get("job_title") or "").strip()
        if not job_id or not title:
            continue
        loc = rec.get("location")
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc if x)
        jobs[job_id] = {
            "id": f"erste_{job_id}",
            "title": title,
            "url": f"{detail_base}/{_slug(title)}/{job_id}",
            "location": loc or "",
            "posted": _posted(rec.get("posting_date", "")),
            # Body comes from the /jobdetail API (the AEM detail page is a JS
            # shell). Fetched inline with the same rotation-safe auth.
            "description": _fetch_description(session, headers, api["url"], job_id),
        }
    if not jobs:
        # The joblist API responded but yielded no usable records — either the
        # `data` envelope came back empty (stale/rotated auth, changed schema)
        # or no record carried an id+title. Erste always has ~150 roles and the
        # API exposes no separate count field to trust, so an empty parse is a
        # failure, not a genuinely empty board. Raise rather than delist.
        raise RuntimeError("erste_btp: no jobs parsed from joblist API")
    return list(jobs.values())
