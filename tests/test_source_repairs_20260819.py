"""Regression tests for the 2026-08-19 source sweep repairs.

The sweep found 5 hard fails across 433 verified sources. Four were real and
are pinned here; the fifth (Rokos) is a dead slug awaiting a new one.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import citadel, umantis


class _Resp:
    def __init__(self, text, url="https://x.umantis.com/Jobs/All"):
        self.text = text
        self.url = url
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        pass


_VACANCY_ROW = (
    '<a class="HSTableLinkSubTitle" href="/Vacancies/{id}/Description/2">{t}</a>'
)


class UmantisTests(unittest.TestCase):
    """Quoniam's tenant started serving /Jobs/All as a language picker while
    J. Safra Sarasin still serves the board directly. Both must work, and an
    empty board must be distinguishable from a layout change."""

    cfg = {"base_url": "https://x.umantis.com", "tenant": "t"}

    def _run(self, pages: dict):
        def _get(url, **kw):
            for frag, body in pages.items():
                if frag in url:
                    return _Resp(body, url=url)
            raise AssertionError(f"unexpected fetch: {url}")
        with patch.object(umantis, "make_session",
                          return_value=type("S", (), {"get": staticmethod(_get)})()):
            return umantis.scrape(self.cfg)

    def test_direct_board_still_parses(self):
        """The J. Safra Sarasin shape — vacancies straight off /Jobs/All."""
        jobs = self._run({"/Jobs/All": "<html>"
                          + _VACANCY_ROW.format(id=506, t="Stewardship Specialist")
                          + _VACANCY_ROW.format(id=505, t="Risk Officer") + "</html>"})
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["id"] for j in jobs}, {"umantis_t_506", "umantis_t_505"})
        self.assertIn("Stewardship Specialist", [j["title"] for j in jobs])

    def test_language_picker_is_followed(self):
        """The Quoniam shape — /Jobs/All only links per-language boards."""
        jobs = self._run({
            "/Jobs/All": '<html><a href="/Jobs/2?lang=eng">EN</a>'
                         '<a href="/Jobs/31?lang=gerger">DE</a></html>',
            "/Jobs/2": "<html>" + _VACANCY_ROW.format(id=7, t="Portfolio Manager") + "</html>",
            "/Jobs/31": "<html>" + _VACANCY_ROW.format(id=8, t="Werkstudent PM") + "</html>",
        })
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["id"] for j in jobs}, {"umantis_t_7", "umantis_t_8"})

    def test_empty_marker_is_trusted_empty(self):
        """Umantis's own prose is the ONLY thing that turns zero anchors into
        a clean [] — otherwise a layout change would silently purge the firm."""
        for marker in ("There are no entries that could be displayed.",
                       "Es wurden noch keine Einträge erfasst, die hier "
                       "angezeigt werden könnten."):
            with self.subTest(marker=marker[:30]):
                jobs = self._run({
                    "/Jobs/All": '<html><a href="/Jobs/2?lang=eng">EN</a></html>',
                    "/Jobs/2": f"<html>{marker}</html>",
                })
                self.assertEqual(jobs, [])

    def test_unrecognised_layout_still_fails_loud(self):
        with self.assertRaises(RuntimeError):
            self._run({"/Jobs/All": "<html><body>totally different page</body></html>"})


class CitadelEmptyPageRetryTests(unittest.TestCase):
    """Page 5 of 5 came back empty on 2026-08-19, so the run collected exactly
    40 of 48 and tripped the completeness raise. An immediate re-fetch had all
    8 — retry once before believing a page is empty."""

    def _card(self, slug):
        return (f'<a class="careers-listing-card" href="/careers/details/{slug}/">'
                f'<div class="careers-listing-card__title"><h2>{slug}</h2></div></a>')

    def _page(self, slugs, total=48):
        return (f'<html><span class="total-post">{total}</span>'
                + "".join(self._card(s) for s in slugs) + "</html>")

    def test_transient_empty_page_is_retried(self):
        pages = {n: self._page([f"r{n}{i}" for i in range(10)]) for n in range(1, 5)}
        pages[5] = self._page([f"r5{i}" for i in range(8)])
        calls = {"n": 0}

        def _fetch(session, url):
            n = 1 if url.endswith("open-opportunities/") else int(url.rstrip("/").rsplit("/", 1)[-1])
            if n == 5:
                calls["n"] += 1
                if calls["n"] == 1:
                    return self._page([])      # the empty render
            return pages[n]

        with patch.object(citadel, "_fetch_listing_page", _fetch), \
             patch.object(citadel, "RETRY_PAUSE_SECONDS", 0), \
             patch.object(citadel.creq, "Session", lambda **kw: object()):
            jobs = citadel.scrape({"base_url": "https://www.citadel.com",
                                   "prefix": "citadel"})
        self.assertEqual(len(jobs), 48)
        self.assertEqual(calls["n"], 2, "page 5 should have been fetched twice")

    def test_persistently_short_listing_still_raises(self):
        def _fetch(session, url):
            n = 1 if url.endswith("open-opportunities/") else int(url.rstrip("/").rsplit("/", 1)[-1])
            return self._page([f"r{n}{i}" for i in range(10)] if n <= 4 else [])

        with patch.object(citadel, "_fetch_listing_page", _fetch), \
             patch.object(citadel, "RETRY_PAUSE_SECONDS", 0), \
             patch.object(citadel.creq, "Session", lambda **kw: object()):
            with self.assertRaises(RuntimeError):
                citadel.scrape({"base_url": "https://www.citadel.com",
                                "prefix": "citadel"})


if __name__ == "__main__":
    unittest.main()
