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
from hours_recon.dates import business_today
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

    def test_rocketlane_project_normalization_preserves_missing_id(self):
        project = RocketlaneClient._normalize_project({"projectName": "Missing ID", "customer": {"companyName": "Acme"}})
        self.assertIsNone(project["id"])

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

    def test_dashboard_renders_governance_and_remediation_workflow(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function renderRemediation()", html)
        self.assertIn("function applyRemediationAction(button)", html)
        self.assertIn("Governance evidence", html)
        self.assertIn("Governed ${fmt(split.governed)}h · Provisional ${fmt(split.provisional)}h", html)
        self.assertIn("data-remediation-action", html)
        self.assertIn("Ready for fresh-pull validation", html)
        self.assertIn("data-remediation-action=\"assign\"", html)
        self.assertIn("data-remediation-action=\"snooze\"", html)
        self.assertIn("data-remediation-action=\"waive\"", html)
        self.assertIn("function loadCaseHistory(button)", html)
        self.assertIn("const cases = queue.cases || []", html)
        self.assertIn("item.payload || {}", html)
        self.assertIn("Resume work", html)
        self.assertIn("X-Hours-Recon-Action-Token", html)
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("X-Frame-Options", app_source)
        self.assertIn("Invalid remediation action token", app_source)


class CacheSafetyTests(unittest.TestCase):
    def test_cache_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "cache.json"
            write_cache(path, {"meta": {"mode": "live"}})
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_stale_cached_mcp_report_is_downgraded_before_queue_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sf = {
                "requester": {"id": "U1", "email": "u@example.com"},
                "accounts": [{"id": "A1", "name": "Acme"}],
                "opportunities": [{
                    "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
                    "close_date": "2026-01-01", "service_start_date": "2026-01-01", "service_end_date": "2027-01-01",
                    "line_items": [{"id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter", "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1}],
                }],
            }
            rl = {
                "projects": [{"id": "P1", "salesforce_account_id": "A1", "start_date": "2026-01-01", "due_date": "2027-01-01", "status": "In progress"}],
                "entries": [{"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 60, "billable": True, "approval_status": "APPROVED", "activity_name": "Work", "category": "Delivery", "user_id": "U1"}],
            }
            coverage = {"complete": True, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True}
            report = reconcile(sf, rl, package_config=PACKAGES, account_aliases=ALIASES, as_of=date(2026, 2, 2), mode="mcp", source_coverage=coverage)
            self.assertEqual(20.0, report["governance"]["metrics"]["sold_hours"]["governed"])
            report["meta"].update({
                "mcp_through_date": "2099-01-01", "mcp_coverage": coverage, "mcp_coverage_complete": True,
                "mcp_data_coverage_complete": True, "mcp_scope_verified": True, "mcp_scope_id": "test-tenant",
                "mcp_retrieval_id": "stale-cache",
            })
            cache = root / "cache.json"
            write_cache(cache, report)
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "u@example.com", "packages": PACKAGES,
                "account_aliases": ALIASES, "cache_path": cache, "mcp_snapshot_path": root / "missing.json",
                "cache_max_age_days": 30, "governance_mode": "observe_only", "remediation_mode": "observe_only",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "test-tenant",
            })
            self.assertTrue(service.data["meta"]["cache_stale_for_governance"])
            self.assertEqual(0.0, service.data["governance"]["metrics"]["sold_hours"]["governed"])
            self.assertFalse(service.data["meta"]["remediation_observation"]["revalidation_performed"])

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
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30,
            })
            self.assertEqual("mcp", service.data["meta"]["mode"])
            self.assertEqual("Salesforce MCP + Rocketlane MCP", service.data["meta"]["source"])
            self.assertEqual(4, service.data["metrics"]["account_count"])

    def test_mcp_snapshot_for_a_different_requester_is_rejected(self):
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
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "",
                "mcp_requester_email": "another.aiom@example.com", "packages": PACKAGES,
                "account_aliases": ALIASES, "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30,
            })
            self.assertEqual("demo", service.data["meta"]["mode"])
            self.assertIn("requester does not match", service.data["meta"]["notice"])

    def test_mcp_observe_mode_creates_idempotent_local_remediation_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {
                    "created_at": "2026-02-02T12:00:00Z", "scope": "test", "retrieval_id": "retrieval-1",
                    "scope_id": "test-tenant", "through_date": "2026-02-02",
                    "coverage": {
                        "complete": True, "accounts": True, "opportunities": True,
                        "projects": True, "time_entries": True, "pagination_complete": True,
                    },
                },
                "salesforce": salesforce,
                "rocketlane": rocketlane,
            }))
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30, "governance_mode": "observe_only",
                "remediation_mode": "observe_only", "remediation_db_path": root / "private" / "queue.sqlite3",
                "remediation_scope_id": "test-tenant",
            })
            first = service.data
            self.assertFalse(first["meta"]["mcp_coverage_complete"])
            self.assertEqual(0.0, first["governance"]["metrics"]["sold_hours"]["governed"])
            self.assertTrue(first["remediation_queue"]["available"])
            self.assertGreater(first["remediation_queue"]["active_case_count"], 0)
            first_count = first["remediation_queue"]["case_count"]
            refreshed = service.refresh()
            self.assertEqual(first_count, refreshed["remediation_queue"]["case_count"])
            self.assertFalse(refreshed["meta"]["remediation_observation"]["new_source_observation"])

    def test_cached_report_rebuilds_missing_remediation_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {
                    "created_at": "2026-02-02T12:00:00Z", "retrieval_id": "retrieval-cache",
                    "scope_id": "test-tenant", "coverage": {
                        "complete": True, "accounts": True, "opportunities": True,
                        "projects": True, "time_entries": True, "pagination_complete": True,
                    },
                },
                "salesforce": salesforce, "rocketlane": rocketlane,
            }))
            settings = {
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path, "cache_max_age_days": 30,
                "governance_mode": "observe_only", "remediation_mode": "observe_only",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "test-tenant",
            }
            first = ReconciliationService(settings)
            expected_cases = first.data["remediation_queue"]["case_count"]
            settings["remediation_db_path"].unlink()
            second = ReconciliationService(settings)
            self.assertEqual(expected_cases, second.data["remediation_queue"]["case_count"])

    def test_scope_mismatch_is_quarantined_before_queue_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            today = business_today("America/Denver").isoformat()
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {
                    "created_at": f"{today}T12:00:00Z", "retrieval_id": "scope-mismatch",
                    "scope_id": "tenant-b", "scope_verified": True, "through_date": today,
                    "coverage": {"complete": True, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True},
                },
                "salesforce": salesforce, "rocketlane": rocketlane,
            }))
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": ALIASES, "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30, "governance_mode": "observe_only", "remediation_mode": "observe_only",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "tenant-a",
            })
            self.assertEqual("scope_mismatch_quarantined", service.data["meta"]["remediation_observation"]["reason"])
            self.assertEqual(0, service.data["remediation_queue"]["case_count"])

    def test_non_boolean_mcp_coverage_cannot_authorize_revalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {
                    "created_at": "2026-02-02T12:00:00Z", "retrieval_id": "retrieval-bad-coverage",
                    "scope_id": "test-tenant", "coverage": {
                        "complete": "false", "accounts": True, "opportunities": True,
                        "projects": True, "time_entries": True, "pagination_complete": True,
                    },
                },
                "salesforce": salesforce, "rocketlane": rocketlane,
            }))
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path, "cache_max_age_days": 30,
                "governance_mode": "observe_only", "remediation_mode": "observe_only",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "test-tenant",
            })
            self.assertFalse(service.data["meta"]["mcp_coverage_complete"])
            self.assertFalse(service.data["meta"]["remediation_observation"]["revalidation_performed"])


if __name__ == "__main__":
    unittest.main()
