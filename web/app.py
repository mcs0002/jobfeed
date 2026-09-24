"""
Browse / filter web app for the roles database.

Server-rendered FastAPI + Jinja2 + HTMX — no JS build step. Reads the same
`jobs.db` the scraper writes (WAL, so concurrent), filters interactively by
sector / function / location / etc., lets you mark roles applied/ignored, and
exposes the same query as JSON at /api/jobs.

Runs as a launchd service on the M1 (next to the live db), reached over
Tailscale, behind a single shared password (WEB_PASSWORD). Open one SQLite
connection per request (cheap, thread-safe under uvicorn's worker threads).

Run locally against a db copy:
    JOBS_DB=/path/to/copy.db WEB_PASSWORD=test \
      .venv/bin/uvicorn web.app:app --reload
"""
import json
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import html as _html
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from jobfeed import tag
from jobfeed.envfile import load_dotenv  # noqa: E402
from jobfeed.tag_telemetry import aggregate as tag_telemetry
from applications import runs as antigravity_runs  # noqa: E402
from applications import autopilot  # noqa: E402
from jobfeed import campus  # noqa: E402
from applications.handoff import handoff_view, safe_application_url, workflow_token  # noqa: E402
from applications.launcher import enqueue_launch  # noqa: E402
from jobfeed.db import (AGENT_WORKFLOW_STATES, APPLICATION_MAIL_ORDER,
                APPLICATION_WORKFLOW_STATES, APPLIED_STATUSES, TRACKS, JobDB,
                _utc_instant,
                mail_rank, plain_company)  # noqa: E402
from scrapers.enrich.descriptions import _extract_text  # noqa: E402
from web.descfmt import format_description  # noqa: E402


_TARGETS_PATH = os.path.join(ROOT, "targets.json")
_TARGETS_CACHE: dict = {"mtime": None, "data": None}



def _load_targets() -> list[dict]:
    """targets.json, cached and invalidated on mtime change."""
    try:
        mtime = os.path.getmtime(_TARGETS_PATH)
    except OSError:
        return []
    if _TARGETS_CACHE["mtime"] != mtime:
        with open(_TARGETS_PATH) as fp:
            _TARGETS_CACHE["data"] = json.load(fp)
        _TARGETS_CACHE["mtime"] = mtime
    return _TARGETS_CACHE["data"] or []


HIDDEN_COOKIE = "src_hidden"


def _hidden_companies(request: Request) -> list[str]:
    """Company names the user toggled off on the Sources page, carried in the
    src_hidden cookie as {"firms": [...names], "cats": [...category names]}.
    Hidden categories expand to their member firms. Used to drop those firms'
    roles (and tab-count badges) from the Browse tab."""
    raw = request.cookies.get(HIDDEN_COOKIE)
    if not raw:
        return []
    try:
        obj = json.loads(urllib.parse.unquote(raw))
    except (ValueError, TypeError):
        return []
    # A malformed cookie (non-dict JSON, non-string members) must degrade to
    # "nothing hidden", not 500 every page until the cookie is cleared.
    if not isinstance(obj, dict):
        return []
    firms = {str(x) for x in (obj.get("firms") or []) if isinstance(x, str)}
    cats = {str(x) for x in (obj.get("cats") or []) if isinstance(x, str)}
    if cats:
        firms.update(t.get("name", "") for t in _load_targets()
                     if t.get("category") in cats)
    firms.discard("")
    return sorted(firms)


def _clean_desc(text: str) -> str:
    """Strip residual HTML from a stored description for clean display (older
    Greenhouse rows are raw HTML). No-op on already-plain text."""
    if not text:
        return text
    if "<" in text or "&lt;" in text or "&amp;" in text:
        cleaned = _extract_text(_html.unescape(text), max_chars=20000)
        if cleaned:
            return cleaned
    return text




load_dotenv()

DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
# Two fixed accounts, each a username + password pair:
#   - the OWNER (full control: status / favorite / notes / mark-seen), and
#   - an optional read-only GUEST: may use only Browse, Technical stats and
#     Sources. Every other route is denied by a central allowlist, so a newly
#     added page is owner-only until deliberately admitted. Owner state
#     (status, favorites and unread markers) is absent from the guest surface.
# Unset WEB_GUEST_PASSWORD = no guest access at all.
WEB_USER = os.environ.get("WEB_USER", "admin").strip().lower()
WEB_GUEST_USER = os.environ.get("WEB_GUEST_USER", "guest").strip().lower()
WEB_GUEST_PASSWORD = os.environ.get("WEB_GUEST_PASSWORD", "")
# Fail closed: with no password configured the app denies EVERYONE, unless the
# operator explicitly opts into open mode via WEB_ALLOW_NO_AUTH=1 (dev / trusted
# tailnet only). This stops the app from silently serving with no auth at all if
# WEB_PASSWORD is ever unset on the production host.
WEB_ALLOW_NO_AUTH = os.environ.get("WEB_ALLOW_NO_AUTH", "").strip() in ("1", "true", "on")
if not WEB_PASSWORD:
    if WEB_ALLOW_NO_AUTH:
        print("WARNING: WEB_PASSWORD is not set and WEB_ALLOW_NO_AUTH=1 — the app "
              "is running WITHOUT authentication (open to anyone who can reach it).",
              file=sys.stderr)
    else:
        print("WARNING: WEB_PASSWORD is not set — failing closed and refusing ALL "
              "requests. Set WEB_PASSWORD, or set WEB_ALLOW_NO_AUTH=1 to run open.",
              file=sys.stderr)
# Cookie-signing secret. Falls back to a per-process random value, which just
# means everyone re-logs-in after a restart — fine for a private tool.
WEB_SECRET = os.environ.get("WEB_SECRET") or secrets.token_hex(32)

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Cache-bust static assets by their mtime so a deploy always serves fresh CSS
# (Safari otherwise holds the old style.css and every layout fix looks dead).
_STYLE_PATH = os.path.join(os.path.dirname(__file__), "static", "style.css")
try:
    ASSET_V = int(os.path.getmtime(_STYLE_PATH))
except OSError:
    ASSET_V = 0
TEMPLATES.env.globals["asset_v"] = ASSET_V

# Navigation is declared once. A new item is owner-only by default: exposing
# it to the shared guest login requires the explicit ``guest=True`` decision.
NAV_ITEMS = (
    {"href": "/", "label": "Browse", "mobile": "Browse", "icon": "▤",
     "active": "browse", "browse": True, "guest": True},
    {"href": "/applications", "label": "Applications", "mobile": "Apply",
     "icon": "A", "active": "applications"},
    {"href": "/stats/applications", "label": "Application stats",
     "mobile": "App stats", "icon": "◈", "active": "application-stats"},
    {"href": "/stats/technical", "label": "Technical stats",
     "mobile": "Tech stats", "icon": "∿", "active": "technical-stats",
     "guest": True},
    {"href": "/sources", "label": "Sources", "mobile": "Sources",
     "icon": "⛁", "active": "sources", "guest": True},
    # group="more": the occasional pages fold into one More menu
    # (2026-09-24), so the bar keeps the five he uses daily.
    {"href": "/review", "label": "Review", "mobile": "Review", "icon": "◌",
     "active": "review", "title": "Audit the roles tagging hid from you",
     "group": "more"},
    {"href": "/limits", "label": "Limits", "mobile": "Limits", "icon": "⚖",
     "active": "limits", "title": "How many applications each firm allows per cycle",
     "group": "more"},
    {"href": "/campus", "label": "Grad schemes", "mobile": "Grad", "icon": "🎓",
     "active": "campus", "title": "Graduate schemes that never reach an ATS board",
     "group": "more"},
)


def _nav_items(request: Request) -> list[dict]:
    if request.session.get("role") == "guest":
        return [item for item in NAV_ITEMS if item.get("guest")]
    return list(NAV_ITEMS)


TEMPLATES.env.globals["nav_items"] = _nav_items


def _safe_url(url) -> str:
    """Only http(s) URLs may render as hrefs. Job URLs come from scraped ATS
    payloads — hostile input. Jinja autoescaping doesn't neutralize the URL
    *scheme*, so a board returning ``javascript:...`` as the apply link would
    otherwise execute in the authed session on click."""
    return safe_application_url(url)


TEMPLATES.env.filters["safe_url"] = _safe_url


def _workflow_token(workflow_id: str) -> str:
    """Deterministic narrow capability for Antigravity status writeback.

    The production WEB_SECRET is stable, so an idempotently re-opened workflow
    gets the same callback URL without storing another credential in jobs.db.
    The capability cannot complete an application; that remains owner-only.
    """
    return workflow_token(WEB_SECRET, workflow_id)


def _valid_workflow_token(workflow_id: str, token: str) -> bool:
    return bool(token) and secrets.compare_digest(_workflow_token(workflow_id), token)


def _public_base_url(request: Request) -> str:
    configured = os.environ.get("WEB_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _workflow_view(workflow: dict | None, request: Request,
                   db: JobDB | None = None) -> dict | None:
    if workflow is None:
        return None
    offer = db.offer_elsewhere(workflow["job_id"]) if db is not None else None
    prior = db.prior_applications(workflow["job_id"]) if db is not None else None
    return handoff_view(workflow, WEB_SECRET, _public_base_url(request),
                        offer_elsewhere=offer, prior_applications=prior)


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _valid_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("csrf_token", "")
    return bool(expected) and bool(token) and secrets.compare_digest(expected, token)


def _days_ago(ts: str) -> str:
    if not ts:
        return "—"
    try:
        d = datetime.fromisoformat(ts[:10]).date()
        delta = (datetime.now(timezone.utc).date() - d).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "1d ago"
        return f"{delta}d ago"
    except Exception:
        return (ts or "")[:10]


TEMPLATES.env.globals["days_ago"] = _days_ago
TEMPLATES.env.globals["plain_company"] = plain_company


def _days_since(ts: str) -> int:
    """Whole days since `ts`, for the S8 recency grouping. A missing or
    unparseable timestamp sorts into the oldest bucket rather than the newest —
    an undated row is not 'today'."""
    if not ts:
        return 10 ** 6
    try:
        d = datetime.fromisoformat(ts[:10]).date()
        return (datetime.now(timezone.utc).date() - d).days
    except Exception:
        return 10 ** 6


TEMPLATES.env.globals["days_since"] = _days_since


def _deadline_text(ts: str) -> str:
    """Human copy for a deadline, or "not stated" when there isn't one.

    NULL means no date reached us, not that recruiting is rolling: it read
    "rolling" until 2026-09-24, over an Equinor posting whose body said
    "submit it before the end of the day on 07.10.2026"."""
    if not ts:
        return "not stated"
    try:
        d = datetime.fromisoformat(ts[:10]).date()
    except Exception:
        return "not stated"
    n = (d - datetime.now(timezone.utc).date()).days
    if n < 0:
        return "closed"
    if n == 0:
        return "closes today"
    if n == 1:
        return "closes tomorrow"
    return f"closes in {n}d"


def _deadline_days(ts: str) -> int | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts[:10]).date()
    except Exception:
        return None
    return (d - datetime.now(timezone.utc).date()).days


TEMPLATES.env.globals["deadline_text"] = _deadline_text
TEMPLATES.env.globals["deadline_days"] = _deadline_days


# Known careers landing pages for first-party bespoke scrapers whose config
# carries no URL — so every source gets a ↗ career-site link.
_FIXED_CAREER_URL = {
    "janestreet": "https://www.janestreet.com/join-jane-street/open-roles/",
    "deshaw": "https://www.deshaw.com/careers",
    "bnpparibas_paced": "https://group.bnpparibas/en/careers/all-job-offers",
    "societegenerale": "https://careers.societegenerale.com/en",
    "wellsfargo": "https://www.wellsfargojobs.com/en/jobs/",
    "euronext": "https://www.euronext.com/en/careers/open-positions",
    "deutscheboerse": "https://careers.deutsche-boerse.com/",
    "bundesbank": "https://www.bundesbank.de/en/bundesbank/career",
    "bis": "https://www.bis.org/careers/vacancies.htm",
    "abnamro": "https://www.werkenbijabnamro.nl/en/vacancies",
    "rwe": "https://jobs.rwe.com/",
    "uniper": "https://careers.uniper.energy/",
    "glencore": "https://www.glencore.com/careers",
    "guidecom": "https://www.helaba.com/int/career/",
}


