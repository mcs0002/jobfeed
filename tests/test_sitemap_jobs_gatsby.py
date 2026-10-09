import json
import unittest

from scrapers import sitemap_jobs

URL = "https://www.example.com/en-be/careers/2565-internship-equity-analyst"


def _page(node):
    return json.dumps({"result": {"data": {"job": node}}})


class GatsbyDetailTests(unittest.TestCase):
    def test_fills_title_body_location_and_date(self):
        node = {
            "translations": [{"extLocale": "en",
                              "externalTitle": "Internship - Equity Analyst",
                              "externalJobDescription": "<p>Analyse listed companies.</p>",
                              "extJobDescHeader": "", "extJobDescFooter": ""}],
            "locationList": [{"city": "Brussels"}],
            "country": "Belgium",
            "createdDateTime": "/Date(1791272783000)/",
        }
        asked = []

        def fetch(url):
            asked.append(url)
            return _page(node)

        job = {"title": "Internship Equity Analyst", "url": URL,
               "location": "", "description": "", "posted": ""}
        sitemap_jobs._gatsby_detail(job, fetch)
        self.assertEqual(asked, ["https://www.example.com/page-data/en-be/careers/"
                                 "2565-internship-equity-analyst/page-data.json"])
        self.assertEqual(job["title"], "Internship - Equity Analyst")
        self.assertIn("Analyse listed companies.", job["description"])
        self.assertEqual(job["location"], "Brussels, Belgium")
        self.assertEqual(job["posted"], "2026-10-06")

    def test_failure_leaves_the_slug_row_untouched(self):
        def fetch(url):
            raise OSError("down")

        job = {"title": "Internship Equity Analyst", "url": URL,
               "location": "", "description": "", "posted": ""}
        before = dict(job)
        sitemap_jobs._gatsby_detail(job, fetch)
        self.assertEqual(job, before)


if __name__ == "__main__":
    unittest.main()
