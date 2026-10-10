"""BNP entity exclusion (2026-10-10): subtraction after the full sweep."""
from unittest.mock import patch

from scrapers import bnpparibas_paced as bnp


def card(href, title, entity):
    logo = f'<div class="offer-logo"><img alt="{entity}"/></div>' if entity else ""
    return (f'<article class="card-offer"><a class="card-link" href="{href}">'
            f'<h3 class="title-4">{title}</h3>{logo}</a></article>')


def test_parse_reads_entity_from_logo_alt():
    jobs = bnp._parse_jobs(card("/a", "Trader", "BNP Paribas Corporate & Institutional Banking"))
    assert jobs[0]["_entity"] == "BNP Paribas Corporate & Institutional Banking"


def test_exclude_entities_keeps_unassigned_and_drops_listed():
    rows = [{"id": "1", "_entity": "French Retail Banking"},
            {"id": "2", "_entity": "BNP Paribas Corporate & Institutional Banking"},
            {"id": "3", "_entity": ""}]
    with patch.object(bnp, "_scrape_all", return_value=rows):
        jobs = bnp.scrape(exclude_entities=["French Retail Banking"])
    assert [j["id"] for j in jobs] == ["2", "3"]
    assert all("_entity" not in j for j in jobs)
