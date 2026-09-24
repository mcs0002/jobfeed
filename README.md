# Job Scraper

A self-hosted job-market scanner for finance roles. It reads 400+ company
career boards directly at the ATS level (Workday, Greenhouse, Oracle HCM,
SuccessFactors, Avature, SmartRecruiters, …), stores every role in SQLite,
tags each one with structured facets through a configured LLM API, and serves a filterable web
app with application tracking.

Why direct ATS reads instead of an aggregator: postings show up the day they
go live (aggregators lag days or miss boards entirely), coverage is exactly
the firms you choose, and nothing is ranked or hidden by someone else's
algorithm.

I built this to run my own graduate job search in European finance. It is
generalized enough to point at any set of firms — the per-ATS handlers don't
care what industry the board belongs to.

## How it works

One scan (`python -m jobfeed`) runs four sequential stages:

1. **Scrape** — per-ATS handlers in `scrapers/` (~90 of them) pull each board
   configured in `targets.json`. Heavy/slow boards run as killable
   subprocesses (`jobfeed/heavy_scrape.py`).
2. **Filter** (`jobfeed/filter.py`) — negative-only: drops unambiguous noise
   (back-office locations, senior rungs, years-of-experience walls,
   pre-degree programmes) and keeps everything else. The design principle is
   **store broad, filter in the UI**: preferences are reversible toggles on
   the site, not destructive scrape-time drops, so widening the net never
   silently loses a role.
3. **Enrich** (`scrapers/enrich/`) — many ATSes omit the description from the
   listing payload, so a per-ATS enricher fetches the real body. Without it
   the tagger sees only title+company+location and mis-tags.
4. **Tag** (`jobfeed/tag.py`) — a structured model pass writes the
   `area / desk / seniority / job_type / location / work_mode` facets the web
   app filters on, plus description-derived ones (language requirements,
   education floor, minimum experience, start date). It feeds the model a
   targeted excerpt — the posting's opening plus its requirements section —
   rather than a blind first-N-chars window.

`web/app.py` (FastAPI + Jinja2 + HTMX) reads the same `jobs.db` and is the
browse / filter / apply-track surface, plus a `/api/jobs` JSON endpoint. Its
Technical stats page reports the latest raw-board → pre-filter acquisition funnel,
source completeness, and tagger coverage, cache use, cost, fallbacks, and
latency. Review turns human confirmations/corrections into a
frozen evaluation set keyed by provider, model, and prompt-rubric hash, with
special metrics for false `manager` and finance-as-`other` labels because both
mistakes hide otherwise relevant jobs.

Everything is designed to **fail loud**: a scraper that hits an anomaly
raises instead of returning an empty list, because downstream logic reads
"empty" as "board has no openings". A weekly `jobfeed/selfcheck.py` catches the quiet
failure modes — silent collapses, dead apply links, stub descriptions,
tagging regressions — and the Sources tab shows broken boards the same day.
For unattended operation, the scheduled run emails on every hard failure
(scan abort, git divergence, tagger auth death — see `jobfeed/notify.py`), and the
web app shows a banner if no scan has touched the DB for two days, so a
silently stopped schedule can't rot unnoticed.

## Repository layout

| Path | What it holds |
| --- | --- |
| `jobfeed/` | the scan: entry point (`python -m jobfeed`), SQLite layer, filter, tagger, health monitors |
| `scrapers/` | one handler per ATS; `scrapers/enrich/` fetches full descriptions |
| `web/` | FastAPI + HTMX app: Browse, applications pipeline, stats, sources |
| `applications/` | optional application tracking and attended filling (see below) |
| `bin/` | stable command-line entry points for the application layer |
| `ops/` | scheduled-run script, launchd setup, macOS helper source |
| `scripts/` | maintenance and evaluation tools (tagger gold set, cost, audits) |
| `tests/`, `evals/`, `docs/` | test suite, tagger evaluation set, operational notes |
| `targets.json` | the configured firms and their ATS boards |

## Quickstart

