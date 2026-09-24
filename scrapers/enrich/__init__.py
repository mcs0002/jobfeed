"""Description enrichment layer.

Many ATSes omit the job body from their listing payload (Oracle HCM, most
Workday tenants, WCN/TAL.net, …). Without a description the Haiku tagger and the
YoE-wall detector see only title + company + location and mis-tag the role. This
package fetches the real body, per-ATS.

Two kinds of enricher live here:

- **Inline-lane enrichers** (Workday, Oracle, Workable, Goldman, CSOD, Balyasny,
  TalentBrew) — own lanes because they need a primed session, tenant config, or
  an id-prefix match. ``enrich_lane`` below is the one router; the inline pass
  (``main._enrich_new_jobs``) and the nightly backstop both call it.
- **Detail enrichers** — the ``DETAIL_ENRICHERS`` registry below, matched by
  ``is_*(url)``. ``main`` and the nightly backstop (``descriptions.py``) both
  consult it via ``detail_enricher(url)``.

The registry is the SINGLE source of truth (it used to live in ``jobfeed/main.py`` and
drift). ``descriptions.py`` imports it back lazily to avoid an init cycle.

Adding a source? ``coverage.DESCRIPTION_STRATEGY`` must classify its ATS, and
``tests/test_enrich_coverage.py`` fails until it does — that guard is what would
have caught the TAL.net listing-only scraper shipping stub descriptions.
"""
from . import (
    balyasny_enrich, brassring_enrich, breezy_enrich, csod_enrich,
    eib_enrich, eightfold_enrich, euronext_enrich, glencore_enrich,
    goldman_enrich, guidecom_enrich, hr_manager_enrich, icims_enrich,
    jibe_enrich, oracle_enrich, smartrecruiters_enrich, societegenerale_enrich,
    successfactors_enrich, talentbrew_enrich, talentview_enrich, talnet_enrich,
    ukg_enrich,
    uniper_enrich, workable_enrich, workday_enrich, zoho_recruit_enrich,
)
from .descriptions import enrich_one  # noqa: F401  (re-export)

# Detail-enricher registry: (matches_url, description_fn) pairs, tried in order.
# Order matters where matchers overlap (TalentBrew id-prefix is handled ahead of
# this list in the routers; SuccessFactors is first here as the broadest match).
DETAIL_ENRICHERS = [
    # Uniper MUST precede successfactors: its careers.uniper.energy URLs match
    # the greedy is_successfactors regex, but the body is on its SF backend via
    # a redirect the plain SF extractor can't reach.
    (uniper_enrich.is_uniper, uniper_enrich.description),
    (successfactors_enrich.is_successfactors, successfactors_enrich.description),
    (smartrecruiters_enrich.is_smartrecruiters, smartrecruiters_enrich.description),
    (glencore_enrich.is_glencore, glencore_enrich.description),
    (eightfold_enrich.is_eightfold, eightfold_enrich.description),
    (jibe_enrich.is_jibe, jibe_enrich.description),
    (breezy_enrich.is_breezy, breezy_enrich.description),
    (brassring_enrich.is_brassring, brassring_enrich.description),
    (talentview_enrich.is_talentview, talentview_enrich.description),
    (icims_enrich.is_icims, icims_enrich.description),
    (talnet_enrich.is_talnet, talnet_enrich.description),
    (guidecom_enrich.is_guidecom, guidecom_enrich.description),
    (eib_enrich.is_eib, eib_enrich.description),
    (euronext_enrich.is_euronext, euronext_enrich.description),
    (societegenerale_enrich.is_societegenerale,
     societegenerale_enrich.description),
    (ukg_enrich.is_ukg, ukg_enrich.description),
    (zoho_recruit_enrich.is_zoho_recruit, zoho_recruit_enrich.description),
    (hr_manager_enrich.is_hr_manager, hr_manager_enrich.description),
]


# Detail enrichers that read the posting's JSON-LD and can report its
# validThrough through an `out` dict, as enrich_one does.
_TAKES_OUT = {glencore_enrich.description, jibe_enrich.description,
              euronext_enrich.description, societegenerale_enrich.description}


def detail_enricher(url: str):
    """Return the matching detail-API description fn for url, or None."""
    for is_fn, desc_fn in DETAIL_ENRICHERS:
        try:
            if is_fn(url):
                return desc_fn
        except Exception:
            pass
    return None


# Every lane a job can be routed to, in routing precedence.
ENRICH_LANES = ("workday", "talentbrew", "oracle", "workable", "goldman",
                "csod", "balyasny", "detail", "http")


def enrich_lane(job: dict, *, workday: bool) -> str:
    """The one routing decision for both the inline pass (jobfeed/main.py) and the
    nightly backstop (descriptions.py). They used to carry separate ladders in
    different orders, and a copy of the detail registry had already drifted
    once.

    ``workday`` is the caller's answer, because the two know it differently:
    the scan carries the tenant config on the job, the backstop only has the
    URL. Workday goes first because its public page is a JS shell. TalentBrew
    routes by id PREFIX before any URL matcher, because its
    /job/<loc>/<slug>/<id> URLs collide with the broad SuccessFactors matcher,
    which would return "" and leave every TalentBrew firm un-enriched.
    Everything else is a JS shell/SPA with a first-party detail API; plain HTML
    is the fallback lane."""
    url = job.get("url", "")
    if workday:
        return "workday"
    if talentbrew_enrich.is_talentbrew(job.get("id", "")):
        return "talentbrew"
    for lane, matches in (("oracle", oracle_enrich.is_oracle),
                          ("workable", workable_enrich.is_workable),
                          ("goldman", goldman_enrich.is_goldman),
                          ("csod", csod_enrich.is_csod),
                          ("balyasny", balyasny_enrich.is_balyasny)):
        if matches(url):
            return lane
    return "detail" if detail_enricher(url) else "http"


def detail_then_http(job: dict, session, *, timeout: int | None = None,
                     http_timeout: int | None = None) -> str:
    """The detail lane. A URL-matched detail enricher coming back empty is NOT
    proof the page has no body. The SuccessFactors matcher claims any
    `careers.*/…/job/<a>/<b>` URL, which is how every Radancy row (Munich Re,
    ERGO, MEAG) sat description-less from July 2026 — the plain-HTML lane reads
    their JSON-LD fine, it just never got a turn. Falling through fixes the
    whole class instead of one ATS at a time."""
    url = job["url"]
    kw = {} if timeout is None else {"timeout": timeout}
    fn = detail_enricher(url)
    if fn in _TAKES_OUT:
        kw["out"] = job  # the page's stated closing date rides back on the job
    text = fn(url, session, **kw)
    if text:
        return text
    kw = {} if http_timeout is None else {"timeout": http_timeout}
    return enrich_one(url, session, out=job, **kw)
