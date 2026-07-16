import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.enrich import talnet_enrich


# Minimal WCN/TAL.net detail page: the vacancy body lives in #vac_desc, and —
# the whole reason the generic stripper failed — that panel wraps the text in
# the application <form>. The enricher must still pull the body out.
_FIXTURE = """
<html><body>
  <div id="header"><nav>Skip to content</nav></div>
  <div id="vac_desc">
    <div class="eform">
      <form class="form-view">
        <div class="type_richtext"><div class="form-control-static">
          <p>Region</p><p>Asia Pacific</p>
          <p>Program description</p>
          <p>What we are looking for: strong analytical skills for a
             Global Markets trading role.</p>
        </div></div>
      </form>
    </div>
  </div>
  <footer>View cookie / data protection and privacy policy</footer>
</body></html>
"""

_FALLBACK = """
<html><body>
  <div class="type_richtext"><div class="form-control-static">
    <p>Corporate audit summer analyst responsibilities and requirements.</p>
  </div></div>
</body></html>
"""


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, text):
        self._text = text

    def get(self, url, headers=None, timeout=None):
        return _Resp(self._text)


class TalnetEnrichTests(unittest.TestCase):
    def test_is_talnet(self):
        self.assertTrue(talnet_enrich.is_talnet("https://evercore.tal.net/vx/opp/1"))
        self.assertTrue(talnet_enrich.is_talnet("https://tal.net/opp/1"))
        self.assertFalse(talnet_enrich.is_talnet("https://example.com/opp/1"))
        self.assertFalse(talnet_enrich.is_talnet("https://nottal.network/x"))
        self.assertFalse(talnet_enrich.is_talnet(""))

    def test_extracts_body_from_inside_form(self):
        # The bug: the generic _extract_text drops <form> content, so this body
        # would come back empty. The enricher must recover it.
        got = talnet_enrich.description("https://x.tal.net/opp/1", _Session(_FIXTURE))
        self.assertIn("What we are looking for", got)
        self.assertIn("Global Markets trading role", got)
        self.assertNotIn("Skip to content", got)
        self.assertNotIn("cookie", got.lower())

    def test_richtext_fallback_when_no_vac_desc(self):
        got = talnet_enrich.description("https://x.tal.net/opp/2", _Session(_FALLBACK))
        self.assertIn("Corporate audit summer analyst", got)

    def test_non_talnet_url_returns_empty(self):
        got = talnet_enrich.description("https://example.com/x", _Session(_FIXTURE))
        self.assertEqual(got, "")

    def test_fetch_failure_returns_empty(self):
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("network down")

        self.assertEqual(
            talnet_enrich.description("https://x.tal.net/opp/3", Boom()), "")


if __name__ == "__main__":
    unittest.main()