def _ats_board_url(t: dict) -> str:
    """Derive a direct career-board URL from the ATS config when career_url is
    absent. Falls back to a known careers page for bespoke first-party scrapers
    (see _FIXED_CAREER_URL) so every source can show the ↗ link."""
    ats = t.get("ats", "")
    slug = t.get("slug", "")
    if ats == "greenhouse":
        if slug:
            base = ("https://job-boards.eu.greenhouse.io" if t.get("eu")
                    else "https://job-boards.greenhouse.io")
            return f"{base}/{slug}"
    elif ats == "lever":
        return f"https://jobs.lever.co/{slug}" if slug else ""
    elif ats == "workday":
        wd = t.get("workday") or {}
        tenant, version, board = wd.get("tenant", ""), wd.get("version", "wd1"), wd.get("board", "")
        return f"https://{tenant}.{version}.myworkdayjobs.com/{board}" if tenant and board else ""
    elif ats == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{slug}" if slug else ""
    elif ats == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}" if slug else ""
    elif ats == "recruitee":
        return f"https://{slug}.recruitee.com/" if slug else ""
    elif ats == "teamtailor":
        return t.get("base_url", "")
    elif ats == "workable":
        account = t.get("account", "")
        return f"https://apply.workable.com/{account}/" if account else ""
    elif ats == "breezy":
        account = t.get("account", "")
        return f"https://{account}.breezy.hr/" if account else ""
    elif ats == "oracle_hcm":
        ohcm = t.get("oracle_hcm") or {}
        base, site = ohcm.get("base_url", ""), ohcm.get("site", "")
        if base and site:
            return f"{base}/hcmUI/CandidateExperience/en/sites/{site}/jobs"
        return base
    elif ats in ("successfactors", "successfactors_api", "successfactors_classic"):
        cfg = (t.get("successfactors_api") or t.get("successfactors_classic") or {})
        return cfg.get("base_url", "") or t.get("base_url", "")
    elif ats == "attrax":
        at = t.get("attrax") or {}
        return at.get("search_url", at.get("base_url", ""))
    elif ats in ("radancy", "avature"):
        return t.get("search_url", "")
    elif ats == "icims":
        return (t.get("icims") or {}).get("base_url", "")
    elif ats == "eightfold":
        return (t.get("eightfold") or {}).get("base_url", "")
    elif ats == "phenom":
        return (t.get("phenom") or {}).get("base_url", "")
    elif ats == "phenom_widgets":
        return (t.get("phenom_widgets") or {}).get("base_url", "")
    elif ats == "talnet":
        return t.get("board_url", "")
    elif ats == "talentbrew":
        return (t.get("talentbrew") or {}).get("base_url", "")
    elif ats == "jibe":
        return (t.get("jibe") or {}).get("base_url", "")
    elif ats == "citadel":
        return (t.get("citadel") or {}).get("base_url", "")
    elif ats == "hibob":
        base = (t.get("hibob") or {}).get("base_url", "")
        return f"{base}/jobs" if base else ""
    elif ats in ("brassring_hosted", "brassring"):
        return (t.get(ats) or {}).get("search_url", "")
    elif ats == "peoplebank":
        return t.get("category_url", "")
    elif ats == "recsolu":
        rc = t.get("recsolu") or {}
        if rc.get("base_url") and rc.get("board_id"):
            return f"{rc['base_url']}/job_boards/{rc['board_id']}"
        return rc.get("base_url", "")
    elif ats == "generic":
        return t.get("url", "")
    elif ats in ("pinpoint", "intervieweb"):
        # feed_url is an RSS/JSON API; link the board's host root instead.
        feed = t.get("feed_url", "")
        parts = feed.split("/", 3)
        return "/".join(parts[:3]) if len(parts) >= 3 else feed
    elif ats == "directemployers":
        host = (t.get("directemployers") or {}).get("host", "")
        return f"https://{host}" if host else ""
    elif ats == "beesite":
        url = (t.get("beesite") or {}).get("base_url", "")
        if url:
            parts = url.split("/", 3)
            host = "/".join(parts[:3]) if len(parts) >= 3 else url
            return host.replace("//jobapi.", "//www.")  # API host -> main site
        return ""
    # First-party bespoke scrapers with no URL in their config: a known
    # careers landing page so every source still gets the ↗ link.
    return _FIXED_CAREER_URL.get(ats, "")


_GUEST_GET_PATHS = frozenset({
    "/", "/login", "/sources", "/stats/technical", "/partials/jobs", "/healthz",
})
_GUEST_POST_PATHS = frozenset({"/login", "/logout"})


class GuestSurfaceMiddleware:
    """Default-deny every guest route outside the deliberately shared UI.

    This sits inside SessionMiddleware, so the decoded role is already in the
    ASGI scope. Path checks are intentionally independent of the navigation:
    hiding a tab must never leave its page reachable by typing the URL.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("session", {}).get("role") == "guest":
            method = scope.get("method", "GET").upper()
            path = scope.get("path", "")
            allowed = (
                method in ("GET", "HEAD")
                and (path in _GUEST_GET_PATHS
                     or path.startswith("/static/")
                     or re.fullmatch(r"/job/[^/]+", path) is not None)
            ) or (method == "POST" and path in _GUEST_POST_PATHS)
            if not allowed:
                response = JSONResponse(
                    {"error": "not available in guest access"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Jobfeed")
# Add this first: Starlette prepends middleware, so SessionMiddleware added
# below becomes the outer layer and populates scope["session"] for the guard.
app.add_middleware(GuestSurfaceMiddleware)
# same_site="strict" makes the session cookie unavailable on cross-site requests,
# so state-changing POSTs (login/logout/status) can't be CSRF-forged — explicit,
# not relying on the middleware default.
app.add_middleware(SessionMiddleware, secret_key=WEB_SECRET,
                   max_age=60 * 60 * 24 * 30, same_site="strict",
                   https_only=True)

# Row cap for the browse/table views. fetch_jobs slices to this; when the
# filtered set is larger the UI must say so rather than silently showing "500".
PAGE_LIMIT = 500
# Hard ceiling for the /api/jobs `limit` param so a caller can't ask the server
# to materialise an unbounded result set.
API_LIMIT_MAX = 2000
# Application CRM funnel — the ordered set of stages a role can move through.
# Kept here (validated server-side) and mirrored in the _status_cell template.
CRM_STATUSES = ("new", "queued", "applied", "oa", "interview",
                "offer", "rejected", "ignored")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# --- DB per request ---
def get_db():
    db = JobDB(DB_FILE, check_same_thread=False)
    try:
        yield db
    finally:
        db.conn.close()


# --- Auth ---
def _authed(request: Request) -> bool:
    # No password configured: open ONLY if the operator explicitly opted in
    # (WEB_ALLOW_NO_AUTH=1); otherwise fail closed and deny everyone.
    if not WEB_PASSWORD:
        return WEB_ALLOW_NO_AUTH
    return bool(request.session.get("auth"))


def require_login(request: Request):
    if _authed(request):
        return
    # API callers get a clean 401; browsers get redirected to the login page.
    if request.url.path.startswith("/api"):
        raise _Unauthorized()
    raise _Redirect("/login")


def _is_guest(request: Request) -> bool:
    # Sessions created before roles existed carry no "role" key — they belong
    # to the owner (guests only ever log in through the guest password).
    return request.session.get("role") == "guest"


def _browse_params(request: Request) -> dict[str, str]:
    """Query parameters visible to this role.

    Status and favorites are the user's state, not public job facts. Dropping
    them here also prevents an old owner URL or saved view from re-enabling the
    filters in a guest session.
    """
    params = dict(request.query_params)
    if _is_guest(request):
        params.pop("status", None)
        params.pop("fav", None)
    return params


def require_owner(request: Request):
    """Mutating endpoints: full login check, then reject the guest role. 403
    (not redirect) so a stray htmx POST from a guest fails visibly."""
    require_login(request)
    if _is_guest(request):
        raise _Forbidden()


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


class _Unauthorized(Exception):
    pass


class _Forbidden(Exception):
    pass


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.location, status_code=303)


@app.exception_handler(_Unauthorized)
async def _unauth_handler(request: Request, exc: _Unauthorized):
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.exception_handler(_Forbidden)
async def _forbidden_handler(request: Request, exc: _Forbidden):
    return JSONResponse({"error": "read-only guest access"}, status_code=403)


# --- Filter parsing ---
RECENCY_DAYS = {"7": 7, "14": 14, "30": 30}
# CRM stages that mean he acted on the role, queued onwards. A view filtered to
# one of these shows delisted rows by default. Same order jobfeed/db.py and
# applications/mail.py work in — imported, not restated.
ACTED_ON_STATUSES = APPLICATION_MAIL_ORDER


def _filters_from_request(request: Request, *, allow_text_search: bool = True) -> dict:
    """Map query params → db.fetch_jobs kwargs. Blank params are dropped."""
    p = _browse_params(request)
    kwargs: dict = {}
    for key in ("status", "category", "area", "desk", "seniority", "job_type",
                "loc_region", "loc_country", "loc_city", "work_mode", "company",
                "firm", "education", "lang_req", "start"):
        if _is_guest(request) and key == "status":
            continue
        val = p.get(key, "").strip()
        if val:
            kwargs[key] = val
    # S12 Closing: a controlled vocabulary, whitelisted here so nothing but a
    # known key can reach the deadline SQL.
    closing = p.get("closing", "").strip()
    if closing in ("7", "30", "rolling"):
        kwargs["closing"] = closing
    q = p.get("q", "").strip()
    if allow_text_search and q:
        kwargs["q"] = q
    days = p.get("days", "").strip()
    if days in RECENCY_DAYS:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS[days])
        kwargs["since"] = cutoff.isoformat()
    # Always newest-first; the sort control was removed from the UI.
    kwargs["sort"] = "recent"
    # Grouped area tab (IBD = ibd + capital-markets/DCM; Private Markets =
    # private-equity + debt): translate the virtual/grouped code into an
    # IN-list before area is used as an exact filter anywhere below.
    group = AREA_GROUPS.get(kwargs.get("area"))
    if group:
        del kwargs["area"]
        kwargs["areas"] = list(group)
    # Negative filter: hide back-office / non-finance ('other'). Always on
    # except when the 'Other' tab itself is the active view — there's no
    # toggle for this (the Other tab is the only way to see it, deliberately;
    # a separate always-visible checkbox just fought with the tab).
    kwargs["hide_other"] = kwargs.get("area") != "other"
    # Negative filter: hide disguised-senior roles (>=3 yrs required in the
    # description). ON by default; show_senior=1 reveals them.
    kwargs["hide_yoe"] = p.get("show_senior", "").strip() not in ("1", "true", "on")
    # Negative filter: hide roles delisted from their company's board. ON by
    # default; show_expired=1 reveals them (still badged "delisted" inline).
    # Off whenever the view is filtered to a stage he has acted on: a firm
    # taking its posting down says nothing about his application, and his
    # decision of 2026-09-17 is that "Status: applied" must list every role he
    # applied to, not only the ones still advertised.
    kwargs["hide_delisted"] = not (
        p.get("show_expired", "").strip() in ("1", "true", "on")
        or kwargs.get("status") in ACTED_ON_STATUSES
        # A starred role that came off its board is exactly what the
        # favourites view should surface, badged delisted (2026-09-17).
        or p.get("fav", "").strip() in ("1", "true", "on")
    )
    # Employment type. Default shows everything (full-time + internships together
    # — internships are normalized like any other role); 'fulltime' hides
    # internships; 'intern' shows ONLY internships (job_type = 'internship').
    # ('any' kept as a legacy alias for the default.)
    employ = p.get("employ", "").strip()
    if employ == "intern":
        kwargs["job_type"] = "internship"
    elif employ == "fulltime":
        kwargs["hide_internships"] = True
    else:
        pass  # default (incl. legacy 'any'): full-time and internships together
    # Seniority: hide the associate rung by default (firm-dependent — entry at
    # PE/AM/consulting, senior at banks). 'Include associate roles' (assoc=1)
    # reveals them. Skip the hide when the user explicitly filters seniority to
    # 'associate' (the explicit facet wins, otherwise the view would be empty).
    if kwargs.get("seniority") != "associate":
        kwargs["hide_associates"] = p.get("assoc", "").strip() not in ("1", "true", "on")
    # Favorites-only view (the ★ toggle).
    if not _is_guest(request) and p.get("fav", "").strip() in ("1", "true", "on"):
        kwargs["favorite"] = True
    # Firms the user toggled off on the Sources page disappear from Browse.
    hidden = _hidden_companies(request)
    if hidden:
        kwargs["exclude_companies"] = hidden
    # A stage he has acted on lists every role in it. The default hides are
    # judgements about unread postings; once he has applied, a tag cannot
    # overrule that. On 2026-09-17 the pipeline board's Tudor and Shell Germany
    # cards opened nothing, both tagged area 'other'.
    # A star is the same kind of decision, so the favourites view lifts them too.
    if kwargs.get("status") in ACTED_ON_STATUSES or kwargs.get("favorite"):
        kwargs["hide_delisted"] = False
        kwargs["hide_other"] = False
        kwargs["hide_yoe"] = False
        kwargs.pop("hide_associates", None)
        kwargs.pop("exclude_companies", None)
    return kwargs


# The facet dropdowns the filter panel actually renders. It used to compute
# nine, one full-table scan each (0.63 s of a 1.7 s Browse request on
# 2026-09-23), for a panel showing three; the rest were left over from the
# 2026-08-19 panel restructure. Add a column here when a dropdown needs it.
RENDERED_FACETS = ("category", "desk", "status")


def _facets(db: JobDB, request: Request | None = None,
            filters: dict | None = None) -> dict:
    """Distinct values for each facet dropdown. Non-location facets list every
    tagged value. The LOCATION facets reflect the current filtered view
    (faceted search): Region/Country/City list only places that actually have a
    role visible under the active filters — so a country whose only role is
    delisted / back-office / hidden-by-default no longer clutters the list.
    Country cascades from the selected Region, City from the selected Country."""
    out = {col: db.distinct(col) for col in RENDERED_FACETS}
    # Start filter options: 'asap' is a fixed option in the template; the
    # years are whatever distinct YYYY values the tagger has produced.
    out["start_years"] = db.start_years()
    f = dict(filters or {})
    region = f.get("loc_region", "")
    country = f.get("loc_country", "")
    # Base = the current view minus the location dimensions, so each location
    # facet offers the alternatives available under the rest of the filters.
    base = {k: v for k, v in f.items()
            if k not in ("loc_region", "loc_country", "loc_city")}
    out["loc_region"] = db.distinct_scoped("loc_region", base)
    country_scope = {**base, **({"loc_region": region} if region else {})}
    out["loc_country"] = db.distinct_scoped("loc_country", country_scope)
    city_scope = dict(base)
    if region:
        city_scope["loc_region"] = region
    if country and country in out["loc_country"]:
        city_scope["loc_country"] = country
    out["loc_city"] = db.distinct_scoped("loc_city", city_scope)
    return out


def _reconcile_location(filters: dict, facets: dict) -> None:
    """Drop a selected country/city that isn't valid for the chosen parent
    (e.g. City=London left over after switching Country to US), so the query
    doesn't return an empty set behind a phantom selection. The dropdown
    self-heals to 'any' because the stale option isn't in the scoped list."""
    if filters.get("loc_country") and filters["loc_country"] not in facets["loc_country"]:
        filters.pop("loc_country", None)
    if filters.get("loc_city") and filters["loc_city"] not in facets["loc_city"]:
        filters.pop("loc_city", None)


LAST_SEEN_DEFAULT = "1970-01-01T00:00:00"

