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
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import html as _html
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from db import JobDB  # noqa: E402
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


def _load_dotenv() -> None:
    """Same minimal .env loader as main.py."""
    env_path = Path(ROOT) / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_FILE = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
# Two fixed accounts, each a username + password pair:
#   - the OWNER (full control: status / favorite / notes / mark-seen), and
#   - an optional read-only GUEST: can browse, filter and read details, but
#     every mutating endpoint rejects the role and its controls are hidden in
#     the UI. A guest's theme / saved views / hidden-firms live in their own
#     browser, so a shared guest login can never disturb the owner's data.
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


def _safe_url(url) -> str:
    """Only http(s) URLs may render as hrefs. Job URLs come from scraped ATS
    payloads — hostile input. Jinja autoescaping doesn't neutralize the URL
    *scheme*, so a board returning ``javascript:...`` as the apply link would
    otherwise execute in the authed session on click."""
    u = (url or "").strip()
    if u.lower().startswith(("http://", "https://")):
        return u
    return ""


TEMPLATES.env.filters["safe_url"] = _safe_url


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


app = FastAPI(title="Jobfeed")
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
SORTS = {"recent", "company"}
RECENCY_DAYS = {"7": 7, "14": 14, "30": 30}


def _filters_from_request(request: Request) -> dict:
    """Map query params → db.fetch_jobs kwargs. Blank params are dropped."""
    p = request.query_params
    kwargs: dict = {}
    for key in ("status", "category", "area", "desk", "seniority", "job_type",
                "loc_region", "loc_country", "loc_city", "work_mode", "company",
                "education", "lang_req", "start"):
        val = p.get(key, "").strip()
        if val:
            kwargs[key] = val
    q = p.get("q", "").strip()
    if q:
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
    kwargs["hide_delisted"] = p.get("show_expired", "").strip() not in ("1", "true", "on")
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
    if p.get("fav", "").strip() in ("1", "true", "on"):
        kwargs["favorite"] = True
    # Firms the user toggled off on the Sources page disappear from Browse.
    hidden = _hidden_companies(request)
    if hidden:
        kwargs["exclude_companies"] = hidden
    return kwargs


def _facets(db: JobDB, request: Request | None = None,
            filters: dict | None = None) -> dict:
    """Distinct values for each facet dropdown. Non-location facets list every
    tagged value. The LOCATION facets reflect the current filtered view
    (faceted search): Region/Country/City list only places that actually have a
    role visible under the active filters — so a country whose only role is
    delisted / back-office / hidden-by-default no longer clutters the list.
    Country cascades from the selected Region, City from the selected Country."""
    out = {}
    for col in ("category", "area", "desk", "seniority", "job_type",
                "work_mode", "status", "education", "lang_req"):
        try:
            out[col] = db.distinct(col)
        except ValueError:
            out[col] = []
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
# (tag.py keeps the fine-grained distinction on purpose) but browse together
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
    # it browses under the Middle Office tab. Actuarial (insurance/pension risk
    # math) is a thin, specialised track (~24 roles) folded here too rather than
    # given its own tab. Accounting keeps its own tab (it's the Finance/CFO
    # function, not middle office). Area codes stay distinct — the specific
    # sub-area is still shown as a secondary tag (see _area_subtag).
    "middle-office": ("middle-office", "risk", "actuarial"),
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
    "actuarial": "actuarial",
}
# Reverse lookup: raw area code -> the group tab it displays under (identity
# for ungrouped codes), used by _area_label for per-job badges.
_AREA_TO_GROUP = {member: group for group, members in AREA_GROUPS.items()
                  for member in members}

