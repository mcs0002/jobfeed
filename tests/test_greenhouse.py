"""Greenhouse department scoping."""
from unittest.mock import Mock, patch

from scrapers import greenhouse


def board(*jobs):
    session = Mock()
    session.get.return_value.json.return_value = {"jobs": [
        {"id": i, "title": t, "departments": [{"name": d} for d in depts]}
        for i, t, depts in jobs]}
    return session


JOBS = [(1, "Controller", ["Finance"]), (2, "Engineer", ["Development"]),
        (3, "Analyst", ["Development", "Finance"]), (4, "Unfiled", [])]


def scrape(**kw):
    with patch.object(greenhouse, "make_session", return_value=board(*JOBS)):
        return sorted(j["id"] for j in greenhouse.scrape("x", **kw))


def test_no_filter_keeps_everything():
    assert scrape() == ["gh_1", "gh_2", "gh_3", "gh_4"]


def test_include_keeps_any_listed_department_and_unfiled():
    assert scrape(departments=["Finance"]) == ["gh_1", "gh_3", "gh_4"]


def test_exclude_drops_only_fully_excluded_jobs():
    assert scrape(exclude_departments=["Development"]) == ["gh_1", "gh_3", "gh_4"]
