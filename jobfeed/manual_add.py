"""Add one role by its posting URL: the roles the scraped boards never see.

LinkedIn alerts on manual firms, referrals and firm sites marked `ats:
"manual"` all end in a posting URL. This turns one into a normal stored,
tagged row, so it can be tracked and an attended application started from
its detail pane like any scraped role.

The pipeline is the scan's, with three differences, each because he chose
the role:

- The hard gates (`filter.drop_reason`) are warnings, not drops.
- The fetch goes through `safe_fetch.GuardedSession` (the URL is typed in,
  so it is SSRF-checked hop by hop).
- No stub is ever stored. A login wall, a JS shell or an outage page yields
  no posting text, and the person is asked to paste it: a stub description is
  the cheapest way to hide a role (see CLAUDE.md, the `other` audit).

The text comes from the matching enricher first (`enrich_lane`: Workday's cxs
API, Oracle's REST detail, the DETAIL_ENRICHERS registry), each of which also
reports the posting's stated deadline; then schema.org JobPosting JSON-LD;
then the readable page text, through `enrich_one` and its shell and outage
guards. Title, firm and location come from the JSON-LD or the page's meta
tags, and anything still missing is asked for.

A manually added row's id starts with `MANUAL_ID_PREFIX` ("manual_"). That is
the whole marker: no seen_jobs column, so export_shared.py's unknown-column
guard is untouched, and the delist and orphan passes, which judge rows by the
boards a scan read, leave these rows alone (jobfeed/db.py).
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from jobfeed.db import MANUAL_ID_PREFIX, YOE_HIDE_THRESHOLD, JobDB
from jobfeed.deadline_text import stated_deadline
from jobfeed.filter import drop_reason, has_experience_wall
from jobfeed.safe_fetch import GuardedSession, Refused, check_url
from scrapers.enrich import detail_then_http, enrich_lane
from scrapers.enrich import goldman_enrich, oracle_enrich, workable_enrich
from scrapers.enrich.descriptions import (HEADERS, _jobposting_description,
                                          _jobposting_nodes, enrich_one,
                                          plausible_deadline)
from scrapers.enrich.workday_enrich import WorkdayEnricher, is_workday

TIMEOUT = 15
# Less than this is not a posting: a cookie banner, "this job is no longer
# available", a login prompt. Real postings run to thousands of characters.
MIN_POSTING_CHARS = 400
_WALL_RE = re.compile(
    r"\b(sign in|log ?in|join now|create an account|authwall|enable javascript|"
    r"access denied|captcha|are you a robot|no longer (available|accepting))\b", re.I)
_TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|gh_src|source|src|ref|refid|trackingid|trk|lipi|mcid|cmpid|"
    r"campaign\w*|fbclid|gclid|codes)$", re.I)


@dataclass
class Result:
    outcome: str                      # added | existing | needs_input | refused
    message: str = ""
    job_id: str = ""
    warnings: list[str] = field(default_factory=list)
    draft: dict = field(default_factory=dict)  # what was read, to prefill the form


# ── URLs and duplicates ──────────────────────────────────────────────────────

def canonical_url(url: str) -> str:
    """The posting URL without its fragment, tracking parameters or trailing
    slash, scheme and host lower-cased."""
    parts = urlsplit(str(url or "").strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def manual_id(url: str) -> str:
    return MANUAL_ID_PREFIX + hashlib.sha1(canonical_url(url).encode()).hexdigest()[:16]


def ats_job_ids(url: str) -> list[str]:
    """The ids the scrapers would have stored this posting under, for the
    boards whose id is readable from the URL. Shapes match scrapers/workday.py
    (wd_<tenant>_<last path segment>), oracle_hcm.py (oracle_<site>_<id>),
    greenhouse.py (gh_<id>), lever.py (lv_<id>), smartrecruiters.py (sr_<id>)."""
    parts = urlsplit(str(url or "").strip())
    host, path = (parts.hostname or "").lower(), parts.path.rstrip("/")
    out = []
    if host.endswith(".myworkdayjobs.com") and "/job/" in path:
        out.append(f"wd_{host.split('.')[0]}_{path.split('/')[-1]}")
    m = oracle_enrich._URL_RE.match(url or "")
    if m:
        out.append(f"oracle_{m.group(2)}_{m.group(3)}")
    if "greenhouse.io" in host:
        gh = dict(parse_qsl(parts.query)).get("gh_jid") or (
            re.search(r"/jobs/(\d+)", path) or [None, None])[1]
        if gh:
            out.append(f"gh_{gh}")
    if host == "jobs.lever.co":
        segs = path.split("/")
        if len(segs) >= 3 and re.fullmatch(r"[0-9a-f-]{36}", segs[2]):
            out.append(f"lv_{segs[2]}")
    if host == "jobs.smartrecruiters.com":
        m = re.search(r"/(\d{6,})", path)
        if m:
            out.append(f"sr_{m.group(1)}")
    gh_param = dict(parse_qsl(parts.query)).get("gh_jid")
    if gh_param and f"gh_{gh_param}" not in out:
        out.append(f"gh_{gh_param}")
    return out


def find_existing(db: JobDB, url: str) -> str:
    """The id of a row already holding this posting, or ""."""
    for jid in [manual_id(url)] + ats_job_ids(url):
        if db.seen(jid):
            return jid
    raw = str(url or "").strip()
    canon = canonical_url(raw)
    variants = {raw, raw.split("#", 1)[0], canon, canon + "/",
                canon.split("?", 1)[0], canon.split("?", 1)[0] + "/"}
    return db.find_job_by_urls(sorted(v for v in variants if v)) or ""


# ── Reading the posting ──────────────────────────────────────────────────────

_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _meta(page: str) -> dict[str, str]:
    out = {}
    for tag in _META_RE.findall(page or ""):
        attrs = {m.group(1).lower(): m.group(3) if m.group(3) is not None else m.group(4)
                 for m in _ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = html_lib.unescape(attrs["content"]).strip()
    return out


def _clean(text) -> str:
    return " ".join(html_lib.unescape(str(text or "")).split())[:300]


def _org_name(value) -> str:
    if isinstance(value, dict):
        return _clean(value.get("name"))
    if isinstance(value, list) and value:
        return _org_name(value[0])
    return _clean(value) if isinstance(value, str) else ""


def _location(value) -> str:
    places = value if isinstance(value, list) else [value]
    out = []
    for place in places:
        if not isinstance(place, dict):
            continue
        addr = place.get("address") or {}
        if isinstance(addr, str):
            out.append(_clean(addr))
            continue
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        bits = [_clean(addr.get(k)) for k in ("addressLocality", "addressRegion")]
        bits.append(_clean(country))
        line = ", ".join(b for b in bits if b)
        if line and line not in out:
            out.append(line)
    return "; ".join(out)[:300]


def read_metadata(page: str) -> dict:
    """title, company, location, posted, deadline from the page itself."""
    meta = _meta(page)
    found = {"title": "", "company": "", "location": "", "posted": "", "deadline": ""}
    for node in _jobposting_nodes(page or ""):
        found["title"] = found["title"] or _clean(node.get("title"))
        found["company"] = found["company"] or _org_name(node.get("hiringOrganization"))
        found["location"] = found["location"] or _location(node.get("jobLocation"))
        found["posted"] = found["posted"] or str(node.get("datePosted") or "")[:10]
        found["deadline"] = found["deadline"] or plausible_deadline(
            node.get("validThrough"), node.get("datePosted"))
    if not found["title"]:
        m = _TITLE_RE.search(page or "")
        found["title"] = meta.get("og:title") or (_clean(m.group(1)) if m else "")
    if not found["company"]:
        found["company"] = meta.get("og:site_name", "")
    return found


def real_posting(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < MIN_POSTING_CHARS:
        return False
    return not (len(text) < 1500 and _WALL_RE.search(text))


def _workday_parts(url: str) -> tuple[str, str, str]:
    """(tenant, board, url without the locale segment) for a
    <tenant>.wdN.myworkdayjobs.com/[xx-XX/]<board>/job/... link."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", segs[0]):
        segs = segs[1:]
    if len(segs) < 2 or "job" not in segs or segs.index("job") == 0:
        return "", "", url
    board = segs[0]
    tenant = (parts.hostname or "").split(".")[0]
    return tenant, board, urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(segs), "", ""))


