# Blocked / unscrapable sources — doctrine + pointers

> Rewritten 2026-06-28. The old per-firm blocked list here had gone stale
> (it listed JPM, Citi, BNP, RBC, TotalEnergies, Airbus as blocked — all live
> now via the heavy subprocess executor). This file is no longer the list.

**Where the live state lives now:**
- **[SOURCE_HEALTH.md](SOURCE_HEALTH.md)** — the single effort-ranked worklist of
  every not-working source (Tier 0 silent failures → Tier X research candidates).
- **`targets.json`** — each unscrapable firm is `"ats": "manual"` with a
  `manual_reason` field carrying its specific blocker (the forensics that used to
  live here). `python manual_check.py` prints them; the Sources page shows them
  via the ⓘ note.

So: don't maintain a separate blocked list here. Update the target's
`manual_reason` and SOURCE_HEALTH.md.

## Before you mark a source `manual` — the probe checklist

A careers page that *looks* like a JS-only SPA usually still has a JSON endpoint
the SPA itself calls. Mark `manual` only after ALL of these fail:

1. **Look for the real feed.** Open the careers page in a browser and watch the
   network tab (or `read_network_requests`). The job list almost always comes
   from an XHR — `?json=`, `/api/`, `/fo/rest/`, `/services/`, an `.rss`, or a
   greenhouse/lever/jobylon/jobs2web host. Discovery via browser is fine; the
   production scraper must stay plain `requests`.
2. **Check the embed token, not just the slug.** A configured slug that resolves
   but returns few is NOT proof the firm is sparse — the real board can be a
   different token (HRT's jobs were under `wehrtyou`, not `hrttalentcommunity`).
   Re-probe the page for `for=…` / the account id.
3. **Try a credible header set — and don't stop at UA/Accept/Referer.** Many
   endpoints 403/404 a generic UA but 200 a real browser UA + Accept + Referer
   (ABN AMRO). Crucially, some gate on the **`Origin`** header specifically:
   TalentView's detail API (Tikehau) 404s "Resource not found" without
   `Origin: https://{company}.talentview.io` and 200s with it (Referer alone
   does NOT satisfy it). Replay the XHR with the *full* header set a browser
   sends: `Origin`, `Referer`, `X-Requested-With: XMLHttpRequest`, `Accept`.
   Isolate which header is the gate by adding them one at a time.
4. **Read the SPA's JS bundle for the real XHR path.** When the network tab
   isn't handy, grep the minified bundle for the API base + how it builds the
   detail URL (Tikehau: `apiWrapper.get("/companies/"+slug+"/campaigns/"+id)`).
   The path and its params are usually right there in plain text.
5. **Try `curl_cffi` (Chrome impersonation)** for TLS/JA3 fingerprint WAFs.
6. **Try a crawler UA for an SSR variant.** Some SPAs server-render a full
   page ONLY for search-engine crawlers: UltiPro's OpportunityDetail is an
   empty React shell for a browser UA but, with a **Googlebot UA**, inlines
   the complete opportunity record as a JS object literal
   (`scrapers/enrich/ukg_enrich.py`, found 2026-07-16). Cheap to test:
   `curl -A "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"`.
7. **Grep the SPA shell for embedded data before hunting an API.** The initial
   HTML often carries the full dataset the SPA hydrates from: Zoho Recruit
   embeds the whole jobs array as an entity-encoded `JSON.parse('[...]')`
   literal (`zoho_recruit_enrich.py`); Drupal boards server-render the body
   in a `field--name-body` div even when the JSON-LD only has a teaser
   (Euronext). Counterexample so no false hope: getnoticed's shell is a real
   JS template with no data — probed 2026-07-16, honestly unreachable.
8. **Identify the real failure mode.** Session budget (rotate sessions, like
   BNP) vs thread-hang (use `heavy: true`, killable subprocess, like UniCredit)
   vs JS-challenge WAF (only a browser passes — Lumesse) vs no endpoint at all.

Only a **JS-challenge WAF** (e.g. AWS WAF token, Cloudflare challenge) or **no
public ATS at all** is a genuine block. Even then, Playwright is a LAST RESORT
gated by `tests/test_playwright_allowlist.py` — adding a browser-driven source
FAILS that test until you edit the allowlist with a justification, so the
project can't silently drift into depending on the browser. Koch and Tikehau
both looked like genuine blocks and both had an HTTP path once someone finished
this ladder. Everything short of a real WAF is fixable with `requests`/`curl_cffi`.

See also [SOURCE_HEALTH.md](SOURCE_HEALTH.md) and the
`job-scraper-mode-doctrine` memory.

## Oleeo / tal.net campus boards — SCRAPEABLE via the `talnet` handler (don't chase the JSON API)

Corrected 2026-07-04. First probe wrongly concluded these were blocked by going down the
JSON-API path; the server-rendered HTML path works and the existing `talnet` handler already
scrapes it. THE LESSON: for tal.net, parse the HTML board, do NOT use `/api/v1/...`.
- The bare `{firm}.tal.net/candidate/jobboard/vacancy/{n}/adv/` redirects to a session-tokenised
  (`xf-<hex>`) JS shell. Its `/api/v1/vacancy/search` etc. return **401** (auth-gated) — a dead end.
- BUT the STABLE, tokenless URL `{firm}.tal.net/vx/lang-en-GB/mobile-0/[channel-N/]appcentre-{ext|N}/brand-N/candidate/jobboard/vacancy/{board}/adv/`
  server-renders the vacancies as `<li.opp-container>` tiles (or a `table.solr_search_list`) —
  exactly what `scrapers/talnet.py` parses. Get the stable `/vx/...brand-N` prefix by fetching the
  bare board once and regexing it out of the HTML. Pagination is `?start=N` (50/page), now handled.
- Wired 2026-07-04: Bank of America (Campus) 67, Nomura (Campus) 6, Evercore (Campus, vacancy/2) 4,
  Perella Weinberg (Students, vacancy/2) 0-but-valid (seasonal, autumn).
- **BNP Paribas is the exception — bnpparibas.tal.net is a JS-ONLY Oleeo board.** Every
  `/vx/...brand-2/candidate/jobboard/vacancy/{n}/adv/` variant (with/without appcentre/channel)
  returns a bare ~21KB shell: no `results_meta`, no `opp-container`, no `/opp/` links. Its jobs
  load via the auth-gated JS API (401), same dead end as the bare-URL path. Genuinely blocked —
  do NOT wire. **But it's also REDUNDANT: BNP's 749 internships + grad-programme + VIE roles
  already come through the working main `group.bnpparibas` board (bnpparibas_paced, scraped in
  full) — roles_on_lateral. No reason to chase the Oleeo board.** FTI: find its stable `/vx/...`
  board URL the same way (untested).
