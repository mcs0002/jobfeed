# Manual sources worklist (40 firms)

Regenerated 2026-07-05 (end of the big HTTP-flip session). Every firm here is a
target the scheduled scan **skips** (`ats: "manual"`). Grouped by what it would
take to scrape it.

## Headline: Buckets A and B are essentially cleared

This session flipped **~35 firms manual→scraped** and added ~18 handlers. The old
"Bucket A (plain-HTTP) ~26" and most of "Bucket B (browser) ~14" turned out to be
scrapable over plain HTTP once the right surface/endpoint was found. What remains
manual is now overwhelmingly **firms with no scrapable board at all** — not a
tooling gap.

New handlers built this session: `bamboohr`, `refline`, `rss`, `hr_manager`,
`beesite_sitemap`, `zoho_recruit`, `sitemap_jobs`, `ukg`, `appellia`, `achmea`,
`getnoticed`, `azimut`, `pfa`, `teamio` (jobs.cz GraphQL), plus firm-bespoke
`generali`/`ossiam`/`vanlanschot`. Config-only flips reused existing handlers
(SEFE/BayernLB/Ampega on SuccessFactors, A&M/DPAM/APG on `sitemap_jobs`,
PGGM/La Française/ODDO/InCommodities/Fulcrum/NBIM on `generic`, BoE on
`oracle_hcm`, PWP on `workday`).

**Playwright verdict — settled, then vindicated.** The 2026-07-06 "final four"
workflow (one deep-probe agent per firm) cracked 3 of the 4 hardest cases
WITHOUT a browser: **McKinsey** (the Avature WAF was the wrong surface — the
real board is a gate-free JSON gateway behind www.mckinsey.com, 591 jobs, new
`mckinsey` handler), **KKR lateral** (the 403-S22 Workday tenant was a red
herring — the board moved to Greenhouse slug `stage`, 166 jobs, config-only),
and **AOT Energy** (mystery solved: the TLS-reset host is a DEAD IP — the
domain is unregistered; AOT was acquired into **ArrowResources AG** (Zug),
whose Workable board is wired, currently 0-but-valid). Only **Marubeni**
genuinely needs a browser artifact (one `cf_clearance` token, Koch-style
~weekly recapture — fully specced in its manual_reason, declined for now).
So: **no browser lane, and it cost us exactly one firm.**

**Addendum 2026-07-06 (verified-but-dead sweep — not manual firms).** A separate
thread fixed a class of `verified:true` sources that silently returned 0 (mis-wired
from day one), and reinforced the no-browser verdict by cracking two JS-rendered
SF tenants over plain HTTP:
- **New handler `successfactors_dwr`** — SF "RCM" career sites (Pictet `banquepict`,
  Sumitomo EMEA `S004690996D`) load the list via a **DWR RPC**, so
  `successfactors_classic`/`_api` both got 0 while the board was live. The handler
  replicates the `search.dwr` call (per-session `_s.crb` token sent as
  `x-csrf-token`/`x-ajax-token`; one call with a big `pageSize` = whole board).
  Pictet 42/42, Sumitomo EMEA 4/4. Discovered via browser DevTools copy-as-cURL —
  **discovery only; scraper stays plain HTTP** (another "JS-shell ≠ needs Playwright"
  data point).
- **E.ON** was pointed at a dead SmartRecruiters slug (`EON1`, totalFound=0); real
  board is SuccessFactors `careers.eon.com` → rewired (`q=finance` scope, ~24).
  **Sumitomo (Americas)** added (SF RMK, careers.sumitomocorp.com). **Ørsted**
  removed (Cloudflare-blocks datacenter IPs + JS-render; unscrapable).
- **`selfcheck.py` never-produced guard** added — flags a verified source at 0 with
  no baseline for ≥2 checks (the blind spot that hid E.ON).
- **Enrichment (descriptions): all ✅** — E.ON (~7.5k), Sumitomo Americas (~6.2k),
  Pictet (~3.3k), Sumitomo EMEA (~5.9k). All four detail pages server-render the
  full body; the `successfactors_dwr` URLs match no detail enricher so they route
  to the inline generic `http` lane (`enrich_one`), which returns clean complete
  descriptions verified via the production path. (An earlier "Sumitomo EMEA is a
  JS stub" note was a test error — a Pictet req id used against the Sumitomo host.)

---

## Would need a browser (1) — the only Playwright candidate, and we said no
- **Marubeni** — Dayforce tenant `mac` on jobs.dayforcehcm.com; the anonymous
  search POST is clean JSON but Cloudflare-403'd without `cf_clearance` (only
  POST is gated; GETs pass, curl_cffi+`__cf_bm` insufficient). Koch-style path
  fully specced (capture `cf_clearance`+UA once in a browser, replay from this
  IP via curl_cffi, ~weekly recapture, fail-loud on 403) — declined for now;
  ↗ points to the careers contact form. Wire instantly if ever approved.

*(McKinsey, KKR lateral and AOT Energy left this file 2026-07-06 — see the
Playwright-verdict paragraph above.)*