# Grouped area tabs: several Haiku-taxonomy area codes are distinct signal
# (jobfeed/tag.py keeps the fine-grained distinction on purpose) but browse together
# as one tab, matching how banks actually organize desks rather than the
# tagger's functional split:
#   - IBD tab = sell-side advisory (ibd) + primary issuance/DCM-ECM
#     (capital-markets) — DCM sits inside IBD at every bank.
#   - Private Markets tab = the two buy-side private-investing areas
#     (private-equity, debt/private-credit).
# `area` column values are untouched; this is a browsing-layer merge only.
AREA_GROUPS = {
    "ibd": ("ibd", "capital-markets"),
    "private-markets": ("private-equity", "debt"),
    # Risk (market/credit/liquidity) is classically a middle-office function, so
    # it browses under the Middle Office tab. Accounting keeps its own tab (it's
    # the Finance/CFO function, not middle office). Area codes stay distinct —
    # the specific sub-area is still shown as a secondary tag (see
    # _area_subtag).
    #
    # Actuarial was folded here until 2026-08-19 and is now its own tab: it is
    # the one genuinely insurance-native FUNCTION, and the insurer/pension
    # cohort (46 firms) is the part of the source list still growing. Sector
    # browsing stays on the "Firm type" facet — tabs are function, not sector.
    "middle-office": ("middle-office", "risk"),
}

# Short label for the specific sub-area shown as a small secondary tag when an
# area is folded under a broader group (e.g. a Middle Office row also shows
# "risk"; an IBD row shows "DCM"). Keeps the fold from hiding the real area.
SUBAREA_LABELS = {
    # capital-markets is the primary-issuance umbrella (ECM *and* DCM
    # origination/syndicate) — do NOT label it just "DCM", that mislabels every
    # equity-capital-markets role as debt.
    "capital-markets": "ECM/DCM",
    "private-equity": "PE",
    "debt": "debt",
    "risk": "risk",
}
# Reverse lookup: raw area code -> the group tab it displays under (identity
# for ungrouped codes), used by _area_label for per-job badges.
_AREA_TO_GROUP = {member: group for group, members in AREA_GROUPS.items()
                  for member in members}

# Top-level Area tabs (label shown on the tab). "" = All.
AREA_TABS = [
    ("", "All"), ("markets", "Markets"), ("quant", "Quant"),
    ("research", "Research"), ("economics", "Econ"), ("ibd", "IBD"),
    ("private-markets", "Private Markets"),
    ("corporate-banking", "Corp Banking"),
    ("asset-management", "AM"),
    ("wealth", "Wealth"),
    ("middle-office", "Middle Office"), ("consulting", "Consulting"),
    ("accounting", "Accounting"), ("actuarial", "Actuarial"),
    ("other", "Other"),
]
# Short, human display label per area code (DCM/AM rather than the raw slug).
AREA_LABELS = {code: label for code, label in AREA_TABS if code}
# Display order for area breakdowns: finance areas first, 'other' last.
AREA_ORDER = [code for code, _ in AREA_TABS if code and code != "other"] + ["other"]


def _area_label(code: str) -> str:
    return AREA_LABELS.get(_AREA_TO_GROUP.get(code, code), code or "—")


def _area_group(code: str) -> str:
    """The display group a raw area code badges under (identity for ungrouped
    codes). Used for the badge COLOUR class so merged members share the tab's
    colour — DCM (capital-markets) under IBD, PE/debt under Private Markets,
    risk under Middle Office — instead of each showing its own colour."""
    return _AREA_TO_GROUP.get(code, code)


def _area_subtag(code: str) -> str:
    """Short label for the specific sub-area, shown as a small secondary tag
    ONLY when the area is folded under a broader group (its group label differs
    from itself). Empty for areas that own their tab — the primary badge already
    names them. Lets 'Middle Office' rows still reveal 'risk'/'actuarial', and
    'IBD' rows reveal 'DCM', without giving each its own tab."""
    if not code or _AREA_TO_GROUP.get(code, code) == code:
        return ""
    return SUBAREA_LABELS.get(code, code)


def _group_count(counts: dict, code: str) -> int:
    """Count for one AREA_ORDER/tab code, summing a group's member codes
    when `code` is a grouped tab (see AREA_GROUPS)."""
    members = AREA_GROUPS.get(code, (code,))
    return sum(counts.get(a, 0) for a in members)


# Sort an area->count dict into display order (finance first, other last),
# dropping zeros. Returns [(code, label, count), ...].
def _area_rows(counts: dict):
    rows = []
    for code in AREA_ORDER:
        n = _group_count(counts, code)
        if n:
            rows.append((code, _area_label(code), n))
    return rows


TEMPLATES.env.globals["area_label"] = _area_label
TEMPLATES.env.globals["area_group"] = _area_group
TEMPLATES.env.globals["area_subtag"] = _area_subtag

# Stable colour slug per region, for the region stacked bar.
REGION_SLUGS = {"Europe": "europe", "Americas": "americas", "APAC": "apac",
                "MEA": "mea", "Other": "other"}


def _bar(rows, total: int, slug=lambda code: code):
    """Turn [(code, label, count), ...] into stacked-bar segments carrying a
    width percentage. Tiny segments keep a visible minimum width."""
    total = total or 1
    segs = []
    for code, label, n in rows:
        segs.append({"code": code, "slug": slug(code), "label": label,
                     "n": n, "pct": round(n / total * 100, 2)})
    return segs


# Active-filter chips shown under the Browse toolbar. Each entry maps a query
# param (and, for the boolean toggles, its "on" value) to a human label; the
# chip's link is the current query with just that param dropped, so clicking it
# removes exactly that one filter. `area` is deliberately excluded — it's the
# tab, not a chip. Order here is the display order.
def _active_chips(request: Request, base_path: str = "/") -> list[dict]:
    p = _browse_params(request)
    params = dict(p)

    def without(*keys) -> str:
        q = {k: v for k, v in params.items() if k not in keys}
        return base_path + "?" + urllib.parse.urlencode(q) if q else base_path

    chips: list[dict] = []

    def add(label: str, *keys):
        chips.append({"label": label, "url": without(*keys)})

    if p.get("company", "").strip():
        add(f"Firm: {p['company'].strip()}", "company")
    if p.get("firm", "").strip():
        add(f"Firm: {p['firm'].strip()}", "firm")
    if p.get("category", "").strip():
        add(p["category"].strip(), "category")
    employ = p.get("employ", "").strip()
    if employ == "fulltime":
        add("Full-time", "employ")
    elif employ == "intern":
        add("Internships", "employ")
    if p.get("desk", "").strip():
        add(f"Desk: {p['desk'].strip()}", "desk")
    if p.get("loc_region", "").strip():
        add(p["loc_region"].strip(), "loc_region")
    if p.get("loc_country", "").strip():
        add(p["loc_country"].strip(), "loc_country")
    if p.get("loc_city", "").strip():
        add(f"City: {p['loc_city'].strip()}", "loc_city")
    days = p.get("days", "").strip()
    if days in RECENCY_DAYS:
        add(f"Last {days}d", "days")
    if not _is_guest(request) and p.get("status", "").strip():
        add(f"Status: {p['status'].strip()}", "status")
    if p.get("education", "").strip():
        add(f"Edu: {p['education'].strip()}", "education")
    lang = p.get("lang_req", "").strip()
    if lang:
        add("English only" if lang == "none" else f"Lang: {lang}", "lang_req")
    start = p.get("start", "").strip()
    if start:
        add("Start: ASAP" if start == "asap" else f"Start: {start}", "start")
    if not _is_guest(request) and p.get("fav", "").strip() in ("1", "true", "on"):
        add("★ Favorites", "fav")
    if p.get("show_senior", "").strip() in ("1", "true", "on"):
        add("Senior 3y+", "show_senior")
    if p.get("assoc", "").strip() in ("1", "true", "on"):
        add("Associates", "assoc")
    closing = p.get("closing", "").strip()
    if closing:
        add({"7": "Closing: 7 days", "30": "Closing: 30 days",
             "rolling": "No stated deadline"}.get(closing, "Closing"), "closing")
    if p.get("show_expired", "").strip() in ("1", "true", "on"):
        add("Delisted", "show_expired")
    return chips


def _browse_reason(job: dict, request: Request) -> str:
    """A short, deterministic explanation for the opt-in Browse preview.

    This deliberately uses only fields already rendered or filterable in
    Browse. It is presentation context, not a score, model judgement, or new
    eligibility rule. Active positive filters win; the unfiltered view falls
    back to up to three concrete attributes already stored on the row.
    """
    p = _browse_params(request)
    facts: list[str] = []

    def add(value) -> None:
        text = str(value or "").strip()
        if text and text not in facts:
            facts.append(text)

    # Explain the actual active slice first. Visibility toggles such as
    # show_senior/show_expired are not matching claims, so they stay out.
    area = p.get("area", "").strip()
    if area:
        add(_area_label(area))
    category = p.get("category", "").strip()
    if category:
        add(category)
    desk = p.get("desk", "").strip()
    if desk:
        add(desk)
    employ = p.get("employ", "").strip()
    if employ == "intern":
        add("internship")
    elif employ == "fulltime":
        add("full-time")
    for key in ("loc_city", "loc_country", "loc_region"):
        value = p.get(key, "").strip()
        if value:
            add(value)
            break
    start = p.get("start", "").strip()
    if start:
        add("ASAP start" if start == "asap" else f"{start} start")
    status = p.get("status", "").strip() if not _is_guest(request) else ""
    if status:
        add(f"status: {status}")
    if not _is_guest(request) and p.get("fav", "").strip() in ("1", "true", "on"):
        add("starred")
    if facts:
        return "Matches active view: " + " · ".join(facts[:3])

    # Default Browse has no positive filter to cite. Show the row's explicit
    # context instead, keeping the line useful without inventing relevance.
    add(_area_label(job.get("area")) if job.get("area") else "")
    add(job.get("desk"))
    if job.get("job_type") == "graduate-programme":
        add("graduate programme")
    elif job.get("job_type") == "internship":
        add("internship")
    add(job.get("loc_city") or job.get("loc_country") or job.get("location"))
    if not facts:
        add(f"direct from {job.get('company')} careers")
    return "Browse context: " + " · ".join(facts[:3])


def _select_job(db: JobDB, jobs: list[dict], request: Request) -> dict | None:
    """The job shown in the desktop detail pane on a full page load: the `sel`
    query param if it points at a still-visible role, else the first row."""
    sel = request.query_params.get("sel", "").strip()
    if sel and any(str(j.get("id")) == sel for j in jobs):
        # List rows carry no description; the pane needs the full row.
        return _full_detail(db, _prepare_detail(db.get_job(sel)), request)
    if jobs:
        return _full_detail(
            db, _prepare_detail(db.get_job(jobs[0]["id"]) or dict(jobs[0])), request)
    return None


def _full_detail(db: JobDB, job: dict, request: Request) -> dict:
    """Everything the detail pane shows, for both ways it is rendered.

    Browse draws the selected role into the page on a full load and swaps
    others in through job_detail; on 2026-09-17 only the second path had the
    application limit, the inbox and the waiting mail, so a reloaded page
    showed a role with none of them while a clicked one showed all three."""
    job["application_workflow"] = _workflow_view(
        db.get_application_workflow_for_job(job["id"]), request, db
    )
    _attach_inbox(db, job)
    job["lim_view"] = _limit_for(db, job.get("company", ""))
    # Owner-only, and decided here rather than in the template: a guest's full
    # Browse page renders the selected role through this function too, and the
    # panel exposes his application history.
    job["company_ctx"] = None if _is_guest(request) else _company_context(db, job)
    return job


RELEVANT_ROLES_SHOWN = 6


def _company_context(db: JobDB, job: dict) -> dict | None:
    """What he has done at this firm, and what else it has open that fits.

    Two deliberately separate lists (his request of 2026-09-23): the history
    of applications, including delisted postings, and a short, collapsed list
    of open roles matching this role's area, job type, desk and location. It
    never lists every opening at the firm; a link does that."""
    firm = plain_company(job.get("company", ""))
    if not firm:
        return None
    aliases = db.company_aliases(job.get("company", "")) or [job.get("company", "")]
    apps = db.company_applications(aliases)
    counts = {}
    for a in apps:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
        a["city"] = a.get("loc_city") or a.get("location") or ""
        a["date"] = (a.get("applied_at") or "")[:10]
        a["is_current"] = a["id"] == job.get("id")
    relevant = db.relevant_company_roles(aliases, job, limit=RELEVANT_ROLES_SHOWN)
    for r in relevant:
        r["city"] = r.get("loc_city") or r.get("location") or ""
    return {
        "firm": firm,
        "aliases": aliases,
        "applications": apps,
        "status_counts": [(st, counts[st]) for st in APPLIED_STATUSES if st in counts],
        "relevant": relevant,
        # Exactly what the link opens: Browse's default view filtered to the
        # firm, so the number on the link is the number of rows he will see.
        "open_total": len(db.fetch_jobs(firm=firm, hide_other=True, hide_yoe=True,
                                        hide_associates=True, hide_delisted=True)),
        "all_url": "/?" + urllib.parse.urlencode({"firm": firm}),
    }


def _prepare_detail(job: dict) -> dict:
    """Clean the stored description and render it to structured HTML for display."""
    cleaned = _clean_desc(job.get("description"))
    job["description"] = cleaned
    job["description_html"] = format_description(
        cleaned, job.get("title"), job.get("company"))
    return job


def _tabs_for(request: Request, db: JobDB, filters: dict,
              base_path: str = "/") -> list[dict]:
    """Area-tab dicts with badge counts that respect the active secondary
    filters (everything except the area/desk being drilled into, hide_other,
    and sort)."""
    count_filters = {k: v for k, v in filters.items()
                     if k not in ("area", "areas", "desk", "hide_other", "sort")}
    return _build_tabs(request, db.area_counts(**count_filters), base_path)


def _build_tabs(request: Request, counts: dict, base_path: str = "/") -> list[dict]:
    """One tab per area with a count badge, preserving the other active query
    params. 'All' = every finance area (excludes 'other')."""
    params = _browse_params(request)
    current = params.get("area", "")
    # 'All' = everything the default view shows: every area except 'other'
    # (untagged '' rows ARE shown, so they're counted here too).
    all_finance = sum(v for a, v in counts.items() if a != "other")
    tabs = []
    for area, label in AREA_TABS:
        p = {k: v for k, v in params.items() if k != "area"}
        if area:
            p["area"] = area
            n = _group_count(counts, area)
        else:
            n = all_finance
        url = base_path + "?" + urllib.parse.urlencode(p) if p else base_path
        tabs.append({"label": label, "count": n, "url": url,
                     "area": area, "active": current == area})
    return tabs


