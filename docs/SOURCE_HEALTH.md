# Source health — doctrine & history

Slimmed 2026-07-06. This file used to carry a ranked worklist of every
not-working source; that duplicated the manual list. The single list of
everything the scan skips and why is `targets.json` itself (`ats: "manual"` +
`manual_reason`), printed by `jobfeed/manual_check.py` and shown on the Sources page. This file
keeps the two things only it does well: **how health is kept** (the monitor
stack) and the **Done log** (the repair history, with the lessons embedded).

## How health is kept

**Primary signal: the `/sources` "stalled / broken" indicator.** Every scan
writes the `failing`/`degraded` sets to `verify_state.json`
(`main._write_health_state`); the Sources tab reads them, so a broken source
shows red the same day. Pull-based, no email needed.

**Weekly backstop: `jobfeed/selfcheck.py`** (M1 launchd `com.example.jobscan-healthcheck`,
Sundays 03:00, `SELFCHECK_EMAIL=1` so alerts also mail). It runs
`python -m jobfeed --verify --verified-only` and catches, transition-alerted (a source
alerts once on breaking, then stays quiet until it recovers and breaks again):

1. **Hard fail** — the scraper raised (bad slug, 4xx/5xx, parse error).
2. **Silent empty / collapse** — succeeded but returned 0, or dropped below
   20% of its rolling baseline (`COLLAPSE_RATIO`); genuinely-tiny boards never
   build a baseline, so they never nag.
3. **Never produced** — verified but 0 with no baseline for 2+ consecutive
   checks (the dead-slug-from-day-one class that hid E.ON).
4. **Stub descriptions** — `jobfeed/description_health.py`: per-ATS stub/null rates in
   the live DB (the TAL.net class: healthy counts, garbage bodies, blind tagger).
5. **Other-heavy tagging** — `jobfeed/tag_health.py`: a company with ≥5 tagged live
   rows and ≥80% `area='other'` is invisible in the UI (the PIC class; also
   catches tagger regressions after prompt changes).
6. **Dead apply links** — `jobfeed/link_health.py`: samples 2 random live URLs per
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

