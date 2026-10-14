"""Description gaps found 2026-10-14: Cloudflare-challenged pages, Jibe
postings with no English version, Point72's title-only Greenhouse content."""
import json
from unittest.mock import Mock, patch

from scrapers import greenhouse, jibe
from scrapers.enrich import descriptions

POSTING = ("<html><body><h1>PEPI Consultant</h1><p>"
           + "Own analytical modules in commercial due diligence. " * 20
           + "</p></body></html>")


def response(status=200, text="", headers=None):
    r = Mock()
    r.status_code, r.text, r.headers = status, text, headers or {}
    return r


CHALLENGE = response(403, '<title>Just a moment...</title>'
                     '<script src="https://challenges.cloudflare.com/x.js"></script>',
                     {"cf-mitigated": "challenge"})


def test_cloudflare_challenge_is_retried_through_curl_cffi():
    session = Mock()
    session.get.return_value = CHALLENGE
    with patch.object(descriptions, "_cffi_get", return_value=response(200, POSTING)) as cffi:
        text = descriptions.enrich_one("https://careers.alvarezandmarsal.com/jobs/1-x", session)
    cffi.assert_called_once()
    assert "commercial due diligence" in text


def test_other_403_is_not_retried():
    session = Mock()
    session.get.return_value = response(403, "Access Denied")
    with patch.object(descriptions, "_cffi_get") as cffi:
        assert descriptions.enrich_one("https://example.com/job/1", session) == ""
    cffi.assert_not_called()


def test_failed_retry_is_a_failed_fetch():
    session = Mock()
    session.get.return_value = CHALLENGE
    with patch.object(descriptions, "_cffi_get", return_value=None):
        assert descriptions.enrich_one("https://www.citadel.com/careers/details/x/", session) == ""


def test_jibe_url_carries_the_posting_language():
    session = Mock()
    session.get.return_value.json.return_value = {"totalCount": 2, "jobs": [
        {"data": {"req_id": "27618", "title": "Data Scientist", "language": "de-de"}},
        {"data": {"req_id": "13711", "title": "Analyst"}}]}
    with patch.object(jibe, "make_session", return_value=session):
        urls = {j["id"]: j["url"] for j in jibe.scrape({"base_url": "https://jobs.axa.com"})}
    assert urls["jibe_company_27618"] == "https://jobs.axa.com/jobs/27618?lang=de-de"
    assert urls["jibe_company_13711"] == "https://jobs.axa.com/jobs/13711"


def salesforce_page(records):
    """careers.point72.com's shape: the records as JSON, inside a JS
    single-quoted string, with `<` as \\u003C and apostrophes as \\&#39;."""
    literal = json.dumps(json.dumps(records))[1:-1]
    literal = literal.replace("<", "\\u003C").replace("&#39;", "\\&#39;")
    return f"<script>CSSearchModule.init('{literal}', 'x');</script>"


BODY = ("<h3>Equity Financing Trader</h3><p>Manage the firm&#39;s securities lending "
        "book across Asia Pacific.</p>" * 10)
RECORDS = [
    {"job": {"Name": "Equity Financing Trader", "Job_Description_External__c": BODY,
             "Apply_Now_URL__c": "https://boards.greenhouse.io/point72/jobs/8780028002?gh_jid=8780028002"}},
    {"job": {"Name": "Elsewhere", "Job_Description_External__c": "<p>Other ATS</p>",
             "Apply_Now_URL__c": "https://example.com/apply/1"}},
]


def test_salesforce_bodies_parses_the_embedded_records():
    session = Mock()
    session.get.return_value = response(200, salesforce_page(RECORDS))
    with patch.object(greenhouse, "make_session", return_value=session):
        bodies = greenhouse._salesforce_bodies("https://careers.point72.com/")
    assert list(bodies) == ["8780028002"]
    assert "the firm's securities lending" in bodies["8780028002"]


def test_point72_short_content_is_replaced_real_content_is_kept():
    long_content = "<p>" + "Build systematic models. " * 20 + "</p>"
    board = Mock()
    board.get.return_value.json.return_value = {"jobs": [
        {"id": 8780028002, "title": "Equity Financing Trader",
         "content": "&lt;p&gt;Equity Financing Trader&lt;/p&gt;"},
        {"id": 1, "title": "Quant", "content": long_content}]}
    site = Mock()
    site.get.return_value = response(200, salesforce_page(
        RECORDS + [{"job": {"Job_Description_External__c": "<p>shorter</p>",
                            "Apply_Now_URL__c": "https://boards.greenhouse.io/point72/jobs/1"}}]))
    with patch.object(greenhouse, "make_session", side_effect=[board, site]):
        jobs = {j["id"]: j for j in greenhouse.scrape(
            "point72", site_bodies="https://careers.point72.com/")}
    assert "securities lending" in jobs["gh_8780028002"]["description"]
    assert jobs["gh_1"]["description"].startswith("Build systematic models.")


def test_no_site_fetch_without_site_bodies():
    board = Mock()
    board.get.return_value.json.return_value = {"jobs": [
        {"id": 1, "title": "Analyst", "content": "&lt;p&gt;Analyst&lt;/p&gt;"}]}
    with patch.object(greenhouse, "make_session", return_value=board) as mk:
        greenhouse.scrape("x")
    assert mk.call_count == 1
