"""Workday scope handling: legacy single scope and the scopes union."""
from unittest.mock import Mock, patch

from scrapers import workday


def response(postings):
    r = Mock()
    r.json.return_value = {"total": len(postings), "jobPostings": postings}
    return r


def posting(path, title):
    return {"externalPath": f"/job/x/{path}", "title": title, "locationsText": "", "postedOn": ""}


def test_legacy_config_sends_one_scope():
    session = Mock()
    session.post.return_value = response([posting("R1", "Trader")])
    with patch.object(workday, "make_session", return_value=session):
        jobs = workday.scrape({"tenant": "t", "board": "B",
                               "applied_facets": {"jobFamilyGroup": ["f"]},
                               "search_text": "Brand"})
    assert [j["id"] for j in jobs] == ["wd_t_R1"]
    sent = session.post.call_args.kwargs["json"]
    assert sent["appliedFacets"] == {"jobFamilyGroup": ["f"]}
    assert sent["searchText"] == "Brand"


def test_scopes_union_and_dedupe_by_path():
    session = Mock()
    session.post.side_effect = [
        response([posting("R1", "Controller"), posting("R2", "Trader")]),
        response([posting("R2", "Trader"), posting("R3", "Originator")]),
    ]
    with patch.object(workday, "make_session", return_value=session):
        jobs = workday.scrape({"tenant": "t", "board": "B", "scopes": [
            {"applied_facets": {"jobFamilyGroup": ["fin"]}},
            {"search_text": "trading"},
        ]})
    assert sorted(j["id"] for j in jobs) == ["wd_t_R1", "wd_t_R2", "wd_t_R3"]
    sent = [c.kwargs["json"] for c in session.post.call_args_list]
    assert sent[0]["appliedFacets"] == {"jobFamilyGroup": ["fin"]} and sent[0]["searchText"] == ""
    assert sent[1]["appliedFacets"] == {} and sent[1]["searchText"] == "trading"


def test_exclude_facets_subtracts_and_keeps_unfiled():
    facets = Mock()
    facets.json.return_value = {"total": 3, "jobPostings": [], "facets": [
        {"facetParameter": "jobFamilyGroup", "values": [
            {"id": "eng"}, {"id": "fin"}, {"id": "new"}]},
        {"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "values": [{"id": "de"}]}]},
    ]}
    session = Mock()
    session.post.side_effect = [
        # whole scope: a categorised controller, an engineer, an unfiled role
        response([posting("R1", "Controller"), posting("R2", "Engineer"),
                  posting("R3", "Unfiled Intern")]),
        facets,
        response([posting("R2", "Engineer")]),  # postings under 'eng'
    ]
    with patch.object(workday, "make_session", return_value=session):
        jobs = workday.scrape({"tenant": "t", "board": "B", "scopes": [
            {"exclude_facets": {"jobFamilyGroup": ["eng", "gone"]}}]})
    assert sorted(j["id"] for j in jobs) == ["wd_t_R1", "wd_t_R3"]
    # the stale id 'gone' is not sent: an unrecognised id could be ignored and
    # the subtraction would then empty the source
    assert session.post.call_args.kwargs["json"]["appliedFacets"] == {
        "jobFamilyGroup": ["eng"]}
