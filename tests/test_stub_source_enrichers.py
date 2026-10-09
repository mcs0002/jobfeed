"""Enrichers for the sources the 2026-09-29 audit found storing no posting:
Phenom (BCG, Oliver Wyman, ...), getnoticed (Hauck Aufhäuser Lampe) and the
Crédit Mutuel RHEC board (La Française). Fixtures are trimmed real markup."""
import json
import unittest

from scrapers import getnoticed
from scrapers.enrich import (
    creditmutuel_enrich, detail_enricher, enrich_lane, getnoticed_enrich,
    phenom_enrich,
)
from scrapers.enrich.coverage import DESCRIPTION_STRATEGY, ENRICHER


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self, text, status=200):
        self._resp = _Resp(text, status)

    def get(self, url, headers=None, timeout=None):
        return self._resp


def _phenom_page(job: dict) -> str:
    ddo = {"jobDetail": {"status": 200, "data": {"job": job}},
           "siteConfig": {}}
    # The expired-job template sits in every Phenom page's DOM, live or not;
    # it is what the generic stripper stored.
    return f"""
<html><head><script>phApp.pageName = "job"; phApp.ddo = {json.dumps(ddo)};
phApp.experimentData = {{}};</script></head><body>
  <nav>About BCG Learn more about BCG Inclusion</nav>
  <ppc-content data-ph-at-id="job-completion-info"><div><span>We're sorry…
    the job you are trying to apply for has been filled.</span></div></ppc-content>
  <div>Explore our teams</div>
</body></html>"""


class PhenomEnrichTests(unittest.TestCase):
    URL = ("https://careers.marsh.com/global/en/job/R_340425/"
           "oliver-wyman-actuarial-health-manager-valuation-reserving")

    def test_matches_job_pages_only(self):
        self.assertTrue(phenom_enrich.is_phenom(self.URL))
        self.assertTrue(phenom_enrich.is_phenom(
            "https://careers.bcg.com/global/en/job/682/manager-financial-institutions"))
        self.assertTrue(phenom_enrich.is_phenom(
            "https://careers.enbw.com/de/de/job/23768/product-development-analyst"))
        self.assertFalse(phenom_enrich.is_phenom(
            "https://careers.bcg.com/global/en/search-results"))
        self.assertFalse(phenom_enrich.is_phenom(
            "https://example.com/global/en/job/1/x"))

    def test_reads_the_ddo_body_not_the_expired_template(self):
        page = _phenom_page({"reqId": "R_340425", "description":
                             "<p><b>Company Overview</b></p><p>The <b>Health "
                             "Practice</b> of Oliver Wyman strives to be</p>"})
        text = phenom_enrich.description(self.URL, _Session(page))
        self.assertIn("The Health Practice of Oliver Wyman", text)
        self.assertNotIn("has been filled", text)
        self.assertNotIn("Explore our teams", text)

    def test_gone_job_returns_nothing(self):
        # A job that really is gone: 410 and an empty `job` object.
        self.assertEqual(
            phenom_enrich.description(self.URL, _Session(_phenom_page({}), 410)), "")
        self.assertEqual(
            phenom_enrich.description(self.URL, _Session(_phenom_page({}))), "")

    def test_routed_ahead_of_the_greedy_successfactors_matcher(self):
        self.assertIs(detail_enricher(self.URL), phenom_enrich.description)
        self.assertEqual(enrich_lane({"url": self.URL, "id": "phenom_x"},
                                     workday=False), "detail")

    def test_coverage_no_longer_claims_the_scraper_returns_a_body(self):
        self.assertEqual(DESCRIPTION_STRATEGY["phenom"], ENRICHER)
        self.assertEqual(DESCRIPTION_STRATEGY["phenom_widgets"], ENRICHER)