# --- Routes ---
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", {"request": request, "error": None})


# Login rate limit — the app is Funnel-exposed to the public internet. This is
# a single-user app, so a plain GLOBAL sliding window is enough and can't be
# dodged by rotating IPs (client addresses behind Funnel are unreliable anyway;
# they're logged best-effort for forensics only). In-memory: a restart resets
# it, which is fine — the budget is per-window, not cumulative.
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 10
_login_failures: list[float] = []


def _login_blocked() -> bool:
    import time as _time
    now = _time.monotonic()
    _login_failures[:] = [t for t in _login_failures
                          if now - t < _LOGIN_WINDOW_SECONDS]
    return len(_login_failures) >= _LOGIN_MAX_FAILURES


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form("")):
    import time as _time
    if _login_blocked():
        return TEMPLATES.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Too many attempts — try again later."},
            status_code=429,
        )
    user = username.strip().lower()
    if (WEB_PASSWORD and user == WEB_USER
            and secrets.compare_digest(password, WEB_PASSWORD)):
        request.session["auth"] = True
        request.session["role"] = "owner"
        return RedirectResponse("/", status_code=303)
    if (WEB_GUEST_PASSWORD and user == WEB_GUEST_USER
            and secrets.compare_digest(password, WEB_GUEST_PASSWORD)):
        request.session["auth"] = True
        request.session["role"] = "guest"
        return RedirectResponse("/", status_code=303)
    _login_failures.append(_time.monotonic())
    client = getattr(request.client, "host", "?")
    fwd = request.headers.get("x-forwarded-for", "")
    # Stamped, because an unstamped line cannot say when a probe happened: on
    # 2026-09-18 the only clock for 38 failures was the log file's mtime.
    print(f"AUTH {datetime.now(timezone.utc).isoformat(timespec='seconds')}: "
          f"failed login attempt #{len(_login_failures)} in window "
          f"(client={client} user={user!r} xff={fwd!r})", flush=True)
    # One generic error for both fields so probing can't tell which was wrong.
    return TEMPLATES.TemplateResponse(
        request, "login.html",
        {"request": request, "error": "Wrong username or password."}, status_code=401
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _scan_stale_days(db: JobDB) -> int:
    """Days since the last successful scan touched any row (MAX(last_seen)),
    0 if fresh (<2 days). The browse page is the one surface the user looks
    at daily, so a banner here is the practical dead-man's-switch for a
    silently stopped launchd schedule — the weekly selfcheck alone leaves up
    to 7 blind days."""
    try:
        row = db.conn.execute("SELECT MAX(last_seen) FROM seen_jobs").fetchone()
        if not row or not row[0]:
            return 0
        newest = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - newest).days
        return age if age >= 2 else 0
    except Exception:
        return 0


# ── Application limits ───────────────────────────────────────────────────────
# A firm's cap on applications per recruitment cycle, researched by
# applications/limits.py or typed in by hand when the application form states
# one. See db.company_limits for the accuracy contract; the UI's job is to
# never present an unsourced number as if it were sourced, which is why the
# quote and the source link ride along with every stated cap.

