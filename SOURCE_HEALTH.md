# Source health — doctrine & history

Slimmed 2026-07-06. This file used to carry a ranked worklist of every
not-working source; that duplicated (and lost to) **[MANUAL_WORKLIST.md](MANUAL_WORKLIST.md)**,
which is now the single list of everything the scan skips and why. This file
keeps the two things only it does well: **how health is kept** (the monitor
stack) and the **Done log** (the repair history, with the lessons embedded).

## How health is kept

**Primary signal: the `/sources` "stalled / broken" indicator.** Every scan
writes the `failing`/`degraded` sets to `verify_state.json`
(`main._write_health_state`); the Sources tab reads them, so a broken source
shows red the same day. Pull-based, no email needed.

**Weekly backstop: `selfcheck.py`** (M1 launchd `com.example.jobscan-healthcheck`,
Sundays 03:00, `SELFCHECK_EMAIL=1` so alerts also mail). It runs
`main.py --verify --verified-only` and catches, transition-alerted (a source
alerts once on breaking, then stays quiet until it recovers and breaks again):

1. **Hard fail** — the scraper raised (bad slug, 4xx/5xx, parse error).
2. **Silent empty / collapse** — succeeded but returned 0, or dropped below
   20% of its rolling baseline (`COLLAPSE_RATIO`); genuinely-tiny boards never
   build a baseline, so they never nag.
3. **Never produced** — verified but 0 with no baseline for 2+ consecutive
   checks (the dead-slug-from-day-one class that hid E.ON).
4. **Stub descriptions** — `description_health.py`: per-ATS stub/null rates in
   the live DB (the TAL.net class: healthy counts, garbage bodies, blind tagger).
5. **Other-heavy tagging** — `tag_health.py`: a company with ≥5 tagged live
   rows and ≥80% `area='other'` is invisible in the UI (the PIC class; also
   catches Haiku regressions after prompt changes).
6. **Dead apply links** — `link_health.py`: samples 2 random live URLs per
   company; flags only when ALL samples 404/410 across 2 consecutive runs
   (the stale-scrape class: healthy counts, dead "Open posting" buttons).

Static guards outside the weekly run: **fail-loud doctrine** (scrapers raise
on anomalies, never return `[]` — the delist/purge reads empty as "board
empty"), and **`tests/test_enrich_coverage.py`** (every ATS must declare a
description strategy in `scrapers/enrich/coverage.py`).

A firm that's *correctly scraped but sparse* is **healthy**, not a task. Only
regressions and genuine misconfigurations are work. The 12-month durability
analysis lives in [SCRAPER_RESILIENCE.md](SCRAPER_RESILIENCE.md).

## What happens when a source breaks (end-to-end)

The timeline from break to fix, in the order the system reacts (updated
2026-07-09 after the hardening pass).

**Night 0 — the 04:00 nightly scan.** A broken scraper *raises* (fail-loud
doctrine; partial pagination is caught by `_http.assert_complete`'s 0.9
completeness band on scrapers with a server-reported total). `scrape_company`
catches the exception and returns it as an error, which has one crucial
effect: the firm is **excluded from the delist pass** — its stored rows are
untouched, worst case they go stale. If a scraper instead *silently* returns
too few rows, three layers stand in the way: the completeness band (raises in
the scraper), the degraded-source guard (count ≤20% of rolling baseline →
excluded from delisting that run), and the **3-day delist→purge grace period**
(nothing is hard-deleted the night it goes missing; a recovered scraper
self-heals because `touch_seen` clears `delisted_at` on reappearance).
Favorites, roles with any status history, and internships are never purged at
all. No email fires on night 0 — transient flakes are common and self-resolve;
the error lands in `verify_state.json`'s `failing` set, so `/sources` shows
the firm as stalled the same day.

