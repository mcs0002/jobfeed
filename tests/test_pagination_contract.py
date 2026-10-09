import unittest

from scrapers._http import UniquePagination


class UniquePaginationTests(unittest.TestCase):
    def test_counts_unique_ids_and_accepts_complete_board(self):
        guard = UniquePagination("ATS/test")
        guard.add_page(["a", "b"], 0)
        guard.add_page(["b", "c"], 2)
        self.assertEqual(guard.finish(3), 3)

    def test_repeated_page_fails_loudly(self):
        guard = UniquePagination("ATS/test")
        guard.add_page(["a"], 0)
        with self.assertRaisesRegex(RuntimeError, "no new posting ids"):
            guard.add_page(["a"], 1)

    def test_unique_partial_fails_loudly(self):
        guard = UniquePagination("ATS/test")
        guard.add_page(["a", "b"], 0)
        with self.assertRaisesRegex(RuntimeError, "fetched 2 of 10"):
            guard.finish(10)


if __name__ == "__main__":
    unittest.main()
