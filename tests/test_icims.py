"""iCIMS listing parser: title labels and per-tenant id scoping."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import icims  # noqa: E402

PAGE = """
<a href="https://ug-chire.icims.com/jobs/4087/summer-analyst/job?in_iframe=1"
   class="iCIMS_Anchor" title="4087 - Summer Analyst">
  <span class="sr-only field-label">Title</span><h3>Summer Analyst</h3></a>
<a href="https://ug-chire.icims.com/jobs/4100/title-insurance-analyst/job?in_iframe=1"
   class="iCIMS_Anchor"><span class="sr-only field-label">Job Title</span>
  <h3>Title Insurance Analyst</h3></a>
"""


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.pages = [PAGE, ""]

    def get(self, url, **kw):
        return _Resp(self.pages.pop(0) if self.pages else "")


class IcimsParseTests(unittest.TestCase):
    def _scrape(self, **kw):
        with patch.object(icims, "make_session", _Session):
            return icims.scrape("https://ug-chire.icims.com", **kw)

    def test_label_markup_is_not_part_of_the_title(self):
        titles = [j["title"] for j in self._scrape()]
        # The label element is dropped, not a text prefix: a real title that
        # starts with "Title" survives intact.
        self.assertEqual(titles, ["Summer Analyst", "Title Insurance Analyst"])

    def test_ids_are_bare_without_scope_and_namespaced_with_it(self):
        self.assertEqual([j["id"] for j in self._scrape()],
                         ["icims_4087", "icims_4100"])
        self.assertEqual([j["id"] for j in self._scrape(id_scope="cornerstone")],
                         ["icims_cornerstone_4087", "icims_cornerstone_4100"])

    def test_url_drops_the_iframe_flag(self):
        self.assertEqual(self._scrape()[0]["url"],
                         "https://ug-chire.icims.com/jobs/4087/summer-analyst/job")


if __name__ == "__main__":
    unittest.main()