# Top-level Area tabs (label shown on the tab). "" = All.
AREA_TABS = [
    ("", "All"), ("markets", "Markets"), ("quant", "Quant"),
    ("research", "Research"), ("ibd", "IBD"),
    ("private-markets", "Private Markets"),
    ("corporate-banking", "Corp Banking"),
    ("asset-management", "AM"),
    ("wealth", "Wealth"),
    ("middle-office", "Middle Office"), ("consulting", "Consulting"),
    ("accounting", "Accounting"),
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
def _active_chips(request: Request) -> list[dict]:
    p = request.query_params
    params = dict(p)

    def without(*keys) -> str:
        q = {k: v for k, v in params.items() if k not in keys}
        return "/?" + urllib.parse.urlencode(q) if q else "/"

    chips: list[dict] = []

    def add(label: str, *keys):
        chips.append({"label": label, "url": without(*keys)})

    q = p.get("q", "").strip()
    if q:
        add(f"“{q}”", "q")
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
    if p.get("status", "").strip():
        add(f"Status: {p['status'].strip()}", "status")
    if p.get("education", "").strip():
        add(f"Edu: {p['education'].strip()}", "education")
    lang = p.get("lang_req", "").strip()
    if lang:
        add("English only" if lang == "none" else f"Lang: {lang}", "lang_req")
    start = p.get("start", "").strip()
    if start:
        add("Start: ASAP" if start == "asap" else f"Start: {start}", "start")
    if p.get("fav", "").strip() in ("1", "true", "on"):
        add("★ Favorites", "fav")
    if p.get("show_senior", "").strip() in ("1", "true", "on"):
        add("Senior 3y+", "show_senior")
    if p.get("assoc", "").strip() in ("1", "true", "on"):
        add("Associates", "assoc")
    if p.get("show_expired", "").strip() in ("1", "true", "on"):
        add("Delisted", "show_expired")
    return chips


def _select_job(db: JobDB, jobs: list[dict], request: Request) -> dict | None:
    """The job shown in the desktop detail pane on a full page load: the `sel`
    query param if it points at a still-visible role, else the first row."""
    sel = request.query_params.get("sel", "").strip()
    if sel:
        for j in jobs:
            if str(j.get("id")) == sel:
                return _prepare_detail(dict(j))
    if jobs:
        return _prepare_detail(db.get_job(jobs[0]["id"]) or dict(jobs[0]))
    return None


def _prepare_detail(job: dict) -> dict:
    """Clean the stored description and render it to structured HTML for display."""
    cleaned = _clean_desc(job.get("description"))
    job["description"] = cleaned
    job["description_html"] = format_description(cleaned, job.get("title"))
    return job


def _tabs_for(request: Request, db: JobDB, filters: dict) -> list[dict]:
    """Area-tab dicts with badge counts that respect the active secondary
    filters (everything except the area/desk being drilled into, hide_other,
    and sort)."""
    count_filters = {k: v for k, v in filters.items()
                     if k not in ("area", "areas", "desk", "hide_other", "sort")}
    return _build_tabs(request, db.area_counts(**count_filters))


def _build_tabs(request: Request, counts: dict) -> list[dict]:
    """One tab per area with a count badge, preserving the other active query
    params. 'All' = every finance area (excludes 'other')."""
    params = dict(request.query_params)
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
        url = "/?" + urllib.parse.urlencode(p) if p else "/"
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
    print(f"AUTH: failed login attempt #{len(_login_failures)} in window "
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    filters = _filters_from_request(request)
    facets = _facets(db, request, filters)
    _reconcile_location(filters, facets)
    jobs = db.fetch_jobs(limit=PAGE_LIMIT, **filters)
    tabs = _tabs_for(request, db, filters)
    last_seen = db.get_meta("last_seen_ts", LAST_SEEN_DEFAULT)
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "request": request,
        "scan_stale_days": _scan_stale_days(db),
        "jobs": jobs,
        "facets": facets,
        "params": dict(request.query_params),
        "count": len(jobs),
        "truncated": len(jobs) >= PAGE_LIMIT,
        "tabs": tabs,
        "chips": _active_chips(request),
        "sel_job": _select_job(db, jobs, request),
        "last_seen": last_seen,
        "new_count": db.count_new(last_seen),
        "active_nav": "browse",
    })


