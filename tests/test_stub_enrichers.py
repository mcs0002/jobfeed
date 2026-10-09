"""Offline tests for the stub-fixing enrichers added 2026-07-16
(ukg, zoho_recruit, hr_manager, euronext) plus the getnoticed non-coverage
decision.

Each fixture reproduces the exact payload shape probed live: UKG's Googlebot-UA
``CandidateOpportunityDetail({...})`` object literal, Zoho's entity-encoded
``JSON.parse('[{\\x22...}]')`` hydration blob, HR-manager's
``<div id="AdvertisementContent">`` container, and Euronext's Drupal
``field--name-body`` divs. No network.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.enrich import (
    DETAIL_ENRICHERS, detail_enricher, euronext_enrich, hr_manager_enrich,
    ukg_enrich, zoho_recruit_enrich,
)


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class _Session:
    def __init__(self, text, status=200):
        self._text = text
        self._status = status

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        return _Resp(self._text, self._status)


# --- UKG: Googlebot-rendered OpportunityDetail with the inlined record. The
# Description holds a `}` and a `"` inside the HTML — the balanced-slice must
# not stop early on either.
_UKG_FIXTURE = r'''
<html><head></head><body>
<script>
$(function () {
    var opportunity = new US.Opportunity.CandidateOpportunityDetail({"Id":"e4f509bf","Title":"Experienced M&A Analyst","Description":"<p><strong>SUMMARY</strong></p><p>Our Technology team works with buyers and sellers { including both } providing M&A solutions. Say \"hello\".</p>","BriefDescription":"teaser","OpportunityType":1});
});
</script>
</body></html>
'''

_UKG_URL = ("https://recruiting2.ultipro.com/STE1004/JobBoard/fbeef081/"
            "OpportunityDetail?opportunityId=e4f509bf")


# --- Zoho: the careers detail page embeds the jobs array as an
# entity-encoded JSON.parse literal. Structural quotes are &#92;x22-style
# after entity-encode; inner HTML quotes carry an extra backslash. We build the
# raw (pre-entity-decode) form the enricher receives from the server: quotes
# written as &quot; and hex escapes as literal \x22.
def _zoho_fixture():
    # The JS string literal fed to JSON.parse. Structural quotes are the hex
    # escape \x22; inner HTML quotes carry an EXTRA backslash (\\x22) — this
    # doubling is exactly what keeps the outer JSON valid, and what the decoder
    # must preserve. `\\` here in a raw string is two literal backslashes.
    lit = (r'[{\x22Salary\x22:null,\x22Posting_Title\x22:\x22Broking Internship\x22,'
           r'\x22City\x22:\x22London\x22,'
           r'\x22Job_Description\x22:\x22<span id=\\\x22spandesc\\\x22>'
           r'<div>Are you curious about markets? M&amp;A and { trading }.</div></span>\x22,'
           r'\x22id\x22:\x2235808000013063797\x22}]')
    # &amp; is what the server ships; the enricher html.unescape()s it to '&'
    # before locating the literal.
    return (f"<html><body><script>window.x = JSON.parse('{lit}');</script>"
            f"</body></html>")


_ZOHO_URL = "https://freightinvestorservices.zohorecruit.eu/jobs/Careers/35808000013063797"


# --- HR-manager: the ASPX ad page, body in #AdvertisementContent, wrapped in
# a form (must survive) with cookie/footer chrome around it (must be dropped).
_HR_FIXTURE = """
<html><body>
  <div id="unsupported-browsers">Din browser er forældet.</div>
  <div id="AdvertisementContent">
    <div class="advert">
      <p><strong>Vi investerer.</strong> Også i dig.</p>
      <p>Vil du være med til at drive ESG-indsatsen i en af Danmarks
         største ejendomsporteføljer?</p>
    </div>
  </div>
  <form id="applyform"><button>Send ansøgning</button></form>
  <footer>Cookie- og privatlivspolitik</footer>
