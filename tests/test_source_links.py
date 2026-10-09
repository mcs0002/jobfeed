"""Sources catalogue links must represent the scraper's configured scope."""
import json
import unittest
from pathlib import Path

from web.app import _scope_model, _source_board_url


ROOT = Path(__file__).resolve().parents[1]


def _target(name: str) -> dict:
    targets = json.loads((ROOT / "targets.json").read_text())
    return next(t for t in targets if t["name"] == name)


class SourceLinkTests(unittest.TestCase):
    def test_koch_links_to_its_supply_and_trading_facet(self):
        target = _target("Koch Supply & Trading")

        self.assertEqual(_scope_model(target), ("Positive facet", "facet"))
        self.assertEqual(_source_board_url(target), target["scope_url"])
        for key, value in target["koch_avature"]["filter_params"].items():
            self.assertIn(f"{key}={value}", target["scope_url"])

    def test_scoped_board_takes_priority_over_broad_career_landing(self):
        target = {
            "career_url": "https://example.test/all-jobs",
            "scope_url": "https://example.test/all-jobs?division=trading",
        }

        self.assertEqual(_source_board_url(target), target["scope_url"])

    def test_other_shareable_division_facets_have_scoped_links(self):
        for name in ("Munich Re", "ERGO Group"):
            target = _target(name)
            self.assertEqual(_source_board_url(target), target["search_url"])

    def test_every_filtered_koch_board_declares_shareable_scope_url(self):
        targets = json.loads((ROOT / "targets.json").read_text())
        filtered = [
            target for target in targets
            if (target.get("koch_avature") or {}).get("filter_params")
        ]

        self.assertTrue(filtered)
        self.assertTrue(all(target.get("scope_url") for target in filtered))

    def test_explicit_scope_types_are_rendered_consistently(self):
        targets = json.loads((ROOT / "targets.json").read_text())
        expected = {
            "facet": ("Positive facet", "facet"),
            "search": ("Search scope", "search"),
        }

        explicit = [target for target in targets if target.get("scope_type")]
        self.assertTrue(explicit)
        for target in explicit:
            self.assertEqual(_scope_model(target), expected[target["scope_type"]])


if __name__ == "__main__":
    unittest.main()
