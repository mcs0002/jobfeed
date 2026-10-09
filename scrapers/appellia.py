"""Appellia / OMNI RMS careers board scraper (ASP.NET WebForms, plain HTTP).

Appellia boards ({tenant}.appellia.com) are ASP.NET WebForms apps whose vacancy
grid IS server-rendered — each row links ``positions/{Title-Slug}/?id={N}`` — but
paginates via `__doPostBack` (a Telerik RadGrid). We walk the pages sequentially:
each page's response carries the ``__VIEWSTATE``/``__EVENTVALIDATION`` needed to
POST for the next page. Title from the slug; the position detail pages are
server-rendered, so bodies fill via the generic enricher (HTTP strategy).
"""
import re

from bs4 import BeautifulSoup

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_ID = re.compile(r"[?&]id=(\d+)")
_MAX_PAGES = 25  # safety backstop


def _hidden(soup, name: str) -> str:
    el = soup.find("input", {"name": name})
    return el["value"] if el and el.has_attr("value") else ""


def _collect(soup, base: str, jobs: dict):
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("positions/"):
            continue
        m = _ID.search(href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in jobs:
            continue
        slug = href.split("/")[1]
        title = re.sub(r"\s+", " ", slug.replace("-", " ")).strip()
        jobs[job_id] = {
            "id": f"appellia_{job_id}",
            "title": title,
            "url": base.split("?")[0].rstrip("/") + "/" + href.lstrip("/"),
            "location": "",
            "description": "",
            "posted": "",
        }


def scrape(config: dict) -> list[dict]:
    """config = {"board_url": "https://uss.appellia.com/?ous=", "prefix": "uss"}"""
    board_url = config["board_url"]
    session = make_session()

    resp = session.get(board_url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = {}
    _collect(soup, board_url, jobs)
    if not any(a["href"].startswith("positions/") for a in soup.find_all("a", href=True)):
        raise RuntimeError(f"appellia: no position rows at {board_url}")

    page = 1
    while page < _MAX_PAGES:
        nxt = None
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True) == str(page + 1) and "__doPostBack" in a["href"]:
                nxt = a["href"]
                break
        if not nxt:
            break
        m = re.search(r"__doPostBack\('([^']+)','([^']*)'", nxt)
        if not m:
            break
        data = {
            "__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2),
            "__VIEWSTATE": _hidden(soup, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _hidden(soup, "__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": _hidden(soup, "__EVENTVALIDATION"),
        }
        resp = session.post(board_url, data=data, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        before = len(jobs)
        _collect(soup, board_url, jobs)
        if len(jobs) == before:  # no new rows -> stop
            break
        page += 1

    return list(jobs.values())
