"""Browse free-text search: an FTS5 index over title, company, location,
city and posting text.

The old box (removed 2026-09-12, d92d9f6) read as a location search but matched
only title and company, so "Munich" missed every role stored as "München".
This one indexes the location and the description too, folds diacritics
(unicode61, remove_diacritics 2: "München" is the token "munchen"), and expands
a city term through `tag._CITY_CANON`, so Munich, München and Muenchen find the
same rows: the canonical `loc_city` (indexed as its own column) and both
spellings in the text.

Storage. The index is contentless (`content=''`, `contentless_delete=1`): it
holds tokens, not a second copy of 275 MB of posting text. Its rowid is a
`docid` from `job_search_ids`, an INTEGER PRIMARY KEY, because seen_jobs has a
TEXT key and VACUUM may renumber an implicit rowid. Triggers on seen_jobs and
job_descriptions keep it in sync with every writer, the JobDB methods and the
maintenance scripts alike.

Rows written before the index existed are filled by the one-off backfill
(`python -m jobfeed.search --build`), in short batches so the web app keeps
reading and a scan keeps writing on the live WAL database. Until it finishes,
`meta.job_search_built` is unset and the search falls back to a LIKE over
title, company, location and city, so a half-built index never hides roles.

Query syntax: words AND together and match as prefixes ("quant" finds
"quantitative"), "quoted phrases" match exactly, a leading minus excludes
(-wealth). Every term reaches MATCH as a quoted FTS5 string, so stray quotes,
operators and brackets are text, never syntax.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import unicodedata
from functools import lru_cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))

FTS_TABLE = "job_search"
IDS_TABLE = "job_search_ids"
BUILT_META_KEY = "job_search_built"
MAX_TERMS = 12
MAX_QUERY = 200

_INDEXED = ("title", "company", "location", "loc_city")


def _refresh_sql(job: str) -> str:
    """Trigger statements that (re)index one job: `job` is an SQL expression
    (new.id, old.job_id, ...). Delete-then-insert, so it serves insert, update
    and a description arriving later alike. No OR IGNORE: an outer statement's
    conflict clause (the description UPSERTs) overrides a trigger's, so the
    map insert guards itself with NOT EXISTS instead."""
    return f"""
        DELETE FROM {FTS_TABLE} WHERE rowid IN
            (SELECT docid FROM {IDS_TABLE} WHERE job_id = {job});
        INSERT INTO {IDS_TABLE}(job_id)
            SELECT id FROM seen_jobs WHERE id = {job}
            AND NOT EXISTS (SELECT 1 FROM {IDS_TABLE} WHERE job_id = {job});
        INSERT INTO {FTS_TABLE}(rowid, title, company, location, loc_city, description)
            SELECT m.docid, s.title, s.company, s.location, s.loc_city, d.description
            FROM {IDS_TABLE} m JOIN seen_jobs s ON s.id = m.job_id
            LEFT JOIN job_descriptions d ON d.job_id = s.id
            WHERE m.job_id = {job};"""


def _changed(cols) -> str:
    return " OR ".join(f"old.{c} IS NOT new.{c}" for c in cols)


SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS {IDS_TABLE} (
            docid INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL UNIQUE)""",
    f"""CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
            title, company, location, loc_city, description,
            content='', contentless_delete=1,
            tokenize='unicode61 remove_diacritics 2')""",
    f"""CREATE TRIGGER IF NOT EXISTS job_search_ai AFTER INSERT ON seen_jobs
        BEGIN {_refresh_sql('new.id')} END""",
    # last_seen and the tag columns change on every scan; only the indexed
    # text re-indexes a row.
    f"""CREATE TRIGGER IF NOT EXISTS job_search_au AFTER UPDATE ON seen_jobs
        WHEN {_changed(('id',) + _INDEXED)}
        BEGIN
            DELETE FROM {FTS_TABLE} WHERE rowid IN
                (SELECT docid FROM {IDS_TABLE} WHERE job_id = old.id);
            DELETE FROM {IDS_TABLE} WHERE job_id = old.id AND old.id IS NOT new.id;
            {_refresh_sql('new.id')}
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS job_search_ad AFTER DELETE ON seen_jobs
        BEGIN
            DELETE FROM {FTS_TABLE} WHERE rowid IN
                (SELECT docid FROM {IDS_TABLE} WHERE job_id = old.id);
            DELETE FROM {IDS_TABLE} WHERE job_id = old.id;
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS job_search_dai AFTER INSERT ON job_descriptions
        BEGIN {_refresh_sql('new.job_id')} END""",
    f"""CREATE TRIGGER IF NOT EXISTS job_search_dau AFTER UPDATE ON job_descriptions
        WHEN {_changed(('job_id', 'description'))}
        BEGIN {_refresh_sql('old.job_id')} {_refresh_sql('new.job_id')} END""",
    f"""CREATE TRIGGER IF NOT EXISTS job_search_dad AFTER DELETE ON job_descriptions
        BEGIN {_refresh_sql('old.job_id')} END""",
]


