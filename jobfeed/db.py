"""
SQLite state tracker. Remembers which job IDs have been seen so the scan only
flags truly new openings, and stores the full role record (category, location,
posted date, tags, status) for browsing/filtering in the web app.
"""
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# DB paths whose schema (CREATE TABLE + _migrate) has already run this process.
# web/app.py builds a fresh JobDB per request; without this gate every request
# re-ran the full DDL + a commit. The schema is stable within a process once
# initialized, so repeat constructions for the same path skip it. Keyed by the
# resolved absolute path so two spellings of the same file share the flag.
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()
# `seen_jobs.company` holds the scraper's source label, so one firm arrives as
# "UBS (Graduate Careers)" or "Citadel (HF)" while its own mail says only UBS or
# Citadel. `plain_company` below is the one normalisation: the inbox pass matches
# firm tokens with it and the web app resolves application caps with it. All
# three had their own copy of this regex.
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
# The platforms an employer's mail is actually sent through. `successfactors.eu`
# is why this matches on the platform name rather than a full domain: RWE's mail
# came from `system@successfactors.eu` while applications/account.py's list only
# carries the .com.
_MAIL_PLATFORMS = (
    "workday", "successfactors", "greenhouse", "lever.co", "smartrecruiters",
    "icims", "taleo", "oraclecloud", "ashbyhq", "eightfold", "brassring",
    "pinpoint", "avature", "cornerstone", "jobvite", "recruitee", "personio",
)


def plain_company(name: str) -> str:
    return _PAREN_SUFFIX_RE.sub("", str(name or "")).strip()


def _token_in(token: str, text: str) -> bool:
    """A firm token delimited by non-alphanumerics, so the three-letter names he
    actually applies to work without matching inside a longer word. A length
    cutoff was tried first and silently excluded RWE, whose own mail carries
    "noreply@rwe.com"; UBS, SIG, DRW and AQR would all have gone the same way,
    while a bare substring test would find "sig" inside "signal"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


# CRM stages that mean an application was actually submitted. 'queued' is an
# intention, not a submission, and 'ignored' is a rejection of the role by
# the user — neither consumes a company's application quota.
APPLIED_STATUSES = ("applied", "oa", "interview", "offer", "rejected")

# Application tracks on Application stats (2026-09-23, his request): internships
# and full-time/graduate roles convert very differently, so the page can show
# either alone and compares the two side by side. The split is the tagger's
# job_type; graduate programmes count as full-time.
TRACKS = ("fulltime", "intern")
_TRACK_SQL = {"fulltime": "COALESCE(job_type, 'job') != 'internship'",
              "intern": "job_type = 'internship'"}


def track_clause(track: str | None) -> str:
    """SQL condition for one track, or '' for all."""
    return _TRACK_SQL.get(track or "", "")

# The attended browser workflow is deliberately separate from the CRM stage on
# seen_jobs.  "queued" here means the website has prepared an Antigravity
# handoff; "completed" can only be recorded by the authenticated owner after
# the final review/submit gate.  The capability page used by the browser agent
# is never allowed to make that last transition.
APPLICATION_WORKFLOW_STATES = (
    "queued",
    "in_progress",
    "needs_user_action",
    "review_ready",
    "completed",
    "failed",
)
# `completed` appears on every open state because the user can finish an
# application by hand at any point, and on 2026-09-12 he did: a bp run stopped
# at needs_user_action, he completed it himself, and the row had no way to say
# so. Only the owner can ever reach it — the actor guard in
# transition_application_workflow keeps `completed` out of AGENT_WORKFLOW_STATES.
#
# A run that never stops for the user has nothing to report until the form is
# filled, so `queued` reaches the two open states directly. Requiring
# `in_progress` first made the BNP run on 2026-09-16 a 409 at the finish line,
# and the agent spent seven minutes and two permission prompts reading this
# file to find out why. `started_at` is seeded by whichever open state arrives
# first, so a skipped `in_progress` never loses the run's start time.
APPLICATION_WORKFLOW_TRANSITIONS = {
    "queued": frozenset({"in_progress", "needs_user_action", "review_ready",
                         "failed", "completed"}),
    "in_progress": frozenset({"queued", "needs_user_action", "review_ready", "failed",
                              "completed"}),
    "needs_user_action": frozenset({"queued", "in_progress", "review_ready", "failed",
                                    "completed"}),
    "review_ready": frozenset({"queued", "in_progress", "completed", "failed"}),
    # A failed run can come back: the browser freezes, he restarts the
    # conversation, and the second run fills the form. On 2026-09-18 IMC's
    # restart reached the gate and was refused 409 'failed' -> 'review_ready',
    # so the board kept reading failed while the form sat ready.
    "failed": frozenset({"queued", "in_progress", "needs_user_action",
                         "review_ready", "completed"}),
    "completed": frozenset(),
}
AGENT_WORKFLOW_STATES = frozenset(
    {"in_progress", "needs_user_action", "review_ready", "failed"}
)
APPLICATION_WORKFLOW_COLUMNS = (
    "workflow_id", "job_id", "application_url", "status", "detail",
    "created_at", "updated_at", "started_at", "review_ready_at",
    "completed_at", "updated_by", "launch_status", "launch_request_id",
    "launch_requested_at", "launch_started_at", "launch_finished_at",
    "launch_error_code", "launch_attempts",
    "submitted_confirmed_at", "submitted_evidence",
)
LIMIT_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                   "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def quote_states_number(n: int, quote: str) -> bool:
    """True when `n` appears in `quote` as a digit or a number word. Catches a
    real sentence with the wrong figure attached; shared by the research pass
    and the attended-form evidence so the two gates cannot drift apart."""
    text = " ".join((quote or "").lower().split())
    word = next((w for w, v in LIMIT_NUM_WORDS.items() if v == n), None)
    return bool(re.search(rf"\b{n}\b", text)
                or (word and re.search(rf"\b{word}\b", text)))


# The one CRM order. A status only ever moves forward, and this is also the
# pool applications/mail.py matches an inbox message against and the funnel the
# web app boards. It was copied into all three; they are imported from here now.
APPLICATION_MAIL_ORDER = ("queued", "applied", "oa", "interview", "offer", "rejected")


def _utc_instant(value: str) -> str:
    """An ISO timestamp as a comparable UTC string, '' when unparseable. A
    stamp with no offset is read as UTC."""
    try:
        at = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def mail_rank(status: str) -> int:
    """Position in the forward-only CRM order. A status outside it (new,
    ignored) ranks below everything, so a first confirmation always applies."""
    return (APPLICATION_MAIL_ORDER.index(status)
            if status in APPLICATION_MAIL_ORDER else -1)
APPLICATION_LAUNCH_ERROR_CODES = frozenset({
    "", "accessibility_denied", "antigravity_missing", "antigravity_not_frontmost",
    "new_conversation_missing", "chat_input_missing", "send_button_missing",
    "paste_verification_failed", "second_message_timeout", "helper_interrupted",
    "invalid_payload", "helper_error",
})


def _norm_title(title: str) -> str:
    """Lowercased, whitespace-collapsed title for dedup comparison."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _dedup_key(row: dict) -> tuple:
    """Identity key for collapsing duplicate roles in fetch_jobs().

    Some ATSes (notably Glencore) re-emit the same logical opening on every
    scan under a fresh internal `id` while the canonical posting `url` stays
    constant, so the id-based dedup in mark_seen() never catches them and the
    web UI shows the role N times. We collapse on (company, stable `url`,
    normalized title) when a URL is present. Company and title are load-bearing:
    some scrapers expose one board-level careers URL for every vacancy, and two
    firms can share an ATS landing URL. URL-only identity silently collapsed
    those legitimate roles. Rows with no URL fall back to (company, normalized
    title, location)."""
    url = (row.get("url") or "").strip()
    if url:
        return (
            "url",
            (row.get("company") or "").strip().lower(),
            url,
            _norm_title(row.get("title", "")),
        )
    return (
        "ctl",
        (row.get("company") or "").strip().lower(),
        _norm_title(row.get("title", "")),
        (row.get("location") or "").strip().lower(),
    )


def _dedup_rows(rows: list[dict], desc_lengths=None) -> list[dict]:
    """Collapse duplicate roles, keeping one representative per identity key
    and preserving the input order. The representative is the first row seen
    for each key, with two overrides applied in priority order:

    1. An acted-on row (status != 'new') is preferred so an 'applied'/'ignored'
       marking is never hidden behind a 'new' duplicate.
    2. When NEITHER duplicate is acted-on, the row with the longer description
       wins. ID-churn ATSes (Glencore) re-emit the same role under a fresh id
       each scan; the newest copy (first per the 'recent' sort) can be an empty
       re-insert that would otherwise hide the enriched older row.

    Callers pass rows already ordered by the requested sort, so 'first seen'
    means 'best per the active sort' (e.g. most recent).

    `desc_lengths(ids) -> {id: length}` supplies description lengths when the
    rows don't carry the text; it is called once, only for ids that actually
    collide, so the list query never has to read descriptions."""
    lengths: dict = {}
    if desc_lengths is not None:
        seen_keys: dict[tuple, str] = {}
        colliding: set[str] = set()
        for row in rows:
            key = _dedup_key(row)
            if key in seen_keys:
                colliding.update((seen_keys[key], row["id"]))
            else:
                seen_keys[key] = row["id"]
        if colliding:
            lengths = desc_lengths(sorted(colliding))

    def _desc_len(row: dict) -> int:
        if desc_lengths is not None:
            return lengths.get(row.get("id"), 0)
        return len(row.get("description") or "")

    chosen: dict[tuple, int] = {}   # key -> index into `out`
    out: list[dict] = []
    for row in rows:
        key = _dedup_key(row)
        idx = chosen.get(key)
        if idx is None:
            chosen[key] = len(out)
            out.append(row)
            continue
        kept = out[idx]
        kept_acted = kept.get("status") != "new"
        row_acted = row.get("status") != "new"
        if kept_acted and not row_acted:
            continue  # an acted-on row already won — never demote it
        if row_acted and not kept_acted:
            out[idx] = row  # an acted-on duplicate outranks the 'new' one
        elif kept_acted == row_acted and _desc_len(row) > _desc_len(kept):
            # Neither (or both) acted-on: prefer the fuller description so a
            # fresh empty ID-churn duplicate can't hide the enriched copy.
            out[idx] = row
    return out

def _split_lang_facet(rows: list[str]) -> list[str]:
    """Explode the comma-separated lang_req values ("de,fr") into the distinct
    individual codes for a facet dropdown, sorted and deduped. A row requiring
    two languages contributes both codes once each."""
    return sorted({c for r in rows for c in (r or "").split(",") if c})


# Shared with scripts/split_descriptions.py, which creates it on a pre-split DB.
JOB_DESCRIPTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS job_descriptions (
        job_id TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        fetched_at TEXT
    )
