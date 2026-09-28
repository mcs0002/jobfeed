"""PFA (Danish pension) careers scraper — bespoke over a SuccessFactors board.

PFA's SF-classic tenant (career2.successfactors.eu, company=pfapensionP) renders
the JS `rcmjobsearch` shell (0 anchors), but PFA's OWN page
``pfa.dk/om-pfa/job-i-pfa/ledige-job/`` server-renders the openings as
``career?...career_job_req_id=N`` links. The list card has no title, so we fetch
each job_listing page for its ``<title>`` (small board — 3 roles). Danish, UTF-8.
"""
import re

from ._http import make_session, fix_encoding

LIST_URL = "https://www.pfa.dk/om-pfa/job-i-pfa/ledige-job/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_REQ = re.compile(r"career_job_req_id=(\d+)")
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)


def scrape(config: dict | None = None) -> list[dict]:
    session = make_session()
    resp = session.get(LIST_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    fix_encoding(resp)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    # dedup by req_id (each appears twice: desktop + mobile)
    req_urls = {}
    for a in soup.find_all("a", href=True):
        m = _REQ.search(a["href"])
        if not m:
            continue
        href = a["href"].replace("&amp;", "&")
        if not href.startswith("http"):
            href = "https://career2.successfactors.eu" + (href if href.startswith("/") else "/" + href)
        req_urls.setdefault(m.group(1), href)

    jobs = {}
    for req_id, url in req_urls.items():
        try:
            jp = session.get(url, headers=HEADERS, timeout=30)
            jp.raise_for_status()
            fix_encoding(jp)
            raw = _TITLE.search(jp.text)
            title = raw.group(1).strip() if raw else ""
            title = title.replace("Karrieremuligheder:", "", 1).strip()
            title = re.sub(r"\s*\(\d+\)\s*$", "", title).strip()
        except Exception:
            title = ""
        if not title:
            continue
        jobs[req_id] = {
            "id": f"pfa_{req_id}",
            "title": title,
            "url": url,
            "location": "",
            "description": "",
            "posted": "",
        }
    if not jobs:
        # No career_job_req_id anchors on the (fetched) list page, or every
        # detail title-fetch failed. Either way the parse yielded nothing from a
        # page we did reach — raise so the delister doesn't purge PFA.
        raise RuntimeError("pfa: no jobs parsed from list page")
    return list(jobs.values())