Requires Python 3.10+ (developed on 3.12) and [uv](https://docs.astral.sh/uv/).
Tagging can use either any OpenAI-compatible `/v1/chat/completions` API or the
authenticated Claude CLI. The production pattern is API-primary: configure
`TAG_PROVIDER=api` plus `TAG_API_BASE_URL`, `TAG_API_MODEL`, and `TAG_API_KEY`
in `.env`; the CLI is optional and is touched only if that API fails. An
Anthropic Messages API key can remain as a final fallback. Skip tagging
entirely with `--no-tag`.

```bash
git clone https://github.com/mcs0002/jobfeed.git
cd jobfeed
uv venv --python 3.12
uv pip sync requirements.lock
cp .env.example .env

.venv/bin/python -m jobfeed --verify --company "Qube Research & Technologies"  # fast smoke test
.venv/bin/python -m jobfeed --verify    # optional full 400+ source sweep; long-running
.venv/bin/python -m jobfeed --no-tag    # keyless first scan: scrape, enrich, store
# After configuring TAG_PROVIDER/API settings in .env:
.venv/bin/python -m jobfeed             # scrape, enrich, tag, store
```

Useful flags: `--all` (report every live match, not just unseen), `--dry-run`
(no DB writes), `--no-tag` (skip the tagging pass), `--workers N` (concurrent
company scrapes, default 6), `--company "Exact Name"` (restrict a scan or
verification; repeatable). The full verification runs the heavy sources (one at a time) alongside the
light pool, so it can run for many minutes and may print nothing between its
opening line and the final per-source report.

The repository intentionally contains no `jobs.db`: a new installation starts
empty and builds its own current dataset. It can reproduce the scraper's
coverage and tagging process, not the maintainer's historical application data.
Most configured boards need no credentials. Koch Supply & Trading is the one
exception: its AWS WAF requires a short-lived browser cookie; either follow
[`docs/KOCH_CAPTURE.md`](docs/KOCH_CAPTURE.md) or accept one explicit Koch error while the
rest of the scan continues normally.

Playwright's Python package is installed from the lock. Its Chromium binary is
only needed if a Workday board returns HTTP 422 and activates the browser
fallback; install it once with:

```bash
.venv/bin/playwright install chromium
```

Then start the web app and open http://localhost:8000:

```bash
.venv/bin/uvicorn web.app:app
```

Set `WEB_PASSWORD` in `.env` (or `WEB_ALLOW_NO_AUTH=1` for localhost-only
use). Setting `WEB_GUEST_PASSWORD` adds a second, read-only login. Guests can
use only Browse, Technical stats and Sources; all other routes are denied by a
server-side allowlist, so new pages stay owner-only until explicitly admitted.
Browse omits owner status, favorites and unread/read state, and every mutating
action is rejected. For an always-on setup, run `python -m jobfeed` from
cron/launchd on whatever schedule you like and put the web app behind your
tunnel of choice — `ops/setup_web.sh` shows a complete launchd example.

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
`python -m jobfeed --verify` to confirm it resolves. Every ATS must also declare a
description strategy in `scrapers/enrich/coverage.py` — a new source without
one fails the test suite, which is the guard that keeps the tagger from
silently working on stub descriptions.

The tagging taxonomy (what counts as markets vs. asset-management vs. risk,
etc.) lives as a prompt in `jobfeed/tag.py` and is finance-specific; repointing the
project at another industry means rewriting that prompt, and nothing else.

## Scope and conduct

The scrapers read the same public, unauthenticated endpoints the firms' own
career pages call — no logins, no paywalls, and volumes far below what a
single human clicking through the site would generate (most boards are one
JSON request per scan). Postings are facts published to be read. Keep it
that way if you fork this: personal use, polite volumes, public data. See
`docs/SCRAPER_RESILIENCE.md` for a longer discussion of the legal and durability
picture.

## Application tracking and attended applications

The web app is also the tracking surface for applications, and a second,
optional layer helps fill them. It is built around one boundary: **software
prepares, the human submits.** Nothing in this repository presses a final
submit button.

- **Status from the inbox** (`applications/mail.py`) moves a role's status from
  confirmation and rejection mails. It matches only against roles already acted
  on, records nothing unless exactly one candidate survives, and stores the
  quoted sentence behind each transition.
- **Application caps** (`applications/limits.py`) record per-firm limits ("at
  most N applications per cycle") before the first form goes out, because the
  choice of where to spend them cannot be undone.
- **Attended filling** (`applications/autopilot.py`, `applications/launcher.py`, `ops/macos/`)
  hands one posting at a time to a browser agent that fills the form up to its
  review page and stops. A serial queue allows exactly one live agent and
  advances only on positive evidence that the previous one has stopped.
- **Run telemetry** (`applications/runs.py`, `scripts/agaudit.py`) reads the
  agent's own trajectory records to report steps, model calls, permission
  prompts and rule breaks per run, so every filled form can be audited.

The scanner, tagger and Browse UI work without any of this. The layer needs a
local applicant profile (`secrets/applicant_profile.example.json` shows the
schema) and is macOS-specific.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests
```

## Docs

The sections above cover the architecture; these files go deeper on the
operational corners:

- `docs/SCRAPER_RESILIENCE.md` — what breaks, why, at what rate; 12-month
  durability analysis.
- `docs/SOURCE_HEALTH.md` — how source health is monitored; repair history with
  lessons embedded.
- `jobfeed/manual_check.py` — prints every source the scan skips (`ats: "manual"`)
  with its reason; the live list, also on the Sources page.
- `docs/BLOCKED.md` — probe-before-you-mark-manual checklist.
- `docs/GRAD_SCHEMES.md` — graduate-programme source hints.

## License

MIT — see `LICENSE`.