## Re-probe / seasonal / board-moved (0) — bucket cleared 2026-07-16

Second re-probe (with the user's leads) flipped everything that had a board:

- **Capital Four** → scraped (new `emply` handler; sectionId GUID unlocked the
  get-page API).
- **Horváth** → scraped (`generic` + `fetch: cffi` + `link_attr: data-href` —
  the full 149-job table is server-rendered in the Cloudflare-gated TYPO3 page;
  the Workday tenant was only ever the apply backend, hence its total=0).
- **Comgest** → scraped (`generic` accordion parse of the internship-offers
  page + `url_fallback: page`; 2 Paris internships at wiring incl. a Financial
  Analyst Talent Program. The full-time job-offers page is application-form
  only — revisit if they ever list roles there).
- **Anima Sgr** → scraped (`generic` on the hosted inRecruiting board
  zinrec.intervieweb.it/animasgr — plain HTML vacancy cards; the annunci.php
  feed key was never needed. Italian-only postings, lang_req handles).
- **Sucden Financial** → moved to no-board bucket: careers now at `/careers/`
  (old `/en/` path 404s), zero on-site listings, hiring is LinkedIn-only
  (confirmed by the user).
- **BC Partners** → moved to no-board bucket: `/current-opportunities` now
  soft-serves the homepage and the real "Life at BC" page links only to
  LinkedIn — the on-site board is gone.


## No scrapable board — email/LinkedIn/prose only (34)
**Playwright would not help — nothing structured to render.** Value here is a 🎓
grad-scheme hint where a real dedicated programme page exists, not a scraper.

**Commodity merchants / brokers / metals:** Freepoint Commodities, Sucden Financial (LinkedIn-only, 2026-07-16), Hartree
Partners, Amalgamated Metal Trading, Toyota Tsusho Metals, SSY, Braemar,
Compagnie Financière Tradition, Copenhagen Energy Trading.

**Hedge funds / macro (email-only):** Andurand, Westbeck, Svelland, KLI.

**PE / private markets:** Capvis, CVC, Arcmont, Park Square, BC Partners (site board removed, LinkedIn-only, 2026-07-16).

**Asset managers (prose page + email/LinkedIn, or legacy no-value ATS):** Marshall
Wace (grad prose → GRAD_SCHEMES), Federated Hermes (PeopleSoft legacy), Record
Financial Group (PeopleHR, evergreen only), Payden & Rygel, Carmignac, Cobas,
azValor, Manulife/CQS (no board exists), Ashmore, Fisch, Ruffer, Sarasin, Polar,
Veritas, Troy, ACATIS, Border to Coast (links to LinkedIn), IXM (LinkedIn-only).


## Mergermarket league-table batch (2026-07-15) — 3 manual + 2 handler opportunities

Added alongside 19 scraped sources (M&A advisors from the Europe LTM league
table). 2026-07-16 re-probe flipped three back to scraped: **Arcano**
(Teamtailor at talento.arcanopartners.com — 3 internships live; the Cloudflare
403 was only on the corporate site), **Canaccord UK/EU** (roles server-rendered
on the global-careers UK page as accordions, apply links -> PeopleHR;
jobs.canaccordgenuity.com is a dead host), **Arma Partners** (server-rendered
after all; selectors recovered from the 2026-02 Wayback snapshot + new generic
`empty_marker` trusted-empty option while their board is empty). These three
remain manual:

- **Centerview Partners** — bespoke ASP.NET `careers.aspx` (main careers path
  WAF-403s curl); applications via Handshake/direct resume. No ATS to find.
- **Equita** — static careers page; roles are PDF postings + mailto. Milan.
- **AZ Capital** — static 'talento' page, no listed vacancies (spontaneous
  applications). Madrid.

**Two new-handler opportunities (probed working endpoints, no handler yet):**

- **Welcome to the Jungle** (unlocks Cambon Partners + Clipperton, both with
  live Paris/Berlin/Munich M&A internships; hosts many more Paris finance
  boutiques). Method: fetch one WTTJ page via curl_cffi (impersonate=chrome) to
  read the embedded rotating Algolia client key, then POST
  `csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_en/query` with
  `filters=organization.slug:<slug>` and Referer `welcometothejungle.com`
  (the Referer is the auth gate). Descriptions inline (`profile`/`key_missions`).
- **JOIN.com** (unlocks Saxenhammer — M&A Intern Frankfurt + Berlin live at
  probe). Method: parse `__NEXT_DATA__` JSON on
  `join.com/companies/saxenhammer-co1` → `initialState.jobs.items`; the public
  REST API 422s. Descriptions need the per-job page.

**Probed dead (not added at all):** HMT (hmt.de is an industrial toolmaker —
the M&A HMT is UK `hmtllp.com`, unprobed), Pava Partners (domain parked),
Amala Partners (no careers page), Vitale & Co (domain doesn't resolve).

---

*The `unknown` bucket is empty — every one of the 423 targets is now scraped,
verified, or manual with a reason. Nothing is staged-and-forgotten.*
