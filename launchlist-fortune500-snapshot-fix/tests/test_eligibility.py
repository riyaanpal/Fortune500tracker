import unittest

from scripts.update_jobs import evaluate_graduation


class GraduationEligibilityTests(unittest.TestCase):
    def test_range_reaches_2029(self):
        self.assertTrue(evaluate_graduation("Expected conferral date between October 2026 and September 2029").eligible)

    def test_range_ending_2028_is_rejected(self):
        self.assertFalse(evaluate_graduation("Graduating between December 2027 and May 2028").eligible)

    def test_or_later_includes_2029(self):
        self.assertTrue(evaluate_graduation("Projected graduation date of December 2027 or later").eligible)

    def test_no_year_is_accepted(self):
        result = evaluate_graduation("Must be currently enrolled in a bachelor's degree program")
        self.assertTrue(result.eligible)
        self.assertEqual(result.status, "No graduation year listed")


if __name__ == "__main__":
    unittest.main()
