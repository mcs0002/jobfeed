# How bulletproof are the scrapers? A 12-month durability estimate

Written 2026-07-06, at the end of the big manual→scraped flip (423 targets, ~380
actively scraped, ~85 distinct handlers). This is an estimate of what breaks,
why, at what rate, and what the system looks like after a year with vs. without
maintenance.

## The one-line answer

The system is not bulletproof and cannot be — but it is **breakage-cheap**: the
architecture concentrates risk into shared handlers, detects failures within one
scan cycle, and makes the typical fix a 10-minute config edit. Expect roughly
**1–3 sources to break per month**. With ~30–60 min/month of attention, coverage
stays at 97–99%. Fully unattended, expect decay to ~85–90% coverage after a year
— degraded, not dead.

## Is scraping even a legitimate data source?

Worth settling before worrying about durability: yes, on all three axes that
matter.

**Legally**, scraping publicly accessible pages is broadly settled as lawful.
The landmark case is *hiQ v. LinkedIn* (US 9th Cir.): accessing public data is
not "unauthorized access" under the CFAA. In the EU there is no anti-scraping
statute; the theoretical levers are terms of service (a civil contract claim,
essentially never enforced against low-volume personal use), the sui generis
database right (aimed at wholesale copying of curated databases, not reading a
careers page), and GDPR (job postings aren't personal data). Job postings are
also the least-protected content imaginable in practice — firms *pay* to
distribute them, and Indeed, LinkedIn, Adzuna and Google for Jobs were all built
substantially on crawling other people's listings.

**Where it turns grey** — and where this project deliberately stays out:
circumventing technical barriers (CAPTCHAs, forging `cf_clearance` — declined
for Marubeni), aggressive load, republishing copyrighted description text, and
scraping personal data at scale. This system does none of the four: one polite
scan a day per firm, fail-loud instead of retry-hammering, private use, no
redistribution. Even the `curl_cffi` TLS impersonation only defeats fingerprint
*heuristics*; it never solves an actual challenge.

**As research data**, scraped job postings are mainstream economics: Lightcast
(ex Burning Glass) data underpins well-cited work on skill demand and hiring
(Hershbein & Kahn in the *AER*, Deming & Kahn), Indeed Hiring Lab data is used
by the Fed and ECB, and online vacancy indices are a standard labor-market
indicator alongside JOLTS. The caveats reviewers care about are data-quality
ones, not legitimacy ones: coverage bias (which firms post online, which boards
you scrape), duplicates, and the fact that a posting is a vacancy *signal*, not
a hire.

So the durability question below is an engineering question, not a compliance
one.

## Why breakage is inevitable

Every scraper is an unwritten contract with a third party who doesn't know we
exist. There are four independent forces working against it:

1. **Vendor API changes** — the ATS platform changes its JSON endpoints.
2. **Firm redesigns** — the careers page/board is rebuilt on the same ATS.
3. **ATS migrations** — the firm switches ATS entirely (the slug dies).
4. **Anti-bot escalation** — Cloudflare et al. tighten what plain HTTP can reach.

These have very different rates and blast radii, which is what makes an estimate
possible.

## Risk tiers (where the 380 scraped sources actually sit)

### Tier 1 — Big multi-tenant SaaS APIs (~290 sources, ~76%)

`workday` (106), `greenhouse` (57), the SuccessFactors family (41),
`oracle_hcm` (18), `avature` (12), plus `smartrecruiters`, `workable`, `lever`,
`ashby`, `recruitee`, `teamtailor`, `personio`, `icims`, `eightfold`, `csod`,
`talnet`, `talentbrew`…

These endpoints power the vendors' own widgets and thousands of integrations.
Breaking them breaks the vendor's own product, so they are versioned and very
stable — Workday's CXS API and Greenhouse's public board API have been unchanged
for years. **Expected: 0–2 breaking changes across the whole year.** Crucially,
a break here hits a *class*, not a firm: one fix in one handler repairs 50–100
sources at once. Risk is concentrated, and concentration is good — maintenance
scales with the number of *handlers*, not the number of *sources*.

What does break in this tier is per-firm wiring: a tenant slug dies because the
firm migrated (the E.ON case — SmartRecruiters slug returned 0 while the real
board had moved to SuccessFactors). That's force #3 wearing a Tier-1 costume.

### Tier 2 — Server-rendered HTML / sitemaps / feeds (~35 sources)

`generic` (12), `sitemap_jobs` (4), `rss`, `beesite_sitemap`, bespoke handlers
like `deutscheboerse`, `bundesbank`, `vanlanschot`, `refline`, `appellia`.

These break when the firm redesigns its site. Corporate careers pages get
rebuilt every ~2–5 years, so per-source annual breakage is roughly 20–40%.
Sitemaps and RSS are the sturdier end (they're infrastructure, not design);
CSS-selector scraping of rendered HTML is the flimsier end. **Expected: ~8–12
of these break within a year**, each a bespoke fix.

### Tier 3 — Fragile by construction (~6 sources)

The handlers that replicate undocumented internals:

- `azimut` — OAuth client credentials mined from a Next.js chunk (the handler
  re-discovers them per run, so it survives hash rotation but not a cred reset).
- `teamio` (CEZ) — public widget API key that LMC could rotate.
- `successfactors_dwr` (Pictet, Sumitomo EMEA) — a DWR RPC with a per-session
  CSRF token dance.
- `ukg` (VanEck) — antiforgery-token POST.
- `azimut`/`appellia`-style viewstate walks.

Any of these can die on a vendor's routine internal refactor with zero notice.
**Expected: 2–4 breaks in the year.** These were accepted knowingly — the
alternative was leaving the firms manual.

### Tier 4 — Anti-bot exposure (a handful, plus latent risk everywhere)

Sources that already sit behind Cloudflare bot-fight mode and pass only because
`curl_cffi` forges a browser TLS fingerprint. This is an arms race we've chosen
not to escalate (no Playwright, no `cf_clearance` forging — Ørsted and Marubeni
were dropped rather than fought). The latent risk: any firm can flip on a JS
challenge tomorrow. Historically this is rare for career boards (firms *want*
postings distributed, and challenges break their own SEO), but **expect 2–5
sources/year to disappear behind a wall we won't climb.** Those move to the
manual bucket with an ↗ link — degraded gracefully, not lost.

## Why detection matters more than prevention

The honest insight from the first month: **you cannot prevent breakage, you can
only refuse to let it be silent.** The system's real bulletproofing is the
detection stack, built specifically after each silent-failure incident:

- **Fail-loud doctrine** — scrapers raise on anomalies instead of returning
  `[]`, because the delist/purge logic reads an empty list as "board empty" and
  would quietly delist every job at a healthy firm.
- **Collapse guard** (`selfcheck.py`) — rolling per-source baseline (max jobs
  ever seen); any source with a baseline ≥5 that drops below 20% of it alerts,
  so partial breakage (ING 793 → 2) trips, not just hard zeros.
- **Never-produced guard** (`selfcheck.py`) — flags a verified source stuck at 0
  with no baseline for 2+ consecutive checks, the exact blind spot that hid the
  dead E.ON slug for weeks.
- **`description_health`** — catches sources whose bodies silently became stubs
  (the TAL.net incident: ~700-char stubs shipped unnoticed, mis-tagging jobs).
- **Coverage guard** (`test_enrich_coverage`) — every ATS must declare where its
  descriptions come from; adding a source forces a conscious choice.
- **Failure-alert email** from the M1 on scan errors.

Mean time-to-detect is therefore **one scan cycle (a day)**, and the failure
mode is "one firm's jobs go stale for a few days," never "the DB quietly rots."

### Honest assessment: where detection is still blind

The stack is strong against *hard* failures (exceptions, zeros, collapses,
stub bodies). Every remaining gap is in the nastier class: **the scraper keeps
returning plausible-looking data that is subtly wrong.** Ranked by value:

1. **Dead apply links** — ~~the biggest true blind spot~~ **built 2026-07-06**
   (`link_health.py`, wired into selfcheck): samples 2 random live URLs per
   company weekly, flags only when all samples hard-404/410 across 2
   consecutive runs (kills same-day-removal and bot-wall false positives).
   First run caught two real cases — Mako and GSA Capital, both Greenhouse
   boards whose `absolute_url` pointed at firm-site paths removed in redesigns
   while the scrape itself stayed healthy. Fixed via a per-target
   `url_template` override. Remaining limitation: SPA detail pages (most
   Workday tenants) 200 on any path, so their deaths stay invisible to this.
2. **Tag-quality drift** — ~~found by eye~~ **built 2026-07-06**
   (`tag_health.py`, wired into selfcheck): flags companies with ≥5 tagged
   live rows and ≥80% `area='other'`. First run surfaced a 40-company backlog
   (state pre-seeded so only new entrants alert): mostly energy/commodity
   full boards and exchanges where `other` is genuinely correct, plus a
   suspicious tail (DNB Markets 15/16, CIBC Campus 8/8, several AMs/pensions)
   worth a tagging-playbook pass.
3. **Mid-range count drift** — a 40 → 15 slide (pagination page 2 breaking,
   a keyword scope silently narrowing) passes the 20% collapse ratio. A slow
   drift check is possible (rolling median over N runs) but noisy on seasonal
   boards; acceptable to leave open.
4. **Manual-bucket rot** — the 40 manual firms have no scheduled re-probe;
   they depend on someone remembering. (The 2026-07-16 pass cleared the whole
   "re-probe when roles reappear" sub-bucket — Capital Four, Anima, Horváth,
   Arcano, Arma, Canaccord, Comgest all flipped to scraped; what remains is
   overwhelmingly LinkedIn-only/no-board firms.) A quarterly automated
   re-probe pass would still close it properly.
5. **The alert channel is itself unmonitored** — if the M1 mailer breaks,
   selfcheck prints a WARN into a log nobody reads. Partially closed
   2026-07-16: the browse page now shows a staleness banner when no scan has
   touched the DB for 2+ days, so daily UI use IS the second channel; a
   broken mailer plus a broken scan can no longer both hide.

Items 1 and 2 were built the same day this was written; both paid for
themselves on their first run. Items 3–4 remain open by choice; 5 is
mitigated by the staleness banner.

## Empirical base rate (first month of prod)

39 runs, 2026-06-06 → 2026-07-05: errors trended from ~4/day down to 0–2/day as
mis-wired sources were fixed, against ~380 scraped sources — a steady-state
transient error rate around **0.5%/day**, mostly network flakes that self-heal
on the next run. Genuine permanent breaks discovered in month one: ~5 (all of
them pre-existing mis-wirings surfaced by the new guards, not fresh decay).

## The 12-month ledger

| Failure source | Expected events / yr | Blast radius | Typical fix |
|---|---|---|---|
| Vendor API change (Tier 1) | 0–2 | 50–100 sources at once | one handler edit |
| Firm ATS migration | ~10–20 firms (~3–5%/yr churn) | 1 source each | re-point config, sometimes new handler |
| Site redesign (Tier 2) | 8–12 | 1 source each | selector/endpoint fix |
| Fragile internals (Tier 3) | 2–4 | 1 source each | re-discover token/key |
| Anti-bot escalation | 2–5 | 1 source each | usually: move to manual |

Net: **~25–40 individual breakage events**, ~2–3/month, most fixable in minutes
because the failure email says exactly which source raised what.

## Two scenarios

**Maintained (~30–60 min/month):** coverage stays 97–99%. The dominant cost is
not fixing breaks but the slow migration churn — firms drifting between ATS
vendors, which is also how the target list stays honest.

**Fully unattended for a year:** Tier 1 mostly survives (the big handlers are
the load-bearing walls), so the floor is high — roughly 85–90% of sources still
producing. But the tail rots: the bespoke single-firm handlers accumulate
breaks, delisted-vs-dead ambiguity creeps in, and the manual bucket silently
goes stale. The DB stays correct (fail-loud prevents poisoning); it just goes
progressively blind at the edges.

## What would actually change the math

- **A Cloudflare-style default-on JS challenge for career pages** — the one
  systemic risk. Low probability (contradicts firms' distribution incentive),
  high impact. Mitigation exists (headless browser lane) and was consciously
  declined; it can be revisited if the landscape shifts.
- **Workday/Greenhouse API deprecation** — would be announced months ahead and
  is a single-handler fix, but it's the biggest single point of failure by
  source count.

## Bottom line

"Bulletproof" is the wrong frame for scrapers; **"cheap to repair and impossible
to fool silently"** is the achievable property, and that's what this system has.
The 76% concentration on stable vendor APIs gives it a high floor, the fail-loud
+ selfcheck stack gives it a one-day detection ceiling, and the long tail of
bespoke handlers is the known, budgeted maintenance cost: a couple of fixes a
month, most of them trivial.
