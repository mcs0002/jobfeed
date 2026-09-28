"""Haufe Umantis (Talent Management) hosted job-board scraper.

Umantis recruiting hubs ({co}.umantis.com or recruitingapp-{n}.de.umantis.com)
server-render the full vacancy list on ``/Jobs/All``: each posting is an
anchor ``a.HSTableLinkSubTitle`` with href ``/Vacancies/{id}/Description/{n}``
and the title as link text. One page, no tokens, plain requests.

Some tenants (Quoniam, seen 2026-08-19) instead serve ``/Jobs/All`` as a
LANGUAGE PICKER — no vacancy anchors at all, just links to per-language boards
at ``/Jobs/{n}?lang={code}``. Those get followed and merged. A tenant that
serves its board directly (J. Safra Sarasin) never reaches that branch.

Umantis also has its own explicit empty-board prose, which is the only safe
way to tell "no open roles" from "layout changed" — both otherwise present as
zero anchors, and guessing wrong either purges a live firm's rows or hides a
broken scraper.
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


# Umantis's own "this board has nothing on it" prose. Only these mean
# trusted-empty; anything else with zero anchors is a layout change.
EMPTY_MARKERS = (
    "no entries that could be displayed",       # EN
    "keine eintr\u00e4ge erfasst",                  # DE
)
# Per-language board links on a picker-style /Jobs/All (…/Jobs/31?lang=gerger).
_LANG_BOARD_RE = re.compile(r"/Jobs/\d+\?lang=", re.I)


def _parse_board(soup, response_url: str, tenant: str, jobs: dict) -> None:
    """Collect every vacancy anchor on one board page into `jobs`."""
    for link in soup.select("a.HSTableLinkSubTitle"):
        href = link.get("href", "")
        match = re.search(r"/Vacancies/(\d+)/Description/", href)
        title = link.get_text(" ", strip=True)
        if not match or not title:
            continue
        job_id = match.group(1)
        jobs[job_id] = {
            "id": f"umantis_{tenant}_{job_id}",
            "title": title,
            "url": urljoin(response_url, href),
            "location": "",
            "posted": "",
        }


def _is_trusted_empty(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in EMPTY_MARKERS)


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://recruitingapp-5064.de.umantis.com",
        "tenant": "quoniam",   # optional label for job IDs
    }
    """
    base_url = config["base_url"].rstrip("/")
    tenant = config.get("tenant", base_url.split("//")[-1].split(".")[0])
    session = make_session()

    response = session.get(f"{base_url}/Jobs/All", headers=HEADERS, timeout=40)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")

    jobs: dict = {}
    _parse_board(soup, response.url, tenant, jobs)
    if jobs:
        return list(jobs.values())

    # No vacancies here. Either this tenant serves /Jobs/All as a language
    # picker, or the board really is empty, or the layout moved.
    lang_boards = [
        urljoin(response.url, a["href"])
        for a in soup.select("a[href]")
        if _LANG_BOARD_RE.search(a.get("href", ""))
    ]
    saw_empty_marker = _is_trusted_empty(response.text)

    for board_url in lang_boards:
        sub = session.get(board_url, headers=HEADERS, timeout=40)
        sub.raise_for_status()
        fix_encoding(sub)
        _parse_board(BeautifulSoup(sub.text, "html.parser"), sub.url, tenant, jobs)
        if _is_trusted_empty(sub.text):
            saw_empty_marker = True

    if jobs:
        return list(jobs.values())
    if saw_empty_marker:
        # The platform said so itself — a genuinely empty board, not a break.
        return []
    if "HSTableLinkSubTitle" in response.text:
        # An empty board normally still ships the table markup.
        return []
    raise RuntimeError(
        f"umantis: no vacancy anchors, no per-language boards and no empty "
        f"marker at {base_url}/Jobs/All (layout change or challenge page?)"
    )
