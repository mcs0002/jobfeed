import json
from datetime import date

from jobfeed import campus, campus_targeted as ct

TODAY = date(2026, 9, 30)


def _job(title, desc="", deadline=""):
    return {"id": "j1", "company": "Capstone", "category": "Systematic HFs & Quant AMs",
            "title": title, "location": "London", "url": "https://x/j1",
            "description": desc, "deadline": deadline}


def test_fits_needs_a_verified_quote():
    job = _job("Investment Internship", "A ten-week programme in London.")
    res = {"kind": "internship", "timing": "fits", "start": "summer 2027",
           "start_quote": "Summer 2027 internship"}  # not in the posting
    rec = ct.decide(job, res, TODAY)
    assert rec["timing"] == "unstated" and not rec["keep"]


def test_title_counts_as_start_evidence():
    job = _job("Summer 2027 - Investment Internship LDN", "Ten weeks.")
    res = {"kind": "internship", "timing": "fits", "start_quote": "Summer 2027 - Investment Internship"}
    rec = ct.decide(job, res, TODAY)
    assert rec["keep"] and rec["status"] == "open"
    assert rec["quote"] == "Summer 2027 - Investment Internship"


def test_clash_is_dropped():
    job = _job("Stage Equity Sales (Janvier 2027)", "Début: janvier 2027")
    rec = ct.decide(job, {"kind": "internship", "timing": "clashes"}, TODAY)
    assert not rec["keep"]


def test_unstated_kept_only_for_a_2027_titled_programme():
    res = {"kind": "graduate_programme", "timing": "unstated"}
    assert ct.decide(_job("2027 Graduate Programme"), res, TODAY)["keep"]
    assert not ct.decide(_job("Graduate Programme (January 2027)"), res, TODAY)["keep"]
    assert not ct.decide(_job("Junior Trader"), {"kind": "job", "timing": "unstated"}, TODAY)["keep"]
    assert ct.decide(_job("Quantitative Research Analyst – University Graduate (Europe)"),
                     {"kind": "job", "timing": "unstated"}, TODAY)["keep"]
    assert not ct.decide(_job("2027 Summer Analyst Program"),
                         {"kind": "internship", "timing": "unstated"}, TODAY)["keep"]


def test_past_deadline_is_dropped():
    job = _job("2027 Graduate Programme", deadline="2026-09-01")
    rec = ct.decide(job, {"kind": "graduate_programme", "timing": "unstated"}, TODAY)
    assert not rec["keep"]


def test_board_rows_merge_into_the_tab(tmp_path, monkeypatch):
    season = tmp_path / "2026-09"
    season.mkdir()
    (season / "results.jsonl").write_text("")
    (tmp_path / "targeted.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"job_id": "a", "keep": True, "status": "open", "firm": "Capstone",
         "title": "Summer 2027 Internship", "location": "London", "url": "https://x/a",
         "quote": "Summer 2027", "start": "summer 2027"},
        {"job_id": "b", "keep": False, "firm": "ODDO", "title": "Stage", "url": "https://x/b"},
    ]) + "\n")
    monkeypatch.setattr(campus, "OUT_ROOT", tmp_path)
    data = campus.load_rows(season, TODAY)
    assert [r["firm"] for r in data["rows"]] == ["Capstone"]
    row = data["rows"][0]
    assert row["source"] == "board" and row["key"].startswith("board|")
    assert data["counts"]["open"] == 1


def test_record_is_json_serialisable():
    rec = ct.decide(_job("2027 Graduate Programme"),
                    {"kind": "graduate_programme", "timing": "unstated"}, TODAY)
    assert rec["keep"] is True
    json.dumps(rec)


def test_offcycle_for_graduates_is_kept_without_a_start():
    res = {"kind": "internship", "timing": "unstated"}
    ok = _job("2027 Institutional Equities Trading Off-Cycle Internship (London)",
              "You are a graduate of 2026 or graduating in 2027.")
    assert ct.decide(ok, res, TODAY)["keep"]
    back = _job("Off-Cycle Internship", "Graduating in 2027 and returning to university afterwards.")
    assert not ct.decide(back, res, TODAY)["keep"]
    both = _job("2027 Sales and Trading Off-Cycle Internship (London)",
                "You are a recent graduate or graduating in 2027 or 2028.")
    assert ct.decide(both, res, TODAY)["keep"]
    old = _job("2026 | EMEA | Off Cycle Internship", "recent graduates welcome")
    assert not ct.decide(old, res, TODAY)["keep"]


def test_new_analyst_title_is_a_graduate_hire():
    rec = ct.decide(_job("2027 | EMEA | London | FICC and Equities | New Analyst"),
                    {"kind": "job", "timing": "unstated"}, TODAY)
    assert rec["keep"]
    old = ct.decide(_job("2026 | EMEA | Frankfurt | FICC and Equities | New Analyst"),
                    {"kind": "job", "timing": "unstated"}, TODAY)
    assert not old["keep"]


def test_dated_internship_for_current_students_is_dropped():
    job = _job("2027 Off-Cycle Internship (Paris)", "January or July 2027. You are currently in study.")
    res = {"kind": "internship", "timing": "fits", "start_quote": "January or July 2027"}
    assert not ct.decide(job, res, TODAY)["keep"]
