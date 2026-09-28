"""Run pytest-style bare `test_*` functions under `unittest discover`.

pytest is not installed in the venv and `unittest discover` collects nothing
from module-level functions, so three whole files (38 tests) ran nowhere from
2026-08-19 until 2026-09-23 and two of them rotted unnoticed. A module opts in
with a `load_tests` hook that calls `function_suite(globals())`.
"""
import unittest


def function_suite(namespace: dict, *, setup=None, teardown=None) -> unittest.TestSuite:
    return unittest.TestSuite(
        unittest.FunctionTestCase(fn, setUp=setup, tearDown=teardown)
        for name, fn in sorted(namespace.items())
        if name.startswith("test_") and callable(fn)
    )
