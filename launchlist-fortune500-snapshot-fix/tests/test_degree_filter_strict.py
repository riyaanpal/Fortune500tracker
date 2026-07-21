import unittest

from scripts.update_jobs import is_graduate_level_internship, normalize_job, existing_job_has_graduate_signal


SOURCE = {
    "company": "Example Co",
    "key": "example-co",
    "domain": "example.com",
    "base_url": "https://example.com/careers",
}


class StrictDegreeFilterTests(unittest.TestCase):
    def test_excludes_current_phd_requirement(self):
        self.assertTrue(is_graduate_level_internship(
            "Software Engineering Intern",
            "Minimum qualifications: Currently pursuing a PhD in Computer Science or a related technical field."
        ))

    def test_excludes_current_masters_requirement(self):
        self.assertTrue(is_graduate_level_internship(
            "Data Analyst Intern",
            "Basic qualifications: Current Master's student in analytics, statistics, or business."
        ))

    def test_excludes_bachelors_masters_or_phd_sentence(self):
        self.assertTrue(is_graduate_level_internship(
            "Product Management Intern",
            "Currently pursuing a Bachelor's, Master's, or PhD degree with an expected graduation date in 2029."
        ))

    def test_excludes_master_or_phd_candidate_reverse_order(self):
        self.assertTrue(is_graduate_level_internship(
            "Research Intern",
            "PhD or Master's candidate required for this summer internship program."
        ))

    def test_keeps_clear_bachelors_only_requirement(self):
        self.assertFalse(is_graduate_level_internship(
            "Software Engineering Intern",
            "Currently pursuing a Bachelor's degree in Computer Science with graduation in 2029."
        ))

    def test_normalize_job_rejects_mixed_current_degree_requirement(self):
        job = normalize_job(
            source=SOURCE,
            title="Business Analyst Intern",
            description="Currently pursuing a Bachelor's, Master's, or PhD degree. Graduation date 2029.",
            location="United States",
            url="https://example.com/careers/business-analyst-intern",
        )
        self.assertIsNone(job)

    def test_existing_retained_job_is_purged_when_grad_evidence_mentions_phd(self):
        self.assertTrue(existing_job_has_graduate_signal({
            "title": "Technology Intern",
            "summary": "Internship opportunity.",
            "grad_evidence": "Currently pursuing a PhD or Master's degree.",
        }))


if __name__ == "__main__":
    unittest.main()