</body></html>
"""

_HR_URL = ("https://candidate.hr-manager.net/ApplicationInit.aspx"
           "?cid=208&ProjectId=184816&DepartmentId=21737&MediaId=5")


class UkgEnrichTests(unittest.TestCase):
    def test_matcher(self):
        self.assertTrue(ukg_enrich.is_ukg(_UKG_URL))
        self.assertTrue(ukg_enrich.is_ukg(
            "https://vaneck.rec.pro.ukg.net/VAN1502VEAC/JobBoard/x/"
            "OpportunityDetail?opportunityId=abc"))
        self.assertFalse(ukg_enrich.is_ukg("https://example.com/OpportunityDetail"))
        self.assertFalse(ukg_enrich.is_ukg(""))

    def test_extracts_full_description(self):
        got = ukg_enrich.description(_UKG_URL, _Session(_UKG_FIXTURE))
        self.assertIn("Our Technology team works", got)
        self.assertIn("including both", got)     # survived the inner `{ }`
        self.assertIn('hello', got)              # survived the inner escaped quote
        self.assertIn("M&A solutions", got)      # entity unescaped
        self.assertNotIn("SUMMARY</strong>", got)  # html stripped

    def test_non_ukg_returns_empty(self):
        self.assertEqual(ukg_enrich.description("https://x.com/y", _Session(_UKG_FIXTURE)), "")

    def test_missing_object_returns_empty(self):
        self.assertEqual(
            ukg_enrich.description(_UKG_URL, _Session("<html>no record</html>")), "")

    def test_http_error_returns_empty(self):
        self.assertEqual(
            ukg_enrich.description(_UKG_URL, _Session(_UKG_FIXTURE, status=404)), "")


class ZohoRecruitEnrichTests(unittest.TestCase):
    def test_matcher(self):
        self.assertTrue(zoho_recruit_enrich.is_zoho_recruit(_ZOHO_URL))
        self.assertTrue(zoho_recruit_enrich.is_zoho_recruit(
            "https://acme.zohorecruit.com/jobs/Careers/12345"))
        self.assertFalse(zoho_recruit_enrich.is_zoho_recruit(
            "https://acme.zohorecruit.eu/jobs/Careers/notanumber"))
        self.assertFalse(zoho_recruit_enrich.is_zoho_recruit("https://example.com/x"))

    def test_extracts_matching_job_description(self):
        got = zoho_recruit_enrich.description(_ZOHO_URL, _Session(_zoho_fixture()))
        self.assertIn("Are you curious about markets", got)
        self.assertIn("M&A and", got)            # entity unescaped
        self.assertIn("trading", got)            # survived inner `{ }`
        self.assertNotIn("spandesc", got)        # html attribute stripped

    def test_wrong_id_returns_empty(self):
        url = _ZOHO_URL.replace("35808000013063797", "99999999999999999")
        self.assertEqual(
            zoho_recruit_enrich.description(url, _Session(_zoho_fixture())), "")

    def test_no_blob_returns_empty(self):
        self.assertEqual(
            zoho_recruit_enrich.description(_ZOHO_URL, _Session("<html></html>")), "")


class HrManagerEnrichTests(unittest.TestCase):
    def test_matcher(self):
        self.assertTrue(hr_manager_enrich.is_hr_manager(_HR_URL))
        self.assertFalse(hr_manager_enrich.is_hr_manager("https://example.com/x"))
        self.assertFalse(hr_manager_enrich.is_hr_manager(""))

    def test_extracts_ad_body_dropping_chrome(self):
        got = hr_manager_enrich.description(_HR_URL, _Session(_HR_FIXTURE))
        self.assertIn("Vi investerer", got)
        self.assertIn("ESG-indsatsen", got)
        self.assertNotIn("browser er forældet", got)  # pre-container chrome
        self.assertNotIn("Cookie", got)               # footer dropped

    def test_no_container_returns_empty(self):
        self.assertEqual(
            hr_manager_enrich.description(_HR_URL, _Session("<html>none</html>")), "")

    def test_http_error_returns_empty(self):
        self.assertEqual(
            hr_manager_enrich.description(_HR_URL, _Session(_HR_FIXTURE, status=500)), "")


# --- Euronext: server-rendered Drupal job page. Three field--name-body
# containers: the real ad (with nested divs — the balanced slice must not stop
# at the first inner </div>), an empty one, and a social-links one (must be
# skipped as too short). The page also carries a JSON-LD teaser the enricher
# must NOT return.
_EURONEXT_FIXTURE = """
<html><body>
  <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[{"@type":"JobPosting",
     "title":"Index Structuring intern","description":"Short teaser only."}]}
  </script>
  <nav>Live Markets Amsterdam Athens Brussels Dublin</nav>
  <div class="clearfix text-formatted field field--name-body field--type-text-with-summary field--label-hidden field__items">
    <div class="inner">
      <p>Are you ready to shape the future of capital markets? We are looking
      for an Index Structuring Intern to join the Index team in Paris.</p>
      <div><ul><li>Design and implement new index concepts</li>
      <li>Support the structuring desk with quantitative analysis of
      benchmark and thematic indices</li></ul></div>
      <p>You are enrolled in a Masters programme in finance or engineering
      with strong Python skills and a genuine interest in financial
      markets and index products. This paragraph pads the fixture body past
      the two-hundred character minimum the enricher requires.</p>
    </div>
  </div>
  <div class="clearfix text-formatted field field--name-body field__items"></div>
  <div class="clearfix text-formatted field field--name-body field__items">
    <a href="#">LinkedIn</a> <a href="#">Twitter</a>
  </div>
  <footer>Cookie policy</footer>