@app.get("/partials/jobs", response_class=HTMLResponse)
def partial_jobs(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    filters = _filters_from_request(request)
    facets = _facets(db, request, filters)
    _reconcile_location(filters, facets)
    jobs = db.fetch_jobs(limit=PAGE_LIMIT, **filters)
    last_seen = db.get_meta("last_seen_ts", LAST_SEEN_DEFAULT)
    return TEMPLATES.TemplateResponse(request, "_jobs_partial.html", {
        "request": request, "jobs": jobs, "count": len(jobs),
        "truncated": len(jobs) >= PAGE_LIMIT,
        "tabs": _tabs_for(request, db, filters),
        "chips": _active_chips(request),
        "facets": facets,
        "params": dict(request.query_params),
        "last_seen": last_seen,
        "new_count": db.count_new(last_seen),
    })


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
    _prepare_detail(job)
    # ?pane=1 → the bare detail-pane fragment loaded into the Browse split view
    # (and the mobile bottom sheet) via htmx. Otherwise the standalone page.
    if request.query_params.get("pane"):
        return TEMPLATES.TemplateResponse(request, "_detail.html", {
            "request": request, "job": job,
        })
    return TEMPLATES.TemplateResponse(request, "job.html", {
        "request": request, "job": job, "active_nav": "browse",
    })


@app.post("/job/{job_id}/status", response_class=HTMLResponse)
def set_status(job_id: str, request: Request, status: str = Form(...),
               db: JobDB = Depends(get_db), _=Depends(require_owner)):
    if status not in CRM_STATUSES:
        return HTMLResponse("bad status", status_code=400)
    db.set_status(job_id, status)
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


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, db: JobDB = Depends(get_db), _=Depends(require_login)):
    # Firms toggled off on Sources drop out of Stats too (same src_hidden set
    # that drops them from Browse) — the whole UI stays consistent.
    hidden = _hidden_companies(request)
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
    runtime = ""
    if run and run.get("duration_s"):
        m, s = divmod(int(run["duration_s"]), 60)
        runtime = f"{m}m {s:02d}s" if m else f"{s}s"
    return TEMPLATES.TemplateResponse(request, "stats.html", {
        "request": request,
        "weeks": weeks,
        "velocity": velocity,
        "total": db.total_seen(exclude_companies=hidden),
        "last_run": run["ran_at"] if run else "never",
        "run": run,
        "runtime": runtime,
        "active_nav": "stats",
    })


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
    if ats in ("manual", "unknown"):
        return ("Manual / research", "manual")
    tb = t.get("talentbrew") or {}
    wd = t.get("workday") or {}
    sfa = t.get("successfactors_api") or {}
    # Positive division facets — isolate the finance arm of a noisy board.
    if (tb.get("facet_filter_sets") or tb.get("facet_filters")
            or wd.get("applied_facets") or sfa.get("facet_filters")):
        return ("Positive facet", "facet")
    if ats == "wellsfargo":
        return ("Positive slug", "facet")
    # Server-side search/keyword scoping (a query string in the board URL too).
    search_url = t.get("search_url", "") or (t.get("attrax") or {}).get("search_url", "")
    if (wd.get("search_text") or (t.get("oracle_hcm") or {}).get("keyword")
            or (t.get("glencore") or {}).get("keyword")
            or t.get("query") or t.get("search_params")
            or "?" in search_url):
        return ("Search scope", "search")
    # Everything else pulls the whole board and lets filter.py + the tagger
    # sort it — i.e. negative scope. (Heavy or light is the execution badge.)
    return ("Full board (negative)", "full")


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
        label, slug = _scope_model(t)
        st = stats.get(name, {})
        career_url = t.get("career_url", "")
        row = {
            "name": name,
            "ats": t.get("ats", ""),
            "scope_label": label,
            "scope_slug": slug,
            # Execution axis, separate from scope: heavy = killable
            # subprocess-per-company for big/slow boards.
            "heavy": bool(t.get("heavy")),
            "verified": t.get("verified", False),
            "stalled": name in stalled_set,
            "total": st.get("total", 0),
            "recent": recent.get(name, 0),
            "finance": st.get("finance", 0),
            "last_seen": (st.get("last_seen") or "")[:10],
            "career_url": career_url,
            # Derived board URL for the ↗ link: career_url first, then a URL
            # computed from the ATS config, then the firm's grad-scheme page —
            # so every source shows a career-site link.
            "board_url": career_url or _ats_board_url(t) or t.get("grad_scheme_url", ""),
            # manual firms carry their blocker in manual_reason; surface it via
            # the same ⓘ note so the retired /manual page isn't needed.
            "notes": t.get("notes") or t.get("manual_reason", ""),
            # Heads-up: firm runs a separate graduate scheme advertised on its
            # own site (prose, deadlines) that never hits the scraped board, so
            # the role list alone understates it. grad_scheme_url links straight
            # to the programme page when known. See GRAD_SCHEMES.md.
            "grad_scheme": bool(t.get("grad_scheme")),
            "grad_scheme_url": t.get("grad_scheme_url", "") or career_url,
        }
        groups.setdefault(t.get("category", "Uncategorised"), []).append(row)
        totals["sources"] += 1
        totals["stored"] += row["total"]
        totals["finance"] += row["finance"]
        # Three honest buckets: a row is only "automated" (scraped on schedule)
        # if it's a real ATS AND verified:true. A configured-but-verified:false
        # row is NOT scraped (main.py skips it) — it gets its own "wired,
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
# note on the Sources row. `manual_check.py` still prints the markdown list on the CLI.


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
    jobs = db.fetch_jobs(limit=n, **filters)
    return JSONResponse({"count": len(jobs), "jobs": jobs})


@app.get("/healthz")
def healthz():
    return {"ok": True}
