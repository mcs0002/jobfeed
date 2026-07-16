"""Azimut careers scraper — Liferay Headless API via OAuth client-credentials.

The Next.js careers page carries a public OAuth client_id/secret in its
``careers-*.js`` chunk (client_credentials grant). We discover those literals
from the chunk (the hash rotates on redeploy, so we don't hardcode them), mint a
Bearer token at ``backend.azimut.it/o/oauth2/token``, and read the Liferay
Headless job object at ``/o/c/jobcareerses/scopes/{scope}``. Body inline
(``jobDescriptionShort``/``Long``); no per-job public URL, so the human link is
the careers page.
"""
import re

from ._http import make_session

CAREERS = "https://www.azimut-group.com/careers"
TOKEN_URL = "https://backend.azimut.it/o/oauth2/token"
JOBS_URL = "https://backend.azimut.it/o/c/jobcareerses/scopes/{scope}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_CID = re.compile(r'"(id-[0-9a-f-]{20,})"')
_SECRET = re.compile(r'"(secret-[0-9a-f-]{20,})"')
_CHUNK = re.compile(r'/_next/static/chunks/pages/careers-[0-9a-f]+\.js')


def _discover_creds(session) -> tuple[str, str]:
    page = session.get(CAREERS, headers=HEADERS, timeout=40)
    page.raise_for_status()
    for path in dict.fromkeys(_CHUNK.findall(page.text)):
        js = session.get("https://www.azimut-group.com" + path,
                         headers=HEADERS, timeout=40)
        cid = _CID.search(js.text)
        secret = _SECRET.search(js.text)
        if cid and secret:
            return cid.group(1), secret.group(1)
    raise RuntimeError("azimut: OAuth client_id/secret not found in careers chunk")


def scrape(config: dict | None = None) -> list[dict]:
    """config = {"scope": "154279"}  (default 154279 — the 'job careers' object)"""
    scope = (config or {}).get("scope", "154279")
    session = make_session()

    client_id, client_secret = _discover_creds(session)
    tok = session.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "grant_type": "client_credentials"},
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=40)
    tok.raise_for_status()
    access_token = tok.json()["access_token"]

    resp = session.get(JOBS_URL.format(scope=scope),
                       headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
                       timeout=40)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    jobs = {}
    for item in items:
        job_id = str(item.get("id") or "").strip()
        title = (item.get("jobTitle") or "").strip()
        if not job_id or not title:
            continue
        body = (item.get("jobDescriptionLong") or item.get("jobDescriptionShort") or "").strip()
        jobs[job_id] = {
            "id": f"azimut_{job_id}",
            "title": title,
            "url": CAREERS,
            "location": (item.get("jobCountry") or "").strip(),
            "description": body,
            "posted": "",
        }
    return list(jobs.values())
