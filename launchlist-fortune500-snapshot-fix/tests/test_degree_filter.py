import unittest

from scripts.update_jobs import is_graduate_level_internship, normalize_job


SOURCE = {
    "company": "Example Co",
    "key": "example-co",
    "domain": "example.com",
    "base_url": "https://example.com/careers",
}


class DegreeFilterTests(unittest.TestCase):
    def test_excludes_phd_title(self):
        self.assertTrue(is_graduate_level_internship(
            "PhD Software Engineering Intern",
            "Currently pursuing a PhD in Computer Science."
        ))

    def test_excludes_mba_title(self):
        self.assertTrue(is_graduate_level_internship(
            "MBA Product Management Intern",
            "Summer internship for MBA candidates."
        ))

    def test_excludes_masters_only_requirement(self):
        self.assertTrue(is_graduate_level_internship(
            "Data Science Intern",
            "Candidates must be pursuing a Master's degree in statistics or computer science."
        ))

    def test_keeps_undergraduate_bachelors_requirement(self):
        self.assertFalse(is_graduate_level_internship(
            "Software Engineering Intern",
            "Currently pursuing a Bachelor's degree in Computer Science with graduation in 2029."
        ))

    def test_keeps_bachelors_or_masters_when_undergrad_is_explicit(self):
        self.assertFalse(is_graduate_level_internship(
            "Data Analyst Intern",
            "Currently pursuing a Bachelor's or Master's degree in analytics, graduation date 2029."
        ))

    def test_normalize_job_rejects_phd_internship(self):
        job = normalize_job(
            source=SOURCE,
            title="PhD Software Engineering Intern",
            description="Currently pursuing a PhD in Computer Science. Graduation date 2029.",
            location="United States",
            url="https://example.com/careers/phd-software-engineering-intern",
        )
        self.assertIsNone(job)


if __name__ == "__main__":
    unittest.main()