</body></html>
"""

_EURONEXT_URL = ("https://www.euronext.com/en/about/careers/job-offers/"
                 "r28006-france-index-structuring-intern")


class EuronextEnrichTests(unittest.TestCase):
    def test_matcher(self):
        self.assertTrue(euronext_enrich.is_euronext(_EURONEXT_URL))
        self.assertTrue(euronext_enrich.is_euronext(
            "https://euronext.com/nl/about/careers/job-offers/r1-x"))
        self.assertFalse(euronext_enrich.is_euronext(
            "https://www.euronext.com/en/about/careers/open-positions"))
        self.assertFalse(euronext_enrich.is_euronext(""))

    def test_extracts_full_body_not_teaser_or_socials(self):
        got = euronext_enrich.description(_EURONEXT_URL, _Session(_EURONEXT_FIXTURE))
        self.assertIn("Index Structuring Intern", got)
        self.assertIn("quantitative analysis", got)   # nested div survived
        self.assertIn("Masters programme", got)       # body read to the end
        self.assertNotIn("Short teaser", got)         # JSON-LD ignored
        self.assertNotIn("LinkedIn", got)             # socials container skipped
        self.assertNotIn("Live Markets", got)         # nav chrome absent

    def test_no_body_container_returns_empty(self):
        self.assertEqual(
            euronext_enrich.description(_EURONEXT_URL, _Session("<html></html>")), "")

    def test_http_error_returns_empty(self):
        self.assertEqual(
            euronext_enrich.description(
                _EURONEXT_URL, _Session(_EURONEXT_FIXTURE, status=403)), "")


class RegistryTests(unittest.TestCase):
    def test_all_four_registered_and_routed(self):
        self.assertIs(detail_enricher(_UKG_URL), ukg_enrich.description)
        self.assertIs(detail_enricher(_ZOHO_URL), zoho_recruit_enrich.description)
        self.assertIs(detail_enricher(_HR_URL), hr_manager_enrich.description)
        self.assertIs(detail_enricher(_EURONEXT_URL), euronext_enrich.description)

    def test_matchers_do_not_overlap_each_other(self):
        for url in (_UKG_URL, _ZOHO_URL, _HR_URL, _EURONEXT_URL):
            hits = [d.__module__ for is_fn, d in DETAIL_ENRICHERS if is_fn(url)]
            self.assertEqual(len(hits), 1, f"{url} -> {hits}")


if __name__ == "__main__":
    unittest.main()
