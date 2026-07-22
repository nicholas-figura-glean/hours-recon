from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from hours_recon.config import ROOT, load_json
from hours_recon.demo import demo_report


class DemoReportTests(unittest.TestCase):
    def test_demo_report_is_json_serializable_and_totals_roll_up(self):
        report = demo_report(
            load_json(ROOT / "config" / "packages.json"),
            load_json(ROOT / "config" / "account_aliases.json"),
            as_of=date(2026, 7, 22),
        )
        json.dumps(report)
        self.assertEqual(4, report["metrics"]["account_count"])
        self.assertAlmostEqual(report["metrics"]["sold_hours"], sum(row["sold_hours"] for row in report["accounts"]), places=2)
        self.assertAlmostEqual(report["metrics"]["billed_hours"], sum(row["billed_hours"] for row in report["accounts"]), places=2)
        self.assertTrue(any(item["type"] == "unresolved_package" for item in report["exceptions"]))
        self.assertTrue(any(item["type"] == "unmatched_project" for item in report["exceptions"]))


if __name__ == "__main__":
    unittest.main()
