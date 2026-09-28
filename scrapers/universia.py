"""Universia scraper using their public job-posting API.

Universia is Santander's own graduate board, and it is where the Santander
Future Talents programmes are actually published. Santander's `SantanderCareers`
Workday tenant does not carry them, so the graduate programmes were invisible to
this scraper until 2026-09-16, when one turned up on the school's JobTeaser board and
the site's own search redirected to universia.net.

The HTML site is an Angular shell that returns ~4.6 KB with no roles in it, but
the API it calls needs no auth and answers plain `requests`:

    GET https://api-manager.universia.net/orientacion-job-posting/v1/api/
        job-posting?boards=<board>&hiringOrganization=<name>&offset=&limit=

returning schema.org `JobPosting` objects with the description inline, so no
enrichment pass is needed. The board is shared by 230 organisations, most of
them Spanish local employers, so a target names ONE organisation rather than
pulling the whole board: `hiringOrganization` matches the organisation's legal
name exactly ("Santander Early Talent" for Future Talents).
"""
from .enrich.descriptions import _extract_text
from ._http import make_session

API = ("https://api-manager.universia.net/orientacion-job-posting/v1/api/"
       "job-posting")
# The public board every universia.net visitor sees. Kept here rather than in
# targets.json: it identifies the site, not the firm.
PUBLIC_BOARD = "00000000-0000-0000-0000-000000000001"
PAGE = 100
MAX_PAGES = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def _location(posting: dict) -> str:
    """The posting's city and country, from schema.org `jobLocation`.

    `jobLocation` is sometimes one object and sometimes a list of them; a
    programme that recruits into several cities carries several."""
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    parts = []
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        where = ", ".join(
            str(address.get(key)).strip()
            for key in ("addressLocality", "addressCountry")
            if isinstance(address.get(key), str) and address.get(key).strip()
        )
        if where and where not in parts:
            parts.append(where)
    return " | ".join(parts)


def _body(posting: dict) -> str:
    """Description plus requirements. The requirements block is a separate HTML
    field and it is where the language, degree and start-date signals live, so
    dropping it would cost the tagger exactly what it needs."""
    html = " ".join(
        str(posting.get(key) or "") for key in ("description", "requirements")
    )
    return _extract_text(html)


def scrape(organization: str, board: str = PUBLIC_BOARD) -> list[dict]:
    session = make_session()
    jobs: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        response = session.get(
            API,
            params={
                "boards": board,
                "hiringOrganization": organization,
                "postingType": ["job", "internship"],
                "dateFrom": "",
                "filterAddressCountry": "false",
                "offset": offset,
                "limit": PAGE,
            },
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results") or []
        for posting in results:
            identifier = str(posting.get("identifier") or "").strip()
            if not identifier:
                continue
            jobs.append({
                "id": f"universia_{identifier}",
                "title": str(posting.get("title") or "").strip(),
                "url": str(posting.get("url") or "").strip(),
                "location": _location(posting),
                "posted": str(posting.get("datePosted") or "")[:10],
                "deadline": str(posting.get("validThrough") or "")[:10],
                "description": _body(posting),
            })
        offset += PAGE
        if len(results) < PAGE or offset >= int(data.get("total") or 0):
            break
    return jobs
