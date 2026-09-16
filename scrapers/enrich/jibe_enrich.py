"""Fetch Jibe ATS job descriptions from the page's JSON-LD JobPosting block.

Jibe career sites (e.g. ICE / Intercontinental Exchange at careers.ice.com)
serve an AngularJS shell: the visible body is rendered client-side, so a plain
GET + HTML strip yields only the cookie banner (~309 chars), not the role.

But the same shell HTML embeds a server-rendered
`<script type="application/ld+json">` schema.org/JobPosting block whose
`description` field is the full posting body (as HTML). No JS, no auth, no
separate API call needed — parse it straight out of the page we already fetch.

The numeric id lives in the URL (careers.ice.com/jobs/12999); we just GET that
URL. The Jibe signature is the `ng-app="jibeapply"` shell + the JobPosting
JSON-LD, so any Jibe host works — but we match an explicit host allowlist, NOT a
generic `careers.<x>.com` pattern, because other ATSes share that host shape
(careers.axpo.com = teamtailor, careers.bain.com = avature) and jibe sits early
in DETAIL_ENRICHERS where it would greedily capture them. Add new Jibe firms to
`_JIBE_HOSTS`.
"""
import re

import requests

from .descriptions import _jobposting_description

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Jibe job-detail URL: careers.<host>/jobs/<numeric id>. Explicit host allowlist
# (see module docstring — a generic careers.*.com pattern would grab teamtailor
# and avature boards that share the shape).
_JIBE_HOSTS = ("ice", "sig")  # ICE / Intercontinental Exchange, SIG / Susquehanna
_URL_RE = re.compile(
    r"^https?://careers\.(?:%s)\.com/jobs/\d+" % "|".join(_JIBE_HOSTS), re.I)


def is_jibe(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one Jibe job URL, or "" on any failure
    (expired posting, network error, no JobPosting block)."""
    if not is_jibe(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
        html = r.text
    except requests.RequestException:
        return ""

    # Shared parser (handles list payloads, @graph, list-@type, control chars).
    return _jobposting_description(html)


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        print(f"=== {u} ===")
        print(description(u)[:400])
        print()