def fetch_description(url: str, session: GuardedSession, out: dict) -> tuple[str, str]:
    """(text, how it was read). `out` collects a stated deadline, as for the
    scan's enrichers."""
    host = (urlsplit(url).hostname or "").lower()
    lane = enrich_lane({"url": url, "id": ""},
                       workday=is_workday(url) and host.endswith(".myworkdayjobs.com"))
    text = ""
    try:
        if lane == "workday":
            tenant, board, clean = _workday_parts(url)
            if tenant:
                enricher = WorkdayEnricher(timeout=TIMEOUT, session_factory=GuardedSession)
                text = enricher.description(clean, tenant, board, out=out)
        elif lane == "oracle":
            text = oracle_enrich.description(url, session, timeout=TIMEOUT, out=out)
        elif lane == "workable":
            text = workable_enrich.description(url, session, timeout=TIMEOUT)
        elif lane == "goldman":
            text = goldman_enrich.description(url, session, timeout=TIMEOUT)
        elif lane == "detail":
            job = {"url": url}
            text = detail_then_http(job, session, timeout=TIMEOUT, http_timeout=TIMEOUT)
            if job.get("deadline"):
                out["deadline"] = job["deadline"]
    except requests.RequestException:
        text = ""
    if text:
        return text, f"{lane} enricher"
    return "", ""


