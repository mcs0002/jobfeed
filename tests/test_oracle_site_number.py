import unittest
from unittest.mock import MagicMock

from scrapers import oracle_hcm


def _session(page):
    s = MagicMock()
    s.get.return_value = MagicMock(text=page, raise_for_status=lambda: None)
    return s


class SiteNumberTests(unittest.TestCase):
    def test_a_number_is_used_as_is_without_a_request(self):
        s = _session("")
        self.assertEqual(oracle_hcm._site_number("https://x", "CX_1001", s), "CX_1001")
        s.get.assert_not_called()

    def test_a_url_name_resolves_from_its_page(self):
        s = _session('{"siteNumber":"CX_2","siteCode":"CX_2"}')
        self.assertEqual(oracle_hcm._site_number("https://x", "LazardStudentCareers", s), "CX_2")

    def test_ambiguous_or_missing_number_refuses_instead_of_scraping_the_tenant(self):
        for page in ("no number here", "CX_1 and CX_2"):
            with self.assertRaises(RuntimeError):
                oracle_hcm._site_number("https://x", "SomeName", _session(page))


if __name__ == "__main__":
    unittest.main()
