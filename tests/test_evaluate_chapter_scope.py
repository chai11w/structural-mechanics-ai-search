import unittest

from scripts.evaluate_chapter_scope import evaluate_suite, load_suite


class EvaluateChapterScopeTest(unittest.TestCase):
    def test_approved_seed_set_has_no_failures(self):
        report = evaluate_suite(load_suite())
        self.assertEqual(report["total"], 60)
        self.assertEqual(report["passed"], 60)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["pass_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