**Run 0 — the scheduled scan.** A broken scraper *raises* (fail-loud
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
monitor itself can't hang) and emails on **transitions** via `jobfeed/notify.py`
(env-driven SMTP: `NOTIFY_EMAIL`/`SMTP_HOST`/`LEGACY_MAIL_PASSWORD` in the M1
`.env`). The six monitor classes above ride this run. Two backstops added
2026-07-09: a **crashed/timed-out verify run itself sends an alert** (it used
to die silently), and a **DB freshness check** alerts when `MAX(last_seen)` is
older than 2 days — i.e. the nightly scan has been dying every night even
though every source verifies fine (the diverged-git failure mode).

**Debugging when the alert arrives.** `healthcheck.log` (Sunday) or
`deliver.log` (every scheduled scan) on the M1 has the run output. Reproduce with a one-off
`main.scrape_company(target)` on either machine, then work
[BLOCKED.md](BLOCKED.md): re-probe for the JSON endpoint the SPA calls, try
browser headers, then `curl_cffi`; classify the real failure mode. Most breaks
are a renamed payload field or moved endpoint — a 10-minute config fix. Push
to main → live at the next scheduled pull. `verify_state.json` clears the entry on
the next clean run; `/sources` un-stalls.

**Nightly alerting (added 2026-07-16, closing the old "no per-night email"
gap):** the scheduled run now emails immediately on a failed/wedged scan, a
diverged git pull, and a dead tagger CLI (`ops/deliver.sh alert()` + the
`cli_down`/`api_fallback` checks in `jobfeed/main.py`); the tagger also falls back to
the Messages API (`ANTHROPIC_TAG_API_KEY`) instead of blanking tags when
OAuth dies; and the browse page shows a staleness banner when
`MAX(last_seen)` is 2+ days old, so a silently stopped schedule is visible on
the surface that gets daily eyeballs.

**Known gaps (accepted for now):** per-firm *source* errors still surface only
via the `/sources` badge until Sunday (the nightly email covers run-level
failures, not individual source breaks — the scan prints only the error
*count*; check `verify_state.json` `failing` for names); selfcheck (Sun 03:00)
and the nightly scan share no lock (since 2026-09-23 they are two hours apart,
so they only overlap if selfcheck runs long); the monthly
`python -m jobfeed.notify --test` heartbeat isn't scheduled.

## Known failure signatures

- **Oleeo Protect (tal.net)**: an ALTCHA proof-of-work anti-bot trips on request
  *volume* and gates the IP for ALL tal.net tenants on that scan (Evercore,
  L.E.K., Fidelity, Schroders). A tal.net source collapsing to 0 with title
  "Quick Check Needed" is Oleeo Protect, not a dead board. Mitigation:
  `talnet_fetch_detail:false` (listing-only, 1 request) — set on Evercore.
- **Cross-tenant id collision (tal.net, iCIMS)**: vacancy numbers are per
  tenant, so a bare `talnet_<n>` / `icims_<n>` id from one firm can match
  another's stored row and the role is never stored. The scan alerts
  "cross-tenant job id collision". New tenants set `talnet_id_scope` /
  `icims.id_scope` (enforced by `tests/test_talnet.py`); the tenants wired
  before 2026-09-23 stay bare because scoping re-keys their stored rows.
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
- **Tagger OAuth death is a refresh RACE, not an expiry.** The token in
  `~/.claude/.credentials.json` rotates on use. Anything that runs several
  `claude` processes at once against an already-expired access token makes them
  all refresh together, and the losers write back a credential the server has
  retired — so the file holds dead auth until a human runs the CLI by hand.
  Signature: `Failed to authenticate: OAuth session expired and could not be
  refreshed` in `tag_debug.log`, on the CLI's **stdout** with a non-zero exit
  (so `deliver.log` only says "unknown error"), plus stale
  `.credentials.json.dead-*` files next to it. Mitigation is
  `claude_cli.warm_auth` — one serialized refresh before any fan-out. If the
  nightly tagging is silently riding `ANTHROPIC_TAG_API_KEY`, grep deliver.log
  for `switching to direct-API fallback`: that line means the subscription is
  down and the Console account is paying.

---

## Done

- **2026-08-24 (scoped source links + live health triage)** — Koch Supply &
  Trading was scraping the correct Avature facet (the configured and supplied
  German board returned the same five job IDs), but `/sources` preferred its
  broad `career_url` and `_scope_model` did not recognize
  `koch_avature.filter_params`. Added explicit `scope_url` precedence, facet
  classification, and regression tests; the same audit fixed Munich Re and
  ERGO's query-scoped Radancy links. Of two hard failures, Simon-Kucher
  recovered on re-probe (136 roles); Rokos had removed its Kernel/apptrkr board
  with no replacement ATS or openings, so it moved honestly to manual. All 21
  degraded sources re-probed without errors; their low counts are baseline
  collapses, not current exceptions.
- **2026-08-19 (post-vacation sweep — tagger auth outage + DACH scope
  cleanup)** — The scan itself was healthy through Aug 17 (Aug 18 aborted on a
  transient `Could not resolve host: github.com` at the catch-up fire; DNS was
  fine again by morning). Two real defects. **(1) OAuth race** — see the
  failure signature above; dead since Aug 15, so the Aug 15/16/17 scans all
  bought their tags from the paid Console key. Fixed with a serialized
  `warm_auth` before any fan-out. **(2) The API fallback was unreachable from
  the nightly re-tag** — `backfill_tags` calls `tag_jobs` once per chunk of
  `BATCH_SIZE` rows, so each call sees exactly ONE batch and the per-call
  consecutive-blank counter can never reach `CIRCUIT_BREAKER_THRESHOLD=3`. The
  hook aborted after 39 rows (3 chunks x 13) every night while the key sat
  unused in `.env`. Fixed by making CLI-death a process-wide sticky flag driven
  by the auth signatures. **Lesson: a circuit breaker scoped to one call is
  dead code for any caller that calls once per batch — check the caller's chunk
  size against the breaker's threshold.** **(3) Scope, not tagging** — August's
  `area='other'` share had gone 48% -> 61%, and tag_health was flagging the new
  DACH insurers as tagger blind spots. They were not: the Aug-3 expansion wired
  ~26 whole-company boards that are 90-99% tied-agency insurance sales, Swiss
  apprenticeships (Schnupperlehre/Lehrstelle/EFZ) and claims admin. The tagger
  was labelling them correctly; they should never have been stored. Filter
  terms added after a dry-run over all 29,443 live rows — 524 rows shed, and
  every non-'other' row among them was a retail insurance agency role. **Three
  candidate terms were rejected by that dry-run and must not be re-proposed:**
  bare `sachbearbeiter` (hits Metzler AM's "Sachbearbeiter Quellensteuer" and
  Barclays accounting), `risikoprüfer` (hits ERGO's actuarial "Risikoprüfer
  Leben"), and bare `kundenberater` ("Kundenberater Firmenkunden" is corporate
  banking). **Still open: the remaining `other` bulk is the heavy full-pull
  banks (BNP 1410, JPMC 1088), which is by design — but the DACH insurer
  sources are still whole-board pulls and want per-source facet/keyword
  scoping, which the filter terms only paper over.**

- **2026-08-03 (three-week-idle sweep — 4 hard fails fixed, backstop-enricher
  regression found, 14 insurers added)** — **Anima Sgr**: board switched from
  server-rendered cards to an AJAX `vacancyListCareer` POST (session-tokened
  endpoint in `#url-for-announces`) → new `intervieweb_career` handler; the
  platform's "Nessun annuncio disponibile" marker is trusted-empty (board is
  currently 0). **Mizuho (EMEA)**: `careers.mizuhoemea.com` DNS was
  decommissioned while the SF tenant `mizuhoba01` stayed live on
  `career2.successfactors.eu` — mizuhogroup.com still links the dead domain.
  `successfactors_dwr._token` now falls back to the plain landing URL when the
  JOB_SEARCH view 302s off-host; rewired, 0 → 24 reqs. **Lesson: a dead vanity
  career domain does not mean a dead tenant — probe the SF/ATS backend host
  before declaring manual.** **MS Campus (talnet) + Bloomberg (avature)**:
  both boards list a requisition twice, so `unique ids < reported total` is
  normal — completeness checks now compare RAW parsed rows, not deduped ids
  (was a hard fail on MS Campus, a nightly WARN on Bloomberg). **Backstop
  enricher DEAD since ~Jul 16**: the July packaging move set
  `descriptions.ROOT` to `scrapers/enrich/`, so `DEFAULT_DB` resolved to a
  phantom auto-created `scrapers/enrich/jobs.db` — "no rows need enrichment"
  every night while 2,238 NULL descriptions piled up and the tagger ran
  title-only (the whole other-heavy alert wave: PIC/EIF 100%, Commerzbank 92%,
  radancy median 0). Fixed ROOT, added a fail-loud guard (missing/empty DB file
  is now an error, not a no-op), drained the backlog manually. **Lesson: a
  maintenance script whose selection query returns nothing must distinguish
  "nothing to do" from "looking in the wrong place".** The ~25-source nightly
  skip list probed clean end-to-end — all genuinely empty (seasonal campus
  boards pre-September + batch-hiring PE/HF boutiques), no repairs needed.
  Also added 14 insurer/reinsurer sources (finance-island scoped: keyword,
  facet, or dedicated-arm boards) — see targets.json notes; LGIM was already
  covered via `attrax`, AXA IM is BNPP's board since the 2025 sale.

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
  `jobfeed/tag_health.py` (other-heavy companies) and `jobfeed/link_health.py` (sampled dead
  apply links), both wired into selfcheck. link_health's first run caught
  **Mako + GSA Capital** (Greenhouse absolute_url → redesign-removed firm
  paths) → new `url_template` override, rows rewritten. tag_health surfaced a
  40-firm other-heavy backlog; Opus triage found **39/40 correct-by-design**
  (ops-dominated boards) and one real flaw class — **physical/energy commodity
  traders & originators leaking to `other`** — fixed in the jobfeed/tag.py prompt
  (markets definition + energy/commodity sector hint), 5 rows re-tagged to
  markets (ADM Farm Trader, Statkraft originators, Axpo origination).