def _cycle_window() -> str:
    """Rolling 12 months. Firms word their cycle differently ("recruitment
    year", "hiring year", "season") and none of them align to the calendar, so
    a rolling year is the closest honest proxy — labelled as such in the UI
    rather than presented as the firm's own accounting."""
    return (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()


def _limit_view(limit: dict | None, used_recent: int, used_total: int) -> dict:
    """Everything the templates need for one company, with the arithmetic done
    here so a Jinja expression can't quietly invent a remaining count for a cap
    we don't actually know."""
    row = limit or {}
    form = row.get("form_verdict") or ""
    if form == "stated" and not row.get("varies_by_region"):
        # The application form itself stated the cap: it outranks anything the
        # research pass read off a careers site, so it replaces the figure and
        # the wording rather than sitting beside them.
        cap, quote, cycle, strength, source = (
            row.get("form_max"), row.get("form_quote") or "", "", "", "")
        varies, proof = False, "form"
    elif form == "stated":
        # A form states the rule for the region it recruits in, and a firm that
        # sets different allowances per region cannot be summed up by that one
        # sentence. UBS's Frankfurt form said "one program per academic year"
        # for EMEA excluding Switzerland and the UK, and replacing the FAQ's
        # regional rule with it told him he had one application worldwide.
        cap, quote, cycle, strength, source = (
            row.get("max_per_cycle"), row.get("quote") or "", row.get("cycle") or "",
            row.get("strength") or "", row.get("source_url") or "")
        varies, proof = True, "form_region"
    else:
        cap, quote, cycle, strength, source = (
            row.get("max_per_cycle"), row.get("quote") or "", row.get("cycle") or "",
            row.get("strength") or "", row.get("source_url") or "")
        varies = bool(row.get("varies_by_region"))
        # A run went through the whole form and it said nothing: the researched
        # figure stays, marked unconfirmed, because an FAQ can state a rule the
        # form omits.
        proof = "unconfirmed" if form == "absent" else ""
    confidence = row.get("confidence") or "unchecked"
    remaining = None if cap is None else max(0, cap - used_recent)
    return {
        "limit": limit,
        "cap": cap,
        "quote": quote,
        "cycle": cycle,
        "strength": strength,
        "source_url": source,
        "varies": varies,
        "proof": proof,
        "form_checked": (row.get("form_checked_at") or "")[:10],
        "form_quote": row.get("form_quote") or "",
        "confidence": confidence,
        "known": cap is not None,
        "checked": limit is not None,
        "used_recent": used_recent,
        "used_total": used_total,
        "remaining": remaining,
        "at_cap": cap is not None and used_recent >= cap,
    }




def _limit_key(company: str) -> str:
    """Normalised firm key for matching a cap to a role.

    An application cap is a property of the FIRM, but seen_jobs.company holds
    the scraper's source label, and the same firm can appear under several:
    "UBS" and "UBS (Graduate Careers)", "Nomura" and "Nomura (Campus)". The cap
    is recorded once, against whichever label the research pass ran under, and
    resolved to the others here — an alias at display time rather than a second
    copy of the fact, so there is nothing to keep in sync."""
    return plain_company(company).lower()


def _resolve_limit(limits: dict[str, dict], company: str) -> dict | None:
    """Exact company match first, then the normalised firm key."""
    if company in limits:
        return limits[company]
    key = _limit_key(company)
    for name, row in limits.items():
        if _limit_key(name) == key:
            return row
    return None


def _limits_map(db: JobDB) -> dict[str, dict]:
    """Per-company limit views for a whole page of roles — three queries total,
    never one per row."""
    limits = db.all_company_limits()
    recent = db.applied_counts_by_company(since=_cycle_window())
    total = db.applied_counts_by_company()
    companies = set(limits) | set(recent) | set(total)
    return {c: _limit_view(_resolve_limit(limits, c), recent.get(c, 0),
                           total.get(c, 0))
            for c in companies}


def _limit_for(db: JobDB, company: str) -> dict:
    return _limit_view(_resolve_limit(db.all_company_limits(), company),
                       db.applied_counts_by_company(since=_cycle_window()).get(company, 0),
                       db.applied_counts_by_company().get(company, 0))


def _tri(raw: str) -> int | None:
    return {"yes": 1, "no": 0}.get((raw or "").strip().lower())


WHY_PREVIEW_PATH = "/preview/why-this-is-here"
WHY_PREVIEW_PARTIAL_PATH = WHY_PREVIEW_PATH + "/partials/jobs"


def _browse_context(request: Request, db: JobDB, *, why_preview: bool = False) -> dict:
    browse_path = WHY_PREVIEW_PATH if why_preview else "/"
    partial_path = WHY_PREVIEW_PARTIAL_PATH if why_preview else "/partials/jobs"
    filters = _filters_from_request(request, allow_text_search=False)
    facets = _facets(db, request, filters)
    _reconcile_location(filters, facets)
    jobs = db.fetch_jobs(limit=PAGE_LIMIT, **filters)
    if why_preview:
        jobs = [{**job, "browse_reason": _browse_reason(job, request)} for job in jobs]
    tabs = _tabs_for(request, db, filters, browse_path)
    show_unread = not _is_guest(request)
    last_seen = db.get_meta("last_seen_ts", LAST_SEEN_DEFAULT) if show_unread else ""
    return {
        "request": request,
        "scan_stale_days": _scan_stale_days(db),
        "jobs": jobs,
        "facets": facets,
        "params": _browse_params(request),
        "count": len(jobs),
        "truncated": len(jobs) >= PAGE_LIMIT,
        "tabs": tabs,
        "chips": _active_chips(request, browse_path),
        "sel_job": _select_job(db, jobs, request),
        # The selected role's pane carries forms (Attach, launch) that need it.
        "csrf_token": _csrf_token(request),
        "last_seen": last_seen,
        "new_count": db.count_new(last_seen) if show_unread else 0,
        "show_unread": show_unread,
        "limits": _limits_map(db),
        "firm_names": db.firm_names(),
        "active_nav": "browse",
        "why_preview": why_preview,
        "browse_path": browse_path,
        "browse_partial_path": partial_path,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    # Free text looked like a location search but only matched title/company.
    # Strip legacy q links rather than leaving an invisible active filter after
    # the search box's removal. The JSON API retains q for programmatic use.
    forbidden = {"q"}
    if _is_guest(request):
        forbidden.update(("status", "fav"))
    if any(key in request.query_params for key in forbidden):
        clean = [(k, v) for k, v in request.query_params.multi_items()
                 if k not in forbidden]
        suffix = urllib.parse.urlencode(clean)
        return RedirectResponse("/?" + suffix if suffix else "/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "index.html", _browse_context(request, db)
    )


@app.get(WHY_PREVIEW_PATH, response_class=HTMLResponse)
def why_this_is_here_preview(request: Request, db: JobDB = Depends(get_db),
                             _=Depends(require_login)):
    """Opt-in, read-only presentation preview over the normal Browse query."""
    return TEMPLATES.TemplateResponse(
        request, "index.html", _browse_context(request, db, why_preview=True)
    )


@app.get("/partials/jobs", response_class=HTMLResponse)
def partial_jobs(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    filters = _filters_from_request(request, allow_text_search=False)
    facets = _facets(db, request, filters)
    _reconcile_location(filters, facets)
    jobs = db.fetch_jobs(limit=PAGE_LIMIT, **filters)
    show_unread = not _is_guest(request)
    last_seen = db.get_meta("last_seen_ts", LAST_SEEN_DEFAULT) if show_unread else ""
    return TEMPLATES.TemplateResponse(request, "_jobs_partial.html", {
        "request": request, "jobs": jobs, "count": len(jobs),
        "truncated": len(jobs) >= PAGE_LIMIT,
        "tabs": _tabs_for(request, db, filters),
        "chips": _active_chips(request),
        "facets": facets,
        "params": _browse_params(request),
        "last_seen": last_seen,
        "new_count": db.count_new(last_seen) if show_unread else 0,
        "show_unread": show_unread,
        "limits": _limits_map(db),
    })


@app.get(WHY_PREVIEW_PARTIAL_PATH, response_class=HTMLResponse)
def why_this_is_here_partial(request: Request, db: JobDB = Depends(get_db),
                             _=Depends(require_login)):
    context = _browse_context(request, db, why_preview=True)
    context.pop("scan_stale_days", None)
    context.pop("sel_job", None)
    return TEMPLATES.TemplateResponse(request, "_jobs_partial.html", context)


@app.post("/seen")
def mark_seen(request: Request, db: JobDB = Depends(get_db), _=Depends(require_owner)):
    db.set_meta("last_seen_ts", datetime.now(timezone.utc).isoformat())
    return RedirectResponse("/", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, db: JobDB = Depends(get_db),
               _=Depends(require_login)):
    job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    _full_detail(db, _prepare_detail(job), request)
    # ?pane=1 → the bare detail-pane fragment loaded into the Browse split view
    # (and the mobile bottom sheet) via htmx. Otherwise the standalone page.
    lim = job["lim_view"]
    if request.query_params.get("pane"):
        return TEMPLATES.TemplateResponse(request, "_detail.html", {
            "request": request, "job": job, "lim": lim,
            "csrf_token": _csrf_token(request),
        })
    return TEMPLATES.TemplateResponse(request, "job.html", {
        "request": request, "job": job, "lim": lim, "active_nav": "browse",
        "csrf_token": _csrf_token(request),
    })


def _mail_firm(match_reason: str) -> str:
    """The firm a queued message was narrowed to, read from its match reason:
    "untracked role at X" (one firm, no tracked role) or "model picked one of N
    X roles; the mail names none" (several roles at one firm). Empty when the
    pass could not name a firm."""
    suggested = match_reason.split("untracked role at ", 1)
    if len(suggested) == 2:
        return suggested[1].strip()
    ambiguous = re.search(r"picked one of \d+ (.+?) roles;", match_reason)
    return ambiguous.group(1).strip() if ambiguous else ""


def _waiting_mail_for(db: JobDB, company: str) -> list[dict]:
    """Recruiting mail the nightly pass could not attach, suggested for this
    firm. The pass names the firm in its match reason ("untracked role at X")
    when the mail matched one firm but not one role, which is DRW's and
    Goldman's case on 2026-09-17: two open roles, and a mail naming neither."""
    key = _limit_key(company)
    waiting = []
    # SocGen's Hong Kong confirmation of 2026-09-18 carried the second form, so
    # setting the role to applied by hand left the mail queued beside it.
    for item in db.pending_application_mail():
        firm = _mail_firm(item["match_reason"])
        if firm and _limit_key(firm) == key:
            waiting.append(item)
    return waiting


def _attach_inbox(db: JobDB, job: dict) -> dict:
    """Everything the role pane's inbox card needs.

    The inbox trail and any employer cool-down belong on the role itself: a
    role applied to by hand has no workflow, so Flow Traders' rejection and its
    12-month lockout (2026-09-17) had nowhere else to show. Mail from the same
    firm still waiting for a role is listed there too, so it is attached where
    he is looking rather than left behind on /applications."""
    job["inbox"] = db.application_mail_for_job(job["id"])
    key = _limit_key(job.get("company", ""))
    job["cooldowns"] = [
        c for c in db.active_reapply_cooldowns(datetime.now(timezone.utc).date().isoformat())
        if _limit_key(c["company"]) == key
    ]
    job["waiting_mail"] = _waiting_mail_for(db, job.get("company", ""))
    return job


def _cap_warning(db: JobDB, job: dict, workflow_id: str) -> dict | None:
    """What to say before he spends a capped firm's one allowance twice.

    bp states one early-careers application per academic year, Jobfeed had that
    recorded verbatim since 2026-09-07, and it still said nothing when a second
    bp workflow was started by misclick on 2026-09-12. The cap was known; the
    moment of action was not where it was shown."""
    view = _limit_for(db, job.get("company", ""))
    if not view["known"] or (view["strength"] != "hard" and view["proof"] != "form"):
        return None
    key = _limit_key(job.get("company", ""))
    others = [
        w for w in db.workflows_by_company()
        if w["workflow_id"] != workflow_id and _limit_key(w["company"]) == key
    ]
    if not others and not view["at_cap"]:
        return None
    return {"view": view, "others": others, "quote": view["quote"]}


def _application_panel(request: Request, db: JobDB, job: dict, workflow: dict):
    job = _prepare_detail(db.get_job(job["id"]) or job)
    job["application_workflow"] = _workflow_view(workflow, request, db)
    job["cap_warning"] = _cap_warning(db, job, workflow["workflow_id"])
    run = db.open_application_run(workflow["workflow_id"])
    ahead = 0
    if run and run["state"] == "queued":
        order = [r["run_id"] for r in db.list_application_runs(states=("queued",))]
        ahead = order.index(run["run_id"]) + (1 if db.active_application_run() else 0)
    job["application_run"] = run
    job["queue_ahead"] = ahead
    job["autopilot"] = db.autopilot_settings()
    response = TEMPLATES.TemplateResponse(request, "_apply_panel.html", {
        "request": request, "job": job, "csrf_token": _csrf_token(request),
    })
    response.headers["HX-Trigger"] = json.dumps({
        "applicationWorkflowChanged": {
            "job_id": job["id"], "crm_status": job["status"]
        }
    })
    return response


@app.post("/job/{job_id}/apply", response_class=HTMLResponse)
def queue_application(job_id: str, request: Request,
                      db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Idempotently prepare the attended Antigravity handoff for one role."""
    job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    application_url = _safe_url(job.get("url"))
    if not application_url:
        return HTMLResponse("This role has no safe application URL", status_code=400)
    workflow, _created = db.create_application_workflow(job_id, application_url)
    return _application_panel(request, db, job, workflow)


@app.get("/application/{workflow_id}/panel", response_class=HTMLResponse)
def application_panel(workflow_id: str, request: Request,
                      db: JobDB = Depends(get_db), _=Depends(require_owner)):
    workflow = db.get_application_workflow(workflow_id)
    if workflow is None:
        return HTMLResponse("Not found", status_code=404)
    job = db.get_job(workflow["job_id"])
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    return _application_panel(request, db, job, workflow)


@app.post("/application/{workflow_id}/launch", response_class=HTMLResponse)
async def launch_application(workflow_id: str, request: Request,
                             db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Queue a fixed workflow ID only; all message content is server-resolved.

    Every launch goes through the serial autopilot (applications/autopilot.py): this queues
    an attempt ahead of automatic ones and starts it at once if no browser
    agent holds the worker lease, otherwise when the current agent is
    confirmed stopped. Clicking Auto apply on several roles therefore fills
    them one after another instead of starting agents that share a cursor."""
    form = await request.form()
    if set(form.keys()) != {"csrf_token"} or not _valid_csrf(request, str(form.get("csrf_token", ""))):
        return HTMLResponse("invalid launch request", status_code=403)
    current = db.get_application_workflow(workflow_id)
    if current is None:
        return HTMLResponse("Not found", status_code=404)
    if current["status"] == "completed":
        job = db.get_job(current["job_id"])
        return _application_panel(request, db, job, current)
    db.queue_application_run(workflow_id, manual=True, priority=10)
    _autopilot_tick(db)
    workflow = db.get_application_workflow(workflow_id)
    job = db.get_job(workflow["job_id"])
    return _application_panel(request, db, job, workflow)


def _agent_runs() -> list[dict]:
    """Antigravity's per-conversation facts; a seam tests replace."""
    return antigravity_runs.all_runs()


def _autopilot_tick(db: JobDB) -> list[str]:
    """One scheduler step. Resolves `enqueue_launch` at call time, so tests
    that patch it here see their patch."""
    try:
        return autopilot.tick(db, runs=_agent_runs(),
                              launch=lambda workflow: enqueue_launch(workflow))
    except Exception as exc:  # never let the scheduler take a page down
        print(f"AUTOPILOT tick failed: {type(exc).__name__}: {exc}", flush=True)
        return []


def _autopilot_view(db: JobDB) -> dict:
    """Queue state for /applications and the role panel."""
    settings = db.autopilot_settings()
    active = db.active_application_run()
    queued = db.list_application_runs(states=("queued",))

    def label(run):
        wf = db.get_application_workflow(run["workflow_id"]) or {}
        job = db.get_job(wf.get("job_id", "")) or {}
        return dict(run, company=job.get("company", ""), title=job.get("title", ""),
                    job_id=wf.get("job_id", ""))

    return {
        "settings": settings,
        "active": label(active) if active else None,
        "queued": [label(r) for r in queued],
        "recent": [label(r) for r in db.list_application_runs(
            states=("review_ready", "parked", "failed"), limit=8)],
    }


def _owner_form_ok(form, request: Request, allowed: set[str]) -> bool:
    return (set(form.keys()) <= allowed | {"csrf_token"}
            and _valid_csrf(request, str(form.get("csrf_token", ""))))


@app.post("/autopilot")
async def autopilot_control(request: Request, db: JobDB = Depends(get_db),
                            _=Depends(require_owner)):
    """Pause or resume the queue. Resume also clears a safety pause, which is
    the owner's explicit statement that he has looked at its reason."""
    form = await request.form()
    if not _owner_form_ok(form, request, {"action"}):
        return HTMLResponse("invalid request", status_code=403)
    action = str(form.get("action", ""))
    if action == "pause":
        db.set_autopilot(enabled=False)
    elif action == "resume":
        db.set_autopilot(enabled=True, paused_reason="")
        _autopilot_tick(db)
    else:
        return HTMLResponse("bad action", status_code=400)
    return RedirectResponse("/applications#autopilot", status_code=303)


@app.post("/application-run/{run_id}/cancel")
async def cancel_application_run(run_id: str, request: Request, db: JobDB = Depends(get_db),
                                 _=Depends(require_owner)):
    form = await request.form()
    if not _owner_form_ok(form, request, set()):
        return HTMLResponse("invalid request", status_code=403)
    autopilot.cancel_queued(db, run_id)
    return RedirectResponse("/applications#autopilot", status_code=303)


@app.post("/application-run/{run_id}/confirm-stopped")
async def confirm_application_run_stopped(run_id: str, request: Request,
                                          db: JobDB = Depends(get_db),
                                          _=Depends(require_owner)):
    """His statement that the run's browser agent is stopped, for when
    Antigravity produced no kill/list evidence. Recorded as owner_confirmed."""
    form = await request.form()
    if not _owner_form_ok(form, request, set()):
        return HTMLResponse("invalid request", status_code=403)
    autopilot.confirm_stopped(db, run_id)
    _autopilot_tick(db)
    return RedirectResponse("/applications#autopilot", status_code=303)


_AUTOPILOT_THREAD_STARTED = False


@app.on_event("startup")
def _start_autopilot_worker() -> None:
    """Tick the queue every 15 seconds for the life of the web service. The
    tick is stateless, so a restart simply resumes. Tests build the app without
    running startup events, and JOBFEED_AUTOPILOT_WORKER=0 turns it off."""
    global _AUTOPILOT_THREAD_STARTED
    if _AUTOPILOT_THREAD_STARTED or os.environ.get("JOBFEED_AUTOPILOT_WORKER") == "0":
        return
    _AUTOPILOT_THREAD_STARTED = True
    import threading
    import time

    def loop():
        while True:
            try:
                db = JobDB(DB_FILE, check_same_thread=False)
                try:
                    for line in _autopilot_tick(db):
                        print(f"AUTOPILOT {datetime.now(timezone.utc).isoformat(timespec='seconds')}: {line}",
                              flush=True)
                finally:
                    db.conn.close()
            except Exception as exc:
                print(f"AUTOPILOT worker error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(15)

    threading.Thread(target=loop, name="autopilot", daemon=True).start()
    print("AUTOPILOT worker started (tick every 15s)", flush=True)


def _elapsed(start: str, end: str) -> str:
    """Human gap between two ISO stamps, or '' when either is missing."""
    if not start or not end:
        return ""
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return ""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m {seconds % 60}s"
    hours, rest = divmod(seconds, 3600)
    if hours < 48:
        return f"{hours}h {rest // 60}m"
    return f"{hours // 24}d {hours % 24}h"




def _workflow_timeline(workflow: dict) -> list[dict]:
    """What happened to this application and how long each leg took. The stamps
    are already on the row; nothing new is recorded to render this."""
    steps = [
        ("queued", workflow.get("created_at", "")),
        ("launch requested", workflow.get("launch_requested_at", "")),
        ("agent started", workflow.get("started_at", "")),
        ("review ready", workflow.get("review_ready_at", "")),
        ("employer confirmed", workflow.get("submitted_confirmed_at", "")),
        ("completed", workflow.get("completed_at", "")),
    ]
    stamped = [(label, at) for label, at in steps if at]
    # A run parked at needs_user_action or failed has no stamp of its own, so
    # the legs above stop at "agent started" and the total claimed 19s for a bp
    # run that had been going 41 minutes. The last update closes the line.
    last = workflow.get("updated_at", "")
    if last and workflow.get("status") not in ("completed",) and (
        not stamped or last > stamped[-1][1]
    ):
        # Labelled "last update" rather than by status: the status is already the
        # badge on the card, and a queued row would otherwise show two legs
        # both called queued.
        stamped.append(("last update", last))
    out = []
    for index, (label, at) in enumerate(stamped):
        previous = stamped[index - 1][1] if index else ""
        # The date is shown once, on the first leg: repeating it on every row
        # buried the only numbers worth reading.
        out.append({"label": label, "at": at[11:16], "date": at[:10],
                    "show_date": index == 0, "since": _elapsed(previous, at)})
    if len(stamped) > 1:
        out.append({"label": "total", "at": "", "date": "", "show_date": False,
                    "since": _elapsed(stamped[0][1], stamped[-1][1])})
    return out


def _parse_workflow_stamp(value: str) -> datetime | None:
    """Parse one stored workflow stamp as UTC, rejecting malformed values."""
    try:
        stamp = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _short_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    if not minutes:
        return f"{secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"


def _application_efficiency(workflows: list[dict], *, now: datetime | None = None) -> dict:
    """Agent-run outcomes without counting queue time or human submission time.

    A success needs both the first `in_progress` stamp and the first
    `review_ready` stamp. A failure is an agent-started workflow whose current
    durable outcome is failed and which never reached review. Open/blocked runs
    stay out of the rate denominator rather than being called successes or
    failures prematurely.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    successes: list[tuple[datetime, float]] = []
    failures: list[datetime] = []
    for workflow in workflows:
        started = _parse_workflow_stamp(workflow.get("started_at", ""))
        ready = _parse_workflow_stamp(workflow.get("review_ready_at", ""))
        if started and ready and ready >= started:
            successes.append((ready, (ready - started).total_seconds()))
            continue
        if (started and workflow.get("status") == "failed"
                and not workflow.get("review_ready_at")):
            failed_at = _parse_workflow_stamp(workflow.get("updated_at", ""))
            if failed_at and failed_at >= started:
                failures.append(failed_at)

    def window(cutoff: datetime | None) -> dict:
        durations = [duration for at, duration in successes
                     if cutoff is None or at >= cutoff]
        failed = sum(1 for at in failures if cutoff is None or at >= cutoff)
        resolved = len(durations) + failed
        ordered = sorted(durations)
        median = None
        if ordered:
            middle = len(ordered) // 2
            median = (ordered[middle] if len(ordered) % 2
                      else (ordered[middle - 1] + ordered[middle]) / 2)
        return {
            "reached_review": len(durations),
            "median": _short_duration(median),
            "fastest": _short_duration(ordered[0] if ordered else None),
            "failure_rate": f"{round(100 * failed / resolved)}%" if resolved else "—",
            "resolved": resolved,
        }

    return {
        "recent": window(now - timedelta(days=7)),
        "all": window(None),
    }


def _application_action_view(action: dict, *, now: datetime | None = None) -> dict:
    """Presentation-only urgency for one stored mail action."""
    item = dict(action)
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    raw = str(item.get("action_deadline") or "").strip()
    due_date = None
    if raw:
        try:
            due_date = datetime.fromisoformat(raw[:10]).date()
        except ValueError:
            pass
    if not raw:
        label, tone, shown = "No due date", "none", ""
    elif due_date is None:
        label, tone, shown = "Due date", "scheduled", raw
    else:
        delta = (due_date - today).days
        shown = due_date.isoformat()
        if delta < 0:
            label, tone = f"Overdue {abs(delta)}d", "overdue"
        elif delta == 0:
            label, tone = "Due today", "urgent"
        elif delta == 1:
            label, tone = "Due tomorrow", "urgent"
        elif delta <= 7:
            label, tone = f"Due in {delta} days", "soon"
        else:
            label, tone = f"Due in {delta} days", "scheduled"
    item.update(due_label=label, due_tone=tone, due_display=shown)
    return item


@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request, db: JobDB = Depends(get_db),
                 _=Depends(require_owner)):
    raw_workflows = db.list_application_workflows()
    # Per-application process numbers from Antigravity's own records, so a run
    # can be judged from the page rather than by an audit (his ask, 2026-09-18).
    runs = antigravity_runs.by_workflow(antigravity_runs.all_runs())
    workflows = []
    run_stats = []
    for workflow in raw_workflows:
        view = _workflow_view(workflow, request)
        view["run_stats"] = antigravity_runs.workflow_stats(
            runs.get(workflow["workflow_id"], []))
        run_stats.append(view["run_stats"])
        view["timeline"] = _workflow_timeline(workflow)
        view["mail"] = db.application_mail_for_job(workflow["job_id"])
        resume_url = safe_application_url(workflow.get("application_url", ""))
        if resume_url and workflow["status"] in ("in_progress", "needs_user_action"):
            view["primary_action"] = {"url": resume_url, "label": "Resume application"}
        else:
            view["primary_action"] = None
        workflows.append(view)
    pending = []
    for item in db.pending_application_mail():
        # The suggestion is a company name the nightly pass found on the whole
        # board; the role is his to pick, since a firm has many open rows.
        # A message the model could not narrow to one of several roles at a firm
        # names that firm in its reason, so those roles are the choice. Anything
        # else falls back to every role he has acted on: an empty picker is a
        # dead end, and on 2026-09-18 SocGen's confirmation for the Hong Kong
        # trainee role sat behind exactly that, beside the role it belonged to.
        company = _mail_firm(item["match_reason"])
        item["roles"] = db.roles_for_company(company) if company else []
        item["any_role"] = not item["roles"]
        if item["any_role"]:
            item["roles"] = db.open_application_roles()
        pending.append(item)
    return TEMPLATES.TemplateResponse(request, "applications.html", {
        "request": request,
        "workflows": workflows,
        "efficiency": _application_efficiency(raw_workflows),
        "process": antigravity_runs.overview(run_stats),
        "queue": _autopilot_view(db),
        "actions": [_application_action_view(a) for a in db.open_application_actions()],
        "pending_mail": pending,
        "recent_mail": db.recent_application_mail(
            (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
        # Mail from an employer that the classifier declined. It moves nothing on
        # its own, but without a surface there was no way to place it by hand.
        "unplaced_mail": [
            dict(m, roles=(db.roles_for_company(m["company"]) if m["company"]
                           else db.open_application_roles()),
                 any_role=not m["company"])
            for m in db.unplaced_employer_mail(
                (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        ],
        "csrf_token": _csrf_token(request),
        "active_nav": "applications",
    })


@app.post("/application-mail/{message_key}/adopt", response_class=HTMLResponse)
async def adopt_application_mail(message_key: str, request: Request,
                                 db: JobDB = Depends(get_db),
                                 _=Depends(require_owner)):
    """Attach a queued message to a role. The status comes from the stored
    message, never from the form, so only the role is caller-supplied."""
    form = await request.form()
    if set(form.keys()) != {"csrf_token", "job_id"} or not _valid_csrf(
        request, str(form.get("csrf_token", ""))
    ):
        return HTMLResponse("invalid request", status_code=403)
    job_id = str(form.get("job_id", ""))
    if not db.adopt_application_mail(message_key, job_id):
        return HTMLResponse("Not found", status_code=404)
    # Attached from the role pane: hand back that role's refreshed inbox card
    # instead of leaving Browse for /applications.
    if request.headers.get("HX-Request") == "true":
        job = db.get_job(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        _attach_inbox(db, job)
        response = TEMPLATES.TemplateResponse(request, "_inbox_note.html", {
            "request": request, "job": job, "csrf_token": _csrf_token(request),
        })
        response.headers["HX-Trigger"] = json.dumps(
            {"applicationWorkflowChanged": {"job_id": job_id, "crm_status": job["status"]}})
        return response
    return RedirectResponse("/applications", status_code=303)


@app.post("/application-mail/{message_key}/dismiss", response_class=HTMLResponse)
async def dismiss_application_mail(message_key: str, request: Request,
                                   db: JobDB = Depends(get_db),
                                   _=Depends(require_owner)):
    form = await request.form()
    if set(form.keys()) != {"csrf_token"} or not _valid_csrf(
        request, str(form.get("csrf_token", ""))
    ):
        return HTMLResponse("invalid request", status_code=403)
    db.resolve_application_mail(message_key)
    return RedirectResponse("/applications", status_code=303)


@app.post("/application/{workflow_id}/status", response_class=HTMLResponse)
def set_application_status(workflow_id: str, request: Request,
                           status: str = Form(...), detail: str = Form(""),
                           db: JobDB = Depends(get_db), _=Depends(require_owner)):
    if status not in APPLICATION_WORKFLOW_STATES:
        return HTMLResponse("bad workflow status", status_code=400)
    try:
        workflow = db.transition_application_workflow(
            workflow_id, status, detail=detail, actor="owner"
        )
    except KeyError:
        return HTMLResponse("Not found", status_code=404)
    except (ValueError, PermissionError) as exc:
        return HTMLResponse(str(exc), status_code=409)
    job = db.get_job(workflow["job_id"])
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    return _application_panel(request, db, job, workflow)


def _agent_workflow_response(request: Request, db: JobDB, workflow: dict,
                             token: str, *, message: str = "",
                             status_code: int = 200):
    job = db.get_job(workflow["job_id"])
    run = db.open_application_run(workflow["workflow_id"])
    response = TEMPLATES.TemplateResponse(request, "application_agent.html", {
        "request": request,
        "workflow": workflow,
        "job": job,
        "token": token,
        "run_id": (run["run_id"] if run and run["state"] != "queued" else ""),
        "agent_states": AGENT_WORKFLOW_STATES,
        "message": message,
    }, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    # The agent reads this over a one-line CLI, not in a browser, so the reason
    # a report was refused has to leave the page. Without it a 409 is a bare
    # number: the BNP run on 2026-09-16 read web/app.py and jobfeed/db.py for seven
    # minutes, and raised two permission prompts, to learn what this says.
    response.headers["X-Jobfeed-Message"] = " ".join(message.split())[:200]
    return response


@app.get("/application/{workflow_id}/agent", response_class=HTMLResponse)
def agent_application_status(workflow_id: str, request: Request, token: str = "",
                             db: JobDB = Depends(get_db)):
    workflow = db.get_application_workflow(workflow_id)
    if workflow is None or not _valid_workflow_token(workflow_id, token):
        return HTMLResponse("Not found", status_code=404)
    return _agent_workflow_response(request, db, workflow, token)


@app.post("/application/{workflow_id}/agent/status", response_class=HTMLResponse)
def write_agent_application_status(workflow_id: str, request: Request,
                                   token: str = Form(""), status: str = Form(...),
                                   detail: str = Form(""),
                                   run_id: str = Form(""),
                                   limit_verdict: str = Form(""),
                                   limit_max: str = Form(""),
                                   limit_quote: str = Form(""),
                                   db: JobDB = Depends(get_db)):
    workflow = db.get_application_workflow(workflow_id)
    if workflow is None or not _valid_workflow_token(workflow_id, token):
        return HTMLResponse("Not found", status_code=404)
    if status not in AGENT_WORKFLOW_STATES:
        return _agent_workflow_response(
            request, db, workflow, token,
            message="That state is owner-only.", status_code=403,
        )
    open_run = db.open_application_run(workflow_id)
    if open_run is not None and run_id != open_run["run_id"]:
        return _agent_workflow_response(
            request, db, workflow, token,
            message="This status report does not identify the active application attempt.",
            status_code=409,
        )
    if run_id and (open_run is None or run_id != open_run["run_id"]
                   or open_run["state"] == "queued"):
        return _agent_workflow_response(
            request, db, workflow, token,
            message="This application attempt is no longer active.", status_code=409,
        )
    try:
        workflow = db.transition_application_workflow(
            workflow_id, status, detail=detail, actor="agent"
        )
    except (ValueError, PermissionError) as exc:
        return _agent_workflow_response(
            request, db, workflow, token, message=str(exc), status_code=409
        )
    message = f"Saved: {status.replace('_', ' ')}"
    if limit_verdict:
        message += "; " + _record_form_limit(db, workflow, status, limit_verdict,
                                             limit_max, limit_quote)
    return _agent_workflow_response(request, db, workflow, token, message=message)


def _record_form_limit(db: JobDB, workflow: dict, status: str, verdict: str,
                       raw_max: str, quote: str) -> str:
    """Store the application-limit observation an attended run made on the form.

    Never fails the status transition it rides on: a limit the gate refuses is
    reported back in the message and the workflow state still saves. `absent`
    is only accepted with review_ready, because only a run that has been
    through the whole form can say the form stated nothing."""
    if verdict == "absent" and status != "review_ready":
        return "limit not recorded: 'no cap stated' needs review_ready"
    job = db.get_job(workflow["job_id"])
    if job is None or not job.get("company"):
        return "limit not recorded: role has no company"
    # Record against the firm's existing cap row when one exists under another
    # scraper label, so the form evidence sits beside the researched figure.
    existing = _resolve_limit(db.all_company_limits(), job["company"])
    company = existing["company"] if existing else job["company"]
    try:
        n = int(raw_max) if raw_max else None
        stored = db.record_form_limit(company, verdict=verdict, max_per_cycle=n,
                                      quote=quote,
                                      workflow_id=workflow["workflow_id"])
    except ValueError as exc:
        return f"limit not recorded: {exc}"
    if not stored:
        return "limit kept: an earlier form already stated a cap"
    return f"limit recorded ({stored})"


@app.post("/job/{job_id}/status", response_class=HTMLResponse)
def set_status(job_id: str, request: Request, status: str = Form(...),
               db: JobDB = Depends(get_db), _=Depends(require_owner)):
    if status not in CRM_STATUSES:
        return HTMLResponse("bad status", status_code=400)
    db.set_status(job_id, status)
    job = db.get_job(job_id)
    # A status he sets by hand is also a choice of role, so mail waiting for
    # that firm attaches when the choice is unambiguous: exactly one unattached
    # message proposes the status he just picked. Two candidates, or none with
    # that status, and the mail stays on the role's inbox card for him to
    # attach, because guessing between two roles is the misattribution the
    # nightly pass refuses to make.
    if job is not None and status in ACTED_ON_STATUSES:
        same = [m for m in _waiting_mail_for(db, job.get("company", ""))
                if m["proposed_status"] == status]
        if len(same) == 1:
            db.adopt_application_mail(same[0]["message_key"], job_id)
            job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    return TEMPLATES.TemplateResponse(request, "_status_cell.html", {"request": request, "job": job})


@app.post("/job/{job_id}/favorite")
def toggle_favorite(job_id: str, request: Request,
                    db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Flip the favorite flag and return the re-rendered star button."""
    job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    db.set_favorite(job_id, not job.get("favorite"))
    job = db.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "_star.html", {"request": request, "job": job})


@app.post("/job/{job_id}/notes", response_class=HTMLResponse)
def set_notes(job_id: str, request: Request, notes: str = Form(""),
              db: JobDB = Depends(get_db), _=Depends(require_owner)):
    if not db.set_notes(job_id, notes.strip()):
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse("saved")


@app.post("/company/limit", response_class=HTMLResponse)
def set_company_limit(request: Request, company: str = Form(...),
                      max_per_cycle: str = Form(""), cycle: str = Form(""),
                      locations_count_separately: str = Form(""),
                      shared_across_programmes: str = Form(""),
                      source_url: str = Form(""), quote: str = Form(""),
                      db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Record a cap the user read on the application form itself.

    This is the highest-quality source there is — the firm stating its own
    policy at the point of submission — and it is the one the research pass
    cannot reach, because it sits behind a login. A blank number clears the cap
    back to 'unknown' rather than storing a zero."""
    raw = (max_per_cycle or "").strip()
    if raw:
        try:
            n = int(raw)
        except ValueError:
            return HTMLResponse("Application limit must be a whole number",
                                status_code=400)
        if not 1 <= n <= 20:
            return HTMLResponse("Application limit must be between 1 and 20",
                                status_code=400)
        confidence = "manual"
    else:
        n, confidence = None, "unknown"
    db.set_company_limit(
        company, max_per_cycle=n, cycle=cycle.strip()[:120],
        locations_count_separately=_tri(locations_count_separately),
        shared_across_programmes=_tri(shared_across_programmes),
        confidence=confidence, quote=quote.strip()[:600],
        source_url=source_url.strip()[:500], updated_by="manual")
    return TEMPLATES.TemplateResponse(request, "_limit_note.html", {
        "request": request, "company": company, "lim": _limit_for(db, company),
        "saved": True,
    })


@app.get("/limits", response_class=HTMLResponse)
def limits_page(request: Request, db: JobDB = Depends(get_db),
                _=Depends(require_login)):
    """Every firm's application cap, ordered by how much it can cost him:
    firms with a known cap and starred roles first, because that is where a
    careless first application spends the whole allowance."""
    limits = db.all_company_limits()
    favs = db.favorite_counts_by_company()
    recent = db.applied_counts_by_company(since=_cycle_window())
    total = db.applied_counts_by_company()
    rows = []
    for company in set(limits) | set(favs) | set(total):
        view = _limit_view(_resolve_limit(limits, company),
                           recent.get(company, 0), total.get(company, 0))
        view["company"] = company
        view["favorites"] = favs.get(company, 0)
        rows.append(view)

    for r in rows:
        # Known cap + more favourites than the cap allows is the alarm case.
        r["over_starred"] = bool(r["known"] and r["favorites"] > (r["cap"] or 0))

    def rank(r):
        return (0 if r["over_starred"] else 1, 0 if r["known"] else 1,
                -r["favorites"], r["company"].lower())

    rows.sort(key=rank)
    checked = sum(1 for r in rows if r["checked"])
    return TEMPLATES.TemplateResponse(request, "limits.html", {
        "request": request, "rows": rows,
        "n_known": sum(1 for r in rows if r["known"]),
        "n_over": sum(1 for r in rows if r["over_starred"]),
        "n_checked": checked,
        "n_starred_unknown": sum(1 for r in rows
                                 if r["favorites"] and not r["known"]),
        "active_nav": "limits",
    })


@app.get("/campus", response_class=HTMLResponse)
def campus_page(request: Request, show: str = "live", q: str = "",
                db: JobDB = Depends(get_db), _=Depends(require_login)):
    """Graduate programmes off the ATS boards, with a tick against each.

    The rows come from the latest `jobfeed/campus_sweep.py` run and are never edited
    here — re-run the sweep and this page is current, which is the whole reason
    it exists instead of a hand-kept markdown list. The tick lives in the DB and
    survives the re-sweep.

    Default view is 'live': open now plus announced-for-later, minus anything
    ticked or skipped. That is the list to work today; everything else is one
    click away and nothing is thrown out."""
    data = campus.load_rows_cached()
    marks = db.all_campus_state()
    needle = (q or "").strip().lower()
    rows, counts = [], {"applied": 0, "skipped": 0, "live": 0}
    for row in data["rows"]:
        mark = marks.get(row["key"]) or {}
        row = dict(row, state=mark.get("state", "todo"), note=mark.get("note", ""),
                   season=data["season"])
        if row["state"] == "applied":
            counts["applied"] += 1
        elif row["state"] == "skipped":
            counts["skipped"] += 1
        elif row["status"] in ("open", "opens_later"):
            counts["live"] += 1
        rows.append(row)

    def visible(r: dict) -> bool:
        if needle and needle not in (
                f"{r['firm']} {r['programme']} {r['category']} {r['locations']}").lower():
            return False
        if show == "all":
            return True
        if show == "applied":
            return r["state"] == "applied"
        if show == "skipped":
            return r["state"] == "skipped"
        if show == "unclear":
            return r["state"] == "todo" and r["status"] == "unclear"
        if show == "closed":
            return r["status"] == "closed"
        return r["state"] == "todo" and r["status"] in ("open", "opens_later")

    return TEMPLATES.TemplateResponse(request, "campus.html", {
        "request": request,
        "rows": [r for r in rows if visible(r)],
        "counts": counts,
        "sweep": data,
        "stale": (data["stale_days"] is not None
                  and data["stale_days"] > campus.STALE_AFTER_DAYS),
        "show": show if show in ("live", "unclear", "closed", "applied",
                                 "skipped", "all") else "live",
        "q": q,
        "active_nav": "campus",
    })


@app.post("/campus/state", response_class=HTMLResponse)
def set_campus_state(request: Request, key: str = Form(...),
                     state: str = Form(...), firm: str = Form(""),
                     programme: str = Form(""), season: str = Form(""),
                     db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Tick / untick / skip one programme, and hand back just that row."""
    if state not in db.CAMPUS_STATES:
        return HTMLResponse("Unknown state", status_code=400)
    data = campus.load_rows_cached()
    row = next((r for r in data["rows"] if r["key"] == key), None)
    if row is None:
        # The key is not in the current sweep — a stale tab, or a re-sweep that
        # reworded the programme. Refusing beats writing a mark against a
        # programme this page can no longer show.
        return HTMLResponse(
            "This programme is no longer in the latest sweep — reload the page.",
            status_code=409)
    mark = db.set_campus_state(key, state=state, firm=firm.strip()[:200],
                               programme=programme.strip()[:300],
                               season=season.strip()[:40] or data["season"])
    row = dict(row, state=mark["state"], note=mark.get("note", ""),
               season=mark.get("season", ""))
    return TEMPLATES.TemplateResponse(request, "_campus_row.html",
                                      {"request": request, "row": row})


@app.get("/stats", response_class=HTMLResponse)
@app.get("/stats/applications", response_class=HTMLResponse)
@app.get("/stats/technical", response_class=HTMLResponse)
def stats(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    # Firms toggled off on Sources drop out of Stats too (same src_hidden set
    # that drops them from Browse) — the whole UI stays consistent.
    hidden = _hidden_companies(request)
    # Track switch (Application stats): all / full-time & graduate / internships.
    track = request.query_params.get("track", "").strip()
    if track not in TRACKS:
        track = ""
    track_kw = ({"hide_internships": True} if track == "fulltime"
                else {"job_type": "internship"} if track == "intern" else {})
    weeks = db.weekly_summary(weeks=4, exclude_companies=hidden)
    for r in weeks:
        area_rows = _area_rows(r["area"])
        finance_rows = [row for row in area_rows if row[0] != "other"]
        r["area_bar"] = _bar(finance_rows, r["finance"])
        region_rows = sorted(r["region"].items(), key=lambda kv: -kv[1])
        region_rows = [(name, name, n) for name, n in region_rows]
        r["region_bar"] = _bar(region_rows, sum(r["region"].values()),
                               slug=lambda name: REGION_SLUGS.get(name, "other"))
    velocity = db.company_weekly_velocity(weeks=8, exclude_companies=hidden)
    run = db.last_run_info()
    scan_volume = {"available": False, "raw": 0, "passed": 0, "rejected": 0}
    try:
        with open(os.path.join(ROOT, "verify_state.json")) as fp:
            scan_volume = _scan_volume(json.load(fp), hidden)
    except (OSError, ValueError):
        pass
    runtime = ""
    if run and run.get("duration_s"):
        m, s = divmod(int(run["duration_s"]), 60)
        runtime = f"{m}m {s:02d}s" if m else f"{s}s"
    # S13 pipeline board: every stage he has acted on, in funnel order (queue,
    # apply, THEN an assessment, THEN an interview), with rejections last so
    # a closed application stays findable. Offer appears once one exists.
    PIPELINE_STAGES = ACTED_ON_STATUSES
    OPEN_STAGES = tuple(st for st in PIPELINE_STAGES if st != "rejected")
    pipeline_rows = db.fetch_jobs(statuses=list(PIPELINE_STAGES),
                                  exclude_companies=hidden, limit=600, **track_kw)
    pipeline = {st: [] for st in PIPELINE_STAGES}
    for row in pipeline_rows:
        if row.get("status") in pipeline:
            pipeline[row["status"]].append(row)
    # Recency is the last thing that happened to the application: the latest
    # mail on it (a rejection dates itself), the application, the attended
    # run, or failing all of those when the role was first seen. Instants are
    # compared in UTC because senders stamp different offsets.
    last_mail: dict[str, str] = {}
    for job_id, at in db.conn.execute(
            "SELECT job_id, received_at FROM application_mail_events "
            "WHERE job_id <> '' AND outcome <> 'unclassified'"):
        if _utc_instant(at) > last_mail.get(job_id, ""):
            last_mail[job_id] = _utc_instant(at)
    run_at = {w["job_id"]: _utc_instant(w.get("updated_at") or "")
              for w in db.list_application_workflows()}

    def recency(row: dict) -> str:
        return max(last_mail.get(row.get("id"), ""), run_at.get(row.get("id"), ""),
                   _utc_instant(row.get("applied_at") or ""),
                   _utc_instant(row.get("first_seen") or ""))

    for row in pipeline_rows:
        row["recency"] = recency(row)
    # Programmes ticked on /campus have no board row, so Deutsche Bank's
    # Graduate Programme, applied through a Yello portal that never mails,
    # was missing from the board (2026-09-17). A tick joins the stage its own
    # mail has reached, and is left out where a board row at the same firm
    # already carries the application.
    acted_firms = {_limit_key(r.get("company", ""))
                   for r in db.fetch_jobs(statuses=list(ACTED_ON_STATUSES),
                                          limit=2000)}
    campus_by_track: dict[str, dict[str, int]] = {t: {} for t in TRACKS}
    for prog in db.campus_applications():
        if _limit_key(prog["firm"]) in acted_firms:
            continue
        stage = "applied"
        for m in prog["mail"]:
            if mail_rank(m["proposed_status"]) > mail_rank(stage):
                stage = m["proposed_status"]
        prog_track = programme_track(prog["programme"])
        if stage in APPLIED_STATUSES:
            counts = campus_by_track[prog_track]
            counts[stage] = counts.get(stage, 0) + 1
        if track and prog_track != track:
            continue
        if stage in pipeline:
            pipeline[stage].append({
                "title": prog["programme"], "company": prog["firm"],
                "deadline": None, "campus": True,
                "href": "/campus?show=applied&q=" + urllib.parse.quote(prog["firm"]),
                "recency": max([_utc_instant(prog["updated_at"])]
                               + [_utc_instant(m["received_at"]) for m in prog["mail"]]),
            })
    # Each stage is a sideways strip, most recent activity first, so older
    # applications are a swipe away.
    for st in pipeline:
        pipeline[st].sort(key=lambda r: r.get("recency") or "", reverse=True)
    if not pipeline["offer"]:
        del pipeline["offer"]
    pipeline_stages = tuple(st for st in PIPELINE_STAGES if st in pipeline)
    # The same default hides as Browse, or "Closing ≤7d: 23" opened a list of
    # 15: the other eight were Deloitte IT roles tagged 'other' (2026-09-17).
    browse_defaults = dict(hide_delisted=True, hide_other=True, hide_yoe=True,
                           hide_associates=True, exclude_companies=hidden, limit=2000)
    closing_7 = len(db.fetch_jobs(closing="7", **browse_defaults, **track_kw))
    # "By status" bars. 'new' is deliberately NOT a bar: it means "not triaged
    # yet" (713 of 765 rows on the QA fixture), so including it pins every real
    # stage to 1-2% of the track and the chart says nothing. It rides above the
    # bars as a plain count instead, and the bars scale to the largest stage he
    # has actually touched.
    STATUS_ORDER = ("queued", "applied", "oa", "interview", "offer",
                    "rejected", "ignored")
    fav_count = len(db.fetch_jobs(favorite=True, limit=2000, **track_kw))
    status_counts = db.status_counts(exclude_companies=hidden, track=track)
    touched = {st: status_counts.get(st, 0) for st in STATUS_ORDER}
    status_max = max(touched.values()) if touched else 0
    by_status = [{"name": st, "n": n,
                  "pct": round(100 * n / status_max, 1) if status_max else 0}
                 for st, n in touched.items() if n]
    untriaged = status_counts.get("new", 0)
    telemetry = tag_telemetry(days=14, by_run=True)[:8]
    evaluation = db.tag_evaluation_summary()
    funnels = {
        "Area": db.application_funnel("area", track=track),
        "Company": db.application_funnel("company", track=track),
        "Location": db.application_funnel("loc_city", track=track),
    }
    by_track = db.application_status_by_track(exclude_companies=hidden)
    for t, counts in campus_by_track.items():
        for st, n in counts.items():
            by_track[t][st] = by_track[t].get(st, 0) + n
    # Tab badges: submitted applications per track.
    apps = {t: sum(by_track[t].get(st, 0) for st in APPLIED_STATUSES) for t in TRACKS}
    track_tabs = [("", "Both", sum(apps.values()))] + [
        (t, TRACK_LABELS[t], apps[t]) for t in TRACKS]
    stats_view = "technical" if request.url.path == "/stats/technical" else "application"
    track_counts: dict[str, int] = {}
    for t in ([track] if track else TRACKS):
        for st, n in by_track[t].items():
            track_counts[st] = track_counts.get(st, 0) + n
    return TEMPLATES.TemplateResponse(request, "stats.html", {
        "apps_chart": _apps_week_chart(db.applications_per_week(
            12, track=track, exclude_companies=hidden)),
        "conversion": _conversion_steps(track_counts),
        "deadlines": _deadline_board(db, hidden, track_kw),
        "intake_chart": _intake_week_chart(db.weekly_intake_counts(8, exclude_companies=hidden)),
        "roles_chart": _roles_day_chart(db.new_roles_per_day(30, exclude_companies=hidden)),
        "cost_chart": _cost_day_chart(tag_telemetry(days=30, by_run=False)),
        "request": request,
        "weeks": weeks,
        "velocity": velocity,
        "total": db.total_seen(exclude_companies=hidden),
        "last_run": run["ran_at"] if run else "never",
        "run": run,
        "scan_volume": scan_volume,
        "runtime": runtime,
        "pipeline": pipeline,
        "pipeline_stages": pipeline_stages,
        "in_pipeline": sum(len(v) for st, v in pipeline.items() if st in OPEN_STAGES),
        "closing_7": closing_7,
        "by_status": by_status,
        "untriaged": untriaged,
        "fav_count": fav_count,
        "telemetry": telemetry,
        "evaluation": evaluation,
        "funnels": funnels,
        "track": track,
        "track_tabs": track_tabs,
        "stats_path": request.url.path,
        "stats_view": stats_view,
        "active_nav": f"{stats_view}-stats",
    })


TRACK_LABELS = {"fulltime": "Full-time", "intern": "Internships"}


def _short_date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def _apps_week_chart(weeks: list[dict]) -> dict:
    """Column-chart payload for static/charts.js: one bar per week."""
    return {"format": "int", "total": sum(w["n"] for w in weeks), "points": [
        {"x": _short_date(w["start"]), "v": w["n"],
         "tip": f"Week of {_short_date(w['start'])}: {w['n']} application{'' if w['n'] == 1 else 's'}"}
        for w in weeks]}


def _intake_week_chart(weeks: list[dict]) -> dict:
    """New finance roles per rolling week, oldest first. The counts arrive
    newest first on windows that end tomorrow, so today is in week 0."""
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    points = []
    for w in reversed(weeks):
        start = (end - timedelta(weeks=w["week_ago"] + 1)).isoformat()
        points.append({"x": _short_date(start), "v": w["finance"],
                       "tip": f"Week from {_short_date(start)}: {w['finance']:,} finance"
                              f" · {w['other_n']:,} other"})
    return {"format": "int", "points": points}


def _roles_day_chart(days: list[dict]) -> dict:
    return {"format": "int", "total": sum(d["n"] for d in days), "points": [
        {"x": _short_date(d["date"]), "v": d["n"],
         "tip": f"{_short_date(d['date'])}: {d['n']:,} new role{'' if d['n'] == 1 else 's'}"}
        for d in days]}


def _cost_day_chart(rows: list[dict], days: int = 30) -> dict:
    """Tagging spend per UTC day from tag_runs.jsonl, zero-filled. A day whose
    runs used an unpriced model is marked, never shown as free."""
    by_day = {r["key"]: r for r in rows}
    today = datetime.now(timezone.utc).date()
    points, total, unpriced = [], 0.0, 0
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        r = by_day.get(day)
        cost = r["cost"] if r else 0.0
        total += cost
        note = ""
        if r and not r["priced"]:
            unpriced += 1
            note = " (partly unpriced)"
        points.append({"x": _short_date(day), "v": round(cost, 4),
                       "tip": f"{_short_date(day)}: ${cost:.2f}{note}"
                              + (f" · {r['tagged']:,} roles tagged" if r else " · no run")})
    return {"format": "usd", "total": round(total, 2), "unpriced_days": unpriced,
            "has_data": bool(by_day), "points": points}


DEADLINE_WINDOW_DAYS = 30


def _deadline_board(db: JobDB, hidden: list[str], track_kw: dict) -> dict:
    """Starred or queued roles he has not submitted yet, split by whether the
    posting states a closing date.

    Only a date the careers site published (schema.org validThrough, or a
    board feed's own field) ever lands in `deadline`; nothing is inferred.
    Most postings state none, so the undated half is listed rather than
    dropped: a timeline alone would imply the rest have no deadline, when
    they simply did not say."""
    base = dict(hide_delisted=True, exclude_companies=hidden, limit=2000, **track_kw)
    seen: dict[str, dict] = {}
    for row in (db.fetch_jobs(favorite=True, **base)
                + db.fetch_jobs(statuses=["queued"], **base)):
        if row.get("status") in ("new", "queued"):
            seen.setdefault(row["id"], row)
    dated, undated, later = [], [], 0
    for row in seen.values():
        days = _deadline_days(row.get("deadline") or "")
        if days is None:
            undated.append(row)
        elif days > DEADLINE_WINDOW_DAYS:
            later += 1
        elif days >= 0:
            d = datetime.fromisoformat(row["deadline"][:10])
            dated.append({**row, "days": days, "when": _deadline_text(row["deadline"]),
                          "date": f"{d.day} {d.strftime('%b')}",
                          "pct": max(2, round(100 * days / DEADLINE_WINDOW_DAYS))})
    dated.sort(key=lambda r: (r["days"], r.get("company") or ""))
    undated.sort(key=lambda r: ((r.get("status") != "queued"), (r.get("company") or "").lower()))
    return {"dated": dated, "later": later, "undated": undated[:12],
            "undated_total": len(undated), "window": DEADLINE_WINDOW_DAYS,
            "total": len(seen)}


def _conversion_steps(counts: dict[str, int]) -> list[dict]:
    """Submitted → heard back → interview → offer, from current statuses.
    A status is where the application is now, so "interview" counts every
    application that reached at least that stage (interview or offer)."""
    submitted = sum(counts.get(st, 0) for st in APPLIED_STATUSES)
    steps = [
        ("Submitted", submitted),
        ("Heard back", sum(counts.get(st, 0) for st in ("oa", "interview", "offer", "rejected"))),
        ("Assessment or later", sum(counts.get(st, 0) for st in ("oa", "interview", "offer"))),
        ("Interview or later", sum(counts.get(st, 0) for st in ("interview", "offer"))),
        ("Offer", counts.get("offer", 0)),
    ]
    return [{"label": label, "n": n,
             "pct": round(100 * n / submitted) if submitted else 0}
            for label, n in steps]

# A programme name that says graduate or new grad is a graduate programme even
# when it also says "students" (Jump Trading's "Students & New Grads").
_GRADUATE_NAME_RE = re.compile(r"\bgrad(?:uate)?s?\b|\bnew\s+grads?\b|full[-\s]?time",
                               re.IGNORECASE)


def programme_track(name: str) -> str:
    """Track of a /campus programme, which has a name but no job_type: the
    tagger's own internship rule on the name, unless it also says graduate."""
    name = name or ""
    if tag._INTERNSHIP_RE.search(name) and not _GRADUATE_NAME_RE.search(name):
        return "intern"
    return "fulltime"


def _scan_volume(state: dict, hidden: set[str] | None = None) -> dict:
    """Aggregate the latest raw → pre-filter scan funnel from health state."""
    hidden = hidden or set()
    raw_by_company = state.get("last_raw_counts") or {}
    filtered_by_company = state.get("last_filtered_counts")
    if not isinstance(filtered_by_company, dict):
        return {"available": False, "raw": 0, "passed": 0, "rejected": 0}
    raw = sum(n for name, n in raw_by_company.items() if name not in hidden)
    passed = sum(n for name, n in filtered_by_company.items() if name not in hidden)
    return {
        "available": True,
        "raw": raw,
        "passed": passed,
        "rejected": max(0, raw - passed),
    }


def _scope_model(t: dict) -> tuple[str, str]:
    """Classify the SCOPE of a target (what subset of the board we pull) for the
    Sources page. Returns (short_label, css_slug).

    NB: this is the scope axis only. Execution (`heavy` = killable
    subprocess) is a SEPARATE axis surfaced as its own badge — it is NOT a
    scope. A heavy board (JPM, BNP) pulls the whole board and filters, exactly
    like a light "Full board" one; the only difference is how it's run. Folding
    `heavy` into the scope label here is what made JPM/BNP look scope-different
    from Deutsche Bank/HSBC when they aren't."""
    ats = t.get("ats", "")
    explicit = t.get("scope_type", "")
    if ats in ("manual", "unknown"):
        return ("Manual", "manual")
    if explicit == "facet":
        return ("Positive facet", "facet")
    if explicit == "search":
        return ("Search scope", "search")
    tb = t.get("talentbrew") or {}
    wd = t.get("workday") or {}
    sfa = t.get("successfactors_api") or {}
    koch = t.get("koch_avature") or {}
    # Positive division facets — isolate the finance arm of a noisy board.
    if (tb.get("facet_filter_sets") or tb.get("facet_filters")
            or wd.get("applied_facets") or sfa.get("facet_filters")
            or koch.get("filter_params")):
        return ("Positive facet", "facet")
    if ats == "wellsfargo":
        return ("Positive facet", "facet")
    # Server-side search/keyword scoping (a query string in the board URL too).
    search_url = t.get("search_url", "") or (t.get("attrax") or {}).get("search_url", "")
    if (wd.get("search_text") or (t.get("oracle_hcm") or {}).get("keyword")
            or (t.get("glencore") or {}).get("keyword")
            or t.get("query") or t.get("search_params")
            or "?" in search_url):
        return ("Search scope", "search")
    # Everything else pulls the whole board and lets jobfeed/filter.py + the tagger
    # sort it — i.e. negative scope. (Heavy or light is the execution badge.)
    return ("Full board (negative)", "full")


def _source_board_url(t: dict) -> str:
    """Return the human board that represents the source's actual scope.

    ``career_url`` is often a friendlier landing page, but it can point at a
    whole-company board while the scraper uses a division facet. Targets with
    a shareable scoped board declare ``scope_url`` explicitly so the Sources
    catalogue does not send users to a materially different job universe.
    """
    if t.get("scope_url"):
        return t["scope_url"]
    search_url = t.get("search_url", "")
    if search_url and "?" in search_url and _scope_model(t)[1] in ("facet", "search"):
        return search_url
    return (t.get("career_url", "") or _ats_board_url(t)
            or t.get("grad_scheme_url", ""))


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    """Catalogue of every scraped source, grouped by sector, with per-company
    scope model + live job counts. Doubles as a coverage/health dashboard and
    explains the three-step pipeline (scope -> tag -> filter)."""
    targets = _load_targets()
    stats = db.company_stats()
    # Roles seen in the last 14 days — the live health signal. Distinct from
    # `total`, which is cumulative all-time stored and never pruned (so a big
    # total can be old cruft, not current volume — e.g. BofA 1500 total / ~70 now).
    recent = db.company_recent_volume(days=14)

    # Health state from verify_state.json (written by each scan + selfcheck):
    # the authoritative broken set. `failing` = scraper errored, `degraded` =
    # succeeded but collapsed far below its rolling baseline. A verified source
    # in either set is "stalled" — wired and supposed to work, but currently
    # producing nothing. Read defensively: absent/garbage file => 0 stalled.
    stalled_set: set = set()
    _vstate: dict = {}
    try:
        with open(os.path.join(ROOT, "verify_state.json")) as fp:
            _vstate = json.load(fp)
        stalled_set = (set(_vstate.get("failing", []))
                       | set(_vstate.get("degraded", [])))
    except (OSError, ValueError):
        pass

    groups: dict[str, list[dict]] = {}
    totals = {"sources": 0, "automated": 0, "stalled": 0, "unverified": 0,
              "manual": 0, "finance": 0, "stored": 0}
    for t in targets:
        name = t.get("name", "")
        ats = t.get("ats", "")
        label, slug = _scope_model(t)
        st = stats.get(name, {})
        career_url = t.get("career_url", "")
        row = {
            "name": name,
            "ats": ats,
            "scope_label": label,
            "scope_slug": slug,
            # Execution axis, separate from scope: heavy = killable
            # subprocess-per-company for big/slow boards.
            "heavy": bool(t.get("heavy")),
            "verified": t.get("verified", False),
            # A source moved to manual can remain in verify_state until the
            # next scan rewrites it; it is no longer an automated failure.
            "stalled": ats not in ("manual", "unknown") and name in stalled_set,
            "total": st.get("total", 0),
            "recent": recent.get(name, 0),
            "finance": st.get("finance", 0),
            "last_seen": (st.get("last_seen") or "")[:10],
            "last_raw": _vstate.get("last_raw_counts", {}).get(name),
            "last_unique": _vstate.get("last_unique_counts", {}).get(name),
            "expected": _vstate.get("baseline", {}).get(name),
            "last_clean": (_vstate.get("last_clean_scan", {}).get(name) or "")[:10],
            "career_url": career_url,
            # Prefer an explicit scoped board when the scraper uses a shareable
            # division/search facet; a broad career landing page is not an
            # honest representation of that source.
            "board_url": _source_board_url(t),
            # manual firms carry their blocker in manual_reason; surface it via
            # the same ⓘ note so the retired /manual page isn't needed.
            "notes": t.get("notes") or t.get("manual_reason", ""),
            # Heads-up: firm runs a separate graduate scheme advertised on its
            # own site (prose, deadlines) that never hits the scraped board, so
            # the role list alone understates it. grad_scheme_url links straight
            # to the programme page when known. See docs/GRAD_SCHEMES.md.
            "grad_scheme": bool(t.get("grad_scheme")),
            "grad_scheme_url": t.get("grad_scheme_url", "") or career_url,
        }
        if row["last_unique"] is not None and row["expected"]:
            row["baseline_delta"] = row["last_unique"] - row["expected"]
        else:
            row["baseline_delta"] = None
        groups.setdefault(t.get("category", "Uncategorised"), []).append(row)
        totals["sources"] += 1
        totals["stored"] += row["total"]
        totals["finance"] += row["finance"]
        # Three honest buckets: a row is only "automated" (scraped on schedule)
        # if it's a real ATS AND verified:true. A configured-but-verified:false
        # row is NOT scraped (jobfeed/main.py skips it) — it gets its own "wired,
        # unverified" count so the headline never hides not-yet-running sources.
        if slug == "manual":
            totals["manual"] += 1
        elif not t.get("verified"):
            totals["unverified"] += 1
        elif name in stalled_set:
            # verified:true but currently broken/collapsed — pulled out of the
            # "scraped" count so a regression can't hide as a healthy 0.
            totals["stalled"] += 1
        else:
            totals["automated"] += 1

    # Both levels alphabetical: category groups A-Z, and firms within each A-Z.
    ordered = sorted(groups.items(), key=lambda kv: kv[0].lower())
    for _name, rows in ordered:
        rows.sort(key=lambda r: r["name"].lower())

    return TEMPLATES.TemplateResponse(request, "sources.html", {
        "request": request,
        "groups": ordered,
        "totals": totals,
        "active_nav": "sources",
    })


# NB: the standalone /manual page was retired 2026-06-28. It was a strict subset
# of /sources — every manual firm already appears there (scope "Manual / research",
# career_url ↗), and its only unique datum (manual_reason) is now shown via the ⓘ
# note on the Sources row. `jobfeed/manual_check.py` still prints the markdown list on the CLI.


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, bucket: str = "manager",
           db: JobDB = Depends(get_db), _=Depends(require_login)):
    """Audit the roles tagging REMOVED from view.

    Every other tagging mistake is self-reporting: a role shows up under the
    wrong desk and you notice. Two do not — `seniority='manager'` is a hard gate
    in the browse query, and `area='other'` is hidden everywhere except its own
    tab and then deleted outright once the role goes off its board. So "I have
    never spotted a wrong tag" is compatible with any false-negative rate at
    all; you cannot spot what was hidden and then purged. This page is the only
    place those two buckets are visible, as a random resampling handful."""
    hidden = _hidden_companies(request)
    if bucket not in JobDB._REVIEW_BUCKETS:
        bucket = "manager"
    rows = db.review_sample(bucket, limit=24, exclude_companies=hidden)
    return TEMPLATES.TemplateResponse(request, "review.html", {
        "request": request,
        "active_nav": "review",
        "bucket": bucket,
        "rows": rows,
        "counts": db.review_counts(exclude_companies=hidden),
        "evaluation": db.tag_evaluation_summary(),
        "areas": tag.AREAS,
        "desks": ("", *tag.DESKS),
        "seniorities": tag.SENIORITIES,
        "job_types": tag.JOB_TYPES,
    })


@app.post("/review/{job_id}/evaluate")
def evaluate_tag(job_id: str, request: Request, bucket: str = Form("manager"),
                 area: str = Form(""), desk: str = Form(""),
                 seniority: str = Form(""), job_type: str = Form("job"),
                 db: JobDB = Depends(get_db), _=Depends(require_owner)):
    """Confirm or correct a hidden prediction into the frozen eval set."""
    if (bucket not in JobDB._REVIEW_BUCKETS or area not in tag.AREAS
            or desk not in ("", *tag.DESKS)
            or seniority not in tag.SENIORITIES
            or job_type not in tag.JOB_TYPES):
        return HTMLResponse("Invalid tag value", status_code=400)
    if area != "markets":
        desk = ""
    if not db.record_tag_evaluation(job_id, bucket=bucket, area=area,
                                    desk=desk, seniority=seniority,
                                    job_type=job_type):
        return HTMLResponse("Not found", status_code=404)
    return RedirectResponse(f"/review?bucket={bucket}", status_code=303)


@app.get("/api/jobs")
def api_jobs(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    filters = _filters_from_request(request)
    limit = request.query_params.get("limit", "")
    # int() directly: .isdigit() accepts unicode digits (e.g. "²") that int()
    # then rejects with a 500.
    try:
        n = int(limit)
    except ValueError:
        n = 1000
    n = max(1, min(n, API_LIMIT_MAX))  # clamp so a caller can't request an unbounded set
    jobs = db.fetch_jobs(limit=n, with_description=True, **filters)
    return JSONResponse({"count": len(jobs), "jobs": jobs})


@app.get("/healthz")
def healthz():
    return {"ok": True}
