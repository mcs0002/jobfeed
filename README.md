# Job Scraper

A self-hosted job-market scanner for finance roles. It reads 400+ company
career boards directly at the ATS level (Workday, Greenhouse, Oracle HCM,
SuccessFactors, Avature, SmartRecruiters, …), stores every role in SQLite,
tags each one with structured facets via Claude, and serves a filterable web
app with application tracking.

Why direct ATS reads instead of an aggregator: postings show up the day they
go live (aggregators lag days or miss boards entirely), coverage is exactly
the firms you choose, and nothing is ranked or hidden by someone else's
algorithm.

I built this to run my own graduate job search in European finance. It is
generalized enough to point at any set of firms — the per-ATS handlers don't
care what industry the board belongs to.

## How it works

One `main.py` run does four sequential stages per scan:

1. **Scrape** — per-ATS handlers in `scrapers/` (~90 of them) pull each board
   configured in `targets.json`. Heavy/slow boards run as killable
   subprocesses (`heavy_scrape.py`).
2. **Filter** (`filter.py`) — negative-only: drops unambiguous noise
   (back-office locations, senior rungs, years-of-experience walls,
   pre-degree programmes) and keeps everything else. The design principle is
   **store broad, filter in the UI**: preferences are reversible toggles on
   the site, not destructive scrape-time drops, so widening the net never
   silently loses a role.
3. **Enrich** (`scrapers/enrich/`) — many ATSes omit the description from the
   listing payload, so a per-ATS enricher fetches the real body. Without it
   the tagger sees only title+company+location and mis-tags.
4. **Tag** (`tag.py`) — a structured Claude Haiku pass writes the
   `area / desk / seniority / job_type / location / work_mode` facets the web
   app filters on, plus description-derived ones (language requirements,
   education floor, minimum experience, start date). It feeds the model a
   targeted excerpt — the posting's opening plus its requirements section —
   rather than a blind first-N-chars window.

`web/app.py` (FastAPI + Jinja2 + HTMX) reads the same `jobs.db` and is the
browse / filter / apply-track surface, plus a `/api/jobs` JSON endpoint.

Everything is designed to **fail loud**: a scraper that hits an anomaly
raises instead of returning an empty list, because downstream logic reads
"empty" as "board has no openings". A weekly `selfcheck.py` catches the quiet
failure modes — silent collapses, dead apply links, stub descriptions,
tagging regressions — and the Sources tab shows broken boards the same day.
For unattended operation, the scheduled run emails on every hard failure
(scan abort, git divergence, tagger auth death — see `notify.py`), and the
web app shows a banner if no scan has touched the DB for two days, so a
silently stopped schedule can't rot unnoticed.

## Quickstart

Requires Python 3.10+ (developed on 3.12) and [uv](https://docs.astral.sh/uv/).
Tagging requires the [Claude Code CLI](https://claude.com/claude-code)
installed and authenticated — it runs on your existing Claude subscription,
no API key needed. Optionally set `ANTHROPIC_TAG_API_KEY` in `.env` as a
fallback: if the CLI's auth expires mid-run, tagging switches to the Messages
API instead of storing blank tags. Skip tagging entirely with `--no-tag`.

```bash
git clone <this repo> && cd job_scraper
uv venv --python 3.12
uv pip sync requirements.lock
cp .env.example .env

.venv/bin/python main.py --verify    # test the configured sources
.venv/bin/python main.py             # scan: scrape, enrich, tag, store
```

Useful flags: `--all` (report every live match, not just unseen), `--dry-run`
(no DB writes), `--no-tag` (skip the Claude pass), `--workers N` (concurrent
company scrapes, default 6).

Then start the web app and open http://localhost:8000:

```bash
.venv/bin/uvicorn web.app:app
```

Set `WEB_PASSWORD` in `.env` (or `WEB_ALLOW_NO_AUTH=1` for localhost-only
use). Setting `WEB_GUEST_PASSWORD` adds a second, read-only login — guests
can browse and filter but every mutating action (favorites, statuses, notes)
is rejected, and their UI state lives in their own browser, so sharing the
site never disturbs your data. For an always-on setup, run `main.py` from
cron/launchd on whatever schedule you like and put the web app behind your
tunnel of choice — `setup_web.sh` shows a complete launchd example.

## Configuring firms

`targets.json` is a list of boards; the shipped file is my curated set of
~450 finance employers (banks, asset managers, hedge funds, consultancies,
insurers) and works out of the box. One entry:

```json
{
  "name": "J.P. Morgan",
  "category": "Global Investment Banks",
  "ats": "oracle_hcm",
  "oracle_hcm": { "base_url": "https://jpmc.fa.oraclecloud.com", "site": "CX_1001" },
  "verified": true
}
```

To add a firm: find which ATS its careers page runs on (the URL usually gives
it away — `myworkdayjobs.com`, `boards.greenhouse.io`, `*.fa.oraclecloud.com`,
…), add an entry with that handler's config block, and run
`main.py --verify` to confirm it resolves. Every ATS must also declare a
description strategy in `scrapers/enrich/coverage.py` — a new source without
one fails the test suite, which is the guard that keeps the tagger from
silently working on stub descriptions.

The tagging taxonomy (what counts as markets vs. asset-management vs. risk,
etc.) lives as a prompt in `tag.py` and is finance-specific; repointing the
project at another industry means rewriting that prompt, and nothing else.

## Scope and conduct

The scrapers read the same public, unauthenticated endpoints the firms' own
career pages call — no logins, no paywalls, and volumes far below what a
single human clicking through the site would generate (most boards are one
JSON request per scan). Postings are facts published to be read. Keep it
that way if you fork this: personal use, polite volumes, public data. See
`SCRAPER_RESILIENCE.md` for a longer discussion of the legal and durability
picture.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests
```

## Docs

The sections above cover the architecture; these files go deeper on the
operational corners:

- `SCRAPER_RESILIENCE.md` — what breaks, why, at what rate; 12-month
  durability analysis.
- `SOURCE_HEALTH.md` — how source health is monitored; repair history with
  lessons embedded.
- `MANUAL_WORKLIST.md` — everything the scan currently skips and why.
- `BLOCKED.md` — probe-before-you-mark-manual checklist.
- `GRAD_SCHEMES.md` — graduate-programme source hints.

## License

MIT — see `LICENSE`.