**Sunday 03:00 — selfcheck.** Re-scrapes every verified source (3h subprocess
timeout; the verify path hard-exits past wedged scraper threads, so the
monitor itself can't hang) and emails on **transitions** via `notify.py`
(env-driven SMTP: `NOTIFY_EMAIL`/`SMTP_HOST`/`LEGACY_MAIL_PASSWORD` in the M1
`.env`). The six monitor classes above ride this run. Two backstops added
2026-07-09: a **crashed/timed-out verify run itself sends an alert** (it used
to die silently), and a **DB freshness check** alerts when `MAX(last_seen)` is
older than 2 days — i.e. the nightly scan has been dying every night even
though every source verifies fine (the diverged-git failure mode).

**Debugging when the alert arrives.** `healthcheck.log` (Sunday) or
`deliver.log` (nightly) on the M1 has the run output. Reproduce with a one-off
`main.scrape_company(target)` on either machine, then work
[BLOCKED.md](BLOCKED.md): re-probe for the JSON endpoint the SPA calls, try
browser headers, then `curl_cffi`; classify the real failure mode. Most breaks
are a renamed payload field or moved endpoint — a 10-minute config fix. Push
to main → live at the next 04:00 pull. `verify_state.json` clears the entry on
the next clean run; `/sources` un-stalls.

**Nightly alerting (added 2026-07-16, closing the old "no per-night email"
gap):** the scheduled run now emails immediately on a failed/wedged scan, a
diverged git pull, and a dead tagger CLI (`deliver.sh alert()` + the
`cli_down`/`api_fallback` checks in `main.py`); the tagger also falls back to
the Messages API (`ANTHROPIC_TAG_API_KEY`) instead of blanking tags when
OAuth dies; and the browse page shows a staleness banner when
`MAX(last_seen)` is 2+ days old, so a silently stopped schedule is visible on
the surface that gets daily eyeballs.

**Known gaps (accepted for now):** per-firm *source* errors still surface only
via the `/sources` badge until Sunday (the nightly email covers run-level
failures, not individual source breaks — the scan prints only the error
*count*; check `verify_state.json` `failing` for names); selfcheck (Sun 03:00)
and the nightly (04:00) share no lock and can overlap; the monthly
`notify.py --test` heartbeat isn't scheduled.

## Known failure signatures

- **Oleeo Protect (tal.net)**: an ALTCHA proof-of-work anti-bot trips on request
  *volume* and gates the IP for ALL tal.net tenants on that scan (Evercore,
  L.E.K., Fidelity, Schroders). A tal.net source collapsing to 0 with title
  "Quick Check Needed" is Oleeo Protect, not a dead board. Mitigation:
  `talnet_fetch_detail:false` (listing-only, 1 request) — set on Evercore.
- **Koch S&T token expiry**: `koch_avature` fails loud (`KOCH_COOKIES_EXPIRED`
  → scan error) when its WAF token staled. The token is egress-IP-bound (Air
  and M1 share the home NAT, so an Air capture works on the M1) and lives
  weeks, not days. Capture + deploy steps: [KOCH_CAPTURE.md](KOCH_CAPTURE.md).
- **Greenhouse `absolute_url` rot**: a firm-site redesign kills the deep-link
  path while the board API stays healthy (boards.greenhouse.io redirects into
  the same dead path). link_health catches it; fix with a per-target
  `url_template` in targets.json (precedent: Mako, GSA Capital).
- **A configured slug that resolves but returns few is NOT proof of sparseness**
  — the real board can be a different token entirely (HRT was under `wehrtyou`,
  not `hrttalentcommunity`). Re-probe the careers page for the embed token
  before concluding "sparse". Probe checklist: [BLOCKED.md](BLOCKED.md).

---

## Done

- **2026-07-09 (Carlyle + Hauck Aufhäuser Lampe FIXED — both a same-side-migration
  and a stale-doctrine bug)** — **Carlyle Group**: the self-hosted Avature portal
  moved; `externalcareers/SearchJobs/` 404s and the bare `carlyle.avature.net`
  root now bounces (via a UA-version gate — confirmed a real fingerprint check,
  not a fluke, by getting through with `curl_cffi impersonate=chrome146` but not
  `chrome124`) to an internal `/Login/` recruiter portal, not the external
  jobs page. `www.carlyle.com/careers` (fetched with curl_cffi chrome
  impersonation past its 403) links a `carlyle.wd1.myworkdayjobs.com/Carlyle`
  Workday tenant — Carlyle switched ATS entirely. Rewired `ats` from
  `"avature"` to `"workday"` (`tenant: carlyle, version: wd1, board: Carlyle`).
  0 → 87 roles. **Hauck Aufhäuser Lampe** (`getnoticed` handler, shared
  `karriere.abnamro.de` tenant with ABN AMRO DE post-merger): the endpoint
  itself was never dead — `api/vacancy/` 404s a bare `requests` UA and 200s a
  real browser UA/Accept/Referer/X-Requested-With, the same bot-filter
  signature already documented for `scrapers/abnamro.py`'s sister endpoint.
  The old "stateful pagination not HTTP-reachable" note in `getnoticed.py` was
  simply wrong — `?pageNumber=N` walks the full board same as ABN AMRO's own
  `pageNumber` handler (verified pages 2 and 3 return distinct vacancies).
  Added browser headers + `pageNumber` pagination loop to `meta.totalPageCount`
  + `assert_complete`. 10 (capped page 1) → 21 (full board).
- **2026-07-06 (monitor stack completed + first catches)** — Added
  `tag_health.py` (other-heavy companies) and `link_health.py` (sampled dead
  apply links), both wired into selfcheck. link_health's first run caught
  **Mako + GSA Capital** (Greenhouse absolute_url → redesign-removed firm
  paths) → new `url_template` override, rows rewritten. tag_health surfaced a
  40-firm other-heavy backlog; Opus triage found **39/40 correct-by-design**
  (ops-dominated boards) and one real flaw class — **physical/energy commodity
  traders & originators leaking to `other`** — fixed in the tag.py prompt
  (markets definition + energy/commodity sector hint), 5 rows re-tagged to
  markets (ADM Farm Trader, Statkraft originators, Axpo origination).
- **2026-07-05/06 (the big manual→scraped flip)** — ~35 firms flipped, ~18 new
  handlers; Buckets A/B/D cleared; Playwright verdict settled (only McKinsey +
  Marubeni would need a browser; declined). Bucket B worktree probe flipped 10
  of 17 "browser-only" firms to plain HTTP. `successfactors_dwr` handler
  cracked the Pictet/Sumitomo-EMEA DWR RPC (JS-shell ≠ needs a browser). E.ON
  rewired off a dead SmartRecruiters slug; never-produced guard added for that
  blind-spot class. Full detail: [MANUAL_WORKLIST.md](MANUAL_WORKLIST.md).
- **2026-07-02 (Janus Henderson + Fortum WIRED — wrong handler, not blocked)** —
  The "CAS-auth, no plain-HTTP path" verdict was a handler mistake: the modern
  RMK JSON API is CAS-gated on both, but the classic server-rendered HTML front
  (`successfactors.py`) is fully public. Janus Henderson 80, Fortum 28. Lesson:
  for a CAS-gated SF tenant, try the HTML handler before declaring it blocked.
- **2026-07-02 (UniCredit CIB FIXED — Avature concurrent-walk drop)** — server
  drops connections under 6-worker pagination; avature.py now retries a dropped
  page sequentially + 0.9 completeness band. 0 → 712 roles.
- **2026-07-02 (Brevan Howard WIRED)** — the 06-29 "401 unfixable" note went
  stale: the Workday cxs endpoint re-opened. 11 roles. **KKR re-probed same
  day: still 403 S22 and hardened — browser-only, stays manual.**
- **2026-07-01 (Tier 0b cleanup — 11 verified, 5 moved to manual)** — Live-tested
  all 16 `verified:false` sources. Real bug found: `boards-api.eu.greenhouse.io`
  doesn't resolve; EU boards (EQT, Permira) serve fine from the standard host —
  greenhouse.py now always uses it. 9 more flipped verified:true (Apax, Nordic
  Capital, Bridgepoint, Evercore, Pictet AM, Sumitomo EMEA, E.ON, Orsted,
  BayernLB). First live run of the new delisting logic: 4,201 delisted, 210
  other-purged.
- **2026-06-29 (anti-bot tier sweep — agent fan-out)** — 8 sources flipped
  manual → live (~221 jobs): Kearney (`recsolu.py`), Tikehau (`talentview.py`),
  OC&C (`eploy.py`), Itochu (`adp_careercenter.py`), L.E.K., Evercore, Rokos
  (`kernel.py`, year-proof slug discovery), NIB (`reachmee.py`). Key facts:
  curl_cffi 0.15.0 IS on the M1 (older notes were stale); curl_cffi does NOT
  beat JS-challenge WAFs or the Workday-422 wall.
- **2026-06-29 (coverage expansion — 51 new targets)** — PE/boutique-IB/AM/
  energy/MDB sweep, 42 wired + 8 manual + 1 unknown. Greenhouse EU flag added.
- **2026-06-29 (reconcile + misc)** — SOURCE_HEALTH diffed against targets.json;
  phantoms removed (Man Numeric, Nordea AM, Clearstream, EY-Parthenon → covered
  by parents, noted on the parent's ⓘ); Linde removed (never a target);
  Simon-Kucher wired (csod, ~127 — "unsupported platform" note was stale);
  Mercuria wired (`wp_job` via `_http.curl_get`, AIA-completes its broken TLS
  chain); Koch re-enabled + root-caused (see failure signatures); workday
  markets banks widened to full-board (BofA ~1655, CIBC ~555, RBC ~1518 heavy);
  Deloitte → heavy (147s walk).
- **2026-06-29 (Tier 0/1/2 agent sweep)** — 12 sources wired: Booz Allen, Bain
  (avature FolderDetail), KPMG US (`kpmg_us.py`), Bloomberg, FTI, Quantlab
  (`jobvite.py`), Scotiabank GBM, BCG (phenom_widgets), Deloitte, KBC,
  Partners Group (`successfactors_classic.py`), Erste (`erste_btp.py`).
- **2026-06-28 (health-alert pass)** — notify.py reads `LEGACY_MAIL_PASSWORD` from
  env/.env before the Keychain (M1 launchd Keychain is locked at scan time);
  ABN AMRO recovered; stale verify_state reset.
- **2026-06-28 (Tier X cleared)** — Natixis CIB via new `bpce` scraper (~260);
  Commerzbank via BeeSite REST base_url (~310); Castleton duplicate deleted.
- **2026-06-28 (fixes)** — ING full-board (793); UniCredit heavy; selfcheck
  made count-aware. Altman Solon + HRT re-slugged (embed-token re-probe
  lesson). DNB Markets (SF `locale=nb_NO`), Arthur D. Little (new `icims`),
  ABN AMRO (browser headers), BIS (new JSON API), Handelsbanken Group (new
  `jobylon`). ADB removed (no vacancies since Dec 2024). Mizuho APAC → manual
  (Lumesse AWS-WAF). Marshall Wace → manual + [GRAD_SCHEMES.md](GRAD_SCHEMES.md).
  ExodusPoint confirmed genuinely ~2 public roles.
- **2026-06-28 (plan phases 3.4-4.8)** — Sources page splits scope vs execution;
  Deutsche Bank (Global Markets) removed (97% duplicate of the broad DB board);
  manual tab retired into /sources ⓘ; BLOCKED.md re-derived as doctrine;
  heavy-flag audit (only JPM + BNP need it, both have it).

See also [BLOCKED.md](BLOCKED.md) (probe-before-manual checklist) and
[MANUAL_WORKLIST.md](MANUAL_WORKLIST.md) (the single not-working list).
