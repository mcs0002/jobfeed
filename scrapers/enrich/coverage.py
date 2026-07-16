"""Description-coverage guarantee.

Every ATS we actively scrape must have a KNOWN way to get the job body, or the
Haiku tagger runs on title-only and mis-tags (this is exactly how the TAL.net
listing-only scraper shipped ~700-char stubs unnoticed for weeks).

``DESCRIPTION_STRATEGY`` declares, per ATS, where the body comes from:

- ``ENRICHER`` — a per-ATS enricher in this package fetches it (registry in
  ``__init__.DETAIL_ENRICHERS`` or an inline lane in ``main._enrich_new_jobs``).
- ``SCRAPER``  — the scraper handler itself returns the body (inline in the
  listing payload, or via its own ``_extract_text`` call).
- ``HTTP``     — no dedicated path; relies on the generic server-rendered GET
  (``descriptions.enrich_one``). Acceptable, but the weakest link.
- ``NONE``     — no body available by design (listing-only, manual/research).

This registry is a STATIC completeness guard: ``tests/test_enrich_coverage.py``
fails if any ATS in ``targets.json`` is missing here, so adding a source forces
a conscious choice. Whether the declared path actually WORKS is a separate,
runtime question answered by ``description_health.py`` (which reads the live DB
and flags sources whose descriptions are, in fact, stubs).
"""
ENRICHER = "enricher"
SCRAPER = "scraper"
HTTP = "http"
NONE = "none"

# ATS values that are intentionally not scraped (no body expected).
UNSCRAPED_ATS = {"unknown", "manual"}

DESCRIPTION_STRATEGY = {
    # --- Filled by a dedicated enricher (this package) ---
    "workday": ENRICHER,
    "oracle_hcm": ENRICHER,
    "workable": ENRICHER,
    "goldman": ENRICHER,
    "csod": ENRICHER,
    "balyasny": ENRICHER,
    "talentbrew": ENRICHER,
    "successfactors": ENRICHER,
    "successfactors_api": ENRICHER,
    "successfactors_classic": ENRICHER,
    "successfactors_dwr": HTTP,
    "smartrecruiters": ENRICHER,
    "glencore": ENRICHER,
    "eightfold": ENRICHER,
    "jibe": ENRICHER,
    "breezy": ENRICHER,
    "brassring": ENRICHER,
    "brassring_hosted": ENRICHER,
    "talentview": ENRICHER,
    "talnet": ENRICHER,
    "icims": ENRICHER,
    "ukg": ENRICHER,          # Googlebot-UA SSR inlines the full Description
    "zoho_recruit": ENRICHER,  # detail page embeds full Job_Description hydration
    "hr_manager": ENRICHER,   # AdvertisementContent div on the ad page
    # --- Body returned by the scraper handler (inline or its own extraction) ---
    "greenhouse": SCRAPER,
    "bamboohr": SCRAPER,
    "beesite_sitemap": SCRAPER,  # per-ad body fetched inline from server-rendered pages
    "ossiam": SCRAPER,
    "generali": SCRAPER,
    "vanlanschot": HTTP,  # list has no body; vacancy pages are server-rendered
    "refline": HTTP,      # list has no body; detail pages are server-rendered
    "rss": HTTP,            # feed carries a teaser; detail pages are server-rendered
    "appellia": HTTP,       # list has no body; position detail pages are server-rendered
    "achmea": NONE,         # detail pages are a JS shell (no server-rendered body); descriptive titles
    "getnoticed": NONE,     # JS shell detail; no SSR-for-bots, no reachable detail API (probed 2026-07-16); descriptive title + city only
    "azimut": SCRAPER,      # jobDescriptionLong inline from the Liferay object
    "pfa": NONE,            # SF job_listing body defeats the generic extractor (0 chars); title from <title>
    "teamio": SCRAPER,      # teaser inline (detail pages are an SPA)
    "mckinsey": SCRAPER,    # whatYouWillDo/yourBackground inline in the gateway payload
    "sitemap_jobs": NONE,   # title derived from URL slug; detail pages JS-rendered
    "ashby": SCRAPER,
    "recruitee": SCRAPER,
    "emply": HTTP,     # get-page list is titles-only; directLink ad pages are server-rendered
    "hibob": SCRAPER,
    "directemployers": SCRAPER,
    "avature": SCRAPER,
    "beesite": SCRAPER,
    "phenom": SCRAPER,
    "phenom_widgets": SCRAPER,
    "teamtailor": SCRAPER,
    "lever": SCRAPER,
    "citadel": SCRAPER,
    "radancy": SCRAPER,
    "janestreet": SCRAPER,
    "deutscheboerse": SCRAPER,
    "recsolu": SCRAPER,
    "kpmg_us": SCRAPER,
    "bpce": SCRAPER,
    "bnpparibas_paced": SCRAPER,
    "societegenerale": SCRAPER,
    "euronext": ENRICHER,  # Drupal field--name-body div; page JSON-LD is only a teaser
    "abnamro": SCRAPER,
    "deshaw": SCRAPER,
    "bundesbank": SCRAPER,
    "generic": SCRAPER,
    "uniper": ENRICHER,
    "guidecom": ENRICHER,
    "wellsfargo": SCRAPER,
    "attrax": SCRAPER,
    "eib": ENRICHER,
    "eploy": SCRAPER,
    "umantis": SCRAPER,
    "personio": SCRAPER,
    "pinpoint": SCRAPER,
    "wp_job": SCRAPER,
    "intervieweb": SCRAPER,
    "rwe": SCRAPER,
    "kernel": SCRAPER,
    "koch_avature": SCRAPER,
    "adp_careercenter": SCRAPER,
    "bis": SCRAPER,
    "talentsoft": SCRAPER,
    "erste_btp": SCRAPER,
    "peoplebank": SCRAPER,
}


def undeclared(ats_values) -> set:
    """ATS values that are scraped but have no declared description strategy.
    Empty set == full coverage."""
    return {a for a in ats_values
            if a not in UNSCRAPED_ATS and a not in DESCRIPTION_STRATEGY}
