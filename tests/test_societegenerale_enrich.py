"""Tests for Société Générale's structured visible-DOM enricher."""
import unittest
from unittest.mock import Mock

import requests

from scrapers.enrich import detail_enricher, societegenerale_enrich


URL = (
    "https://careers.societegenerale.com/en/job-offers/"
    "trainee-12-months-contract-fic-sales-26000HN5-en"
)


PAGE = """
<html><head>
  <script type="application/ld+json">
    {"@type":"JobPosting","description":"ResponsibilitiesThis is joined.Profile requiredDegree."}
  </script>
</head><body>
  <section id="job-detail-description">
    <h2>Responsibilities</h2>
    <p>This is a fixed-term trainee contract supporting the desk.</p>
    <ul><li>Prepare sales reports.</li><li>Support client requests.</li></ul>
  </section>
  <section id="job-detail-profile">
    <h2>Profile required</h2>
    <ul><li>Bachelor's degree in finance.</li><li>Strong analytical skills.</li></ul>
  </section>
  <section id="job-detail-group">
    <h2>Business insight</h2>
    <p>Join an international markets team serving institutional clients.</p>
    <p>Our culture values responsibility, innovation, and teamwork across regions.</p>
  </section>
</body></html>
"""


class SocieteGeneraleEnrichTests(unittest.TestCase):
    def test_url_matcher_and_registry(self):
        self.assertTrue(societegenerale_enrich.is_societegenerale(URL))
        self.assertFalse(societegenerale_enrich.is_societegenerale(
            "https://example.com/job-offers/x"))
        self.assertIs(detail_enricher(URL), societegenerale_enrich.description)

    def test_extracts_visible_sections_instead_of_joined_jsonld(self):
        text = societegenerale_enrich.extract_description(PAGE)
        self.assertIn("Responsibilities\n\n", text)
        self.assertIn("• Prepare sales reports.\n• Support client requests.", text)
        self.assertIn("\n\nProfile required\n", text)
        self.assertIn("\n\nBusiness insight\n", text)
        self.assertNotIn("ResponsibilitiesThis", text)

    def test_fetch_failure_is_empty(self):
        session = Mock()
        session.get.side_effect = requests.RequestException("offline")
        self.assertEqual(societegenerale_enrich.description(URL, session), "")


if __name__ == "__main__":
    unittest.main()