"""


# Columns added after the original (id, company, title, url, first_seen)
# schema. Existing databases are upgraded in place via ALTER TABLE.
EXTRA_COLUMNS = [
    ("category", "TEXT DEFAULT ''"),
    ("location", "TEXT DEFAULT ''"),
    ("posted", "TEXT DEFAULT ''"),
    ("last_seen", "TEXT"),
    ("status", "TEXT DEFAULT 'new'"),
    # description / description_fetched_at lived here until 2026-09-23; they
    # are now the job_descriptions table (see _split_descriptions).
    # Structured tags from the all-Haiku tagging pass (jobfeed/tag.py). Populated
    # for every stored role so the web UI can filter by sector + function +
    # location without re-filtering at scrape time. Empty string = untagged
    # (still filterable as "unclassified"). See jobfeed/tag.py for the label sets.
    ("function", "TEXT DEFAULT ''"),       # LEGACY (flat taxonomy) — superseded by area/desk, kept for back-compat
    ("area", "TEXT DEFAULT ''"),           # markets/quant/research/ibd/capital-markets/asset-management/wealth/risk/other
    ("desk", "TEXT DEFAULT ''"),           # markets function: trading/sales/structuring/research (only when area=markets)
    ("seniority", "TEXT DEFAULT ''"),      # intern/graduate/analyst/associate
    ("job_type", "TEXT DEFAULT 'job'"),    # job/internship/graduate-programme
    ("loc_city", "TEXT DEFAULT ''"),
    ("loc_country", "TEXT DEFAULT ''"),
    ("loc_region", "TEXT DEFAULT ''"),     # Europe/Americas/APAC/MEA
    ("work_mode", "TEXT DEFAULT ''"),      # onsite/hybrid/remote
    # Description-derived facets from the Haiku pass (jobfeed/tag.py). Unlike the
    # location/area columns, these default to NULL — NOT '' — on purpose. A NULL
    # means "never tagged with a description present" (the tagger couldn't see
    # the requirements), which the nightly re-tag hook keys off to re-tag rows
    # that gained a description after their first tag pass. A '' means "tagged
    # WITH a description, genuinely no requirement" (English-only / no degree /
    # no start date). The two must stay distinct, hence no DEFAULT.
    ("lang_req", "TEXT"),                  # comma-sep ISO codes beyond English; ''=English-only
    ("education", "TEXT"),                 # bachelor/master/phd required floor; ''=none stated
    ("start_date", "TEXT"),                # asap / YYYY / YYYY-MM; ''=unstated
    ("tagged_at", "TEXT"),
    ("tag_provider", "TEXT DEFAULT ''"),
    ("tag_model", "TEXT DEFAULT ''"),
    ("tag_rubric_version", "TEXT DEFAULT ''"),
    # Required years of experience detected in the description (0 = none / not
    # detected). A junior-titled role with "minimum 5 years" buried in the body
    # is really senior; the web app hides min_yoe >= 3 by default.
    ("min_yoe", "INTEGER DEFAULT 0"),
    # Application CRM: when the role was moved to 'applied' (stamped once), and
    # free-text notes the user keeps per role. NULL until set.
    ("applied_at", "TEXT"),
    ("notes", "TEXT"),
    # User "keep an eye on this" flag, set from the star toggle in the web UI.
    # Orthogonal to status: a role can be both favorite and applied/interview.
    ("favorite", "INTEGER DEFAULT 0"),
    # Stamped when a role no longer appears in a successful scrape of its
    # company's board (presumably taken down). Cleared if the role reappears.
    # NULL = still live / not yet checked since being stored.
    ("delisted_at", "TEXT"),
    # Application deadline as an ISO date (YYYY-MM-DD). NULL = rolling / no
    # stated deadline, which is the overwhelmingly common case and is rendered
    # explicitly as "rolling" rather than as a blank. Not every source states
    # one and no scraper is required to find one — the UI is built to read NULL
    # as a real answer, not as missing data.
    ("deadline", "TEXT"),
    # Set when a row was delisted by the AGE floor rather than by being absent
    # from a clean board scrape. These two are not interchangeable: an absent-
    # from-board delist is evidence the role is gone, an age delist is only
    # evidence nobody has confirmed it recently. purge_delisted_other therefore
    # refuses to hard-delete age-delisted rows — a multi-week scraper outage
    # (2026-08-31 DNS failure, or the M1 being off) would otherwise turn "we
    # stopped looking" into "delete it". Cleared when the role reappears.
    ("delisted_by_age", "INTEGER DEFAULT 0"),
]


# Columns returned by fetch_jobs() / surfaced in the web UI and JSON API.
# `description` comes from job_descriptions: get_job always carries it,
# fetch_jobs only when asked (with_description=True).
DISPLAY_COLUMNS = [
    "id", "company", "category", "title", "location", "posted", "status",
    "first_seen", "last_seen", "url", "area", "desk", "seniority", "job_type",
    "loc_city", "loc_country", "loc_region", "work_mode", "min_yoe",
    "lang_req", "education", "start_date",
    "applied_at", "notes", "favorite", "delisted_at", "deadline",
]

# Roles requiring this many years (detected in the description) are treated as
# senior and hidden from the web app by default.
YOE_HIDE_THRESHOLD = 3


# Deadline (S12). NULL deadline = rolling, which is the common case and a real
# answer, not missing data — every clause below is written NULL-safe on purpose.
# A role whose deadline has passed is as dead as a delisted one, so it rides the
# same "Include delisted" toggle rather than inventing a second control.
_CLOSING_SQL = {
    "7": "deadline IS NOT NULL AND date(deadline) BETWEEN date('now') AND date('now', '+7 day')",
    "30": "deadline IS NOT NULL AND date(deadline) BETWEEN date('now') AND date('now', '+30 day')",
    "rolling": "deadline IS NULL",
}
_NOT_PAST_DEADLINE_SQL = "(deadline IS NULL OR date(deadline) >= date('now'))"


def _closing_clause(value: str | None) -> str | None:
    return _CLOSING_SQL.get((value or "").strip()) if value else None

# Columns the UI may request distinct values for (facet dropdowns). Whitelist
# guards the column name interpolated into distinct().
FACET_COLUMNS = {
    "category", "area", "desk", "seniority", "job_type", "loc_region",
    "loc_country", "loc_city", "work_mode", "status", "company",
    "education", "lang_req",
}


def _job_filter_sql(f: dict, *, omit: set[str] | None = None,
                    initial: list[str] | None = None) -> tuple[list[str], list]:
    """Single source of truth for Browse, area badges, and scoped facets."""
    omit = omit or set()
    clauses = list(initial or [])
    params: list = []
    def val(key): return None if key in omit else f.get(key)
    def exact(key, col=None):
        if val(key): clauses.append(f"{col or key} = ?"); params.append(val(key))
    exact("status")
    if val("statuses"):
        clauses.append("status IN (%s)" % ",".join("?" * len(val("statuses")))); params.extend(val("statuses"))
    for key in ("category", "company"):
        if val(key): clauses.append(f"{key} LIKE ?"); params.append(f"%{val(key)}%")
    # `firm`: one firm across its source labels ("UBS" and "UBS (Graduate
    # Careers)"), matched exactly -- the plain_company rule in SQL. `company`
    # is a substring match, which made "EY" include Morgan Stanley and Walleye.
    if val("firm"):
        firm_name = str(val("firm")).strip()
        esc = (firm_name.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_"))
        clauses.append("(company = ? COLLATE NOCASE OR company LIKE ? ESCAPE '\\')")
        params.extend([firm_name, esc + " (%)"])
    for key in ("area", "desk", "seniority", "job_type", "loc_region",
                "loc_country", "loc_city", "work_mode", "education"):
        exact(key)
    if val("areas"):
        clauses.append(f"area IN ({','.join('?' * len(val('areas')))})"); params.extend(val("areas"))
    if val("hide_other"): clauses.append("area != 'other'")
    if val("hide_yoe"):
        clauses.append("(min_yoe IS NULL OR min_yoe < ?) AND seniority != 'manager'"); params.append(YOE_HIDE_THRESHOLD)
    if val("hide_internships"): clauses.append("job_type != 'internship'")
    if val("hide_associates"): clauses.append("(seniority IS NULL OR seniority != 'associate')")
    if val("hide_delisted"): clauses.extend(["delisted_at IS NULL", _NOT_PAST_DEADLINE_SQL])
    if (cc := _closing_clause(val("closing"))): clauses.append(cc)
    if val("favorite"): clauses.append("favorite = 1")
    if val("lang_req"):
        if val("lang_req") == "none": clauses.append("lang_req = ''")
        else: clauses.append("(',' || lang_req || ',') LIKE ?"); params.append(f"%,{val('lang_req')},%")
    if val("start"):
        if val("start") == "asap": clauses.append("start_date = 'asap'")
        else: clauses.append("start_date LIKE ?"); params.append(f"{val('start')}%")
    if val("companies") is not None:
        companies = val("companies")
        if not companies: clauses.append("0")
        else: clauses.append(f"company IN ({','.join('?' * len(companies))})"); params.extend(companies)
    if val("exclude_companies"):
        xs = val("exclude_companies"); clauses.append(f"company NOT IN ({','.join('?' * len(xs))})"); params.extend(xs)
    if val("q"): clauses.append("(title LIKE ? OR company LIKE ?)"); params.extend([f"%{val('q')}%"] * 2)
    if val("since"): clauses.append("first_seen >= ?"); params.append(val("since"))
    return clauses, params


class JobDB:
    def __init__(self, path: str, check_same_thread: bool = True):
        self._transaction_depth = 0
        self.conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        # WAL lets the web app read while the scraper writes; busy_timeout
        # avoids "database is locked" if a write is mid-flight on read.
        self.conn.execute("PRAGMA journal_mode=WAL")
        # 15s (was 5s): during a scan's commit bursts the writer holds the DB
        # long enough that concurrent web writes (favorite/status/notes) were
        # hitting "database is locked" at 5s. A generous timeout just waits.
        self.conn.execute("PRAGMA busy_timeout=15000")
        # Gate the schema init (DDL + commit) behind a per-path flag so repeat
        # constructions for the same file (web/app.py builds one JobDB per
        # request) skip it — the schema doesn't change within a process. An
        # in-memory DB (":memory:") is a fresh empty database per connection, so
        # it must always init. See _SCHEMA_READY.
        key = ":memory:" if path == ":memory:" else os.path.abspath(path)
        if key == ":memory:" or key not in _SCHEMA_READY:
            self._init()
            if key != ":memory:":
                with _SCHEMA_LOCK:
                    _SCHEMA_READY.add(key)

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    def begin_batch(self) -> None:
        self._transaction_depth += 1

    def end_batch(self) -> None:
        if self._transaction_depth <= 0:
            raise RuntimeError("end_batch without begin_batch")
        self._transaction_depth -= 1
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """Batch many setter calls into one atomic SQLite commit."""
        outer = self._transaction_depth == 0
        self._transaction_depth += 1
        try:
            yield self
        except Exception:
            if outer:
                self.conn.rollback()
            raise
        else:
            if outer:
                self.conn.commit()
        finally:
            self._transaction_depth -= 1

    def _init(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                id TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                url TEXT,
                first_seen TEXT,
                category TEXT DEFAULT '',
                location TEXT DEFAULT '',
                posted TEXT DEFAULT '',
                last_seen TEXT,
                status TEXT DEFAULT 'new'
            )
        """)
        self._migrate()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT,
                new_jobs INTEGER,
                firms_checked INTEGER,
                errors INTEGER,
                duration_s REAL
            )
        """)
        # Tiny key/value store for app state (e.g. the web app's "last check"
        # timestamp for the new-since-last-visit highlight).
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # Backoff state for the nightly description backstop. Without it the
        # 400-row budget went to the same hopeless rows every night: 14 nights
        # to 2026-09-22 filled 0-6 rows each, and 1,166 of the 1,272 rows it
        # kept retrying were delisted postings that can never be fetched. A
        # side table rather than seen_jobs columns, because seen_jobs is the
        # scanned-on-every-request table and export_shared aborts on any new
        # column in it.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS description_attempts (
                job_id TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL
            )
        """)
        # Human-labelled, frozen evaluation set for measuring tagger quality.
        # The baseline prediction/provenance is captured once; later edits only
        # update the human truth, so model comparisons never move the goalposts.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_evaluations (
                job_id TEXT PRIMARY KEY,
                evaluated_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                review_bucket TEXT NOT NULL,
                baseline_area TEXT DEFAULT '', baseline_desk TEXT DEFAULT '',
                baseline_seniority TEXT DEFAULT '', baseline_job_type TEXT DEFAULT '',
                human_area TEXT DEFAULT '', human_desk TEXT DEFAULT '',
                human_seniority TEXT DEFAULT '', human_job_type TEXT DEFAULT '',
                provider TEXT DEFAULT '', model TEXT DEFAULT '',
                rubric_version TEXT DEFAULT '', corrected INTEGER DEFAULT 0,
                FOREIGN KEY(job_id) REFERENCES seen_jobs(id)
            )
        """)
        # Company-level application caps ("you may submit at most N
        # applications per recruitment year"). Deliberately a separate table
        # keyed by company, not a seen_jobs column: the cap is a property of
        # the firm's recruiting policy, shared by every role it posts, and it
        # outlives any individual posting.
        #
        # ACCURACY CONTRACT — max_per_cycle is NON-NULL only when the number
        # came from text we actually fetched (confidence='stated', with the
        # verbatim `quote` verified as a substring of that page) or from
        # the user typing it in after seeing it on the application form
        # (confidence='manual'). It is never inferred, never recalled, never
        # guessed by a model. 'unknown' and 'fetch_failed' are real, useful
        # answers and keep max_per_cycle NULL.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS company_limits (
                company TEXT PRIMARY KEY,
                max_per_cycle INTEGER,
                cycle TEXT DEFAULT '',
                locations_count_separately INTEGER,
                shared_across_programmes INTEGER,
                varies_by_region INTEGER,
                confidence TEXT NOT NULL DEFAULT 'unknown',
                strength TEXT DEFAULT '',
                quote TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                checked_at TEXT,
                updated_at TEXT,
                updated_by TEXT DEFAULT '',
                form_verdict TEXT DEFAULT '',
                form_max INTEGER,
                form_quote TEXT DEFAULT '',
                form_workflow_id TEXT DEFAULT '',
                form_checked_at TEXT
            )
        """)
        # One durable attended-application workflow per role.  The UNIQUE job
        # key makes repeated Apply clicks idempotent, while application_url is
        # snapshotted at queue time so the exact URL handed to /browser remains
        # auditable even if a later scrape changes the role row.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS application_mail_events (
                message_key TEXT PRIMARY KEY,
                received_at TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                job_id TEXT NOT NULL DEFAULT '',
                proposed_status TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                match_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                -- The sentence asking him to do something. Independent of
                -- status: an assessment invitation moves the CRM to `oa`, but
                -- "action required: complete your pre-interview assessment"
                -- moves nothing and is the costliest mail to miss.
                action_required TEXT NOT NULL DEFAULT '',
                -- Slug for the task itself, so three Shell mails about one
                -- assessment collapse into one thing to do.
                task_key TEXT NOT NULL DEFAULT '',
                -- Where to go and by when, copied from the mail, so a task is
                -- one click rather than a hunt back through the inbox.
                action_url TEXT NOT NULL DEFAULT '',
                action_deadline TEXT NOT NULL DEFAULT '',
                resolved_at TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS application_workflows (
                workflow_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                application_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','in_progress','needs_user_action',
                                     'review_ready','completed','failed')),
                detail TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                review_ready_at TEXT,
                completed_at TEXT,
                updated_by TEXT NOT NULL DEFAULT 'owner',
                launch_status TEXT NOT NULL DEFAULT 'idle'
                    CHECK(launch_status IN ('idle','pending','running','succeeded','failed')),
                launch_request_id TEXT,
                launch_requested_at TEXT,
                launch_started_at TEXT,
                launch_finished_at TEXT,
                launch_error_code TEXT NOT NULL DEFAULT '',
                launch_attempts INTEGER NOT NULL DEFAULT 0,
                -- Stamped only from an employer's own confirmation mail. The
                -- agent may never write it and `completed` still belongs to
                -- the user: this records that the firm says it has the
                -- application, which is the one thing neither of them can fake.
                submitted_confirmed_at TEXT,
                submitted_evidence TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES seen_jobs(id)
            )
        """)
        # One row per attempt of a browser agent at a workflow, kept apart from
        # the workflow's outcome and the launcher's delivery state
        # (notes/ATTENDED_AUTOMATION.md, steps 2-3). `state` says whether an
        # agent may be alive: launching/running/draining hold the one global
        # worker lease, and only a positively confirmed stop (`agent_stopped_at`)
        # releases it. Every conversation shares one browser cursor, so two live
        # agents is the failure this table exists to make impossible.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS application_runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'queued'
                    CHECK(state IN ('queued','launching','running','draining',
                                    'parked','review_ready','failed','cancelled')),
                priority INTEGER NOT NULL DEFAULT 0,
                manual INTEGER NOT NULL DEFAULT 0,
                queued_at TEXT NOT NULL,
                launching_at TEXT,
                running_at TEXT,
                draining_at TEXT,
                finished_at TEXT,
                last_activity_at TEXT,
                agent_stopped_at TEXT,
                stop_source TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                recovery_required INTEGER NOT NULL DEFAULT 0,
                recovery_reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(workflow_id) REFERENCES application_workflows(workflow_id)
            )
        """)
        # At most one run may hold the worker lease. A partial unique index makes
        # a second concurrent holder a constraint error, not a code-path promise.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS application_runs_one_active "
            "ON application_runs((1)) WHERE state IN ('launching','running','draining')")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS application_runs_one_open_per_workflow "
            "ON application_runs(workflow_id) "
            "WHERE state IN ('queued','launching','running','draining')")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS application_autopilot (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                paused_reason TEXT NOT NULL DEFAULT '',
                paused_at TEXT,
                updated_at TEXT,
                safety_version INTEGER NOT NULL DEFAULT 1,
                foreign_confirmed_at TEXT
            )
        """)
        autopilot_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(application_autopilot)")
        }
        for col, decl in (("safety_version", "INTEGER NOT NULL DEFAULT 0"),
                          ("foreign_confirmed_at", "TEXT")):
            if col not in autopilot_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE application_autopilot ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        # Automatic advancement is opt-in. Version 1 invalidates the unsafe
        # rollout that defaulted to enabled before it had qualification runs;
        # existing installations are paused once and require an owner resume.
        self.conn.execute(
            "INSERT OR IGNORE INTO application_autopilot "
            "(id, enabled, safety_version) VALUES (1, 0, 1)")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE application_autopilot SET enabled=0, "
            "paused_reason='safety fixes deployed; qualification and explicit resume required', "
            "paused_at=?, updated_at=?, safety_version=1 "
            "WHERE safety_version < 1", (now, now))
        mail_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(application_mail_events)")
        }
        # rejection_reason / reason_kind: why an application was declined, the
        # sentence and its deterministic category. reapply_kind distinguishes
        # a fixed duration from a cycle rule, so "per academic year" can be
        # preserved without fabricating an end date.
        for col in ("action_required", "task_key", "action_url", "action_deadline",
                    "rejection_reason", "reason_kind", "reapply_kind",
                    "reapply_after", "reapply_quote"):
            if mail_cols and col not in mail_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE application_mail_events "
                        f"ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    pass
        workflow_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(application_workflows)")
        }
        for col, decl in (
            ("launch_status", "TEXT NOT NULL DEFAULT 'idle'"),
            ("launch_request_id", "TEXT"),
            ("launch_requested_at", "TEXT"),
            ("launch_started_at", "TEXT"),
            ("launch_finished_at", "TEXT"),
            ("launch_error_code", "TEXT NOT NULL DEFAULT ''"),
            ("launch_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("submitted_confirmed_at", "TEXT"),
            ("submitted_evidence", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in workflow_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE application_workflows ADD COLUMN {col} {decl}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_workflows_status_updated "
            "ON application_workflows(status, updated_at DESC)"
        )
        # Account metadata only. Passwords live in macOS Keychain and email
        # stays in the authoritative applicant profile, never in jobs.db.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS application_accounts (
                account_scope TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                credential_service TEXT NOT NULL,
                prepared_at TEXT NOT NULL,
                verified_at TEXT,
                FOREIGN KEY(workflow_id) REFERENCES application_workflows(workflow_id)
            )
        """)
        # company_limits gained `strength` after the first research pass: a
        # firm saying "we recommend no more than three" (J.P. Morgan) and one
        # saying "we only accept one application per person" (Macquarie) are
        # not the same constraint, and treating advice as a rule would cost
        # applications he was allowed to make.
        #
        # `varies_by_region` came out of UBS, whose FAQ sets one allowance for
        # the UK, another for the rest of EMEA, and a third for Switzerland /
        # US / APAC. the user has starred that programme in nine locations
        # spanning all three, so collapsing it to a single integer would have
        # been wrong for most of them. When this is set the UI must not present
        # max_per_cycle as the rule — the quote is the answer.
        lim_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(company_limits)")}
        # The form_* columns hold what an attended run saw on the application
        # form itself (2026-09-13). They sit beside the research fields rather
        # than in them because set_company_limit() overwrites every research
        # field on each sweep, and a sweep must never be able to erase the one
        # observation made at the point of submission.
        for col, decl in (("strength", "TEXT DEFAULT ''"),
                          ("varies_by_region", "INTEGER"),
                          ("form_verdict", "TEXT DEFAULT ''"),
                          ("form_max", "INTEGER"),
                          ("form_quote", "TEXT DEFAULT ''"),
                          ("form_workflow_id", "TEXT DEFAULT ''"),
                          ("form_checked_at", "TEXT")):
            if lim_cols and col not in lim_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE company_limits ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        # ── Campus programmes: the user's marks, not the sweep's data ──
        # `jobfeed/campus_sweep.py` owns what a graduate programme is and what its page
        # said; that stays in results.jsonl and is re-read fresh on every
        # sweep. This table holds ONLY what he does about it, so a new sweep
        # never overwrites a tick and a tick never contradicts a live page.
        # Keyed by firm+programme (campus._key), which survives a re-sweep.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campus_state (
                key TEXT PRIMARY KEY,
                firm TEXT NOT NULL DEFAULT '',
                programme TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'todo',
                season TEXT DEFAULT '',
                note TEXT DEFAULT '',
                updated_at TEXT
            )
        """)
        # Index the canonical posting url so the scan loop's ID-churn lookup
        # (find_active_by_url — touch/forward-fill an existing row instead of
        # inserting a fresh-id duplicate) is a single indexed probe, not a
        # per-job table scan.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_jobs_url ON seen_jobs(url)"
        )
        # Posting text, one row per job that has any; no row = no description.
        # Kept out of seen_jobs because a TEXT column there put every later
        # column behind the description's overflow pages: on the live DB
        # (58.7k rows, 275 MB of text, 2026-09-23) each filter query walked the
        # descriptions to read `area` or `delisted_at`, and Browse spent
        # 1.5 s of a 1.7 s request in SQLite. Same queries without the text
        # ran 4-8x faster.
        self.conn.execute(JOB_DESCRIPTIONS_DDL)
        # A database from before the split still carries the text in
        # seen_jobs. Moving it rewrites the whole table, so it is an explicit,
        # backed-up step (scripts/split_descriptions.py), never a side effect
        # of opening the DB. Until then refuse to run: this code would read
        # an empty job_descriptions and show every role without its text.
        if "description" in {r[1] for r in
                             self.conn.execute("PRAGMA table_info(seen_jobs)")}:
            raise RuntimeError(
                "seen_jobs still holds descriptions; run "
                "scripts/split_descriptions.py on this database first")
        # Read-side compatibility: the old shape, for the scripts and health
        # checks that only read. Writes go through the JobDB methods.
        self.conn.execute("""
            CREATE VIEW IF NOT EXISTS jobs_with_description AS
            SELECT s.*, d.description AS description,
                   d.fetched_at AS description_fetched_at
            FROM seen_jobs s LEFT JOIN job_descriptions d ON d.job_id = s.id
        """)
        self._commit()

    def _migrate(self):
        """Non-destructively add any columns missing from older databases.

        Tolerant of the cross-process race on deploy: the KeepAlive web app and
        the scheduled scraper can both restart together after a schema change,
        both read table_info before either commits the ALTER, and both then try
        the same `ADD COLUMN`. SQLite raises "duplicate column name" on the
        loser, which used to crash JobDB.__init__ (the web app crash-looped
        until the scraper won). Swallowing only that specific error makes the
        migration idempotent regardless of which process runs it first."""
        cur = self.conn.execute("PRAGMA table_info(seen_jobs)")
        existing = {row[1] for row in cur.fetchall()}
        for name, definition in EXTRA_COLUMNS:
            if name not in existing:
                try:
                    self.conn.execute(
                        f"ALTER TABLE seen_jobs ADD COLUMN {name} {definition}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        # runs.duration_s: wall-clock scan runtime (2026-07-10, Stats page).
        # Same duplicate-column race tolerance as above. On a FRESH database
        # the runs table doesn't exist yet at this point (it's created after
        # _migrate, with duration_s in the CREATE) — empty run_cols skips.
        run_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if run_cols and "duration_s" not in run_cols:
            try:
                self.conn.execute("ALTER TABLE runs ADD COLUMN duration_s REAL")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        # Drop dead tag columns the tagger never populated (<0.2% filled):
        # asset_class / coverage_sector / language. Idempotent + race-tolerant
        # (a concurrent process may have already dropped them). Requires
        # SQLite >= 3.35; on an older build the drop is skipped, not fatal.
        for name in ("asset_class", "coverage_sector", "language"):
            if name in existing:
                try:
                    self.conn.execute(f"ALTER TABLE seen_jobs DROP COLUMN {name}")
                except sqlite3.OperationalError as exc:
                    if "no such column" not in str(exc).lower():
                        raise

    def seen(self, job_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen_jobs WHERE id = ?", (job_id,))
        return cur.fetchone() is not None

    def stored_url(self, job_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT url FROM seen_jobs WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def touch_seen(self, job_id: str) -> bool:
        """Bump last_seen for a role still appearing on its board, without
        touching first_seen/status/description. Drives the Sources/Stats
        silent-zero detection (company_recent_volume keys off last_seen).
        Clears delisted_at — a role found again is no longer delisted, even
        if it was pulled and reposted. Returns True if the row existed."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE seen_jobs SET last_seen = ?, delisted_at = NULL, "
            "delisted_by_age = 0 WHERE id = ?",
            (now, job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def find_active_by_url(self, url: str, exclude_id: str, *,
                           company: str = "", title: str = "") -> str | None:
        """Return the id of an existing row carrying this canonical `url` under
        a DIFFERENT id (exclude_id), or None. Backs the scan loop's ID-churn
        guard: some ATSes (Glencore) re-emit the same logical opening under a
        fresh internal id every scan while the posting `url` is stable, so the
        id-keyed mark_seen() sees each as brand-new, inserts a duplicate row,
        and the old enriched row goes delist-eligible — its captured
        description then risks being hidden by the empty fresh copy at display
        time (see _dedup_rows). Instead we touch/forward-fill the existing row.

        When company/title are supplied, both must match too. URL-only matching
        is unsafe because some sources expose one board-level careers URL for
        every vacancy. Prefers a still-live row over a delisted one, then the
        most recently seen. Empty/blank URL returns None."""
        if not url or not url.strip():
            return None
        params: list = [url.strip(), exclude_id]
        sql = "SELECT id, title FROM seen_jobs WHERE url = ? AND id != ?"
        if company:
            sql += " AND company = ?"
            params.append(company)
        sql += " ORDER BY (delisted_at IS NOT NULL), last_seen DESC"
        rows = self.conn.execute(sql, params).fetchall()
        wanted_title = _norm_title(title)
        if wanted_title:
            rows = [row for row in rows if _norm_title(row[1]) == wanted_title]
        return rows[0][0] if rows else None

    def fill_description_if_missing(self, job_id: str, text: str) -> bool:
        """Forward-fill a listing-payload description onto a row that has
        none; never overwrites an existing one. Heals rows stored before
        their ATS started shipping descriptions inline (e.g. the ~500
        Greenhouse rows predating content=true, 2026-07-01) — the scan's
        already-seen path otherwise discards the freshly fetched text."""
        if not text:
            return False
        cur = self.conn.execute(
            "INSERT INTO job_descriptions (job_id, description, fetched_at) "
            "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM seen_jobs WHERE id = ?) "
            "ON CONFLICT(job_id) DO NOTHING",
            (job_id, text, datetime.now(timezone.utc).isoformat(), job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def mark_seen(self, job_id: str, company: str = "", title: str = "", url: str = "",
                  category: str = "", location: str = "", posted: str = "",
                  description: str = "", deadline: str = ""):
        """`deadline` is only ever set by a source that states one outright.

        The column and the whole S12 closing filter shipped before any scraper
        could fill it, so every row in the database had a NULL deadline and
        "closing in 7 days" could never match anything. Universia is the first
        source that publishes `validThrough`, and a graduate programme is
        exactly the case the filter was built for. A later scrape never clears
        a stored date: a board that stops repeating a deadline has not
        withdrawn it."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO seen_jobs
                (id, company, title, url, first_seen, category, location, posted,
                 last_seen, status, deadline)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen,
                delisted_at = NULL, delisted_by_age = 0,
                deadline = COALESCE(excluded.deadline, seen_jobs.deadline)
            """,
            (job_id, company, title, url, now, category, location, posted,
             now, deadline or None),
        )
        # No row for an empty description, so enrichment can target the gap.
        if description:
            self.conn.execute(
                "INSERT OR IGNORE INTO job_descriptions (job_id, description, fetched_at) "
                "VALUES (?, ?, ?)", (job_id, description, now))
        self._commit()

    def set_deadline(self, job_id: str, deadline: str) -> bool:
        """Record or move the closing date a source states for a stored role.

        Separate from mark_seen because that only runs for brand-new rows: a
        board that starts stating a deadline, or moves one, has to reach rows
        it already created. Only a stated date is ever written, so this can
        never blank one out."""
        if not deadline:
            return False
        cur = self.conn.execute(
            "UPDATE seen_jobs SET deadline = ? WHERE id = ? "
            "AND coalesce(deadline, '') <> ?", (deadline, job_id, deadline))
        self._commit()
        return cur.rowcount > 0

    def upgrade_description_if_better(self, job_id: str, text: str) -> bool:
        """Forward-fill a missing description OR upgrade a STUB one. Overwrites
        only when the row currently holds a stub (<800 chars — e.g. a JS-shell
        title captured before the ATS's inline-description path existed) AND the
        incoming text is materially longer. Never shrinks a real description.

        Fixes the freeze where fill_description_if_missing (NULL-only) left an
        inline-capture source permanently stuck on a pre-capability stub even
        though every later scrape returned the full body."""
        if not text:
            return False
        cur = self.conn.execute(
            "INSERT INTO job_descriptions (job_id, description, fetched_at) "
            "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM seen_jobs WHERE id = ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "description = excluded.description, fetched_at = excluded.fetched_at "
            "WHERE length(job_descriptions.description) < 800 "
            "AND length(excluded.description) > length(job_descriptions.description)",
            (job_id, text, datetime.now(timezone.utc).isoformat(), job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def set_description(self, job_id: str, description: str) -> bool:
        """Populate or overwrite the description for one job (used by the
        enrichment pass). Returns True if the row exists."""
        if not self.seen(job_id):
            return False
        if description:
            self.conn.execute(
                "INSERT INTO job_descriptions (job_id, description, fetched_at) "
                "VALUES (?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                "description = excluded.description, fetched_at = excluded.fetched_at",
                (job_id, description, datetime.now(timezone.utc).isoformat()))
            self.conn.execute(
                "DELETE FROM description_attempts WHERE job_id = ?", (job_id,))
        else:
            self.conn.execute(
                "DELETE FROM job_descriptions WHERE job_id = ?", (job_id,))
        self._commit()
        return True

    # Days before a failed backstop fetch is retried: 1, 2, 4, 8, 16, then
    # every 30. A transient failure is retried the next night; a page that
    # never yields text costs one fetch a month instead of one a night.
    DESCRIPTION_RETRY_CAP_DAYS = 30

    def record_description_miss(self, job_id: str) -> None:
        """Push a row's next backstop attempt out by exponential backoff."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT attempts FROM description_attempts WHERE job_id = ?",
            (job_id,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        wait = min(2 ** (attempts - 1), self.DESCRIPTION_RETRY_CAP_DAYS)
        self.conn.execute(
            "INSERT INTO description_attempts "
            "(job_id, attempts, last_attempt_at, next_attempt_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "attempts = excluded.attempts, "
            "last_attempt_at = excluded.last_attempt_at, "
            "next_attempt_at = excluded.next_attempt_at",
            (job_id, attempts, now.isoformat(),
             (now + timedelta(days=wait)).isoformat()))
        self._commit()

    def jobs_missing_description(self, limit: int = 100,
                                 max_age_days: int | None = None,
                                 exclude_prefixes: tuple = ()) -> list[dict]:
        """Return live rows that have no description yet and are due for an
        attempt. If max_age_days is set, skip rows whose first_seen is older
        than that — supports a rolling window so we don't enrich roles that
        have already aged out. exclude_prefixes drops id prefixes known to be
        unenrichable (JS-shell ATSes with no detail enricher) so hopeless rows
        can't starve the nightly budget by re-entering the queue every run.

        Delisted rows are skipped: the posting is gone, so the fetch cannot
        succeed, and a role that reappears has delisted_at cleared and
        re-enters the queue. Rows whose last attempt failed wait out their
        backoff (record_description_miss)."""
        sql = (
            "SELECT id, company, title, url FROM seen_jobs "
            "WHERE delisted_at IS NULL "
            "AND id NOT IN (SELECT job_id FROM job_descriptions) "
            "AND id NOT IN (SELECT job_id FROM description_attempts "
            "WHERE next_attempt_at > ?)"
        )
        params: list = [datetime.now(timezone.utc).isoformat()]
        if max_age_days is not None:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            sql += " AND first_seen >= ?"
            params.append(cutoff)
        for p in exclude_prefixes:
            # Escape the LIKE '_' wildcard so 'beesite_' means that literal
            # prefix, not 'beesiteX'.
            sql += " AND id NOT LIKE ? ESCAPE '\\'"
            params.append(p.replace("_", "\\_") + "%")
        sql += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return [dict(zip(("id", "company", "title", "url"), row)) for row in cur.fetchall()]

    def prune_old_descriptions(self, max_age_days: int) -> int:
        """NULL the description text on rows older than max_age_days to bound
        file size; keeps the row itself so status history is preserved. Returns
        rows updated.

        Descriptions on rows you've acted on (status != 'new', i.e. applied or
        ignored) are NEVER pruned — once you apply, the firm's posting often
        disappears, so we keep our captured copy permanently for lookup."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM job_descriptions WHERE job_id IN "
            "(SELECT id FROM seen_jobs WHERE first_seen < ? AND status = 'new')",
            (cutoff,),
        )
        self._commit()
        return cur.rowcount

    # --- inbox-driven CRM updates (applications/mail.py) -------------------

    def record_application_mail(self, event: dict) -> None:
        """Append one processed message. Every row keeps the sentence that drove
        it, so a wrong transition can be seen rather than merely suspected."""
        self.conn.execute(
            """INSERT OR IGNORE INTO application_mail_events
               (message_key, received_at, sender, subject, job_id,
                proposed_status, outcome, evidence, match_reason, action_required,
                task_key, action_url, action_deadline, rejection_reason,
                reason_kind, reapply_kind, reapply_after, reapply_quote, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event["message_key"], event["received_at"], event["sender"],
             event["subject"], event["job_id"], event["proposed_status"],
             event["outcome"], event["evidence"], event["match_reason"],
             event.get("action_required", ""), event.get("task_key", ""),
             event.get("action_url", ""), event.get("action_deadline", ""),
             event.get("rejection_reason", ""), event.get("reason_kind", ""),
             event.get("reapply_kind", ""),
             event.get("reapply_after", ""), event.get("reapply_quote", ""),
             datetime.now(timezone.utc).isoformat()),
        )
        self._commit()

    def application_mail_seen(self, message_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM application_mail_events WHERE message_key=?",
            (message_key,),
        ).fetchone() is not None

    def open_application_actions(self, limit: int = 40) -> list[dict]:
        """Mail that asks him to do something and has not been ticked off.

        Kept separate from the status the mail implies: an assessment
        invitation moves the role to `oa` automatically and still leaves a task
        nobody would otherwise see until the deadline had passed."""
        cur = self.conn.execute(
            "SELECT e.message_key, e.received_at, e.subject, e.action_required, "
            "e.task_key, e.action_url, e.action_deadline, e.job_id, "
            "j.company, j.title "
            "FROM application_mail_events e LEFT JOIN seen_jobs j ON j.id = e.job_id "
            "WHERE e.action_required <> '' AND e.resolved_at IS NULL "
            # An action has to belong to an application: either it matched a
            # role, or it classified as a recruiting status. Without this, the school
            # Career Services mail about "first sessions and important
            # reminders" reads as a task on his job board.
            "AND (e.job_id <> '' OR e.proposed_status <> '') "
            # Nothing is owed on an application that has been turned down.
            "AND (j.status IS NULL OR j.status NOT IN ('rejected', 'ignored')) "
            "ORDER BY CASE WHEN e.action_deadline <> '' THEN 0 ELSE 1 END, "
            "e.action_deadline, e.received_at DESC LIMIT ?", (limit,),
        )
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, row)) for row in cur]
        # Three Shell mails described one assessment. Where the reading gave a
        # task slug, the newest mail for that role and task stands for all of
        # them; without a slug every mail is its own item, as before.
        # Account plumbing is our own doing, not a task for him: the broker
        # resets a recruiting password during an application and the reset mail
        # then reads as something he must action.
        admin = ("password-reset", "password", "account-verification",
                 "email-verification", "account-activation")
        seen: set[tuple] = set()
        out = []
        for row in rows:
            if row["task_key"] in admin:
                continue
            # With a slug, one item per role and task. Without one, fall back to
            # the subject, which collapses a firm sending the same notice twice.
            key = (row["job_id"], row["task_key"] or row["subject"])
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def pending_application_mail(self, limit: int = 50) -> list[dict]:
        """Messages that were classified but not applied: the review queue."""
        cur = self.conn.execute(
            "SELECT message_key, received_at, sender, subject, job_id, "
            "proposed_status, outcome, evidence, match_reason, rejection_reason, "
            "reason_kind, reapply_kind, reapply_after, reapply_quote "
            "FROM application_mail_events "
            "WHERE outcome IN ('queued','unmatched') AND proposed_status <> '' "
            "AND resolved_at IS NULL ORDER BY received_at DESC LIMIT ?", (limit,),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def confirm_application_submission(self, job_id: str, received_at: str,
                                       evidence: str) -> bool:
        """Record that the employer acknowledged receiving this application.
        First confirmation wins, so a later reminder cannot rewrite the date."""
        cur = self.conn.execute(
            "UPDATE application_workflows SET submitted_confirmed_at=?, "
            "submitted_evidence=? WHERE job_id=? AND submitted_confirmed_at IS NULL",
            (received_at, evidence[:600], job_id),
        )
        self._commit()
        self._close_confirmed_workflow(job_id)
        return cur.rowcount > 0

    def _close_confirmed_workflow(self, job_id: str) -> None:
        """An employer's confirmation is proof the application was submitted, so
        the attended run is over whoever pressed the button. Without this a run
        read review_ready indefinitely beside its own confirmation (Maven,
        2026-09-12), and the runs the agent submitted by mistake (Engie, SIG,
        2026-09-13) needed a manual click to stop inviting a second submission."""
        row = self.conn.execute(
            "SELECT workflow_id, status, submitted_confirmed_at FROM application_workflows "
            "WHERE job_id=?", (job_id,)
        ).fetchone()
        if row and row[2] and row[1] != "completed":
            self.transition_application_workflow(
                row[0], "completed", actor="employer",
                detail="Closed by the employer's confirmation email")

    def campus_applications(self) -> list[dict]:
        """Programmes ticked as applied, with whatever the inbox has said about
        them. These never had a `seen_jobs` row: the sweep found them on a
        programme page and the scrape never saw them."""
        cur = self.conn.execute(
            "SELECT key, firm, programme, updated_at FROM campus_state "
            "WHERE state='applied' ORDER BY firm"
        )
        out = []
        for key, firm, programme, updated_at in cur:
            out.append({
                "key": key, "firm": firm, "programme": programme,
                "updated_at": updated_at or "",
                "mail": self.application_mail_for_job(f"campus:{key}"),
            })
        return out

    def backfill_submission_confirmations(self) -> int:
        """Stamp any workflow that has a confirmation mail but no stamp.

        The stamp is written when a message is processed, so a message
        processed before that rule existed — or one resolved by hand and
        therefore skipped by a later re-sweep, which is how Barclays slipped
        through — leaves a workflow reading as never confirmed while the proof
        sits in its own mail. This reconciles the two and is idempotent."""
        rows = self.conn.execute(
            "SELECT w.job_id, e.received_at, e.evidence "
            "FROM application_workflows w JOIN application_mail_events e "
            "  ON e.job_id = w.job_id "
            "WHERE w.submitted_confirmed_at IS NULL AND e.proposed_status = 'applied' "
            "GROUP BY w.job_id HAVING min(e.received_at)"
        ).fetchall()
        stamped = sum(
            1 for job_id, received, evidence in rows
            if self.confirm_application_submission(job_id, received, evidence or "")
        )
        for (job_id,) in self.conn.execute(
            "SELECT job_id FROM application_workflows "
            "WHERE submitted_confirmed_at IS NOT NULL AND status != 'completed'"
        ).fetchall():
            self._close_confirmed_workflow(job_id)
        return stamped

    def application_mail_for_job(self, job_id: str, limit: int = 20) -> list[dict]:
        """Everything the inbox said about one role, newest first. This is the
        audit trail behind an automatic status change: the row it moved, the
        message that moved it, and the sentence inside that message."""
        if not job_id:
            return []
        cur = self.conn.execute(
            "SELECT received_at, sender, subject, proposed_status, outcome, "
            "evidence, rejection_reason, reason_kind, reapply_kind, "
            "reapply_after, reapply_quote "
            "FROM application_mail_events WHERE job_id=? "
            "ORDER BY received_at DESC LIMIT ?", (job_id, limit),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def recent_application_mail(self, since: str, limit: int = 50) -> list[dict]:
        """Mail attached to his roles that arrived at or after `since` (an ISO
        UTC instant), newest first, for the Applications page's inbox column.
        Offsets differ between senders, so the cut is made on parsed instants
        rather than on the stored strings."""
        cur = self.conn.execute(
            "SELECT e.message_key, e.received_at, e.subject, e.job_id, e.proposed_status, "
            "e.outcome, e.evidence, e.rejection_reason, e.reason_kind, e.reapply_kind, "
            "e.reapply_after, e.reapply_quote, "
            "e.action_required, j.company, j.title FROM application_mail_events e "
            "JOIN seen_jobs j ON j.id = e.job_id "
            "WHERE e.outcome <> 'unclassified' ORDER BY e.received_at DESC LIMIT ?", (limit,),
        )
        names = [c[0] for c in cur.description]
        cutoff = _utc_instant(since)
        rows = [dict(zip(names, row)) for row in cur]
        return [r for r in rows if (_utc_instant(r["received_at"]) or "") >= cutoff]

    def unplaced_employer_mail(self, since: str, limit: int = 30) -> list[dict]:
        """Mail the classifier declined that still looks like it came from an
        employer, newest first, so it can be placed by hand. 295 of 441 stored
        messages are unclassified and most are ordinary life (a Tesla thread, a
        climbing gym), so the sender has to earn its place here: an ATS platform
        it was sent through, or the name of a firm he has an open application
        with. Matching on the SENDER and never on the subject is deliberate --
        a subject match is what hung a LinkedIn advert for Santander on his
        Santander programme."""
        cur = self.conn.execute(
            "SELECT message_key, received_at, sender, subject, evidence "
            "FROM application_mail_events WHERE outcome='unclassified' "
            "AND job_id='' AND resolved_at IS NULL "
            "ORDER BY received_at DESC LIMIT ?", (limit * 8,),
        )
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, row)) for row in cur]
        cutoff = _utc_instant(since)
        rows = [r for r in rows if (_utc_instant(r["received_at"]) or "") >= cutoff]
        firms: dict[str, str] = {}
        for (company,) in self.conn.execute(
                "SELECT DISTINCT company FROM seen_jobs WHERE status IN "
                "('queued','applied','oa','interview','offer')"):
            parts = plain_company(company).split()
            if parts and len(parts[0]) >= 3:
                firms.setdefault(parts[0].lower(), company)
        out = []
        for row in rows:
            sender = str(row["sender"]).lower()
            platform = next((p for p in _MAIL_PLATFORMS if p in sender), None)
            # The firm is resolved independently of the platform, because the
            # platform name is not a company and cannot look up a role: on
            # 2026-09-17 the first version put "successfactors" into the role
            # lookup, so RWE's own mail listed with an empty picker and the only
            # button on offer was Dismiss -- the same dead end it was meant to
            # end. RWE's sender carries "noreply@rwe.com" in its display name,
            # so a token match finds the firm even when the envelope is the ATS.
            firm = next((f for f in firms if _token_in(f, sender)), None)
            if platform or firm:
                row["sender_match"] = platform or firm
                row["company"] = firms[firm] if firm else ""
                out.append(row)
            if len(out) >= limit:
                break
        return out

    def open_application_roles(self, limit: int = 60) -> list[dict]:
        """Every role he has acted on, for filing a message whose firm cannot be
        read off the sender at all. The pool is small by construction, so an
        unrecognised sender is a pick from a list rather than a dead end."""
        cur = self.conn.execute(
            "SELECT id, company, title, location, status FROM seen_jobs "
            "WHERE status IN ('queued','applied','oa','interview','offer') "
            "ORDER BY company, last_seen DESC LIMIT ?", (limit,),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def active_reapply_cooldowns(self, today: str) -> list[dict]:
        """Waiting periods an employer stated that have not ended yet, newest
        end date first, each with the role it came from and the sentence."""
        cur = self.conn.execute(
            "SELECT j.company, e.job_id, j.title, e.reapply_after, e.reapply_quote, "
            "e.received_at FROM application_mail_events e "
            "JOIN seen_jobs j ON j.id = e.job_id "
            "WHERE e.reapply_after <> '' AND e.reapply_after >= ? "
            "ORDER BY e.reapply_after DESC", (today,),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def roles_for_company(self, company: str, limit: int = 60) -> list[dict]:
        """Roles at one firm, for attaching a confirmation whose role was never
        tracked. Acted-on rows first, then the most recently seen, because the
        mail is about something he applied to rather than something new."""
        if not company:
            return []
        # A delisted row is only offered when he has already acted on it, so a
        # closed posting he applied to stays selectable while dead duplicates
        # do not. Glencore's board churned its ids daily until 2026-07-15 and
        # left 205 delisted copies of six live postings; a Glencore
        # confirmation was attached to one of them, so the live row kept
        # reading "new" on Browse while the applied status sat on a corpse.
        cur = self.conn.execute(
            "SELECT id, title, location, status FROM seen_jobs WHERE company=? "
            "AND status <> 'ignored' AND (delisted_at IS NULL OR status <> 'new') "
            "ORDER BY CASE WHEN delisted_at IS NULL THEN 0 ELSE 1 END, "
            "CASE WHEN status='new' THEN 1 ELSE 0 END, last_seen DESC LIMIT ?",
            (company, limit),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    # --- company context (role detail pane) -------------------------------

    def company_aliases(self, company: str) -> list[str]:
        """Every source label that names the same firm as `company`.

        seen_jobs.company is the scraper's source label, so one firm arrives
        under several: "UBS" and "UBS (Graduate Careers)", "Bank of America" and
        "Bank of America (Campus)". plain_company is the one normalisation."""
        key = plain_company(company).lower()
        if not key:
            return []
        rows = self.conn.execute("SELECT DISTINCT company FROM seen_jobs").fetchall()
        return sorted({c for (c,) in rows if c and plain_company(c).lower() == key})

    def company_applications(self, companies: list[str]) -> list[dict]:
        """Every role at these source labels he has actually applied to, newest
        first. Delisted rows are included on purpose: a firm taking the posting
        down says nothing about the application. 'queued' is an intention and
        'ignored' a decision against the role, so neither is history here."""
        if not companies:
            return []
        q = ",".join("?" * len(companies))
        s = ",".join("?" * len(APPLIED_STATUSES))
        cur = self.conn.execute(
            f"SELECT id, company, title, location, loc_city, loc_country, status, "
            f"applied_at, delisted_at, first_seen FROM seen_jobs "
            f"WHERE company IN ({q}) AND status IN ({s}) "
            f"ORDER BY COALESCE(applied_at, first_seen) DESC",
            [*companies, *APPLIED_STATUSES])
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    # Open roles at a firm that are worth showing next to one of its roles:
    # live, not past a stated deadline, not already acted on, and visible under
    # Browse's default hides (not 'other', not a disguised senior role).
    _OPEN_ROLE_SQL = (
        "delisted_at IS NULL AND " + _NOT_PAST_DEADLINE_SQL + " AND "
        "status IN ('new', 'queued') AND area NOT IN ('', 'other') AND "
        "(min_yoe IS NULL OR min_yoe < ?) AND COALESCE(seniority, '') != 'manager'")

    def relevant_company_roles(self, companies: list[str], job: dict,
                               limit: int = 8) -> list[dict]:
        """Open roles at the same firm that match `job` on its structured
        facets. Area and job type must match; desk and location rank the
        matches (same city, then country, then region), newest first after
        that. A role with no area yet (untagged) has nothing to match on."""
        area = (job.get("area") or "").strip()
        if not companies or not area or area == "other":
            return []
        q = ",".join("?" * len(companies))
        cur = self.conn.execute(
            f"SELECT id, company, title, location, loc_city, loc_country, area, "
            f"desk, job_type, first_seen, deadline FROM seen_jobs "
            f"WHERE company IN ({q}) AND id != ? AND area = ? "
            f"AND COALESCE(job_type, 'job') = ? AND " + self._OPEN_ROLE_SQL + " "
            "ORDER BY (COALESCE(desk, '') = ? AND ? != '') DESC, "
            "(COALESCE(loc_city, '') = ? AND ? != '') DESC, "
            "(COALESCE(loc_country, '') = ? AND ? != '') DESC, "
            "(COALESCE(loc_region, '') = ? AND ? != '') DESC, "
            "first_seen DESC LIMIT ?",
            [*companies, job.get("id", ""), area, job.get("job_type") or "job",
             YOE_HIDE_THRESHOLD,
             job.get("desk") or "", job.get("desk") or "",
             job.get("loc_city") or "", job.get("loc_city") or "",
             job.get("loc_country") or "", job.get("loc_country") or "",
             job.get("loc_region") or "", job.get("loc_region") or "",
             limit])
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def pending_application_mail_one(self, message_key: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT message_key, job_id, proposed_status, evidence, received_at "
            "FROM application_mail_events WHERE message_key=? AND resolved_at IS NULL",
            (message_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row))

    def adopt_application_mail(self, message_key: str, job_id: str) -> bool:
        """Attach a queued message to a role he picked and move that role. The
        status still comes from the message, never from the request, so a
        crafted form cannot set an arbitrary state."""
        event = self.pending_application_mail_one(message_key)
        if event is None:
            return False
        job = self.get_job(job_id)
        if job is None:
            return False
        # A message the classifier declined carries no status, so placing it files
        # the evidence and moves nothing. Without this there was no way to attach
        # one at all: RWE's "Welcome to the RWE Careers Portal - Account created"
        # on 2026-09-17 was real mail from the firm's own ATS about a role he had
        # just applied to, and the only paths on offer were classified mail and
        # nothing.
        if not event["proposed_status"]:
            self.conn.execute(
                "UPDATE application_mail_events SET job_id=?, outcome='placed' "
                "WHERE message_key=?", (job_id, message_key),
            )
            self._commit()
            return True
        # A duplicate confirmation must not drag a role backwards. Optiver sent
        # both an assessment invitation and a plain thank-you; adopting the
        # second onto a role already at `oa` would have regressed it to
        # `applied`. The evidence is still kept and the message still resolves.
        if mail_rank(event["proposed_status"]) > mail_rank(job["status"]):
            self.set_status(job_id, event["proposed_status"])
        if event["proposed_status"] == "applied":
            self.confirm_application_submission(
                job_id, event["received_at"], event["evidence"]
            )
        # Placing a message is not doing what it asks. resolved_at is also the
        # To do list's Done, so stamping it here dropped Goldman's HackerRank
        # assessment the moment he attached the mail (2026-09-17). Outcome
        # 'applied' already takes it out of the placing queue.
        self.conn.execute(
            "UPDATE application_mail_events SET job_id=?, outcome='applied', "
            "resolved_at=CASE WHEN action_required <> '' THEN resolved_at ELSE ? END "
            "WHERE message_key=?",
            (job_id, datetime.now(timezone.utc).isoformat(), message_key),
        )
        self._commit()
        return True

    def resolve_application_mail(self, message_key: str) -> None:
        self.conn.execute(
            "UPDATE application_mail_events SET resolved_at=? WHERE message_key=?",
            (datetime.now(timezone.utc).isoformat(), message_key),
        )
        self._commit()

    def set_status(self, job_id: str, status: str) -> bool:
        """Update a role's CRM status (new / queued / applied / oa / interview /
        offer / rejected / ignored). The first time a role reaches 'applied' we
        stamp applied_at (and never overwrite it on later transitions). Returns
        True if the job exists."""
        if status == "applied":
            now = datetime.now(timezone.utc).isoformat()
            cur = self.conn.execute(
                "UPDATE seen_jobs SET status = ?, "
                "applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                (status, now, job_id),
            )
        else:
            cur = self.conn.execute(
                "UPDATE seen_jobs SET status = ? WHERE id = ?", (status, job_id)
            )
        self._commit()
        return cur.rowcount > 0

    def set_notes(self, job_id: str, notes: str) -> bool:
        """Persist free-text application notes for a role. Empty string clears
        them. Returns True if the row exists."""
        cur = self.conn.execute(
            "UPDATE seen_jobs SET notes = ? WHERE id = ?",
            (notes or None, job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def set_favorite(self, job_id: str, favorite: bool) -> bool:
        """Set/clear the favorite flag for a role. Orthogonal to status.
        Returns True if the row exists."""
        cur = self.conn.execute(
            "UPDATE seen_jobs SET favorite = ? WHERE id = ?",
            (1 if favorite else 0, job_id),
        )
        self._commit()
        return cur.rowcount > 0

    # ── Attended application workflows ──────────────────────────────────
    @staticmethod
    def _workflow_dict(row) -> dict | None:
        return dict(zip(APPLICATION_WORKFLOW_COLUMNS, row)) if row else None

    def create_application_workflow(
        self, job_id: str, application_url: str, *, workflow_id: str | None = None
    ) -> tuple[dict, bool]:
        """Create or return the one attended workflow for a role.

        The job-level UNIQUE constraint is the idempotency key: double-clicks,
        retries after a lost response and later revisits all return the same
        workflow and the original application URL.  Returns (workflow, created).
        """
        if not application_url:
            raise ValueError("application URL is required")
        if self.conn.execute(
            "SELECT 1 FROM seen_jobs WHERE id = ?", (job_id,)
        ).fetchone() is None:
            raise KeyError(job_id)
        workflow_id = workflow_id or f"apply_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO application_workflows
                (workflow_id, job_id, application_url, status, detail,
                 created_at, updated_at, updated_by)
            VALUES (?, ?, ?, 'queued', '', ?, ?, 'owner')
            """,
            (workflow_id, job_id, application_url, now, now),
        )
        created = cur.rowcount > 0
        if created:
            # Queueing is an intention, not a submission.  Never demote a role
            # that has already reached an applied/response CRM stage.
            self.conn.execute(
                "UPDATE seen_jobs SET status = 'queued' "
                "WHERE id = ? AND status IN ('new', 'ignored', 'queued')",
                (job_id,),
            )
        self._commit()
        workflow = self.get_application_workflow_for_job(job_id)
        if workflow is None:  # only possible on a colliding caller-supplied id
            raise RuntimeError("application workflow could not be created")
        return workflow, created

    def get_application_workflow(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {', '.join(APPLICATION_WORKFLOW_COLUMNS)} "
            "FROM application_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        return self._workflow_dict(row)

    def offer_elsewhere(self, job_id: str) -> bool:
        """Whether he holds an offer at a firm other than this role's, read off
        the board at the moment it is asked. Application forms ask about offers
        and late-stage processes elsewhere, and the truthful answer changes as
        the inbox pass moves roles, so it is computed per launch rather than
        stored as a standing answer that goes stale."""
        def firm(name: str) -> str:
            name = re.sub(r"\s*\([^)]*\)\s*$", "", name or "")
            return re.sub(r"[^a-z0-9]", "", name.lower())
        row = self.conn.execute("SELECT company FROM seen_jobs WHERE id=?", (job_id,)).fetchone()
        own = firm(row[0]) if row else ""
        firms = [firm(r[0]) for r in self.conn.execute(
            "SELECT company FROM seen_jobs WHERE status='offer' AND id<>?", (job_id,))]
        firms += [firm(r[0].split(":", 1)[1].split("|", 1)[0]) for r in self.conn.execute(
            "SELECT job_id FROM application_mail_events "
            "WHERE proposed_status='offer' AND job_id LIKE 'campus:%'")]
        return any(f and f != own for f in firms)

    def prior_applications(self, job_id: str, *, days: int = 365) -> list[dict]:
        """His recorded applications at this role's firm (every source label of
        it) in the last `days`, excluding the role itself, newest first. Forms
        ask "is this your first application to us?" and Barclays makes it a
        confirmation box; the browser agent has no other way to know (Barclays
        Frankfurt, 2026-09-24, confirmed "first" with Singapore already at OA)."""
        row = self.conn.execute("SELECT company FROM seen_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [a for a in self.company_applications(self.company_aliases(row[0]))
                if a["id"] != job_id and (not a["applied_at"] or a["applied_at"] >= cutoff)]

    def workflows_by_company(self) -> list[dict]:
        """Every non-failed workflow with the firm it belongs to, for warning
        before a second application at a capped firm. Cheap: there are tens of
        these, not thousands."""
        cur = self.conn.execute(
            "SELECT w.workflow_id, w.status, j.company, j.title "
            "FROM application_workflows w JOIN seen_jobs j ON j.id = w.job_id "
            "WHERE w.status <> 'failed'"
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur]

    def get_application_workflow_for_job(self, job_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {', '.join(APPLICATION_WORKFLOW_COLUMNS)} "
            "FROM application_workflows WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return self._workflow_dict(row)

    def list_application_workflows(self) -> list[dict]:
        """All workflows with the role labels needed by the website queue."""
        rows = self.conn.execute(
            f"SELECT {', '.join('w.' + c for c in APPLICATION_WORKFLOW_COLUMNS)}, "
            "j.company, j.title, j.location, j.status AS crm_status "
            "FROM application_workflows w JOIN seen_jobs j ON j.id = w.job_id "
            "ORDER BY w.updated_at DESC"
        ).fetchall()
        cols = APPLICATION_WORKFLOW_COLUMNS + (
            "company", "title", "location", "crm_status"
        )
        return [dict(zip(cols, row)) for row in rows]

    def request_application_launch(self, workflow_id: str, *, retry: bool = False,
                                   max_pending: int = 5,
                                   new_attempt: bool = False) -> tuple[dict, bool]:
        """Queue one bounded local GUI launch. Repeated requests are idempotent.
        `new_attempt` is the autopilot starting a fresh run of a workflow whose
        earlier launch already succeeded; it holds the worker lease, so a second
        launch cannot overlap a live agent."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            workflow = self.get_application_workflow(workflow_id)
            if workflow is None:
                raise KeyError(workflow_id)
            current = workflow["launch_status"]
            if current in {"pending", "running"} or (current == "succeeded" and not new_attempt):
                self.conn.commit()
                return workflow, False
            if current == "failed" and not retry:
                self.conn.commit()
                return workflow, False
            queued = self.conn.execute(
                "SELECT COUNT(*) FROM application_workflows "
                "WHERE launch_status IN ('pending','running')"
            ).fetchone()[0]
            if queued >= max_pending:
                raise RuntimeError("application launch queue is full")
            now = datetime.now(timezone.utc).isoformat()
            request_id = f"launch_{uuid.uuid4().hex}"
            self.conn.execute(
                """UPDATE application_workflows SET launch_status='pending',
                   launch_request_id=?, launch_requested_at=?, launch_started_at=NULL,
                   launch_finished_at=NULL, launch_error_code='',
                   launch_attempts=launch_attempts+1 WHERE workflow_id=?""",
                (request_id, now, workflow_id),
            )
            self.conn.commit()
            return self.get_application_workflow(workflow_id), True
        except Exception:
            self.conn.rollback()
            raise

    def claim_application_launch(self, request_id: str, *, stale_seconds: int = 180) -> dict | None:
        """Atomically claim one matching pending request; globally single-flight."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
            self.conn.execute(
                """UPDATE application_workflows SET launch_status='failed',
                   launch_finished_at=?, launch_error_code='helper_interrupted'
                   WHERE launch_status='running' AND launch_started_at < ?""",
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            if self.conn.execute(
                "SELECT 1 FROM application_workflows WHERE launch_status='running' LIMIT 1"
            ).fetchone():
                self.conn.commit()
                return None
            workflow = self.get_application_workflow_by_request(request_id)
            if workflow is None or workflow["launch_status"] != "pending":
                self.conn.commit()
                return None
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE application_workflows SET launch_status='running', "
                "launch_started_at=?, launch_error_code='' WHERE launch_request_id=?",
                (now, request_id),
            )
            self.conn.commit()
            return self.get_application_workflow_by_request(request_id)
        except Exception:
            self.conn.rollback()
            raise

    def get_application_workflow_by_request(self, request_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {', '.join(APPLICATION_WORKFLOW_COLUMNS)} FROM application_workflows "
            "WHERE launch_request_id=?", (request_id,),
        ).fetchone()
        return self._workflow_dict(row)

    def finish_application_launch(self, request_id: str, succeeded: bool,
                                  error_code: str = "") -> dict:
        code = "" if succeeded else error_code
        if code not in APPLICATION_LAUNCH_ERROR_CODES or (not succeeded and not code):
            raise ValueError("bad application launch error code")
        workflow = self.get_application_workflow_by_request(request_id)
        if workflow is None:
            raise KeyError(request_id)
        if workflow["launch_status"] != "running":
            raise ValueError("launch request is not running")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE application_workflows SET launch_status=?, launch_finished_at=?, "
            "launch_error_code=? WHERE launch_request_id=? AND launch_status='running'",
            ("succeeded" if succeeded else "failed", now, code, request_id),
        )
        self._commit()
        return self.get_application_workflow_by_request(request_id)

    # ── Application runs: the serial autopilot's queue and worker lease ──────
    APPLICATION_RUN_COLUMNS = (
        "run_id", "workflow_id", "attempt_no", "state", "priority", "manual",
        "queued_at", "launching_at", "running_at", "draining_at", "finished_at",
        "last_activity_at", "agent_stopped_at", "stop_source", "outcome",
        "error_code", "recovery_required", "recovery_reason",
    )
    ACTIVE_RUN_STATES = ("launching", "running", "draining")
    OPEN_RUN_STATES = ("queued", "launching", "running", "draining")

    def _run_dict(self, row) -> dict | None:
        return dict(zip(self.APPLICATION_RUN_COLUMNS, row)) if row else None

    def get_application_run(self, run_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {', '.join(self.APPLICATION_RUN_COLUMNS)} FROM application_runs "
            "WHERE run_id=?", (run_id,)).fetchone()
        return self._run_dict(row)

    def active_application_run(self) -> dict | None:
        """The run holding the worker lease, if any. At most one by index."""
        row = self.conn.execute(
            f"SELECT {', '.join(self.APPLICATION_RUN_COLUMNS)} FROM application_runs "
            "WHERE state IN ('launching','running','draining')").fetchone()
        return self._run_dict(row)

    def open_application_run(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {', '.join(self.APPLICATION_RUN_COLUMNS)} FROM application_runs "
            "WHERE workflow_id=? AND state IN ('queued','launching','running','draining')",
            (workflow_id,)).fetchone()
        return self._run_dict(row)

    def list_application_runs(self, *, states: tuple[str, ...] = (), limit: int = 200) -> list[dict]:
        where = ""
        params: list = []
        if states:
            where = f"WHERE state IN ({', '.join('?' * len(states))})"
            params = list(states)
        rows = self.conn.execute(
            f"SELECT {', '.join(self.APPLICATION_RUN_COLUMNS)} FROM application_runs {where} "
            "ORDER BY CASE state WHEN 'launching' THEN 0 WHEN 'running' THEN 0 "
            "WHEN 'draining' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
            "priority DESC, queued_at ASC LIMIT ?", params + [limit]).fetchall()
        return [self._run_dict(r) for r in rows]

    def queue_application_run(self, workflow_id: str, *, manual: bool = False,
                              priority: int = 0) -> tuple[dict, bool]:
        """Queue one attempt. Idempotent: a workflow with an open run gets that
        run back, never a second one."""
        if self.get_application_workflow(workflow_id) is None:
            raise KeyError(workflow_id)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.open_application_run(workflow_id)
            if existing is not None:
                self.conn.commit()
                return existing, False
            attempt = (self.conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) FROM application_runs WHERE workflow_id=?",
                (workflow_id,)).fetchone()[0]) + 1
            run_id = f"run_{uuid.uuid4().hex}"
            self.conn.execute(
                "INSERT INTO application_runs (run_id, workflow_id, attempt_no, state, "
                "priority, manual, queued_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (run_id, workflow_id, attempt, priority, int(manual),
                 datetime.now(timezone.utc).isoformat()))
            self.conn.commit()
            return self.get_application_run(run_id), True
        except Exception:
            self.conn.rollback()
            raise

    def acquire_application_run(self, run_id: str) -> dict | None:
        """Take the worker lease for one queued run: queued -> launching, in one
        transaction with the check that nothing else holds it. None if another
        run is active or this one is no longer queued."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute(
                    "SELECT 1 FROM application_runs WHERE state IN "
                    "('launching','running','draining') LIMIT 1").fetchone():
                self.conn.commit()
                return None
            now = datetime.now(timezone.utc).isoformat()
            cur = self.conn.execute(
                "UPDATE application_runs SET state='launching', launching_at=?, "
                "last_activity_at=? WHERE run_id=? AND state='queued'", (now, now, run_id))
            self.conn.commit()
            return self.get_application_run(run_id) if cur.rowcount else None
        except Exception:
            self.conn.rollback()
            raise

    def update_application_run(self, run_id: str, *, expect_state: str | None = None,
                               **fields) -> dict | None:
        """Set fields on a run, optionally only if it is still in `expect_state`
        (a compare-and-set, so two ticks cannot both advance one run)."""
        allowed = set(self.APPLICATION_RUN_COLUMNS) - {"run_id", "workflow_id", "attempt_no"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown run fields: {sorted(bad)}")
        if not fields:
            return self.get_application_run(run_id)
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [run_id]
        sql = f"UPDATE application_runs SET {sets} WHERE run_id=?"
        if expect_state is not None:
            sql += " AND state=?"
            params.append(expect_state)
        cur = self.conn.execute(sql, params)
        self._commit()
        return self.get_application_run(run_id) if cur.rowcount else None

    def autopilot_settings(self) -> dict:
        row = self.conn.execute(
            "SELECT enabled, paused_reason, paused_at, updated_at, safety_version, "
            "foreign_confirmed_at FROM application_autopilot "
            "WHERE id=1").fetchone()
        if row is None:
            return {"enabled": False, "paused_reason": "", "paused_at": None,
                    "updated_at": None, "safety_version": 1,
                    "foreign_confirmed_at": None}
        return {"enabled": bool(row[0]), "paused_reason": row[1], "paused_at": row[2],
                "updated_at": row[3], "safety_version": row[4],
                "foreign_confirmed_at": row[5]}

    def set_autopilot(self, *, enabled: bool | None = None, paused_reason: str | None = None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        if enabled is not None:
            self.conn.execute(
                "UPDATE application_autopilot SET enabled=?, updated_at=?, "
                "foreign_confirmed_at=CASE WHEN ? THEN ? ELSE foreign_confirmed_at END "
                "WHERE id=1", (int(enabled), now, int(enabled), now))
            if not enabled and paused_reason is None:
                paused_reason = "paused by owner"
        if paused_reason is not None:
            self.conn.execute(
                "UPDATE application_autopilot SET paused_reason=?, paused_at=?, updated_at=? "
                "WHERE id=1", (paused_reason, now if paused_reason else None, now))
        self._commit()
        return self.autopilot_settings()

    def record_application_account(self, scope: str, workflow_id: str,
                                   credential_service: str) -> dict:
        if self.get_application_workflow(workflow_id) is None:
            raise KeyError(workflow_id)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO application_accounts
               (account_scope, workflow_id, credential_service, prepared_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account_scope) DO UPDATE SET
                 workflow_id=excluded.workflow_id,
                 credential_service=excluded.credential_service,
                 prepared_at=excluded.prepared_at""",
            (scope, workflow_id, credential_service, now),
        )
        self._commit()
        return self.get_application_account(scope)

    def get_application_account(self, scope: str) -> dict | None:
        row = self.conn.execute(
            "SELECT account_scope, workflow_id, credential_service, prepared_at, verified_at "
            "FROM application_accounts WHERE account_scope=?", (scope,),
        ).fetchone()
        return dict(zip(("account_scope", "workflow_id", "credential_service",
                         "prepared_at", "verified_at"), row)) if row else None

    def verify_application_account(self, scope: str) -> None:
        cur = self.conn.execute(
            "UPDATE application_accounts SET verified_at=? WHERE account_scope=?",
            (datetime.now(timezone.utc).isoformat(), scope),
        )
        if not cur.rowcount:
            raise KeyError(scope)
        self._commit()

    def transition_application_workflow(
        self,
        workflow_id: str,
        status: str,
        *,
        detail: str = "",
        actor: str = "owner",
    ) -> dict:
        """Move one workflow through the explicit attended state machine.

        Repeating the current state is idempotent.  An agent capability can
        report progress, a blocker, review readiness or failure, but can never
        mark the application completed/applied; only the authenticated owner
        may do that after personally using the external site's submit control.
        """
        if status not in APPLICATION_WORKFLOW_STATES:
            raise ValueError(f"bad application workflow status {status!r}")
        if actor not in {"owner", "agent", "employer"}:
            raise ValueError(f"bad application workflow actor {actor!r}")
        if actor == "agent" and status not in AGENT_WORKFLOW_STATES:
            raise PermissionError("agent cannot set that workflow status")
        # `employer` is the inbox pass acting on a stored confirmation email; it
        # may only close a run, never report progress on one.
        if actor == "employer" and status != "completed":
            raise PermissionError("an employer confirmation can only complete a workflow")
        workflow = self.get_application_workflow(workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        current = workflow["status"]
        detail = (detail or "").strip()
        if len(detail) > 2000:
            raise ValueError("workflow detail is too long")
        if status != current and status not in APPLICATION_WORKFLOW_TRANSITIONS[current]:
            raise ValueError(f"forbidden workflow transition {current!r} -> {status!r}")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE application_workflows SET
                status = ?, detail = ?, updated_at = ?, updated_by = ?,
                started_at = CASE WHEN ? IN ('in_progress', 'needs_user_action',
                                             'review_ready')
                                  THEN COALESCE(started_at, ?) ELSE started_at END,
                review_ready_at = CASE WHEN ? = 'review_ready'
                                       THEN COALESCE(review_ready_at, ?) ELSE review_ready_at END,
                completed_at = CASE WHEN ? = 'completed'
                                    THEN COALESCE(completed_at, ?) ELSE completed_at END
            WHERE workflow_id = ?
            """,
            (status, detail, now, actor, status, now, status, now, status, now, workflow_id),
        )
        if status == "queued":
            self.conn.execute(
                "UPDATE seen_jobs SET status = 'queued' "
                "WHERE id = ? AND status IN ('new', 'ignored', 'queued')",
                (workflow["job_id"],),
            )
        elif status == "completed":
            # This is a local CRM write after the user's manual submit.  No
            # request is made to the employer and no external form is touched.
            # Forward only: a role the inbox already moved to oa, interview,
            # offer or rejected keeps that status when the run is closed later.
            self.conn.execute(
                "UPDATE seen_jobs SET status = 'applied', "
                "applied_at = COALESCE(applied_at, ?) WHERE id = ? "
                "AND COALESCE(status, 'new') IN ('new', 'ignored', 'queued', 'applied')",
                (now, workflow["job_id"]),
            )
            self.conn.execute(
                "UPDATE seen_jobs SET applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                (now, workflow["job_id"]),
            )
        self._commit()
        return self.get_application_workflow(workflow_id)

    # ── Campus programmes (graduate schemes off the ATS boards) ──────────
    # See the campus_state CREATE in _init: this table is his marks only.
    CAMPUS_STATES = ("todo", "applied", "skipped")

    def all_campus_state(self) -> dict[str, dict]:
        """Every mark keyed by programme key. One query — the campus page
        renders hundreds of rows and must not go per-row."""
        cur = self.conn.execute("SELECT * FROM campus_state")
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def set_campus_state(self, key: str, *, state: str, firm: str = "",
                         programme: str = "", season: str = "",
                         note: str | None = None) -> dict:
        """Tick, untick or skip one programme. Returns the stored row.

        'todo' is the default and is stored rather than deleted, because a note
        can outlive a tick — he may have written why a programme was worth
        looking at before deciding not to apply yet."""
        if state not in self.CAMPUS_STATES:
            raise ValueError(f"bad campus state {state!r}")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO campus_state (key, firm, programme, state, season, note, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, ''), ?)
            ON CONFLICT(key) DO UPDATE SET
                state = excluded.state,
                firm = CASE WHEN excluded.firm != '' THEN excluded.firm ELSE campus_state.firm END,
                programme = CASE WHEN excluded.programme != '' THEN excluded.programme ELSE campus_state.programme END,
                season = CASE WHEN excluded.season != '' THEN excluded.season ELSE campus_state.season END,
                note = COALESCE(?, campus_state.note),
                updated_at = excluded.updated_at
        """, (key, firm, programme, state, season, note, now, note))
        self._commit()
        cur = self.conn.execute("SELECT * FROM campus_state WHERE key = ?", (key,))
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))

    # ── Company application limits ────────────────────────────────────────
    # See the company_limits CREATE in _init for the accuracy contract.
    LIMIT_CONFIDENCES = ("stated", "manual", "unknown", "fetch_failed")

    def get_company_limit(self, company: str) -> dict | None:
        """One company's cap record, or None if we have never looked."""
        cur = self.conn.execute(
            "SELECT * FROM company_limits WHERE company = ?", (company,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def all_company_limits(self) -> dict[str, dict]:
        """Every cap record keyed by company. One query — the web app renders
        a whole page of roles from this, so it must not be per-role."""
        cur = self.conn.execute("SELECT * FROM company_limits")
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def set_company_limit(self, company: str, *, max_per_cycle: int | None,
                          cycle: str = "", locations_count_separately: int | None = None,
                          shared_across_programmes: int | None = None,
                          varies_by_region: int | None = None,
                          confidence: str = "unknown", strength: str = "",
                          quote: str = "", source_url: str = "",
                          updated_by: str = "manual") -> None:
        """Upsert a company's cap record.

        Enforces the accuracy contract in code, not just in prose: a number
        without a confidence of 'stated' or 'manual' is dropped to NULL rather
        than stored. That makes it impossible for a research pass that failed
        its quote check to leave a plausible-looking figure behind."""
        if confidence not in self.LIMIT_CONFIDENCES:
            raise ValueError(f"bad confidence {confidence!r}")
        if confidence not in ("stated", "manual"):
            max_per_cycle = None
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO company_limits
                 (company, max_per_cycle, cycle, locations_count_separately,
                  shared_across_programmes, varies_by_region, confidence,
                  strength, quote, source_url, checked_at, updated_at,
                  updated_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company) DO UPDATE SET
                 max_per_cycle = excluded.max_per_cycle,
                 cycle = excluded.cycle,
                 locations_count_separately = excluded.locations_count_separately,
                 shared_across_programmes = excluded.shared_across_programmes,
                 varies_by_region = excluded.varies_by_region,
                 confidence = excluded.confidence,
                 strength = excluded.strength,
                 quote = excluded.quote,
                 source_url = excluded.source_url,
                 checked_at = excluded.checked_at,
                 updated_at = excluded.updated_at,
                 updated_by = excluded.updated_by""",
            (company, max_per_cycle, cycle or "", locations_count_separately,
             shared_across_programmes, varies_by_region, confidence,
             strength if strength in ("hard", "advisory") else "",
             quote or "", source_url or "", now, now, updated_by),
        )
        self._commit()

    def record_form_limit(self, company: str, *, verdict: str,
                          max_per_cycle: int | None = None, quote: str = "",
                          workflow_id: str = "") -> str:
        """Record what an attended run saw on the application form itself.

        The form is the strongest evidence there is: it states the policy at
        the point of submission, behind the login the research pass cannot
        pass. Two verdicts. `stated` carries the number and the sentence, and
        the number must appear in its own sentence, the same gate the research
        pass applies. `absent` means a run went through the whole form and it
        said nothing about a cap; the researched figure is then shown as
        unconfirmed rather than deleted, because a careers-site FAQ can state a
        rule the form omits.

        A stated observation is sticky: a later form that says nothing (often a
        different programme at the same firm) never downgrades it. Returns the
        verdict actually stored, or "" when an `absent` was declined for that
        reason."""
        if verdict not in ("stated", "absent"):
            raise ValueError(f"bad form verdict {verdict!r}")
        quote = " ".join((quote or "").split())[:600]
        if verdict == "stated":
            if not max_per_cycle or max_per_cycle < 1 or max_per_cycle > 20:
                raise ValueError("a stated form limit needs a number from 1 to 20")
            if not quote_states_number(max_per_cycle, quote):
                raise ValueError("the number must appear in its own quote")
        else:
            max_per_cycle, quote = None, ""
            current = self.get_company_limit(company)
            if current and current.get("form_verdict") == "stated":
                return ""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO company_limits
                 (company, confidence, updated_at, updated_by, form_verdict,
                  form_max, form_quote, form_workflow_id, form_checked_at)
               VALUES (?, 'unknown', ?, 'form', ?, ?, ?, ?, ?)
               ON CONFLICT(company) DO UPDATE SET
                 form_verdict = excluded.form_verdict,
                 form_max = excluded.form_max,
                 form_quote = excluded.form_quote,
                 form_workflow_id = excluded.form_workflow_id,
                 form_checked_at = excluded.form_checked_at""",
            (company, now, verdict, max_per_cycle, quote, workflow_id or "", now),
        )
        self._commit()
        return verdict

    def limits_checked_since(self, since_iso: str) -> set[str]:
        """Companies whose cap was looked at on/after `since_iso`. Lets a
        research pass resume without re-fetching what it already checked."""
        return {r[0] for r in self.conn.execute(
            "SELECT company FROM company_limits WHERE checked_at >= ?", (since_iso,)
        )}

    def favorite_counts_by_company(self) -> dict[str, int]:
        """Starred roles per company — the exposure side of a cap. A firm
        capped at one application with nine favourites is the case this whole
        feature exists for."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT company, COUNT(*) FROM seen_jobs "
            "WHERE favorite = 1 GROUP BY company"
        )}

    def applied_counts_by_company(self, since: str | None = None) -> dict[str, int]:
        """How many applications have actually gone out, per company.

        Counts `status`, not `applied_at`: applied_at is a stamp that survives
        a later status change, and rows exist where it is set while the status
        was moved back to 'new'. The funnel stages at or past 'applied' are the
        ones that imply a form was submitted; 'queued' and 'ignored' do not."""
        sql = ("SELECT company, COUNT(*) FROM seen_jobs "
               f"WHERE status IN ({','.join('?' * len(APPLIED_STATUSES))})")
        params: list = list(APPLIED_STATUSES)
        if since:
            sql += " AND COALESCE(applied_at, first_seen) >= ?"
            params.append(since)
        sql += " GROUP BY company"
        return {r[0]: r[1] for r in self.conn.execute(sql, params)}

    def set_tags(self, job_id: str, *, area: str = "", desk: str = "",
                 seniority: str = "", job_type: str = "job",
                 loc_city: str = "", loc_country: str = "", loc_region: str = "",
                 work_mode: str = "",
                 lang_req: str | None = None, education: str | None = None,
                 start_date: str | None = None,
                 min_yoe: int | None = None, provider: str = "",
                 model: str = "", rubric_version: str = "") -> bool:
        """Persist the structured tags from the Haiku tagging pass (jobfeed/tag.py).
        Returns True if the row exists. Empty area = untagged (filterable as
        'unclassified').

        The description-derived facets (lang_req/education/start_date) carry
        NULL-vs-'' semantics: pass None (the jobfeed/tag.py sentinel when no description
        was present at tag time) to LEAVE the column NULL — 'tagged
        pre-description', which the nightly re-tag hook re-visits once a
        description lands. Pass '' to record 'tagged WITH a description, no
        requirement'. min_yoe is written only when not None so the LLM value can
        win over the regex; leave it None to keep the regex-set value intact."""
        now = datetime.now(timezone.utc).isoformat()
        sets = ["area = ?", "desk = ?", "seniority = ?", "job_type = ?",
                "loc_city = ?", "loc_country = ?", "loc_region = ?",
                "work_mode = ?", "lang_req = ?", "education = ?",
                "start_date = ?", "tagged_at = ?", "tag_provider = ?",
                "tag_model = ?", "tag_rubric_version = ?"]
        params: list = [area, desk, seniority, job_type, loc_city, loc_country,
                        loc_region, work_mode, lang_req, education, start_date,
                        now, provider, model, rubric_version]
        # min_yoe: only touch the column when the tagger produced a value (a
        # description was present). Otherwise the regex-set value stands.
        if min_yoe is not None:
            sets.append("min_yoe = ?")
            params.append(int(min_yoe or 0))
        params.append(job_id)
        cur = self.conn.execute(
            f"UPDATE seen_jobs SET {', '.join(sets)} WHERE id = ?", params
        )
        self._commit()
        return cur.rowcount > 0

    def set_yoe(self, job_id: str, years: int) -> bool:
        """Persist the required-years-of-experience detected in the description
        (0 = none). Returns True if the row exists."""
        cur = self.conn.execute(
            "UPDATE seen_jobs SET min_yoe = ? WHERE id = ?", (int(years or 0), job_id)
        )
        self._commit()
        return cur.rowcount > 0

    def rows_needing_desc_facet_retag(self, columns: list[str],
                                      limit: int = 300) -> list[dict]:
        """Rows tagged BEFORE their description arrived, now that it has —
        i.e. the ones whose description-derived facets (lang_req/education/
        start_date) are still NULL despite the row being tagged and now
        carrying a description.

        Precise condition: tagged_at IS NOT NULL (a tag pass ran) AND
        description present AND lang_req IS NULL (the NULL sentinel a
        no-description tag leaves — distinct from '' which means 'tagged with a
        description, no language required'). This is the honest home for the
        'tagged before description arrived' wrinkle: many ATSes enrich
        descriptions on the nightly backstop AFTER the scan's tag pass, so those
        rows never saw a requirements section. Bounded by `limit` (default 300)
        so a nightly re-tag can't blow the shared Haiku quota; oldest-first so
        the backlog drains deterministically."""
        cols = ", ".join(columns)
        sql = (
            f"SELECT {cols} FROM jobs_with_description "
            "WHERE tagged_at IS NOT NULL "
            "AND description IS NOT NULL "
            "AND lang_req IS NULL "
            # Delisted rows are excluded: they're hidden from browsing by
            # default and can't be applied to, so spending bounded nightly
            # quota back-filling their facets starves the rows that matter.
            "AND delisted_at IS NULL "
            # Newest-first: yesterday's scan rows (tagged before the enrich
            # backstop filled their description) get facets the very next
            # night instead of queuing behind the historical backlog.
            "ORDER BY tagged_at DESC LIMIT ?"
        )
        cur = self.conn.execute(sql, (limit,))
        return [dict(zip(columns, r)) for r in cur.fetchall()]

    def find_delistable(self, company_board_ids: dict[str, set[str]]) -> list[str]:
        """Given {company: ids currently on that company's board this run},
        return stored ids for those companies that are missing from the fresh
        board and aren't already marked delisted. Callers must only pass
        companies that scraped cleanly this run — a company that errored or
        was skipped must be excluded, or a flaky scrape looks like every one
        of its roles got taken down."""
        if not company_board_ids:
            return []
        companies = list(company_board_ids)
        placeholders = ",".join("?" * len(companies))
        cur = self.conn.execute(
            f"SELECT id, company FROM seen_jobs "
            f"WHERE company IN ({placeholders}) AND delisted_at IS NULL",
            companies,
        )
        return [
            id_ for id_, company in cur.fetchall()
            if id_ not in company_board_ids.get(company, set())
        ]

    def mark_delisted(self, job_ids: list[str]) -> int:
        """Stamp delisted_at for rows no longer found on their company's
        board. Idempotent (only rows not already delisted are touched, so the
        original delist time is preserved). Returns rows updated."""
        if not job_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(job_ids))
        cur = self.conn.execute(
            f"UPDATE seen_jobs SET delisted_at = ? "
            f"WHERE id IN ({placeholders}) AND delisted_at IS NULL",
            [now, *job_ids],
        )
        self._commit()
        return cur.rowcount

    def purge_orphaned_companies(self, configured_names: set[str]) -> int:
        """Hard-delete every row for a company no longer in targets.json at
        all (removed source, not just a bad scrape this run) — those rows
        will never get a last_seen update or a delisted_at stamp again, so
        they'd otherwise sit stale forever. Never touches a role with ANY
        status history (queued/oa/interview/offer/rejected/ignored, not just
        applied): applied_at alone is only stamped on 'applied', so guarding
        on it would let renaming a source in targets.json delete a queued
        shortlist. Favorited roles are also spared regardless of status —
        a starred role must never be silently deleted. Returns rows deleted."""
        cur = self.conn.execute("SELECT DISTINCT company FROM seen_jobs")
        orphans = [name for (name,) in cur.fetchall() if name not in configured_names]
        if not orphans:
            return 0
        placeholders = ",".join("?" * len(orphans))
        return self._delete_jobs(
            f"company IN ({placeholders}) "
            f"AND applied_at IS NULL AND status = 'new' AND COALESCE(favorite, 0) = 0",
            orphans,
        )

    def _delete_jobs(self, where: str, params) -> int:
        """Hard-delete seen_jobs rows matching `where`, with their side-table
        rows, in one commit. Returns seen_jobs rows deleted."""
        params = list(params)
        for side in ("job_descriptions", "description_attempts"):
            self.conn.execute(
                f"DELETE FROM {side} WHERE job_id IN "
                f"(SELECT id FROM seen_jobs WHERE {where})", params)
        cur = self.conn.execute(f"DELETE FROM seen_jobs WHERE {where}", params)
        self._commit()
        return cur.rowcount

    def find_age_delistable(self, days: int,
                            exclude_companies: set[str] | None = None) -> list[str]:
        """Rows nobody has seen for `days`, still marked live.

        The delist pass only considers companies that scraped cleanly this run,
        and jobfeed/main.py additionally excludes degraded sources. Both guards are
        right, but together they mean a source whose board legitimately empties
        keeps its final roles live forever: it returns 0, trips the clean-zero
        backstop, is marked degraded, and is never eligible for delisting again.
        Observed 2026-09-02 — Muzinich's `Trader` and `High Yield Credit
        Analyst` still showed as live seven weeks after that board went to zero,
        which are exactly the rows a user acts on.

        This is a floor, not a replacement: it only fires well beyond any normal
        scan cadence, and touch_seen() re-lists a role the moment it reappears.

        `exclude_companies` must be the sources that DID produce roles this run,
        and passing it is not optional in practice. Without it the floor caught
        886 rows, 295 of them J.P. Morgan: a 7k-role board whose scrape cannot
        prove completeness leaves old rows un-refreshed even though they may
        still be live, and their public URLs are JS shells so liveness cannot be
        checked. Hiding possibly-live roles at a healthy source is a worse
        failure than leaving stale ones. Restricting the floor to sources that
        produced nothing addresses the case it was built for — a board that
        emptied — and leaves incomplete-but-productive boards alone.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "SELECT id, company FROM seen_jobs WHERE delisted_at IS NULL "
            "AND last_seen IS NOT NULL AND last_seen < ?",
            (cutoff,),
        )
        skip = exclude_companies or set()
        return [id_ for id_, company in cur.fetchall() if company not in skip]

    def mark_delisted_by_age(self, job_ids: list[str]) -> int:
        """Delist by age, flagged so the 'other' purge will not hard-delete."""
        if not job_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(job_ids))
        cur = self.conn.execute(
            f"UPDATE seen_jobs SET delisted_at = ?, delisted_by_age = 1 "
            f"WHERE id IN ({placeholders}) AND delisted_at IS NULL",
            [now, *job_ids],
        )
        self._commit()
        return cur.rowcount

    def purge_delisted_other(self, grace_days: int = 3) -> int:
        """Hard-delete 'other'-tagged roles once confirmed off their board —
        pure noise, no reason to keep it. Real categories are kept (with
        delisted_at set) so the web app can badge them instead. Never touches
        a role with any status history (not just applied — see
        purge_orphaned_companies), regardless of area. Favorited roles are
        also spared regardless of area or status — a starred role must never
        be silently deleted. Internships are also spared: ~60% carry
        area='other' (the prompt files student programmes there), so purging
        on area alone silently erodes the internship pool faster than the
        junior one — keep them and let the web app badge them.

        grace_days: a role must have been delisted for at least this many days
        before it is eligible for purging. This prevents a single-run partial
        scrape (21-90% of a board, below the degraded-guard threshold) from
        permanently deleting rows the same night they were first missed.
        touch_seen() clears delisted_at when a role reappears, so a recovered
        scraper self-heals within the grace window. delisted_at is stored as
        an ISO-format UTC timestamp (e.g. 2026-07-09T04:00:00.123456+00:00),
        and the cutoff is computed in the same format so the string comparison
        is lexicographically correct.

        Returns rows deleted."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat()
        return self._delete_jobs(
            "area = 'other' AND delisted_at IS NOT NULL "
            "AND delisted_at < ? "
            "AND applied_at IS NULL AND status = 'new' "
            "AND COALESCE(favorite, 0) = 0 "
            "AND COALESCE(delisted_by_age, 0) = 0 "
            "AND job_type != 'internship'",
            (cutoff,),
        )

    # --- app state (key/value) ---
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._commit()

    def count_new(self, since_ts: str) -> int:
        """Count new finance roles (not other / not senior) first seen after
        `since_ts` — drives the 'new since last check' badge. Internships are
        counted like any other role (they're no longer hidden by default)."""
        cur = self.conn.execute(
            "SELECT count(*) FROM seen_jobs WHERE first_seen > ? AND area != '' "
            "AND area != 'other' "
            "AND (min_yoe IS NULL OR min_yoe < ?)",
            (since_ts, YOE_HIDE_THRESHOLD),
        )
        return cur.fetchone()[0]

    def area_counts(self, *, status: str | None = None, category: str | None = None,
                    company: str | None = None, firm: str | None = None,
                    seniority: str | None = None,
                    job_type: str | None = None, loc_region: str | None = None,
                    loc_country: str | None = None, loc_city: str | None = None,
                    work_mode: str | None = None,
                    education: str | None = None, lang_req: str | None = None,
                    start: str | None = None,
                    hide_yoe: bool = False, hide_internships: bool = False,
                    hide_associates: bool = False, hide_delisted: bool = False,
                    closing: str | None = None,
                    favorite: bool | None = None,
                    companies: list[str] | None = None,
                    exclude_companies: list[str] | None = None,
                    q: str | None = None,
                    since: str | None = None) -> dict[str, int]:
        """Per-area role counts respecting the secondary filters (everything
        EXCEPT area/desk/hide_other) — drives the tab badges. Counts are
        pre-dedup (fine for an indicative badge). Untagged ('') rows ARE
        counted (the 'All' view shows them), so the All badge matches the
        view; per-area tabs key off their own code and ignore the '' bucket."""
        clauses, params = _job_filter_sql(
            locals(), omit={"area", "areas", "desk", "hide_other"},
            initial=["area IS NOT NULL"])
        sql = "SELECT area, count(*) FROM seen_jobs WHERE " + " AND ".join(clauses) + " GROUP BY area"
        return {a: n for a, n in self.conn.execute(sql, params)}

    def fetch_jobs(self, *, status: str | None = None,
                   statuses: list[str] | None = None, category: str | None = None,
                   company: str | None = None, firm: str | None = None,
                   area: str | None = None,
                   areas: list[str] | None = None,
                   desk: str | None = None,
                   seniority: str | None = None,
                   job_type: str | None = None, loc_region: str | None = None,
                   loc_country: str | None = None, loc_city: str | None = None,
                   work_mode: str | None = None,
                   education: str | None = None, lang_req: str | None = None,
                   start: str | None = None,
                   hide_other: bool = False,
                   hide_yoe: bool = False, hide_internships: bool = False,
                   hide_associates: bool = False, hide_delisted: bool = False,
                   closing: str | None = None,
                   favorite: bool | None = None,
                   companies: list[str] | None = None,
                   exclude_companies: list[str] | None = None,
                   q: str | None = None, since: str | None = None,
                   sort: str = "recent", limit: int | None = None,
                   with_description: bool = False) -> list[dict]:
        """Generalized role query used by the web app, JSON API, and jobfeed/query.py.
        All filters are AND-combined; None/empty means 'no constraint'.
        `companies` restricts to an explicit set (e.g. alumni firms).
        `q` is a free-text LIKE over title + company. Returns dict rows keyed
        by DISPLAY_COLUMNS, plus `description` when with_description is set
        (the list views never render it; the JSON API does)."""
        sql = f"SELECT {', '.join(DISPLAY_COLUMNS)} FROM seen_jobs"
        clauses, params = _job_filter_sql(locals())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        order = {
            "recent": "first_seen DESC",
            "company": "company, title",
        }.get(sort, "first_seen DESC")
        sql += f" ORDER BY {order}"
        # NB: dedup happens in Python below, so the LIMIT must be applied AFTER
        # collapsing duplicates (otherwise N copies of one role would eat the
        # limit). Fetching the whole filtered set is cheap now that it carries
        # no description text; that is fetched only for the rows returned.

        cur = self.conn.execute(sql, params)
        rows = [dict(zip(DISPLAY_COLUMNS, row)) for row in cur.fetchall()]
        rows = _dedup_rows(rows, self._description_lengths)
        if limit:
            rows = rows[:limit]
        if with_description:
            texts = self._descriptions([r["id"] for r in rows])
            for r in rows:
                r["description"] = texts.get(r["id"])
        return rows

    def _description_query(self, select: str, ids: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            out.update(self.conn.execute(
                f"SELECT job_id, {select} FROM job_descriptions "
                f"WHERE job_id IN ({','.join('?' * len(chunk))})", chunk))
        return out

    def _descriptions(self, ids: list[str]) -> dict[str, str]:
        return self._description_query("description", ids)

    def _description_lengths(self, ids: list[str]) -> dict[str, int]:
        return self._description_query("length(description)", ids)

    def firm_names(self) -> list[str]:
        """Every firm with a live role, source-label suffixes folded
        ("UBS (Graduate Careers)" -> "UBS"), for the Browse firm search."""
        rows = self.conn.execute(
            "SELECT DISTINCT company FROM seen_jobs "
            "WHERE delisted_at IS NULL AND company IS NOT NULL AND company <> ''")
        return sorted({plain_company(c) for (c,) in rows if plain_company(c)},
                      key=str.lower)

    def distinct(self, column: str, where: dict | None = None) -> list[str]:
        """Distinct non-empty values for a facet column (populates UI
        dropdowns). Column (and any `where` keys) is whitelisted against
        FACET_COLUMNS. `where` scopes the values to a parent facet — e.g.
        distinct('loc_city', {'loc_country': 'UK'}) lists only UK cities."""
        if column not in FACET_COLUMNS:
            raise ValueError(f"not a facet column: {column}")
        clauses = [f"{column} IS NOT NULL", f"{column} != ''"]
        params: list = []
        for k, v in (where or {}).items():
            if k not in FACET_COLUMNS:
                raise ValueError(f"not a facet column: {k}")
            clauses.append(f"{k} = ?")
            params.append(v)
        sql = (f"SELECT DISTINCT {column} FROM seen_jobs "
               f"WHERE {' AND '.join(clauses)} ORDER BY {column}")
        rows = [row[0] for row in self.conn.execute(sql, params).fetchall()]
        return _split_lang_facet(rows) if column == "lang_req" else rows

    def start_years(self) -> list[str]:
        """Distinct 4-digit years present in start_date, ascending — drives the
        Start filter dropdown ('asap' is a fixed option in the template; ''/NULL
        = unstated are not options). Prefix-derived so '2026' and '2026-09'
        both contribute the year 2026 once."""
        rows = self.conn.execute(
            "SELECT DISTINCT substr(start_date, 1, 4) FROM seen_jobs "
            "WHERE start_date GLOB '[0-9][0-9][0-9][0-9]*' ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def distinct_scoped(self, column: str, f: dict) -> list[str]:
        """Distinct non-empty values of a facet column among rows matching the
        active browse filters `f` — so a facet dropdown lists only values that
        actually have a role visible under the current view (faceted search).
        Mirrors the fetch_jobs filters that shape the visible set, so e.g. a
        country whose only role is delisted / back-office / hidden-by-default no
        longer clutters the Country list."""
        if column not in FACET_COLUMNS:
            raise ValueError(f"not a facet column: {column}")
        clauses, params = _job_filter_sql(
            f, omit={column}, initial=[f"{column} IS NOT NULL", f"{column} != ''"])
        sql = (f"SELECT DISTINCT {column} FROM seen_jobs "
               f"WHERE {' AND '.join(clauses)} ORDER BY {column}")
        rows = [row[0] for row in self.conn.execute(sql, params).fetchall()]
        return _split_lang_facet(rows) if column == "lang_req" else rows

    def get_job(self, job_id: str) -> dict | None:
        """One role by id, with all display columns. None if not found."""
        cols = DISPLAY_COLUMNS + ["description"]
        cur = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM jobs_with_description WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def log_run(self, new_jobs: int, firms_checked: int, errors: int,
                duration_s: float | None = None):
        self.conn.execute(
            "INSERT INTO runs (ran_at, new_jobs, firms_checked, errors, duration_s)"
            " VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), new_jobs, firms_checked,
             errors, duration_s),
        )
        self._commit()

    def last_run_info(self) -> dict | None:
        """The most recent scan run, or None if none logged yet."""
        row = self.conn.execute(
            "SELECT ran_at, new_jobs, firms_checked, errors, duration_s"
            " FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {"ran_at": row[0], "new_jobs": row[1], "firms_checked": row[2],
                "errors": row[3], "duration_s": row[4]}

    @staticmethod
    def _exclude_clause(exclude_companies: list[str] | None) -> tuple[str, list]:
        """Build an ` AND company NOT IN (...)` fragment (+ params) for the stats
        queries, so firms toggled off on Sources drop out of Stats too."""
        if not exclude_companies:
            return "", []
        ph = ",".join("?" * len(exclude_companies))
        return f" AND company NOT IN ({ph})", list(exclude_companies)

    def total_seen(self, exclude_companies: list[str] | None = None) -> int:
        exc, params = self._exclude_clause(exclude_companies)
        sql = "SELECT COUNT(*) FROM seen_jobs" + (" WHERE 1=1" + exc if exc else "")
        return self.conn.execute(sql, params).fetchone()[0]

    def status_counts(self, exclude_companies: list[str] | None = None,
                      track: str | None = None) -> dict[str, int]:
        """{status: live-role count}, for the Stats "by status" bars.

        Delisted rows are excluded so the bars describe the list he is
        actually looking at — a role he applied to that has since come off the
        board still counts, because his own status on it is still true; one
        that was never touched and is gone is not.
        """
        exc, params = self._exclude_clause(exclude_companies)
        tc = track_clause(track)
        sql = ("SELECT status, COUNT(*) FROM seen_jobs "
               "WHERE (delisted_at IS NULL OR status != 'new')" + exc
               + (" AND " + tc if tc else "") + " GROUP BY status")
        return {st or "new": n for st, n in self.conn.execute(sql, params)}

    def company_recent_volume(self, days: int = 14) -> dict[str, int]:
        """Return {company: count of jobs last_seen within `days` days}. Drives
        the recent-volume table on the /stats page (and flags silent-zero
        scrapers — a company that historically had N>0 but returns 0 is likely
        broken: ATS migration, anti-bot block, paused hiring)."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "SELECT company, COUNT(*) FROM seen_jobs "
            "WHERE last_seen >= ? GROUP BY company",
            (cutoff,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def company_stats(self) -> dict[str, dict]:
        """Per-company live counts for the Sources page: total stored, finance
        subset (tagged area present and != 'other'), and most-recent last_seen.
        Keyed by company name."""
        cur = self.conn.execute(
            "SELECT company, COUNT(*), "
            "SUM(CASE WHEN area != '' AND area != 'other' THEN 1 ELSE 0 END), "
            "MAX(last_seen) "
            "FROM seen_jobs GROUP BY company"
        )
        return {
            row[0]: {"total": row[1], "finance": row[2] or 0, "last_seen": row[3]}
            for row in cur.fetchall()
        }

    # Tagging errors are not symmetric. Most wrong facets only mislabel a role
    # and you still see it; two REMOVE it from view, so they can never show up
    # as "a tag I noticed was wrong" no matter how long you browse:
    #   seniority='manager' — a hard gate in the browse query (hide_yoe).
    #   area='other'        — hidden by default everywhere except its own tab,
    #                         and purge_delisted_other() DELETES it once the
    #                         role goes off its board.
    # These two samplers exist to make that invisible bucket auditable: a random
    # handful you can eyeball, rather than a metric you have to trust.
    _REVIEW_BUCKETS = {
        # Live roles the manager gate is hiding right now.
        "manager": "seniority = 'manager' AND delisted_at IS NULL",
        # Live roles binned as non-finance.
        "other": "area = 'other' AND delisted_at IS NULL",
        # The sharp end: binned 'other', already off its board, so the next
        # purge deletes them permanently. Last chance to catch a bad bin.
        "purge": ("area = 'other' AND delisted_at IS NOT NULL "
                  "AND applied_at IS NULL AND status = 'new' "
                  "AND COALESCE(favorite, 0) = 0 AND job_type != 'internship'"),
    }

    def review_sample(self, bucket: str, limit: int = 24,
                      exclude_companies: list[str] | None = None) -> list[dict]:
        """A random sample of roles that tagging removed from view.

        Random, not newest-first: a fixed window would show the same rows every
        visit and only ever audit whatever the last scan happened to store."""
        where = self._REVIEW_BUCKETS.get(bucket)
        if not where:
            return []
        exc, exc_p = self._exclude_clause(exclude_companies)
        cur = self.conn.execute(
            "SELECT id, company, title, url, location, area, desk, seniority, "
            "job_type, min_yoe, first_seen, delisted_at "
            f"FROM seen_jobs WHERE {where}{exc} "
            "AND NOT EXISTS (SELECT 1 FROM tag_evaluations e "
            "WHERE e.job_id = seen_jobs.id) "
            "ORDER BY RANDOM() LIMIT ?",
            (*exc_p, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def record_tag_evaluation(self, job_id: str, *, bucket: str,
                              area: str, desk: str, seniority: str,
                              job_type: str) -> bool:
        """Freeze the current prediction/provenance and store human truth.

        Corrections immediately update the four browse tags. Confirmations are
        equally valuable: without true negatives the false-hide rate is not a
        rate at all, merely a pile of mistakes.
        """
        row = self.conn.execute(
            "SELECT area, desk, seniority, job_type, tag_provider, tag_model, "
            "tag_rubric_version FROM seen_jobs WHERE id = ?", (job_id,),
        ).fetchone()
        if not row:
            return False
        now = datetime.now(timezone.utc).isoformat()
        baseline = tuple(v or "" for v in row[:4])
        human = (area, desk, seniority, job_type)
        corrected = int(baseline != human)
        self.conn.execute(
            """INSERT INTO tag_evaluations
               (job_id, evaluated_at, updated_at, review_bucket,
                baseline_area, baseline_desk, baseline_seniority,
                baseline_job_type, human_area, human_desk, human_seniority,
                human_job_type, provider, model, rubric_version, corrected)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 human_area=excluded.human_area, human_desk=excluded.human_desk,
                 human_seniority=excluded.human_seniority,
                 human_job_type=excluded.human_job_type,
                 corrected=excluded.corrected""",
            (job_id, now, now, bucket, *baseline, *human,
             row[4] or "", row[5] or "", row[6] or "", corrected),
        )
        self.conn.execute(
            "UPDATE seen_jobs SET area=?, desk=?, seniority=?, job_type=? "
            "WHERE id=?", (*human, job_id),
        )
        self._commit()
        return True

    def tag_evaluation_summary(self) -> dict:
        rows = self.conn.execute("SELECT * FROM tag_evaluations").fetchall()
        cols = [d[0] for d in self.conn.execute(
            "SELECT * FROM tag_evaluations LIMIT 0").description]
        data = [dict(zip(cols, row)) for row in rows]
        total = len(data)
        false_manager = sum(r["baseline_seniority"] == "manager" and
                            r["human_seniority"] != "manager" for r in data)
        false_other = sum(r["baseline_area"] == "other" and
                          r["human_area"] != "other" for r in data)
        grouped: dict[tuple, dict] = {}
        for r in data:
            key = (r["provider"] or "unknown", r["model"] or "unknown",
                   r["rubric_version"] or "unknown")
            g = grouped.setdefault(key, {"provider": key[0], "model": key[1],
                                         "rubric": key[2], "n": 0,
                                         "corrected": 0, "false_manager": 0,
                                         "false_other": 0})
            g["n"] += 1
            g["corrected"] += r["corrected"]
            g["false_manager"] += int(r["baseline_seniority"] == "manager" and r["human_seniority"] != "manager")
            g["false_other"] += int(r["baseline_area"] == "other" and r["human_area"] != "other")
        return {"total": total, "corrected": sum(r["corrected"] for r in data),
                "false_manager": false_manager, "false_other": false_other,
                "models": sorted(grouped.values(), key=lambda g: -g["n"])}

    def application_funnel(self, column: str, limit: int = 8,
                           track: str | None = None) -> list[dict]:
        """Response funnel for applications, grouped by a whitelisted facet.

        An application is a row whose CURRENT status is a submitted stage, not
        any row that once had applied_at stamped: nine rows he moved back to
        'new' by hand were still counted until 2026-09-23."""
        if column not in {"area", "company", "loc_city"}:
            raise ValueError("unsupported funnel column")
        tc = track_clause(track)
        rows = self.conn.execute(
            f"""SELECT COALESCE(NULLIF({column}, ''), 'Unknown') label,
                       COUNT(*) applications,
                       SUM(status IN ('oa','interview','offer','rejected')) responses,
                       SUM(status IN ('oa','interview','offer')) progressed,
                       SUM(status = 'offer') offers
                FROM seen_jobs
                WHERE status IN ({",".join("?" * len(APPLIED_STATUSES))})
                {"AND " + tc if tc else ""}
                GROUP BY 1 ORDER BY applications DESC, label LIMIT ?""",
            (*APPLIED_STATUSES, limit),
        ).fetchall()
        return [{"label": r[0], "applications": r[1], "responses": r[2],
                 "progressed": r[3], "offers": r[4],
                 "response_rate": round(100 * r[2] / r[1]) if r[1] else 0}
                for r in rows]

    def application_status_by_track(self, exclude_companies: list[str] | None = None
                                    ) -> dict[str, dict[str, int]]:
        """{track: {status: n}} over submitted applications, for the
        internship vs full-time comparison on Application stats."""
        exc, params = self._exclude_clause(exclude_companies)
        out: dict[str, dict[str, int]] = {t: {} for t in TRACKS}
        q = ",".join("?" * len(APPLIED_STATUSES))
        for track in TRACKS:
            for st, n in self.conn.execute(
                    f"SELECT status, COUNT(*) FROM seen_jobs WHERE status IN ({q}) "
                    f"AND {track_clause(track)}{exc} GROUP BY status",
                    [*APPLIED_STATUSES, *params]):
                out[track][st] = n
        return out

    def applications_per_week(self, weeks: int = 12, track: str | None = None,
                              exclude_companies: list[str] | None = None) -> list[dict]:
        """Submitted applications per ISO-style 7-day bucket ending today,
        oldest first and zero-filled, for the Application stats chart.

        Counts rows whose CURRENT status is a submitted stage (as
        application_funnel does), dated by applied_at; a row that reached a
        later stage without passing through 'applied' has no stamp, so it
        falls back to first_seen rather than dropping out."""
        from datetime import timedelta
        exc, exc_p = self._exclude_clause(exclude_companies)
        tc = track_clause(track)
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=7 * weeks - 1)
        q = ",".join("?" * len(APPLIED_STATUSES))
        counts = [0] * weeks
        for (day,) in self.conn.execute(
                f"SELECT substr(COALESCE(applied_at, first_seen), 1, 10) FROM seen_jobs "
                f"WHERE status IN ({q}) AND COALESCE(applied_at, first_seen) >= ?"
                + (" AND " + tc if tc else "") + exc,
                (*APPLIED_STATUSES, start.isoformat(), *exc_p)):
            try:
                idx = (datetime.fromisoformat(day).date() - start).days // 7
            except (TypeError, ValueError):
                continue
            if 0 <= idx < weeks:
                counts[idx] += 1
        return [{"start": (start + timedelta(days=7 * i)).isoformat(), "n": n}
                for i, n in enumerate(counts)]

    def weekly_intake_counts(self, weeks: int = 8,
                             exclude_companies: list[str] | None = None) -> list[dict]:
        """Finance / other new-role counts per rolling week, newest first, on
        the same windows as weekly_summary (week 0 = the last 7 days, today
        included). One grouped scan: weekly_summary costs two scans per week,
        which doubled the stats page's query time when stretched to 8 weeks."""
        from datetime import timedelta
        exc, exc_p = self._exclude_clause(exclude_companies)
        today = datetime.now(timezone.utc).date()
        end = today + timedelta(days=1)
        out = [{"week_ago": w, "finance": 0, "other_n": 0} for w in range(weeks)]
        for day, is_other, n in self.conn.execute(
                "SELECT substr(first_seen, 1, 10), COALESCE(NULLIF(area,''),'other') = 'other',"
                " COUNT(*) FROM seen_jobs WHERE first_seen >= ? AND first_seen < ?" + exc
                + " GROUP BY 1, 2",
                ((end - timedelta(weeks=weeks)).isoformat(), end.isoformat(), *exc_p)):
            try:
                w = (today - datetime.fromisoformat(day).date()).days // 7
            except (TypeError, ValueError):
                continue
            if 0 <= w < weeks:
                out[w]["other_n" if is_other else "finance"] += n
        return out

    def new_roles_per_day(self, days: int = 30,
                          exclude_companies: list[str] | None = None) -> list[dict]:
        """Roles first stored per UTC day, oldest first and zero-filled."""
        from datetime import timedelta
        exc, exc_p = self._exclude_clause(exclude_companies)
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days - 1)
        got = dict(self.conn.execute(
            "SELECT substr(first_seen, 1, 10), COUNT(*) FROM seen_jobs "
            "WHERE first_seen >= ?" + exc + " GROUP BY 1",
            (start.isoformat(), *exc_p)).fetchall())
        return [{"date": d, "n": got.get(d, 0)}
                for d in ((start + timedelta(days=i)).isoformat() for i in range(days))]

    def review_counts(self, exclude_companies: list[str] | None = None
                      ) -> dict[str, int]:
        """Size of each hidden bucket, for the header."""
        exc, exc_p = self._exclude_clause(exclude_companies)
        out = {}
        for name, where in self._REVIEW_BUCKETS.items():
            cur = self.conn.execute(
                f"SELECT COUNT(*) FROM seen_jobs WHERE {where}{exc} "
                "AND NOT EXISTS (SELECT 1 FROM tag_evaluations e "
                "WHERE e.job_id = seen_jobs.id)", exc_p)
            out[name] = cur.fetchone()[0]
        return out

    def weekly_summary(self, weeks: int = 4,
                       exclude_companies: list[str] | None = None) -> list[dict]:
        """Per-week new-role counts by area and region, newest first."""
        from datetime import timedelta
        exc, exc_p = self._exclude_clause(exclude_companies)
        # Windows end TOMORROW: first_seen is a timestamp, so `< today` (a bare
        # date string) silently dropped every role stored today from "This week".
        end = datetime.now(timezone.utc).date() + timedelta(days=1)
        out = []
        for w in range(weeks):
            lo = (end - timedelta(weeks=w + 1)).isoformat()
            hi = (end - timedelta(weeks=w)).isoformat()
            area = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(area,''),'other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen >= ? AND first_seen < ?" + exc + " GROUP BY 1",
                (lo, hi, *exc_p),
            ).fetchall())
            region = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(loc_region,''),'Other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen >= ? AND first_seen < ?" + exc + " GROUP BY 1",
                (lo, hi, *exc_p),
            ).fetchall())
            out.append({
                "week_ago": w,
                "label": "This week" if w == 0 else ("Last week" if w == 1 else f"{w}w ago"),
                "total": sum(area.values()),
                "finance": sum(n for a, n in area.items() if a != "other"),
                "other_n": area.get("other", 0),
                "area": area,
                "region": region,
            })
        return out

    def company_weekly_velocity(self, weeks: int = 8,
                                exclude_companies: list[str] | None = None) -> list[dict]:
        """Per-company finance role counts for the last `weeks` weeks.
        Returns [{company, counts[0..weeks-1], total, recent, max_wk}] sorted
        by this-week count desc then total desc. counts[0] = most recent week."""
        today = datetime.now(timezone.utc).date().isoformat()
        from datetime import timedelta
        exc, exc_p = self._exclude_clause(exclude_companies)
        cutoff = (datetime.now(timezone.utc).date() - timedelta(weeks=weeks)).isoformat()
        rows = self.conn.execute(
            """
            SELECT company,
                   CAST((julianday(?) - julianday(substr(first_seen,1,10))) / 7 AS INTEGER) AS week_ago,
                   COUNT(*) AS n
            FROM seen_jobs
            WHERE first_seen >= ? AND area != '' AND area != 'other'""" + exc + """
            GROUP BY company, week_ago
            """,
            (today, cutoff, *exc_p),
        ).fetchall()
        data: dict[str, list[int]] = {}
        for company, week_ago, n in rows:
            w = int(week_ago)
            if w < 0 or w >= weeks:
                continue
            if company not in data:
                data[company] = [0] * weeks
            data[company][w] = n
        result = []
        for company, counts in data.items():
            total = sum(counts)
            mx = max(counts) if counts else 0
            result.append({
                "company": company,
                "counts": counts,
                "total": total,
                "recent": counts[0] if counts else 0,
                "max_wk": mx,
            })
        result.sort(key=lambda r: (-r["recent"], -r["total"]))
        return result
