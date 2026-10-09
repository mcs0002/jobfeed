"""Regression checks for complete source pulls and genuine role descriptions."""
from unittest.mock import Mock, patch

import pytest
from bs4 import BeautifulSoup

from scrapers import generic, lear, orsted, verbund, successfactors_dwr


def response(payload=None, text=""):
    r = Mock(text=text)
    r.json.return_value = payload
    return r


def test_orsted_reads_all_pages_before_scoping(monkeypatch):
    monkeypatch.setattr(orsted, "PAGE_SIZE", 1)
    rows = [
        {"Link": "https://orsted.com/en/careers/vacancies/1-trader", "JobName": "Trader", "AreaOfWork": "Trading and Risk"},
        {"Link": "https://orsted.com/en/careers/vacancies/2-engineer", "JobName": "Engineer", "AreaOfWork": "Engineering"},
    ]
    session = Mock()
    session.post.side_effect = [response({"recordsFiltered": 2, "data": [row]}) for row in rows]
    with patch.object(orsted, "make_session", return_value=session):
        jobs = orsted.scrape({"item": "item", "country_filter_id": "country", "areas": ["Trading and Risk"]})
    assert [j["title"] for j in jobs] == ["Trader"]
    assert [c.kwargs["json"]["start"] for c in session.post.call_args_list] == [0, 1]


@pytest.mark.parametrize("payloads", [
    [{}],
    [{"recordsFiltered": 2, "data": []}],
    [{"recordsFiltered": 1, "data": [{"JobName": "Trader"}]}],
])
def test_orsted_does_not_report_broken_payload_as_empty(payloads):
    session = Mock()
    session.post.side_effect = [response(p) for p in payloads]
    with patch.object(orsted, "make_session", return_value=session), pytest.raises(RuntimeError):
        orsted.scrape({"item": "item", "country_filter_id": "country"})


def test_verbund_incomplete_pull_raises():
    session = Mock()
    session.get.return_value = response({"data": {"pages": 1, "resultsCount": 2, "jobs": [
        {"link": "https://example.onlyfy.jobs/job/abc123", "title": "Trader", "locations": ["Vienna"], "country": "Austria"}
    ]}})
    with patch.object(verbund, "make_session", return_value=session), pytest.raises(RuntimeError, match="reported 2"):
        verbund.scrape({"module_id": "module"})


def test_verbund_rejects_shell_payload():
    session = Mock()
    session.get.return_value = response({})
    with patch.object(verbund, "make_session", return_value=session), pytest.raises(RuntimeError, match="malformed"):
        verbund.scrape({"module_id": "module"})


def test_generic_pagination_deduplicates_overlap_and_preserves_ids():
    pages = [BeautifulSoup(t, "html.parser") for t in [
        '<a href="/a">Trader</a>', '<a href="/a">Trader</a><a href="/b">Risk Analyst</a>', ''
    ]]
    selectors = {"container": "a", "title": ":self", "link": ":self"}
    with patch.object(generic, "_fetch_soup", return_value=pages[0]):
        old = generic.scrape("https://example.org/jobs", selectors, "firm")
    with patch.object(generic, "_fetch_soup", side_effect=pages):
        jobs = generic.scrape("https://example.org/jobs", {**selectors, "page_param": "page"}, "firm")
    assert jobs[0]["id"] == old[0]["id"]
    assert len(jobs) == 2


def test_generic_page_limit_raises_instead_of_returning_partial():
    pages = [BeautifulSoup(f'<a href="/{n}">Job {n}</a>', "html.parser") for n in range(3)]
    with patch.object(generic, "_fetch_soup", side_effect=pages), pytest.raises(RuntimeError, match="pagination limit"):
        generic.scrape("https://example.org/jobs", {"container": "a", "title": ":self", "link": ":self", "page_param": "page", "max_pages": 3})


def test_sabadell_language_search_primes_before_listing():
    session = Mock()
    session.post.return_value = response(text='// DWR reply\ns0.detailURLPrefix="/career?career_job_req_id=";s0.postingCount="1";s1.title="Economist";s1.id=123;')
    with patch.object(successfactors_dwr, "make_session", return_value=session), patch.object(successfactors_dwr, "_token", return_value=("token", "https://example.org/career")):
        jobs = successfactors_dwr.scrape({"base_url": "https://example.org", "company": "bank", "keyword_languages": ["en_US", "es_ES"]})
    assert len(jobs) == 1
    prime, listing = session.post.call_args_list
    assert "searchJobs.dwr" in prime.args[0]
    assert "c0-l1=string:es_ES" in prime.kwargs["data"]
    assert "search.dwr" in listing.args[0]


def test_lear_keeps_each_role_description_separate():
    html = '<div class="main-page"><p><strong>Economist</strong></p><p>' + 'Economic research and modelling. ' * 20 + '</p><p>This position is reserved for <strong>protected categories</strong>.</p><p><strong>Data Analyst</strong></p><p>' + 'Data pipelines and statistics. ' * 20 + '</p></div>'
    session = Mock()
    session.get.return_value = response(text=html)
    with patch.object(lear, "make_session", return_value=session), patch.object(lear, "fix_encoding"):
        jobs = lear.scrape("https://example.org/vacancies")
    assert [j["title"] for j in jobs] == ["Economist", "Data Analyst"]
    assert "protected categories" in jobs[0]["description"]
    assert "Data pipelines" not in jobs[0]["description"]
    assert "Economic research" not in jobs[1]["description"]


def test_altays_reads_requirements_instead_of_short_jsonld():
    from scrapers.enrich import detail_then_http
    session = Mock()
    session.get.return_value = response(text='<script type="application/ld+json">{"@type":"JobPosting","description":"Short tasks only"}</script><div class="row job"><div class="job__left"><h2>Responsibilities</h2><p>Quantitative strategies.</p><h2>Requirements</h2><p>German and Python required.</p></div><aside>Location of the vacancy: Düsseldorf</aside></div>')
    with patch("scrapers.enrich.altays_enrich.fix_encoding"):
        body = detail_then_http({"url": "https://recrutement.altays-progiciels.com/oddo/en/offres/analyst.html"}, session)
    assert "German and Python required" in body
    assert "Short tasks only" not in body
    assert "Düsseldorf" in body