_SUPPORTED: bool | None = None


def supported(conn: sqlite3.Connection) -> bool:
    """FTS5 with contentless_delete needs SQLite 3.43. An older build gets no
    index and keeps the LIKE fallback rather than failing to open the DB.
    The answer is the linked library's, so it is computed once per process."""
    global _SUPPORTED
    if _SUPPORTED is None:
        _SUPPORTED = sqlite3.sqlite_version_info >= (3, 43, 0) and _probe(conn)
    return _SUPPORTED


def _probe(conn) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.Error:
        return False


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the index and its triggers if this SQLite can. Cheap and
    idempotent; called from JobDB._init. Returns whether the index exists."""
    if not supported(conn):
        return False
    for stmt in SCHEMA:
        conn.execute(stmt)
    return True


def is_built(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (BUILT_META_KEY,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


# ── Query parsing ────────────────────────────────────────────────────────────

_TERM_RE = re.compile(r'(-?)"([^"]*)"?|(\S+)')
_WORD_RE = re.compile(r"\w", re.UNICODE)


def parse(q: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """(include, exclude) term lists of (text, is_phrase). Quotes pair up; an
    unclosed quote runs to the end; a term with no letter or digit is dropped
    (it would be an empty FTS5 phrase)."""
    include, exclude = [], []
    for m in _TERM_RE.finditer(str(q or "")[:MAX_QUERY]):
        if m.group(3) is not None:
            raw = m.group(3)
            neg = raw.startswith("-") and len(raw) > 1
            text, phrase = (raw[1:] if neg else raw), False
        else:
            neg, text, phrase = bool(m.group(1)), m.group(2), True
        text = " ".join(text.split())
        if not _WORD_RE.search(text):
            continue
        (exclude if neg else include).append((text, phrase))
    return include[:MAX_TERMS], exclude[:MAX_TERMS]


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _fold(text: str) -> str:
    """Case and diacritics folded the way the index's tokenizer folds them,
    so "munchen" typed without the umlaut still finds the alias group."""
    t = unicodedata.normalize("NFKD", " ".join(str(text).split()).casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def _city_groups() -> dict[str, tuple[str, ...]]:
    """folded spelling -> every spelling of that city, built from
    tag._CITY_CANON (the one definition of the aliases)."""
    from jobfeed.tag import _CITY_CANON
    groups: dict[str, set[str]] = {}
    for alias, canon in _CITY_CANON.items():
        groups.setdefault(canon.lower(), {canon.lower()}).add(alias.lower())
    out = {}
    for spellings in groups.values():
        for s in spellings:
            out[_fold(s)] = tuple(sorted(spellings))
    return out


def city_spellings(text: str) -> tuple[str, ...]:
    """Every spelling of `text` when it names a city with known aliases."""
    return _city_groups().get(_fold(text), ())


def _fts_term(text: str, phrase: bool) -> str:
    spellings = city_spellings(text)
    if spellings:
        return "(" + " OR ".join(_quote(s) for s in spellings) + ")"
    if phrase:
        return _quote(text)
    return _quote(text) + "*"


def match_expressions(q: str) -> tuple[str, str]:
    """(include MATCH, exclude MATCH); either may be empty."""
    include, exclude = parse(q)
    inc = " AND ".join(_fts_term(t, p) for t, p in include)
    exc = " OR ".join(_fts_term(t, p) for t, p in exclude)
    return inc, exc


def fts_clauses(q: str) -> tuple[list[str], list]:
    """SQL clauses over seen_jobs (column `id`) for the index path."""
    inc, exc = match_expressions(q)
    clauses, params = [], []
    sub = (f"id IN (SELECT m.job_id FROM {IDS_TABLE} m WHERE m.docid IN "
           f"(SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?))")
    if inc:
        clauses.append(sub)
        params.append(inc)
    if exc:
        clauses.append("NOT " + sub)
        params.append(exc)
    if not clauses and str(q or "").strip():
        clauses.append("0")  # only punctuation: match nothing, never everything
    return clauses, params


_LIKE_COLS = ("title", "company", "location", "loc_city")


def _like_term(text: str) -> tuple[str, list]:
    spellings = city_spellings(text) or (text,)
    ors, params = [], []
    for s in spellings:
        esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for col in _LIKE_COLS:
            ors.append(f"{col} LIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")
    return "(" + " OR ".join(ors) + ")", params


def like_clauses(q: str) -> tuple[list[str], list]:
    """The fallback before the index is built: same syntax, no description
    text, case-insensitive for ASCII only (LIKE), aliases still expanded."""
    include, exclude = parse(q)
    clauses, params = [], []
    for text, _ in include:
        c, p = _like_term(text)
        clauses.append(c)
        params += p
    for text, _ in exclude:
        c, p = _like_term(text)
        clauses.append("NOT " + c)
        params += p
    if not clauses and str(q or "").strip():
        clauses.append("0")
    return clauses, params


# ── One-off backfill ─────────────────────────────────────────────────────────

def backfill(conn: sqlite3.Connection, *, batch: int = 500, pause: float = 0.05,
             log=print) -> int:
    """Index every row the triggers have not, in short write transactions.

    Safe on the live WAL database: each batch takes the write lock for a few
    milliseconds, readers never block, and a scan writing at the same time is
    indexed by the triggers (a row it touches first is skipped here, because
    the batch only takes rows absent from job_search_ids, inside the same
    transaction). Resumable: an interrupted run continues where it stopped."""
    if not ensure_schema(conn):
        raise RuntimeError(f"this SQLite ({sqlite3.sqlite_version}) has no "
                           "FTS5 contentless_delete; needs 3.43 or newer")
    conn.commit()
    done = 0
    started = time.monotonic()
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM seen_jobs WHERE id NOT IN (SELECT job_id FROM {IDS_TABLE}) "
                "LIMIT ?", (batch,))]
            if ids:
                ph = ",".join("?" * len(ids))
                conn.execute(f"INSERT OR IGNORE INTO {IDS_TABLE}(job_id) "
                             f"SELECT id FROM seen_jobs WHERE id IN ({ph})", ids)
                conn.execute(
                    f"INSERT INTO {FTS_TABLE}(rowid, title, company, location, loc_city, description) "
                    "SELECT m.docid, s.title, s.company, s.location, s.loc_city, d.description "
                    f"FROM {IDS_TABLE} m JOIN seen_jobs s ON s.id = m.job_id "
                    "LEFT JOIN job_descriptions d ON d.job_id = s.id "
                    f"WHERE m.job_id IN ({ph})", ids)
            else:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                             (BUILT_META_KEY, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not ids:
            break
        done += len(ids)
        if log and done % (batch * 20) == 0:
            log(f"indexed {done} rows ({time.monotonic() - started:.0f}s)")
        if pause:
            time.sleep(pause)
    conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('optimize')")
    conn.commit()
    if log:
        log(f"done: {done} rows indexed in {time.monotonic() - started:.1f}s")
    return done


def main(argv=None) -> int:
    from jobfeed.db import JobDB
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", action="store_true",
                    help="index every row not yet indexed, then mark the index built")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args(argv)
    if not args.build:
        ap.error("--build is the only mode")
    db = JobDB(args.db)  # runs the schema setup, refuses a pre-split DB
    db.conn.isolation_level = None  # explicit BEGIN/COMMIT below
    try:
        backfill(db.conn, batch=args.batch)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
