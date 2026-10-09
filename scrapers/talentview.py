"""TalentView (talentview.io) public funnel API scraper.

The TalentView careers SPA loads roles ("campaigns") from a clean public JSON
endpoint — no auth, plain HTTP (curl_cffi not needed):

    GET https://api.talentview.io/funnel/v2/companies/{slug}/campaigns
        ?company_website_id={id}&display_mode=list&offset_start={n}

Returns a JSON array of campaigns; paginate by advancing offset_start until an
empty array. The numeric ``company_website_id`` is stable but also derivable:

    GET https://api.talentview.io/funnel/v2/companies/{slug}/websites?website_type=public
    -> [{"id": <website_id>, "locale": "en"}, ...]

Config (in targets.json under ``talentview``):
    slug                 company funnel slug (e.g. "tikehau-capital-career")
    company_website_id   optional; auto-discovered from /websites if omitted
"""
from ._http import make_session

API = "https://api.talentview.io"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
MAX_PAGES = 40


def _website_id(slug: str, session) -> str | None:
    # HTTP errors propagate — a transient 500 here must surface as a failed
    # scrape, not as "board is empty" (which would delist the whole source).
    r = session.get(f"{API}/funnel/v2/companies/{slug}/websites",
                    params={"website_type": "public"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    sites = r.json()
    if not isinstance(sites, list) or not sites:
        return None
    # Prefer an English public site, else the first.
    en = next((s for s in sites if str(s.get("locale", "")).startswith("en")), None)
    return str((en or sites[0]).get("id", "")) or None


def scrape(config: dict) -> list[dict]:
    slug = config["slug"]
    session = make_session()

    website_id = config.get("company_website_id")
    if not website_id:
        website_id = _website_id(slug, session)
    if not website_id:
        raise RuntimeError(f"TALENTVIEW_NO_WEBSITE_ID slug={slug}")

    jobs: dict[str, dict] = {}
    offset = 1
    for _ in range(MAX_PAGES):
        r = session.get(
            f"{API}/funnel/v2/companies/{slug}/campaigns",
            params={"company_website_id": website_id, "display_mode": "list",
                    "offset_start": offset},
            headers=HEADERS, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        new = 0
        for c in batch:
            cid = str(c.get("id") or "")
            title = c.get("name") or ""
            cslug = c.get("slug") or cid
            if not cid or not title or cid in jobs:
                continue
            addr = c.get("address") or {}
            if isinstance(addr, dict):
                location = ", ".join(p for p in (addr.get("city"), addr.get("country")) if p) \
                    or addr.get("name", "")
            else:
                location = str(addr)
            jobs[cid] = {
                "id": f"talentview_{cid}",
                "title": title,
                "url": f"https://{slug}.talentview.io/jobs/{cslug}",
                "location": location,
                "posted": str(c.get("last_activation_at") or "")[:10],
            }
            new += 1
        offset += len(batch)
        if new == 0:
            break

    return list(jobs.values())
