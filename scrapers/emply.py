"""Emply hosted career-site scraper (JSON page API).

Emply career sites ({tenant}.career.emply.com) render the vacancy list
client-side, but the backing endpoint is plain JSON:
``POST {base}/api/integration/vacancy/get-page``. The catch — and why an
earlier probe wrote this API off as "500s without the exact frontend
payload" — is that the payload must carry the ``sectionId`` GUID of the
page's vacancies widget, which only exists in the page HTML
(``sectionId: '<guid>'`` inside the inline JS config). So: fetch the
vacancies page once, lift the sectionId, then page through the API.

Job URL preference: ``directLink`` (canonical ad page) falling back to
``applyLink``. The API reports the true ``count``, so a zero-vacancy board
is a trusted empty (explicit signal), not a parse failure.
"""
import re

from ._http import assert_complete, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}

_SECTION_RE = re.compile(r"sectionId:\s*'([0-9a-f-]{36})'")
PAGE_SIZE = 50


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://capital-four.career.emply.com",
        "tenant": "capitalfour",   # optional label for job IDs
        "lang": "en",              # optional, defaults to en
    }
    """
    base_url = config["base_url"].rstrip("/")
    tenant = config.get("tenant", base_url.split("//")[-1].split(".")[0])
    lang = config.get("lang", "en")

    session = make_session()
    page = session.get(f"{base_url}/vacancies", headers=HEADERS, timeout=30)
    page.raise_for_status()
    m = _SECTION_RE.search(page.text)
    if not m:
        raise RuntimeError(
            f"emply: no vacancies-widget sectionId found on {base_url}/vacancies "
            "(page layout changed?)")
    section_id = m.group(1)

    jobs = []
    offset, total = 0, None
    while total is None or offset < total:
        payload = {
            "count": PAGE_SIZE, "filters": [], "langCode": lang,
            "offset": offset, "searchText": "", "sectionId": section_id,
            "sortByProjectDataId": "", "sortAscending": False,
            "light": False, "isJobAgent": False, "siteId": None,
        }
        r = session.post(f"{base_url}/api/integration/vacancy/get-page",
                         json=payload, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        total = int(data.get("count", 0))
        batch = data.get("vacancies", [])
        if not batch:
            break
        for v in batch:
            title = (v.get("title") or "").strip()
            job_id = str(v.get("shortId") or v.get("id") or "").strip()
            if not title or not job_id:
                continue
            # directLink/applyLink are null on this API; the canonical ad page
            # is /ad/{titleAsUrl}/{shortId} (verified 200).
            url = v.get("directLink") or v.get("applyLink")
            if not url and v.get("titleAsUrl") and v.get("shortId"):
                url = f"{base_url}/ad/{v['titleAsUrl']}/{v['shortId']}"
            url = url or f"{base_url}/vacancies"
            jobs.append({
                "id": f"emply_{tenant}_{job_id}",
                "title": title,
                "url": url,
                "location": (v.get("location") or "").strip(),
                "posted": (v.get("published") or "")[:10],
            })
        offset += len(batch)

    # total==0 is a trusted empty (the API states it explicitly); a partial
    # page haul against a nonzero total is not.
    if total:
        assert_complete(len(jobs), total, f"Emply/{tenant}")
    return jobs
