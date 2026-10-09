import unittest

from scrapers.enrich import detail_enricher, talentsoft_enrich

# Trimmed CA-CIB offer page: the portal chrome the generic stripper kept, then
# the labelled posting fields inside #contenu-ficheoffre (real CA-CIB markup).
_FIXTURE = """
<html><body>
  <div class="ts-content">Espace candidat J'ai déjà un espace candidat
    Ma sélection : 0 offre(s) Suivez nous</div>
  <div class="ts-offer-page__body">
    <div class="ts-offer-page__content-details" id="contenu-ficheoffre"
         data-class="ts-offer-details-content">
      <h3>Intitulé du poste</h3><p id="fldjobdescription_jobtitle">Structureur H/F</p>
      <h3>Missions</h3>
      <div id="fldjobdescription_description1"><p>Structurer des produits
        de taux et d'actions pour la clientèle institutionnelle.</p></div>
    </div>
  </div>
  <div class="ts-related-offers">Offres similaires</div>
</body></html>
"""


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def get(self, url, headers=None, timeout=None):
        return _Resp(_FIXTURE)


class TalentsoftEnrichTests(unittest.TestCase):
    URL = "https://jobs.ca-cib.com/job/job-structureur-h-f_109437.aspx"

    def test_matches_offer_pages_only(self):
        self.assertTrue(talentsoft_enrich.is_talentsoft(self.URL))
        self.assertTrue(talentsoft_enrich.is_talentsoft(
            "https://jobs.amundi.com/job/job--client-success-manager-m-f_114224.aspx"))
        self.assertFalse(talentsoft_enrich.is_talentsoft("https://jobs.ca-cib.com/offre-de-emploi/liste.aspx"))
        self.assertFalse(talentsoft_enrich.is_talentsoft("https://example.com/job/x_1.aspx"))

    def test_keeps_the_posting_and_drops_the_portal_chrome(self):
        text = talentsoft_enrich.description(self.URL, _Session())
        self.assertIn("Structurer des produits", text)
        self.assertNotIn("Espace candidat", text)
        self.assertNotIn("Offres similaires", text)

    def test_routed_through_the_detail_registry(self):
        self.assertIs(detail_enricher(self.URL), talentsoft_enrich.description)


if __name__ == "__main__":
    unittest.main()
