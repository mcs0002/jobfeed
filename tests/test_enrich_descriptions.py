import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.enrich import descriptions


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _Session:
    def __init__(self, text):
        self._text = text

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        return _Resp(self._text)


# The actual UniCredit CIB (Avature) incident, 2026-07-08: a plain 200 page,
# short, with the outage phrase. Real Avature markup wraps this in nav/footer
# chrome that _extract_text strips away, so the stored junk was exactly this
# short.
_MAINTENANCE_PAGE = """
<html><body>
  <nav>Careers</nav>
  <div class="article__header__text">
    <h1>Junior Firmenkundenbetreuer:in (w/m/d)</h1>
    <p>Maintenance</p>
    <p>We're sorry, the system is currently undergoing maintenance. Please
    check back later.</p>
  </div>
  <footer>© UniCredit</footer>
</body></html>
"""

# A long, real JD that happens to namedrop "scheduled maintenance" in passing
# (a facilities/ops-adjacent role) — must NOT be rejected just for containing
# the phrase.
_REAL_JD_MENTIONING_MAINTENANCE = """
<html><body>
  <div class="article__header__text">
    <h1>Corporate Banking Analyst</h1>
    <p>""" + ("We are looking for an analyst to join our corporate banking "
               "coverage team. You will support relationship managers on "
               "client onboarding, credit analysis and portfolio reviews. "
               ) * 20 + """
    Note: our systems undergo scheduled maintenance every second Sunday of
    the month; access to internal tools may be briefly limited during that
    window, but this does not affect client-facing responsibilities.
    </p>
  </div>
</body></html>
"""

# A normal short JD with no outage phrasing — must be persisted, same as
# today, so the guard doesn't regress ordinary short postings.
_NORMAL_SHORT_JD = """
<html><body>
  <div class="article__header__text">
    <h1>Graduate Analyst</h1>
    <p>Join our graduate program in Milan. You will rotate across corporate
    and investment banking desks over 18 months, working alongside senior
    bankers on live transactions.</p>
  </div>
</body></html>
"""


class OutageGuardTests(unittest.TestCase):
    def test_outage_body_rejected_not_persisted(self):
        got = descriptions.enrich_one(
            "https://careers.unicredit.eu/en_GB/jobsuche/JobDetail/1",
            _Session(_MAINTENANCE_PAGE),
        )
        self.assertEqual(got, "")

    def test_long_real_jd_mentioning_maintenance_is_persisted(self):
        got = descriptions.enrich_one(
            "https://careers.example.com/job/1",
            _Session(_REAL_JD_MENTIONING_MAINTENANCE),
        )
        self.assertNotEqual(got, "")
        self.assertIn("corporate banking", got.lower())

    def test_normal_short_jd_without_outage_phrases_is_persisted(self):
        got = descriptions.enrich_one(
            "https://careers.example.com/job/2",
            _Session(_NORMAL_SHORT_JD),
        )
        self.assertNotEqual(got, "")
        self.assertIn("Graduate Analyst", got)

    def test_reject_if_outage_helper_directly(self):
        self.assertTrue(descriptions._reject_if_outage(
            "We're sorry, the system is currently undergoing maintenance.",
            "https://careers.unicredit.eu/en_GB/jobsuche/JobDetail/1",
        ))
        self.assertFalse(descriptions._reject_if_outage(
            "x" * 2000 + " scheduled maintenance " + "y" * 2000,
            "https://careers.example.com/job/1",
        ))
        self.assertFalse(descriptions._reject_if_outage(
            "A normal short job description with no outage phrase at all.",
            "https://careers.example.com/job/2",
        ))
        self.assertFalse(descriptions._reject_if_outage("", "https://x"))


if __name__ == "__main__":
    unittest.main()
