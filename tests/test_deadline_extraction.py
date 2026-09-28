"""Closing dates read from schema.org validThrough on the posting page."""
import json
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scrapers.enrich.descriptions import (  # noqa: E402
    MAX_DEADLINE_DAYS, jobposting_deadline,
)

TODAY = date(2026, 9, 16)


def page(**over):
    node = {"@type": "JobPosting", "title": "Graduate Programme",
            "description": "<p>Body</p>"}
    node.update(over)
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(node) + "</script></head><body></body></html>")


class DeadlineExtractionTests(unittest.TestCase):
    def test_a_stated_future_date_is_read(self):
        html = page(validThrough="2026-10-04T00:00:00+00:00")
        self.assertEqual("2026-10-04", jobposting_deadline(html, today=TODAY))

    def test_a_posting_with_no_validthrough_yields_nothing(self):
        self.assertEqual("", jobposting_deadline(page(), today=TODAY))

    def test_seo_filler_far_in_the_future_is_refused(self):
        """Google requires the field on any posting that expires, so boards
        with no real deadline invent one: a sampled insurer stated 2028-07-28
        for a role posted in 2026."""
        html = page(validThrough="2028-07-28")
        self.assertEqual("", jobposting_deadline(html, today=TODAY))
        edge = TODAY + timedelta(days=MAX_DEADLINE_DAYS)
        self.assertEqual(edge.isoformat(),
                         jobposting_deadline(page(validThrough=edge.isoformat()),
                                             today=TODAY))

    def test_a_date_already_past_is_refused(self):
        """The board still lists the role, which contradicts the date. The web
        app hides a role past its deadline, so trusting it would make a live
        role disappear; declining leaves it visible."""
        self.assertEqual("", jobposting_deadline(page(validThrough="2026-09-15"),
                                                 today=TODAY))
        self.assertEqual("2026-09-16",
                         jobposting_deadline(page(validThrough="2026-09-16"),
                                             today=TODAY))

    def test_junk_in_the_field_is_ignored_not_guessed_at(self):
        for bad in ("", "soon", "31/12/2026", "2026-13-45", None):
            self.assertEqual("", jobposting_deadline(page(validThrough=bad),
                                                     today=TODAY), bad)

    def test_a_type_list_and_a_graph_wrapper_are_both_read(self):
        """Same JSON-LD quirks the description parser carries: TalentBrew ships
        @type as a list, Radancy wraps nodes in @graph."""
        listed = page(**{"@type": ["JobPosting", "Thing"],
                         "validThrough": "2026-10-04"})
        self.assertEqual("2026-10-04", jobposting_deadline(listed, today=TODAY))
        graph = ('<script type="application/ld+json">' + json.dumps(
            {"@graph": [{"@type": "WebPage"},
                        {"@type": "JobPosting", "validThrough": "2026-11-04"}]})
            + "</script>")
        self.assertEqual("2026-11-04", jobposting_deadline(graph, today=TODAY))

    def test_a_non_jobposting_node_never_supplies_a_deadline(self):
        other = ('<script type="application/ld+json">' + json.dumps(
            {"@type": "Event", "validThrough": "2026-10-04"}) + "</script>")
        self.assertEqual("", jobposting_deadline(other, today=TODAY))

    def test_unparseable_jsonld_is_skipped_without_raising(self):
        self.assertEqual("", jobposting_deadline(
            '<script type="application/ld+json">{not json</script>', today=TODAY))



class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _Session:
    """Serves one fixed page for every GET."""
    def __init__(self, text):
        self.text = text

    def get(self, *a, **k):
        return _Resp(self.text)


class DetailEnricherDeadlineTests(unittest.TestCase):
    """The JSON-LD detail enrichers parsed validThrough's own block for the
    description and dropped the date until 2026-09-24."""

    def setUp(self):
        self.when = (date.today() + timedelta(days=20)).isoformat()
        self.session = _Session(page(validThrough=self.when))

    def test_each_jsonld_enricher_reports_the_date(self):
        from scrapers.enrich import (glencore_enrich, jibe_enrich,
                                     societegenerale_enrich, talentbrew_enrich)
        cases = [
            (jibe_enrich, "https://careers.ice.com/jobs/12345"),
            (talentbrew_enrich, "https://jobs.example.com/job/london/analyst/1/2"),
        ]
        for mod, url in cases:
            out = {}
            mod.description(url, self.session, out=out)
            self.assertEqual(out.get("deadline"), self.when, mod.__name__)
        # The router passes the job dict through for the ones that take it.
        for mod in (glencore_enrich, societegenerale_enrich):
            self.assertIn(mod.description, __import__(
                "scrapers.enrich", fromlist=["_TAKES_OUT"])._TAKES_OUT)

    def test_detail_router_fills_the_job(self):
        from scrapers.enrich import detail_then_http
        job = {"url": "https://careers.ice.com/jobs/12345"}
        detail_then_http(job, self.session)
        self.assertEqual(job.get("deadline"), self.when)

    def test_no_out_dict_is_fine(self):
        from scrapers.enrich import jibe_enrich
        self.assertEqual("Body", jibe_enrich.description(
            "https://careers.ice.com/jobs/12345", self.session))



class _JsonSession:
    """Answers every GET/POST with one JSON payload."""
    def __init__(self, payload):
        self.payload = payload

    def get(self, *a, **k):
        return self

    post = get
    status_code = 200

    def json(self):
        return self.payload


class AtsEndDateTests(unittest.TestCase):
    """Workday endDate and Oracle ExternalPostedEndDate, stored since the
    2026-09-24 M1 probe showed them to be application windows."""

    def setUp(self):
        self.today = date.today()
        self.posted = self.today.isoformat()
        self.end = (self.today + timedelta(days=14)).isoformat()

    def test_a_whole_year_after_posting_is_a_default_not_a_deadline(self):
        from scrapers.enrich.descriptions import plausible_deadline
        year = (self.today + timedelta(days=365)).isoformat()
        self.assertEqual("", plausible_deadline(year, self.posted))
        self.assertEqual(year, plausible_deadline(year))  # no posting date: cap only
        self.assertEqual(self.end, plausible_deadline(self.end, self.posted))
        self.assertEqual("", plausible_deadline("2020-01-01", self.posted))

    def test_workday_end_date_is_reported(self):
        from scrapers.enrich.workday_enrich import WorkdayEnricher
        enr = WorkdayEnricher()
        payload = {"jobPostingInfo": {"jobDescription": "<p>Body</p>",
                                      "startDate": self.posted, "endDate": self.end}}
        enr._sessions[("https://x.wd3.myworkdayjobs.com", "Board")] = _JsonSession(payload)
        out = {}
        text = enr.description("https://x.wd3.myworkdayjobs.com/Board/job/London/Analyst_R1",
                               "x", "Board", out=out)
        self.assertEqual("Body", text)
        self.assertEqual(self.end, out.get("deadline"))

    def test_oracle_end_date_is_reported_and_defaults_refused(self):
        from scrapers.enrich import oracle_enrich
        url = "https://x.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123"
        for end, want in ((self.end, self.end),
                          ((self.today + timedelta(days=365)).isoformat(), None)):
            out = {}
            oracle_enrich.description(url, _JsonSession({"items": [{
                "ExternalDescriptionStr": "<p>Body</p>",
                "ExternalPostedStartDate": self.posted,
                "ExternalPostedEndDate": end}]}), out=out)
            self.assertEqual(want, out.get("deadline"))


if __name__ == "__main__":
    unittest.main()