_GETNOTICED_PAGE = """
<html><body class="page page_vacancy-detail">
  <div class="component_vacancy-caption">Specialist (m/w/d) Transfer Agent Munsbach</div>
  <main class="layout layout_vacancy-main">
    <div class="partial partial_vacancy_toolbar">Zurück zur Übersicht Teilen</div>
    <div class="component component_vacancy-content">
      <h1>Specialist (m/w/d) Transfer Agent &amp; Registrar</h1>
      <h2>Im Überblick</h2><p>Nutzen Sie Ihre Chance in unserem Unternehmen.</p>
      <h2>Ihre Aufgaben</h2><ul><li>Durchführung der <b>Investor Due Diligence</b></li></ul>
    </div>
  </main>
  <div class="component component_related-vacancy">Ähnliche Stellenangebote</div>
</body></html>
"""


class GetnoticedEnrichTests(unittest.TestCase):
    URL = ("https://karriere.abnamro.de/stellenangebot/7547/"
           "specialist-m-w-d-transfer-agent-registrar-investor-due-diligence-1")

    def test_matches_vacancy_pages_only(self):
        self.assertTrue(getnoticed_enrich.is_getnoticed(self.URL))
        # The search page the scraper used to link: never a posting.
        self.assertFalse(getnoticed_enrich.is_getnoticed(
            "https://karriere.abnamro.de/stellenangebote/specialist-m-w-d-transfer"))

    def test_keeps_the_posting_and_drops_the_chrome(self):
        text = getnoticed_enrich.description(self.URL, _Session(_GETNOTICED_PAGE))
        self.assertIn("Durchführung der Investor Due Diligence", text)
        self.assertNotIn("Zurück zur Übersicht", text)
        self.assertNotIn("Ähnliche Stellenangebote", text)
        self.assertIs(detail_enricher(self.URL), getnoticed_enrich.description)

    def test_scraper_links_the_vacancy_page(self):
        payload = {"vacancies": [{"id": 7547, "slug": "specialist-x", "title":
                                  "Specialist", "city": "Munsbach"}],
                   "meta": {"num_total_hits": 1, "totalPageCount": 1}}

        class _S:
            def get(self, url, headers=None, params=None, timeout=None):
                r = _Resp("")
                r.json = lambda: payload
                return r

        orig = getnoticed.make_session
        getnoticed.make_session = lambda: _S()
        try:
            jobs = getnoticed.scrape({"base": "https://karriere.abnamro.de",
                                      "prefix": "hal"})
        finally:
            getnoticed.make_session = orig
        self.assertEqual(jobs[0]["url"],
                         "https://karriere.abnamro.de/stellenangebot/7547/specialist-x")
        self.assertTrue(getnoticed_enrich.is_getnoticed(jobs[0]["url"]))


_CREDITMUTUEL_PAGE = """
<html><body><main class="ei_content">
  <div class="ei_rhec">Votre candidature en 4 étapes Premier contact</div>
  <div class="_c1 rhec_detailoffre _c1">
    <div>Retour à la liste des offres</div>
    <div class="_c1 ei_mainblocfctl _c1">
      <div>Type de contrat <span>Stage</span></div>
      <h2>Vos missions</h2><p>Les analyses <b>ESG</b> des dossiers pré-investissement</p>
      <div class="_c1 rhec_offres _c1">Ces autres offres peuvent vous intéresser
        Investisseur - Projets infrastructures (F/H)</div>
    </div>
  </div>
</main></body></html>
"""


class CreditMutuelEnrichTests(unittest.TestCase):
    URL = "https://recrutement.creditmutuel.fr/fr/offre.html?annonce=114121"

    def test_matches_offer_pages_only(self):
        self.assertTrue(creditmutuel_enrich.is_creditmutuel(self.URL))
        self.assertFalse(creditmutuel_enrich.is_creditmutuel(
            "https://recrutement.creditmutuel.fr/fr/nos_offres.html?socs=39"))

    def test_keeps_the_posting_and_drops_related_offers(self):
        text = creditmutuel_enrich.description(self.URL, _Session(_CREDITMUTUEL_PAGE))
        self.assertIn("Les analyses ESG des dossiers", text)
        self.assertNotIn("4 étapes", text)
        self.assertNotIn("Projets infrastructures", text)
        self.assertIs(detail_enricher(self.URL), creditmutuel_enrich.description)


if __name__ == "__main__":
    unittest.main()
