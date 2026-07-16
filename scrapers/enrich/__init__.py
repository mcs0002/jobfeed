"""Description enrichment layer.

Many ATSes omit the job body from their listing payload (Oracle HCM, most
Workday tenants, WCN/TAL.net, …). Without a description the Haiku tagger and the
YoE-wall detector see only title + company + location and mis-tag the role. This
package fetches the real body, per-ATS.

Two kinds of enricher live here:

- **Inline-lane enrichers** (Workday, Oracle, Workable, Goldman, CSOD, Balyasny,
  TalentBrew) — routed explicitly by ``main._enrich_new_jobs`` because they need
  a primed session, tenant config, or an id-prefix match.
- **Detail enrichers** — the ``DETAIL_ENRICHERS`` registry below, matched by
  ``is_*(url)``. ``main`` and the nightly backstop (``descriptions.py``) both
  consult it via ``detail_enricher(url)``.

The registry is the SINGLE source of truth (it used to live in ``main.py`` and
drift). ``descriptions.py`` imports it back lazily to avoid an init cycle.

Adding a source? ``coverage.DESCRIPTION_STRATEGY`` must classify its ATS, and
``tests/test_enrich_coverage.py`` fails until it does — that guard is what would
have caught the TAL.net listing-only scraper shipping stub descriptions.
"""
from . import (
    balyasny_enrich, brassring_enrich, breezy_enrich, csod_enrich,
    eib_enrich, eightfold_enrich, euronext_enrich, glencore_enrich,
    goldman_enrich, guidecom_enrich, hr_manager_enrich, icims_enrich,
    jibe_enrich, oracle_enrich, smartrecruiters_enrich, successfactors_enrich,
    talentbrew_enrich, talentview_enrich, talnet_enrich, ukg_enrich,
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
    (ukg_enrich.is_ukg, ukg_enrich.description),
    (zoho_recruit_enrich.is_zoho_recruit, zoho_recruit_enrich.description),
    (hr_manager_enrich.is_hr_manager, hr_manager_enrich.description),
]


def detail_enricher(url: str):
    """Return the matching detail-API description fn for url, or None."""
    for is_fn, desc_fn in DETAIL_ENRICHERS:
        try:
            if is_fn(url):
                return desc_fn
        except Exception:
            pass
    return None
