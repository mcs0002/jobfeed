"""Universia parsing contract: one organisation's postings off a shared board."""
import unittest
from unittest import mock

from scrapers import universia


def posting(**over):
    base = {
        "identifier": "de7aae80-117e-4faf-82a9-d3e5a38eeb24",
        "title": "Santander Future Talents: WM&I Summer Internship Program 2027",
        "url": "https://www.universia.net/es/empleo/de7aae80/asset-manager",
        "datePosted": "2026-09-08T18:09:48.562994+00:00",
        "validThrough": "2026-10-11T00:00:00+00:00",
        "description": "<p><strong>IT STARTS HERE</strong></p><p>Global markets.</p>",
        "requirements": "<p>WHAT YOU’LL BRING</p><p>Fluent English required.</p>",
        "jobLocation": {"address": {"addressLocality": "Madrid",
                                    "addressCountry": "ES"}},
    }
    base.update(over)
    return base


class Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class UniversiaTests(unittest.TestCase):
    def _scrape(self, pages):
        calls = []

        class Session:
            def get(self, url, params=None, headers=None, timeout=None):
                calls.append(params)
                return Response(pages[len(calls) - 1])

        with mock.patch.object(universia, "make_session", Session):
            jobs = universia.scrape("Santander Early Talent")
        return jobs, calls

    def test_one_organisation_is_requested_off_the_public_board(self):
        jobs, calls = self._scrape([{"results": [posting()], "total": 1}])
        self.assertEqual("Santander Early Talent", calls[0]["hiringOrganization"])
        self.assertEqual(universia.PUBLIC_BOARD, calls[0]["boards"])
        self.assertEqual(1, len(jobs))
        job = jobs[0]
        self.assertEqual("universia_de7aae80-117e-4faf-82a9-d3e5a38eeb24", job["id"])
        self.assertEqual("Madrid, ES", job["location"])
        self.assertEqual("2026-09-08", job["posted"])
        self.assertEqual("2026-10-11", job["deadline"])

    def test_the_requirements_block_is_kept_with_the_description(self):
        """The language, degree and start-date signals the tagger needs live in
        `requirements`, which is a separate HTML field from `description`."""
        jobs, _ = self._scrape([{"results": [posting()], "total": 1}])
        body = jobs[0]["description"]
        self.assertIn("IT STARTS HERE", body)
        self.assertIn("Fluent English required.", body)
        self.assertNotIn("<p>", body)

    def test_a_programme_recruiting_into_several_cities_lists_them_all(self):
        jobs, _ = self._scrape([{"results": [posting(jobLocation=[
            {"address": {"addressLocality": "Hong Kong", "addressCountry": "HK"}},
            {"address": {"addressLocality": "Singapore", "addressCountry": "SG"}},
        ])], "total": 1}])
        self.assertEqual("Hong Kong, HK | Singapore, SG", jobs[0]["location"])

    def test_a_posting_with_no_identifier_is_skipped_not_stored_blank(self):
        jobs, _ = self._scrape([{"results": [posting(identifier=""), posting()],
                                 "total": 2}])
        self.assertEqual(1, len(jobs))

    def test_paging_stops_on_a_short_page(self):
        jobs, calls = self._scrape([{"results": [posting()], "total": 1}])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(jobs))


if __name__ == "__main__":
    unittest.main()
