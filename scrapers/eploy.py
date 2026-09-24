"""Eploy (ASP.NET WebForms) careers board scraper.

Eploy boards are server-rendered HTML — each role is a ``div.vsr-job-big`` block
with an ``<h2>`` title and a ``vacancy-apply.aspx?VacancyID=N`` link. There is no
JSON/RSS feed. Pagination is a classic WebForms ``__doPostBack`` pager, so pages
2+ require posting the page's ``__VIEWSTATE``/``__EVENTVALIDATION`` back with the
pager control as ``__EVENTTARGET`` — done over plain HTTP, no browser.

Two host quirks (seen on OC&C): the cert chain is incomplete, so we fetch with
``curl_cffi`` (Chrome impersonation) and ``verify=False``; that also clears any
JA3 gate. Config (in targets.json under ``eploy``):
    base_url      e.g. "https://careers.occstrategy.com"
    search_path   e.g. "/vacancies/vacancy-search-results.aspx"
    pager         optional __EVENTTARGET of the pager control
                  (default ctl00$ContentContainer$ctl01$VacancyPager)
"""
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
DEFAULT_PAGER = "ctl00$ContentContainer$ctl01$VacancyPager"
MAX_PAGES = 25


def _hidden(soup, name: str) -> str:
    el = soup.find("input", {"name": name})
    return el.get("value", "") if el else ""


def _parse(soup, base_url: str, jobs: dict) -> int:
    new = 0
    for block in soup.select("div.vsr-job-big"):
        link = block.select_one('a[href*="VacancyID"]')
        head = block.find(["h1", "h2", "h3", "h4"])
        if not link or not head:
            continue
        m = re.search(r"VacancyID=(\d+)", link.get("href", ""))
        if not m:
            continue
        vid = m.group(1)
        if vid in jobs:
            continue
        # Location: the job body repeats "Location:  <city>" after the metadata
        # labels; grab the value-bearing occurrence, skipping the "Please Select"
        # filter label.
        location = ""
        for lm in re.finditer(r"Location:\s+([A-Za-z][^\n]{0,40})", block.get_text("\n")):
            cand = lm.group(1).strip()
            if cand and "please select" not in cand.lower():
                location = cand.split("  ")[0].strip()
                break
        jobs[vid] = {
            "id": f"eploy_{vid}",
            "title": head.get_text(" ", strip=True),
            # The details page is the real posting (body + apply link); the
            # apply URL 302s to a login gate, so storing it broke both the
            # description enrichment and the "Open posting" link.
            "url": f"{base_url}/vacancies/vacancy-details.aspx?VacancyID={vid}",
            "location": location,
            "posted": "",
        }
        new += 1
    return new


def _fetch_description(session, url: str) -> str:
    """Body from an Eploy vacancy-details page. Empty on any failure. The whole
    posting lives in a single ASP.NET <form>, which the generic _extract_text
    skips as chrome — so target the description container directly."""
    try:
        r = session.get(url, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"EPLOY_DETAIL_FAIL {url} {type(e).__name__}: {e}")
        return ""
    body = BeautifulSoup(r.text, "html.parser").select_one(
        "div.vac-details__description")
    return body.get_text("\n", strip=True) if body else ""


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    url = base_url + config.get("search_path", "/vacancies/vacancy-search-results.aspx")
    pager = config.get("pager", DEFAULT_PAGER)

    session = creq.Session(impersonate="chrome120", verify=False)
    session.headers.update(HEADERS)

    r = session.get(url, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs: dict[str, dict] = {}
    _parse(soup, base_url, jobs)

    # Eploy exposes no server-side total count — no band check is possible;
    # we rely on MAX_PAGES exhaustion raising and on per-page retry below.
    # Follow the WebForms pager until a page yields nothing new (or the cap).
    for page in range(2, MAX_PAGES + 1):
        data = {
            "__EVENTTARGET": pager,
            "__EVENTARGUMENT": str(page),
            "__VIEWSTATE": _hidden(soup, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _hidden(soup, "__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": _hidden(soup, "__EVENTVALIDATION"),
        }
        first_exc: Exception | None = None
        try:
            r = session.post(url, data=data, timeout=40)
            r.raise_for_status()
        except Exception as e:
            first_exc = e

        if first_exc is not None:
            # Retry once before giving up — transient network blip.
            try:
                r = session.post(url, data=data, timeout=40)
                r.raise_for_status()
            except Exception as retry_exc:
                raise RuntimeError(
                    f"Eploy pager failed on page {page} after retry"
                ) from retry_exc

        soup = BeautifulSoup(r.text, "html.parser")
        if _parse(soup, base_url, jobs) == 0:
            break
    else:
        raise RuntimeError(
            f"Eploy pagination exceeded MAX_PAGES={MAX_PAGES}; "
            "board may be larger than expected or pager is stuck"
        )

    # Capture the body inline (small boards, ~15-50 roles → a handful of extra
    # GETs on the session that's already open). Keeps coverage.SCRAPER honest.
    for job in jobs.values():
        job["description"] = _fetch_description(session, job["url"])

    return list(jobs.values())
