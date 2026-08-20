"""Invariant guard for apply.py: the tool must never auto-submit applications.

The docstring guarantees: "There is no .click() on any submit/apply button and
no form .submit() anywhere."  This test reads apply.py as source text and
asserts those patterns are absent, so the contract is enforceable in CI and
future edits that break it are caught immediately.

The checks are intentionally structural (source-level grep), not behavioural,
because behavioural tests would need a live browser and a real ATS.  The
structural check is the right tool here: the invariant is about which Playwright
API calls appear in the code, not about runtime output.
"""
import ast
import re
import os
import unittest


APPLY_PY = os.path.join(os.path.dirname(__file__), "..", "apply.py")


def _read_source() -> str:
    with open(APPLY_PY, encoding="utf-8") as fh:
        return fh.read()


class TestApplyNoSubmit(unittest.TestCase):
    """apply.py must not contain calls that would submit a form automatically."""

    def setUp(self):
        self.src = _read_source()

    def test_no_click_on_submit_selector(self):
        """No .click( call on a submit/apply selector.

        Playwright's page.locator(selector).click() is the mechanism that would
        auto-submit.  We look for '.click(' anywhere in the source; a hit only
        matters when it is paired with a submit/apply selector, but the safest
        rule for this safety-critical file is: no .click( at all.  apply.py has
        no legitimate need to click anything — it fills inputs and attaches files
        only, per its own docstring.
        """
        # Strip single-line comments and string literals before scanning so that
        # the explanatory comment in the docstring ("There is no .click()") does
        # not itself trip the check.
        try:
            tree = ast.parse(self.src)
        except SyntaxError as exc:
            self.fail(f"apply.py has a syntax error: {exc}")

        # Walk the AST and collect all attribute-access calls named 'click'.
        click_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "click"
        ]
        self.assertEqual(
            click_calls, [],
            f"apply.py contains {len(click_calls)} .click() call(s) at lines "
            + str([getattr(n, 'lineno', '?') for n in click_calls])
            + " — the tool must never click submit/apply buttons",
        )

    def test_no_form_submit_call(self):
        """No .submit( call anywhere in the source (form submission shortcut).

        Playwright exposes page.locator(sel).evaluate('form => form.submit()')
        and similar patterns.  A raw .submit( call (as an attribute access) is
        the most direct route; this check catches it at the AST level.
        """
        try:
            tree = ast.parse(self.src)
        except SyntaxError as exc:
            self.fail(f"apply.py has a syntax error: {exc}")

        submit_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit"
        ]
        self.assertEqual(
            submit_calls, [],
            f"apply.py contains {len(submit_calls)} .submit() call(s) at lines "
            + str([getattr(n, 'lineno', '?') for n in submit_calls])
            + " — the tool must never call .submit() on a form",
        )

    def test_no_submit_selector_in_locator(self):
        """No string literal containing a submit/apply selector passed to locator().

        Catches patterns like page.locator("button[type='submit']").some_action()
        even if the action itself is not .click() — a belt-and-braces check.
        The regex looks for selector-like strings referencing submit or apply
        button variants.
        """
        # Submit-related selector patterns that should never appear.
        SUBMIT_SELECTOR_RE = re.compile(
            r"""['"](
                button\s*\[type\s*=\s*['"]submit['"]  |
                input\s*\[type\s*=\s*['"]submit['"]   |
                button[^'"]*\bsubmit\b                |
                button[^'"]*\bapply\b
            )""",
            re.VERBOSE | re.IGNORECASE,
        )
        # Only scan string literals (not comments or docstrings) — parse via AST.
        try:
            tree = ast.parse(self.src)
        except SyntaxError as exc:
            self.fail(f"apply.py has a syntax error: {exc}")

        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if SUBMIT_SELECTOR_RE.search(node.value):
                    hits.append((getattr(node, 'lineno', '?'), node.value[:80]))

        self.assertEqual(
            hits, [],
            f"apply.py contains submit/apply selector string(s): {hits}",
        )

    def test_autofill_fills_inputs_only(self):
        """autofill() uses loc.fill() — the input-filling method — never .click().

        Confirms that the autofill helper, which is the only place field-level
        Playwright calls are made, uses .fill() (sets a text value) and not
        .click() or .check() (which could trigger submit-adjacent side-effects).
        This is an AST-level check on the source, not a runtime check.
        """
        try:
            tree = ast.parse(self.src)
        except SyntaxError as exc:
            self.fail(f"apply.py has a syntax error: {exc}")

        # Find the autofill function definition.
        autofill_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "autofill"),
            None,
        )
        self.assertIsNotNone(autofill_fn, "autofill() function not found in apply.py")

        # Within autofill, collect all attribute-access calls.
        dangerous_calls = [
            node for node in ast.walk(autofill_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("click", "check", "submit", "press")
        ]
        self.assertEqual(
            dangerous_calls, [],
            f"autofill() contains dangerous call(s): "
            + str([(n.func.attr, getattr(n, 'lineno', '?')) for n in dangerous_calls]),
        )


if __name__ == "__main__":
    unittest.main()