def add(db: JobDB, url: str, *, title: str = "", company: str = "",
        location: str = "", description: str = "", session=None,
        tag_fn=None) -> Result:
    """Store the posting at `url` (or open the row that already holds it).

    `title`, `company`, `location` and `description` are what he typed on the
    form; each wins over what the page says. A pasted description is the
    answer to a login wall, so it is used as given."""
    url = str(url or "").strip()
    draft = {"url": url, "title": title.strip(), "company": company.strip(),
             "location": location.strip(), "description": description.strip()}
    try:
        check_url(url)
    except Refused as exc:
        return Result("refused", f"Not fetched: {exc}.", draft=draft)
    existing = find_existing(db, url)
    if existing:
        return Result("existing", "Already stored; opened the existing role.", job_id=existing)

    session = session or GuardedSession(timeout=TIMEOUT)
    out: dict = {}
    page, how, note = "", "", ""
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        page = r.text if r.status_code < 400 else ""
        if r.status_code >= 400:
            note = f"the page answered HTTP {r.status_code}"
        elif "/authwall" in (r.url or "") or "/login" in (urlsplit(r.url or "").path or ""):
            note = "the page redirected to a login wall"
    except Refused as exc:
        return Result("refused", f"Not fetched: {exc}.", draft=draft)
    except requests.RequestException as exc:
        note = f"the page could not be fetched ({type(exc).__name__})"

    found = read_metadata(page)
    text = draft["description"]
    if text:
        how = "pasted"
    else:
        text, how = fetch_description(url, session, out)
        if not text and page:
            text = _jobposting_description(page)
            how = "JSON-LD" if text else ""
        if not text and page and not note:
            text = enrich_one(url, session, timeout=TIMEOUT, out=out)
            how = "page text" if text else ""
    for key in ("title", "company", "location"):
        draft[key] = draft[key] or found[key]

    if not real_posting(text):
        if how == "pasted":
            return Result("needs_input",
                          "Nothing stored: the pasted description is too short to "
                          f"be a posting (under {MIN_POSTING_CHARS} characters).",
                          draft=draft)
        why = note or ("no posting text came back (a login wall, a page built in "
                       "the browser, or an expired posting)")
        draft["description"] = draft["description"] or ""
        return Result("needs_input",
                      f"Nothing stored: {why}. Paste the description below and "
                      "submit again.", draft=draft)
    missing = [k for k in ("title", "company") if not draft[k]]
    if missing:
        draft["description"] = text
        return Result("needs_input",
                      "Nothing stored yet: the page does not say the "
                      f"{' or '.join(missing)}. Fill it in below.", draft=draft)

    deadline = (out.get("deadline") or found["deadline"]
                or stated_deadline(text)[0] or "")
    job = {"id": manual_id(url), "url": url, "title": draft["title"][:300],
           "company": draft["company"][:200], "location": draft["location"][:300],
           "description": text, "deadline": deadline}
    warnings = []
    reason = drop_reason(job)
    if reason:
        warnings.append(f"A scan would have dropped this role ({reason}); "
                        "it is kept because you added it.")
    db.mark_seen(job["id"], company=job["company"], title=job["title"], url=url,
                 location=job["location"], posted=found["posted"],
                 description=text, deadline=deadline or "")
    warnings += _tag(db, job, tag_fn)
    return Result("added", f"Added; description from the {how}.",
                  job_id=job["id"], warnings=warnings)


def _tag(db: JobDB, job: dict, tag_fn=None) -> list[str]:
    """Tag through the configured transport and set min_yoe, as a scan does
    for every new role. Never fails the add: a role with blank tags is still
    stored and the backfill re-tags it."""
    from jobfeed import tag
    notes = []
    try:
        (tag_fn or tag.tag_jobs)([job])
        if job.get("area"):
            db.set_tags(job["id"], area=job.get("area", ""), desk=job.get("desk", ""),
                        seniority=job.get("seniority", ""),
                        job_type=job.get("job_type", "job"),
                        loc_city=job.get("loc_city", ""),
                        loc_country=job.get("loc_country", ""),
                        loc_region=job.get("loc_region", ""),
                        work_mode=job.get("work_mode", ""),
                        lang_req=job.get("lang_req"), education=job.get("education"),
                        start_date=job.get("start_date"), min_yoe=job.get("min_yoe"),
                        ai_tech=job.get("ai_tech"), **tag.tag_provenance())
        else:
            notes.append("Tagging returned nothing; the role is stored untagged "
                         "and the tag backfill will retry it.")
    except Exception as exc:  # tagging must never lose the role
        notes.append(f"Tagging failed ({type(exc).__name__}); the role is stored "
                     "untagged and the tag backfill will retry it.")
    years = job.get("min_yoe")
    if years is None:
        _, years = has_experience_wall(job.get("description", ""))
        db.set_yoe(job["id"], years)
    if years and years >= YOE_HIDE_THRESHOLD:
        notes.append(f"The posting asks for {years}+ years of experience, so Browse "
                     "hides it unless Senior 3y+ is on.")
    if job.get("seniority") == "manager":
        notes.append("Tagged manager-level; Browse hides that by default.")
    if job.get("area") in ("other", "wealth"):
        notes.append(f"Tagged area '{job['area']}'; Browse's All tab leaves it out "
                     "(it is under its own tab).")
    return notes
