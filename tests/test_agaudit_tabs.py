import importlib.util
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "agaudit", Path(os.path.dirname(__file__)).parent / "scripts" / "agaudit.py")
agaudit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agaudit)


def call(step, name, **args):
    return {"step_index": step, "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": f"mcp_chrome_devtools_{name}", "args": args}]}


def result(step, selected, others=()):
    lines = [f"{n}: https://other/{n}" for n in others] + [f"{selected}: https://x/{selected} [selected]"]
    return {"step_index": step, "type": "GENERIC", "content": "## Pages\n" + "\n".join(lines)}


class TabOwnershipTests(unittest.TestCase):
    def test_attempt_id_is_read_from_the_opening_message(self):
        run_id = "run_0123456789abcdef0123456789abcdef"
        rows = [{"content": f"Use /assisted-apply. Attempt {run_id}."}]
        self.assertEqual(run_id, agaudit.run_of("missing-conversation", rows))

    def test_fresh_tab_then_work_is_clean(self):
        rows = [call(1, "list_pages"), result(2, 22),
                call(3, "new_page", url="https://ats/job"), result(4, 23, others=[22]),
                call(5, "click", uid="1_2"), result(6, 23), call(7, "navigate_page", url="u"),
                result(8, 23)]
        found = agaudit.tab_ownership(rows)
        self.assertEqual([23], found["owned"])
        self.assertEqual([], found["violations"])

    def test_adopting_the_preselected_tab_is_one_finding(self):
        # Gunvor 2026-09-18: list_pages, then navigate_page on Glencore's tab.
        rows = [call(1, "list_pages"), result(2, 22), call(3, "navigate_page", url="u"),
                result(4, 22), call(5, "click", uid="a"), call(7, "fill", uid="b", value="v")]
        found = agaudit.tab_ownership(rows)
        self.assertEqual([], found["owned"])
        self.assertEqual(1, len(found["violations"]))
        self.assertIn("preselected", found["violations"][0])

    def test_selecting_or_closing_a_foreign_tab(self):
        rows = [call(1, "new_page", url="u"), result(2, 30, others=[22]),
                call(3, "select_page", pageId=22), call(5, "close_page", pageId=22)]
        found = agaudit.tab_ownership(rows)
        self.assertEqual(2, len(found["violations"]))

    def test_a_second_own_tab_is_allowed(self):
        # Aurora opened the firm's about page in a new tab for a firm question.
        rows = [call(1, "new_page", url="job"), result(2, 30),
                call(3, "new_page", url="about"), result(4, 31, others=[30]),
                call(5, "select_page", pageId=30), result(6, 30)]
        found = agaudit.tab_ownership(rows)
        self.assertEqual([30, 31], found["owned"])
        self.assertEqual([], found["violations"])

    def test_action_landing_on_a_foreign_tab(self):
        rows = [call(1, "new_page", url="u"), result(2, 30),
                call(3, "click", uid="x"), result(4, 22, others=[30])]
        found = agaudit.tab_ownership(rows)
        self.assertEqual(1, len(found["violations"]))
        self.assertIn("landed on tab 22", found["violations"][0])


if __name__ == "__main__":
    unittest.main()


class OutsideReadTests(unittest.TestCase):
    def _calls(self, *paths):
        rows = [{"step_index": i + 1, "tool_calls": [{"name": "view_file",
                 "args": {"AbsolutePath": pth}}]} for i, pth in enumerate(paths)]
        return list(agaudit.tool_calls(rows))

    def test_granted_files_and_own_snapshots_are_not_findings(self):
        calls = self._calls(
            "/Users/x/projects/job_scraper/skills/assisted-apply/SKILL.md",
            "/Users/x/projects/job_scraper/secrets/applicant_profile.json",
            "/Users/x/.gemini/antigravity/brain/c/.tempmediaStorage/snapshot_full_1.txt")
        self.assertEqual(agaudit.outside_reads(calls), [])

    def test_reading_a_document_is_a_finding_with_its_step(self):
        # World Bank, 2026-09-23: the transcript PDF, step 303.
        calls = self._calls(
            "/Users/x/projects/job_scraper/skills/assisted-apply/SKILL.md",
            "/Users/x/Library/Mobile Documents/cv/Bewerbungsunterlagen/Record_of_Results.pdf")
        self.assertEqual(agaudit.outside_reads(calls),
                         [(2, "/Users/x/Library/Mobile Documents/cv/Bewerbungsunterlagen/Record_of_Results.pdf")])
