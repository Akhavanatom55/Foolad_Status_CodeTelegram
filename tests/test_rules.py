import unittest
from datetime import date

from calendar_utils import parse_date_input
from rules import expected_safety_grade, build_checks


class RuleTests(unittest.TestCase):
    def test_jalali_parse(self):
        g, j = parse_date_input("۱۴۰۵/۰۷/۰۳", "Asia/Tehran")
        self.assertEqual(j, "1405/07/03")
        self.assertEqual(g.isoformat(), "2026-09-25")

    def test_general_safety_grade(self):
        mode, grade, _ = expected_safety_grade("آزمون ایمنی عمومی", ("ایمنی عمومی",), ("ایمنی تخصصی",))
        self.assertEqual((mode, grade), ("general", 14.0))

    def test_special_safety_grade(self):
        mode, grade, _ = expected_safety_grade("ایمنی تخصصی برق", ("ایمنی عمومی",), ("ایمنی تخصصی",))
        self.assertEqual((mode, grade), ("special", 17.0))

    def test_ambiguous_safety(self):
        mode, grade, _ = expected_safety_grade("آزمون ایمنی", ("ایمنی عمومی",), ("ایمنی تخصصی",))
        self.assertEqual(mode, "ambiguous")
        self.assertIsNone(grade)

    def test_checks(self):
        exam = {
            "name":"ایمنی عمومی",
            "question_count":10,
            "enrolled_count":25,
            "grade_pass":14,
            "shuffle_questions":True,
            "timelimit":2700,
            "timeopen":1000,
            "timeclose":2000,
            "override_count":0,
        }
        checks, problems = build_checks(exam, day_start=0, expected_open=1000, expected_close=2000, safety_general=("ایمنی عمومی",), safety_special=("ایمنی تخصصی",))
        self.assertEqual(problems, 0)
        self.assertTrue(all(c.icon in {"✅", "ℹ️"} for c in checks))


if __name__ == "__main__":
    unittest.main()
