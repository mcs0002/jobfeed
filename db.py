"""
SQLite state tracker. Remembers which job IDs have been seen so the scan only
flags truly new openings, and stores the full role record (category, location,
posted date, tags, status) for browsing/filtering in the web app.
"""
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

# DB paths whose schema (CREATE TABLE + _migrate) has already run this process.
# web/app.py builds a fresh JobDB per request; without this gate every request
# re-ran the full DDL + a commit. The schema is stable within a process once
# initialized, so repeat constructions for the same path skip it. Keyed by the
# resolved absolute path so two spellings of the same file share the flag.
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


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


def _dedup_rows(rows: list[dict]) -> list[dict]:
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
    means 'best per the active sort' (e.g. most recent)."""
    def _desc_len(row: dict) -> int:
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


# Columns added after the original (id, company, title, url, first_seen)
# schema. Existing databases are upgraded in place via ALTER TABLE.
EXTRA_COLUMNS = [
    ("category", "TEXT DEFAULT ''"),
    ("location", "TEXT DEFAULT ''"),
    ("posted", "TEXT DEFAULT ''"),
    ("last_seen", "TEXT"),
    ("status", "TEXT DEFAULT 'new'"),
    # description captured at scrape time when the ATS includes it in the
    # search response (Lever, SmartRecruiters, Greenhouse), otherwise filled
    # in by the enrichment pass. Plain text. NULL until populated.
    ("description", "TEXT"),
    ("description_fetched_at", "TEXT"),
    # Structured tags from the all-Haiku tagging pass (tag.py). Populated
    # for every stored role so the web UI can filter by sector + function +
    # location without re-filtering at scrape time. Empty string = untagged
    # (still filterable as "unclassified"). See tag.py for the label sets.
    ("function", "TEXT DEFAULT ''"),       # LEGACY (flat taxonomy) — superseded by area/desk, kept for back-compat
    ("area", "TEXT DEFAULT ''"),           # markets/quant/research/ibd/capital-markets/asset-management/wealth/risk/other
    ("desk", "TEXT DEFAULT ''"),           # markets function: trading/sales/structuring/research (only when area=markets)
    ("seniority", "TEXT DEFAULT ''"),      # intern/graduate/analyst/associate
    ("job_type", "TEXT DEFAULT 'job'"),    # job/internship/graduate-programme
    ("loc_city", "TEXT DEFAULT ''"),
    ("loc_country", "TEXT DEFAULT ''"),
    ("loc_region", "TEXT DEFAULT ''"),     # Europe/Americas/APAC/MEA
    ("work_mode", "TEXT DEFAULT ''"),      # onsite/hybrid/remote
    # Description-derived facets from the Haiku pass (tag.py). Unlike the
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
]


# Columns returned by fetch_jobs() / surfaced in the web UI and JSON API.
DISPLAY_COLUMNS = [
    "id", "company", "category", "title", "location", "posted", "status",
    "first_seen", "last_seen", "url", "area", "desk", "seniority", "job_type",
    "loc_city", "loc_country", "loc_region", "work_mode", "min_yoe",
    "lang_req", "education", "start_date",
    "applied_at", "notes", "favorite", "delisted_at", "deadline",
    "description",
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
        # Index the canonical posting url so the scan loop's ID-churn lookup
        # (find_active_by_url — touch/forward-fill an existing row instead of
        # inserting a fresh-id duplicate) is a single indexed probe, not a
        # per-job table scan.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_jobs_url ON seen_jobs(url)"
        )
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

    def touch_seen(self, job_id: str) -> bool:
        """Bump last_seen for a role still appearing on its board, without
        touching first_seen/status/description. Drives the Sources/Stats
        silent-zero detection (company_recent_volume keys off last_seen).
        Clears delisted_at — a role found again is no longer delisted, even
        if it was pulled and reposted. Returns True if the row existed."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE seen_jobs SET last_seen = ?, delisted_at = NULL WHERE id = ?",
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
            "UPDATE seen_jobs SET description = ? "
            "WHERE id = ? AND (description IS NULL OR description = '')",
            (text, job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def mark_seen(self, job_id: str, company: str = "", title: str = "", url: str = "",
                  category: str = "", location: str = "", posted: str = "",
                  description: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        desc = description or None  # store NULL instead of "" so enrichment
        desc_at = now if desc else None  # can target description IS NULL.
        self.conn.execute(
            """
            INSERT INTO seen_jobs
                (id, company, title, url, first_seen, category, location, posted,
                 last_seen, status, description, description_fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen,
                delisted_at = NULL
            """,
            (job_id, company, title, url, now, category, location, posted,
             now, desc, desc_at),
        )
        self._commit()

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
            "UPDATE seen_jobs SET description = ? "
            "WHERE id = ? AND length(coalesce(description, '')) < 800 "
            "AND length(?) > length(coalesce(description, ''))",
            (text, job_id, text),
        )
        self._commit()
        return cur.rowcount > 0

    def set_description(self, job_id: str, description: str) -> bool:
        """Populate or overwrite the description for one job (used by the
        enrichment pass). Returns True if the row exists."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE seen_jobs SET description = ?, description_fetched_at = ? "
            "WHERE id = ?",
            (description or None, now if description else None, job_id),
        )
        self._commit()
        return cur.rowcount > 0

    def jobs_missing_description(self, limit: int = 100,
                                 max_age_days: int | None = None,
                                 exclude_prefixes: tuple = ()) -> list[dict]:
        """Return rows that have no description yet. If max_age_days is set,
        skip rows whose first_seen is older than that — supports a rolling
        window so we don't enrich roles that have already aged out.
        exclude_prefixes drops id prefixes known to be unenrichable (JS-shell
        ATSes with no detail enricher) so hopeless rows can't starve the
        nightly budget by re-entering the queue every run."""
        sql = (
            "SELECT id, company, title, url FROM seen_jobs "
            "WHERE description IS NULL"
        )
        params: list = []
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
            "UPDATE seen_jobs SET description = NULL, description_fetched_at = NULL "
            "WHERE description IS NOT NULL AND first_seen < ? AND status = 'new'",
            (cutoff,),
        )
        self._commit()
        return cur.rowcount

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

    def set_tags(self, job_id: str, *, area: str = "", desk: str = "",
                 seniority: str = "", job_type: str = "job",
                 loc_city: str = "", loc_country: str = "", loc_region: str = "",
                 work_mode: str = "",
                 lang_req: str | None = None, education: str | None = None,
                 start_date: str | None = None,
                 min_yoe: int | None = None, provider: str = "",
                 model: str = "", rubric_version: str = "") -> bool:
        """Persist the structured tags from the Haiku tagging pass (tag.py).
        Returns True if the row exists. Empty area = untagged (filterable as
        'unclassified').

        The description-derived facets (lang_req/education/start_date) carry
        NULL-vs-'' semantics: pass None (the tag.py sentinel when no description
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
            f"SELECT {cols} FROM seen_jobs "
            "WHERE tagged_at IS NOT NULL "
            "AND description IS NOT NULL AND description != '' "
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
        cur = self.conn.execute(
            f"DELETE FROM seen_jobs WHERE company IN ({placeholders}) "
            f"AND applied_at IS NULL AND status = 'new' AND COALESCE(favorite, 0) = 0",
            orphans,
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
        cur = self.conn.execute(
            "DELETE FROM seen_jobs WHERE area = 'other' AND delisted_at IS NOT NULL "
            "AND delisted_at < ? "
            "AND applied_at IS NULL AND status = 'new' "
            "AND COALESCE(favorite, 0) = 0 "
            "AND job_type != 'internship'",
            (cutoff,),
        )
        self._commit()
        return cur.rowcount

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
                    company: str | None = None,
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
                   company: str | None = None, area: str | None = None,
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
                   sort: str = "recent", limit: int | None = None) -> list[dict]:
        """Generalized role query used by the web app, JSON API, and query.py.
        All filters are AND-combined; None/empty means 'no constraint'.
        `companies` restricts to an explicit set (e.g. alumni firms).
        `q` is a free-text LIKE over title + company. Returns dict rows keyed
        by DISPLAY_COLUMNS."""
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
        # limit). The full filtered set is small enough (~14k rows worst case)
        # that fetching it all and slicing in Python is fine.

        cur = self.conn.execute(sql, params)
        rows = [dict(zip(DISPLAY_COLUMNS, row)) for row in cur.fetchall()]
        rows = _dedup_rows(rows)
        if limit:
            rows = rows[:limit]
        return rows

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
        cur = self.conn.execute(
            f"SELECT {', '.join(DISPLAY_COLUMNS)} FROM seen_jobs WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
        return dict(zip(DISPLAY_COLUMNS, row)) if row else None

    def recent_runs(self, limit: int = 20) -> list[dict]:
        """Most-recent scan records for the /stats page."""
        cur = self.conn.execute(
            "SELECT ran_at, new_jobs, firms_checked, errors FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = ("ran_at", "new_jobs", "firms_checked", "errors")
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def run_breakdowns(self, limit: int = 12) -> list[dict]:
        """Per-run new-job breakdowns by area and region, for the Stats page.

        A job belongs to the run whose window (previous run's ran_at, this
        run's ran_at] contains its first_seen — ran_at is logged at run end, so
        a run's new rows land just under it. Counts use each row's current
        area/region (so a later re-tag is reflected)."""
        rows = self.conn.execute(
            "SELECT ran_at, new_jobs, firms_checked, errors FROM runs "
            "ORDER BY ran_at DESC LIMIT ?",
            (limit + 1,),
        ).fetchall()
        out = []
        for i in range(min(limit, len(rows))):
            ran_at, new_jobs, firms, errors = rows[i]
            lo = rows[i + 1][0] if i + 1 < len(rows) else ""
            area = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(area,''),'other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen > ? AND first_seen <= ? GROUP BY 1",
                (lo, ran_at),
            ).fetchall())
            region = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(loc_region,''),'Other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen > ? AND first_seen <= ? GROUP BY 1",
                (lo, ran_at),
            ).fetchall())
            in_window = sum(area.values())
            finance = sum(n for a, n in area.items() if a != "other")
            out.append({
                "ran_at": ran_at, "new_jobs": new_jobs,
                "firms_checked": firms, "errors": errors,
                "area": area, "region": region,
                "in_window": in_window, "finance": finance,
            })
        return out

    def daily_breakdowns(self, days: int = 7) -> list[dict]:
        """Per-calendar-day new-role breakdowns (area + region) for the last
        `days` UTC days, newest first. A row belongs to the day of its
        first_seen. Drives the Stats page."""
        from datetime import timedelta
        out = []
        today = datetime.now(timezone.utc).date()
        for d in range(days):
            day = today - timedelta(days=d)
            lo = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()
            hi = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
                  + timedelta(days=1)).isoformat()
            area = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(area,''),'other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen >= ? AND first_seen < ? GROUP BY 1",
                (lo, hi),
            ).fetchall())
            region = dict(self.conn.execute(
                "SELECT COALESCE(NULLIF(loc_region,''),'Other'), COUNT(*) FROM seen_jobs "
                "WHERE first_seen >= ? AND first_seen < ? GROUP BY 1",
                (lo, hi),
            ).fetchall())
            out.append({
                "day": day.isoformat(),
                "total": sum(area.values()),
                "finance": sum(n for a, n in area.items() if a != "other"),
                "other_n": area.get("other", 0),
                "area": area,
                "region": region,
            })
        return out

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

    def status_counts(self, exclude_companies: list[str] | None = None
                      ) -> dict[str, int]:
        """{status: live-role count}, for the Stats "by status" bars.

        Delisted rows are excluded so the bars describe the list he is
        actually looking at — a role he applied to that has since come off the
        board still counts, because his own status on it is still true; one
        that was never touched and is gone is not.
        """
        exc, params = self._exclude_clause(exclude_companies)
        sql = ("SELECT status, COUNT(*) FROM seen_jobs "
               "WHERE (delisted_at IS NULL OR status != 'new')" + exc
               + " GROUP BY status")
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

    def application_funnel(self, column: str, limit: int = 8) -> list[dict]:
        """Response funnel for applications, grouped by a whitelisted facet."""
        if column not in {"area", "company", "loc_city"}:
            raise ValueError("unsupported funnel column")
        rows = self.conn.execute(
            f"""SELECT COALESCE(NULLIF({column}, ''), 'Unknown') label,
                       COUNT(*) applications,
                       SUM(status IN ('oa','interview','offer','rejected')) responses,
                       SUM(status IN ('oa','interview','offer')) progressed,
                       SUM(status = 'offer') offers
                FROM seen_jobs WHERE applied_at IS NOT NULL
                GROUP BY 1 ORDER BY applications DESC, label LIMIT ?""", (limit,),
        ).fetchall()
        return [{"label": r[0], "applications": r[1], "responses": r[2],
                 "progressed": r[3], "offers": r[4],
                 "response_rate": round(100 * r[2] / r[1]) if r[1] else 0}
                for r in rows]

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
        today = datetime.now(timezone.utc).date()
        out = []
        for w in range(weeks):
            lo = (today - timedelta(weeks=w + 1)).isoformat()
            hi = (today - timedelta(weeks=w)).isoformat()
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

    def last_run(self) -> str:
        cur = self.conn.execute("SELECT ran_at FROM runs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else "never"