- **2026-07-05/06 (the big manual→scraped flip)** — ~35 firms flipped, ~18 new
  handlers; Buckets A/B/D cleared; Playwright verdict settled (only McKinsey +
  Marubeni would need a browser; declined). Bucket B worktree probe flipped 10
  of 17 "browser-only" firms to plain HTTP. `successfactors_dwr` handler
  cracked the Pictet/Sumitomo-EMEA DWR RPC (JS-shell ≠ needs a browser). E.ON
  rewired off a dead SmartRecruiters slug; never-produced guard added for that
  blind-spot class. (Detail was in MANUAL_WORKLIST.md, deleted 2026-09-23; see git history.)
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
  (`kernel.py`, year-proof slug discovery), NIB (`reachmee.py`). (`kernel.py` and
  `reachmee.py` were deleted 2026-09-23 as unreachable: no target uses them.) Key facts:
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
  (`jobvite.py`, deleted 2026-09-23 as unreachable), Scotiabank GBM, BCG (phenom_widgets), Deloitte, KBC,
  Partners Group (`successfactors_classic.py`), Erste (`erste_btp.py`).
- **2026-06-28 (health-alert pass)** — jobfeed/notify.py reads `LEGACY_MAIL_PASSWORD` from
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
`jobfeed/manual_check.py` (the live not-working list).

## 2026-09-23: Trackr discovery comparison

Trackr's opening dates are not its discovery timestamps. PAI's DealCloud
URL labelled 22 September was already stored on 29 June; Ardian, Lazard,
Morgan Stanley and Natixis samples matched Jobfeed's first-seen calendar day.
KKR campus job 6179236004 was available in a read-only probe but absent from
the last completed scan: a cadence gap, not a broken handler.

UBS Graduate Careers used LinkID 10846, which returned 39 roles and omitted
London off-cycle Quants job 349656. LinkID 0 returned 151 and included it;
the existing source name and IDs are preserved. Breega's public Teamtailor
board was added using the existing handler, with portfolio technical and
placeholder titles excluded and two distinct internship bodies verified.
Teamtailor's coverage declaration now correctly says generic HTTP enrichment,
since its listing handler supplies no descriptions.

Bank of America campus remains manual behind its documented bot check.
Amazon and Tura Advisory were also absent from the sampled corpus; neither
is enabled by this repair. Do not treat running known boards more often as
a fix for missing boards.
