"""Koch Supply & Trading careers scraper (Avature, behind AWS WAF).

Koch's Avature instance at koch.avature.net is protected by an awselb/2.0
WAF that returns HTTP 202 + ``x-amzn-waf-action: challenge`` to plain
``requests`` clients. There is no cookie-free public API. The deal is:
the user opens the site in his real Chrome every ~7 days, copies the
WAF cookies into a gitignored JSON file, and this scraper consumes them.

When the cookies expire (or the file is missing), the scraper RAISES.
main.py catches per-company exceptions, so the rest of the run still
proceeds — but the error keeps Koch out of the delist pass. (It used to
fail soft with ``[]``, which the pipeline read as "board is empty" and
mass-delisted every Koch row on each ~weekly cookie expiry.) The error
surfaces in the scan summary / Sources page; refresh the file per
``KOCH_CAPTURE.md`` (60-second capture procedure).

Config (in targets.json):
    cookies_path     path to the JSON cookie file (default: secrets/koch_cookies.json)
    search_url       Avature search URL (default: /en_US/careers/SearchJobs/)
    business_unit    optional folderRecruitmentProcess id to scope to KST only
"""
import json
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COOKIES_PATH = ROOT / "secrets" / "koch_cookies.json"
DEFAULT_SEARCH_URL = "https://koch.avature.net/en_US/careers/SearchJobs/"

# Reserved keys in the cookies JSON that aren't cookies themselves. Everything
# else in the file is forwarded to the request as a cookie name/value pair, so
# you can extend the capture set without code changes (Koch's Avature uses
# aws-waf-token, __cf_bm, and a ScustomPortal-* session id; other Avature
# instances may use a different mix).
_META_KEYS = {"user_agent", "captured_at", "notes"}


def _load_cookies(path: Path) -> tuple[dict, str] | None:
    """Return (cookie_dict, user_agent) or None if missing/malformed.
    aws-waf-token is the sentinel: without it the WAF won't accept any
    other cookies, so its absence means recapture is needed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    cookies = {k: v for k, v in data.items()
               if k not in _META_KEYS and isinstance(v, str) and v}
    if not cookies.get("aws-waf-token"):
        return None
    return cookies, data.get("user_agent") or "Mozilla/5.0"


def _challenged(response: requests.Response) -> bool:
    if response.status_code == 202:
        return True
    if "x-amzn-waf-action" in {h.lower() for h in response.headers}:
        return True
    text = response.text[:2000].lower()
    return "captcha-delivery" in text or "challenge" in text and "aws" in text


def scrape(config: dict) -> list[dict]:
    path = Path(config.get("cookies_path") or DEFAULT_COOKIES_PATH)
    loaded = _load_cookies(path)
    if loaded is None:
        raise RuntimeError(
            f"KOCH_COOKIES_MISSING path={path} — recapture per KOCH_CAPTURE.md"
        )
    cookies, user_agent = loaded

    search_url = config.get("search_url") or DEFAULT_SEARCH_URL
    # Koch's portal caps server-side at 6 jobs/page regardless of the
    # jobRecordsPerPage value. Paginate step=6 with a hard cap so a runaway
    # WAF response doesn't burn the token or wedge the daily run.
    page_size = int(config.get("page_size", 6))
    max_pages = int(config.get("max_pages", 30))
    # filter_params lets the targets.json entry name the Avature facet
    # query-string keys directly (KST today: 732=6319 + 732_format=1077 +
    # listFilterMode=1). The facet key is numeric and instance-specific —
    # captured manually from the filter sidebar, see KOCH_CAPTURE.md.
    base_params: dict = {"jobRecordsPerPage": page_size}
    base_params.update(config.get("filter_params") or {})

    session = make_session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="koch.avature.net")

    jobs: dict[str, dict] = {}
    for page in range(max_pages):
        params = dict(base_params, jobOffset=page * page_size)
        # Network errors propagate: a partial list would delist the jobs on
        # the pages we never reached, which is worse than an errored run.
        response = session.get(search_url, params=params, timeout=25)

        if _challenged(response):
            raise RuntimeError(
                "KOCH_COOKIES_EXPIRED — recapture and rewrite "
                "secrets/koch_cookies.json (see KOCH_CAPTURE.md)"
            )
        response.raise_for_status()

        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        new_on_page = 0
        # Avature JobDetail anchors have the shape /en_US/careers/JobDetail/<slug>/<id>
        for link in soup.select('a[href*="/JobDetail/"]'):
            href = link.get("href") or ""
            parts = href.rstrip("/").split("/")
            if not parts or not parts[-1].isdigit():
                continue
            job_id = parts[-1]
            if job_id in jobs:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            url = href if href.startswith("http") else f"https://koch.avature.net{href}"
            card = link.find_parent(["article", "li", "div"])
            location = ""
            if card is not None:
                loc_el = card.select_one(
                    ".article__meta-info-text, .article__details__data, .job-info-icon_world"
                )
                if loc_el:
                    location = loc_el.get_text(" ", strip=True)
            jobs[job_id] = {
                "id": f"koch_{job_id}",
                "title": title,
                "url": url,
                "location": " ".join(location.split()),
                "posted": "",
            }
            new_on_page += 1
        if new_on_page == 0:
            break  # end of the corpus

    return list(jobs.values())
