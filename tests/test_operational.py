from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hours_recon.config import ROOT, load_json
from hours_recon.http_client import ApiError, request_json
from hours_recon.reconcile import reconcile
from hours_recon.rocketlane import RocketlaneClient
from hours_recon.sample_data import build_demo_sources
from hours_recon.service import ReconciliationService
from hours_recon.storage import write_cache

PACKAGES = load_json(ROOT / "config" / "packages.json")
ALIASES = {"aliases": {}}


def base_sources(entries, close_date="2026-01-01"):
    return (
        {
            "requester": {"id": "U1", "name": "Alex", "email": "alex@example.com"},
            "accounts": [{"id": "A1", "name": "Acme"}],
            "opportunities": [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": close_date, "line_items": []}],
        },
        {"projects": [{"id": "P1", "name": "Acme", "customer_name": "Acme"}], "entries": entries},
    )


class OperationalCorrectnessTests(unittest.TestCase):
    def test_future_entries_are_excluded(self):
        sf, rl = base_sources([
            {"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 60, "billable": True},
            {"id": "T2", "project_id": "P1", "date": "2026-02-03", "minutes": 600, "billable": True},
        ])
        result = reconcile(sf, rl, package_config=PACKAGES, account_aliases=ALIASES, as_of=date(2026, 2, 2))
        self.assertEqual(1.0, result["accounts"][0]["billed_hours"])
        self.assertTrue(any(item["type"] == "future_entries_excluded" for item in result["exceptions"]))

    def test_future_entitlement_is_not_usable_remaining(self):
        sf, rl = base_sources([
            {"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 60, "billable": True},
        ], close_date="2026-03-01")
        account = reconcile(sf, rl, package_config=PACKAGES, account_aliases=ALIASES, as_of=date(2026, 2, 1))["accounts"][0]
        self.assertEqual(0.0, account["remaining_hours"])
        self.assertEqual(20.0, account["future_entitlement_hours"])
        self.assertEqual(1.0, account["overage_hours"])
        self.assertEqual(0.0, account["pre_entitlement_hours"])

    def test_excess_negative_correction_is_auditable(self):
        sf, rl = base_sources([
            {"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 60, "billable": True},
            {"id": "T2", "project_id": "P1", "date": "2026-02-02", "minutes": -180, "billable": True},
        ])
        result = reconcile(sf, rl, package_config=PACKAGES, account_aliases=ALIASES, as_of=date(2026, 2, 2))
        account = result["accounts"][0]
        self.assertEqual(-2.0, account["billed_hours"])
        self.assertEqual(2.0, account["unapplied_correction_hours"])
        self.assertTrue(any(item["type"] == "unapplied_negative_correction" for item in result["exceptions"]))


class ConnectorSafetyTests(unittest.TestCase):
    def test_http_client_rejects_non_https_and_cross_origin(self):
        with self.assertRaises(ApiError):
            request_json("GET", "http://api.example.com/data")
        with self.assertRaises(ApiError):
            request_json("GET", "https://evil.example/data", allowed_origin="https://api.example.com")

    def test_rocketlane_projects_include_archived(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return {"data": [], "pagination": {"hasMore": False}}

        with patch.dict(os.environ, {"ROCKETLANE_API_KEY": "test-key"}), patch("hours_recon.rocketlane.request_json", side_effect=fake_request):
            RocketlaneClient().fetch_projects()
        self.assertEqual("true", captured["includeArchive.eq"])


class DashboardMarkupTests(unittest.TestCase):
    def test_account_detail_renders_matched_projects(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const projectRows = (account.projects || []).map", html)
        self.assertIn("Matched Rocketlane projects", html)
        self.assertIn("No matched Rocketlane projects.", html)


class CacheSafetyTests(unittest.TestCase):
    def test_cache_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "cache.json"
            write_cache(path, {"meta": {"mode": "live"}})
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_demo_mode_ignores_live_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache.json"
            write_cache(cache, {"meta": {"mode": "live"}, "accounts": [{"name": "Real Customer"}]})
            service = ReconciliationService({
                "mode": "demo", "timezone": "America/Denver", "requester_email": "", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"), "cache_path": cache,
                "cache_max_age_days": 30,
            })
            self.assertEqual("demo", service.data["meta"]["mode"])
            self.assertFalse(any(item.get("name") == "Real Customer" for item in service.data["accounts"]))

    def test_mcp_mode_imports_private_normalized_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {"created_at": "2026-02-02T12:00:00Z", "scope": "test"},
                "salesforce": salesforce,
                "rocketlane": rocketlane,
            }))
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30,
            })
            self.assertEqual("mcp", service.data["meta"]["mode"])
            self.assertEqual("Salesforce MCP + Rocketlane MCP", service.data["meta"]["source"])
            self.assertEqual(4, service.data["metrics"]["account_count"])


if __name__ == "__main__":
    unittest.main()
