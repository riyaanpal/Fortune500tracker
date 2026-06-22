import json
import unittest
from datetime import datetime, timezone

from scripts.update_jobs import (
    choose_companies,
    extract_ranked_companies_from_html,
    is_allowed_official_url,
    official_snapshot_diagnostics,
)


class ScannerQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = [
            {"rank": rank, "company": f"Company {rank}", "key": f"company-{rank}", "domain": f"company{rank}.com"}
            for rank in range(1, 501)
        ]

    def test_batch_is_exact_size_and_starts_with_direct_adapters(self):
        selected = choose_companies(
            self.directory,
            {"companies": {}},
            batch_size=50,
            full_sweep=False,
            requested=set(),
            priority_keys={"company-10", "company-20"},
        )
        self.assertEqual(len(selected), 50)
        self.assertEqual([selected[0]["key"], selected[1]["key"]], ["company-10", "company-20"])

    def test_oldest_checked_companies_rotate_to_front(self):
        state = {"companies": {
            f"company-{rank}": {"checked_at": "2026-06-19T12:00:00Z"}
            for rank in range(1, 501)
        }}
        state["companies"]["company-499"]["checked_at"] = "2026-06-18T12:00:00Z"
        selected = choose_companies(self.directory, state, 1, False, set(), {"company-1"})
        self.assertEqual(selected[0]["key"], "company-499")

    def test_full_sweep_returns_500(self):
        self.assertEqual(len(choose_companies(self.directory, {}, 50, True, set(), set())), 500)


class FortuneDirectoryParserTests(unittest.TestCase):
    def test_extracts_all_500_from_escaped_next_payload(self):
        payload = json.dumps([
            {"rank": rank, "companyName": f"Company {rank}"}
            for rank in range(1, 501)
        ])
        push_argument = json.dumps([1, payload])
        records = extract_ranked_companies_from_html(
            f"<script>self.__next_f.push({push_argument})</script>"
        )
        self.assertEqual(len(records), 500)
        self.assertEqual(records[0], {"rank": 1, "company": "Company 1"})
        self.assertEqual(records[-1], {"rank": 500, "company": "Company 500"})

    def test_preserves_500_companies_when_two_ranks_are_tied(self):
        # 500 companies but only 498 distinct rank values. This mirrors Fortune
        # rankings where ties cause subsequent rank numbers to be skipped.
        ranked = []
        for company_number in range(1, 501):
            if company_number == 101:
                rank = 100
            elif company_number == 251:
                rank = 250
            else:
                rank = company_number
            ranked.append({"rank": rank, "companyName": f"Company {company_number}"})
        payload = json.dumps(ranked)
        push_argument = json.dumps([1, payload])
        records = extract_ranked_companies_from_html(
            f"<script>self.__next_f.push({push_argument})</script>"
        )
        self.assertEqual(len(records), 500)
        self.assertEqual(sum(record["rank"] == 100 for record in records), 2)
        self.assertEqual(sum(record["rank"] == 250 for record in records), 2)

    def test_rejects_dashboard_mix_with_hundreds_of_rank_collisions(self):
        records = [
            {"rank": rank, "company": f"Company {rank}"}
            for rank in range(1, 501)
        ]
        # Simulate explorer cards assigning alternate ranks to existing names,
        # plus two extra names. This is the shape of the 502/373 production log.
        records.extend([
            {"rank": 2, "company": "Company 16"},
            {"rank": 21, "company": "Company 66"},
            {"rank": 125, "company": "Extra Company A"},
            {"rank": 155, "company": "Extra Company B"},
        ])
        diagnostics = official_snapshot_diagnostics(records)
        self.assertFalse(diagnostics["coherent"])
        self.assertGreater(len(diagnostics["multi_rank_companies"]), 0)

    def test_accepts_500_company_snapshot_with_two_true_ties(self):
        records = []
        for company_number in range(1, 501):
            rank = company_number
            if company_number == 101:
                rank = 100
            elif company_number == 251:
                rank = 250
            records.append({"rank": rank, "company": f"Company {company_number}"})
        diagnostics = official_snapshot_diagnostics(records)
        self.assertTrue(diagnostics["coherent"])
        self.assertEqual(diagnostics["distinct_rank_count"], 498)


class OfficialLinkTests(unittest.TestCase):
    def test_base_url_host_is_allowed(self):
        source = {"base_url": "https://www.amazon.jobs"}
        self.assertTrue(is_allowed_official_url("https://www.amazon.jobs/en/jobs/123/example", source))

    def test_unverified_third_party_is_rejected(self):
        source = {"domain": "example.com"}
        self.assertFalse(is_allowed_official_url("https://random-job-board.test/posting", source))


if __name__ == "__main__":
    unittest.main()
