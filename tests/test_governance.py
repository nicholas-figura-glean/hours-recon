from __future__ import annotations

import copy
import sqlite3
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from hours_recon.evidence import attach_governance
from hours_recon.inference import infer_packages
from hours_recon.matching import match_projects_with_evidence
from hours_recon.reconcile import reconcile
from hours_recon.remediation import case_fingerprint, gap_fingerprint
from hours_recon.remediation_store import QueueConflict, QueueValidationError, RemediationStore


PACKAGE_CONFIG = {
    "outcome_tiers": {"starter": 20, "standard": 50},
    "outcome_list_prices": {"10000": 20, "25000": 50},
    "growth_hours": [20, 50],
    "product_codes": {
        "Glean-Outcomes-Packages-Starter": {"hours_per_unit": 20, "family": "outcome", "tier": "Starter"},
    },
    "overrides": {"opportunities": {}, "line_items": {}, "product_names": {}},
}


def minimal_account(*, package=None, project=None, entry=None):
    account = {
        "id": "A1", "name": "Acme", "packages": [package] if package else [],
        "projects": [project] if project else [], "entries": [entry] if entry else [],
        "sold_hours": float(package["sold_hours"]) if package else 0.0,
        "billed_hours": float(entry.get("hours", 0)) if entry else 0.0,
        "remaining_hours": float(package["sold_hours"]) - float(entry.get("hours", 0)) if package and entry else float(package["sold_hours"]) if package else 0.0,
        "at_risk_hours": 0.0, "expired_unused_hours": 0.0, "future_entitlement_hours": 0.0,
        "overage_hours": 0.0,
    }
    return account


def queue_report(gaps, *, overall_tier="T3", as_of="2026-07-22"):
    return {
        "meta": {"as_of": as_of},
        "metrics": {},
        "accounts": [{
            "id": "A1", "name": "Acme", "sold_hours": 20, "billed_hours": 3,
            "remaining_hours": 17, "at_risk_hours": 0, "expired_unused_hours": 0, "overage_hours": 0,
            "packages": [],
            "governance": {"overall_tier": overall_tier, "policy_version": "evidence-v1", "gaps": gaps},
        }],
    }


class EvidencePolicyTests(unittest.TestCase):
    def test_exact_product_code_is_tier_one_mapping(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        packages, exceptions = infer_packages(opportunity, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(20.0, packages[0]["sold_hours"])
        self.assertEqual("product_code", packages[0]["inference_source"])
        self.assertEqual("Glean-Outcomes-Packages-Starter", packages[0]["mapping_key"])

    def test_explicit_service_dates_are_used_and_score_tier_one(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "service_start_date": "2026-02-01", "service_end_date": "2026-08-01",
            "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        package = infer_packages(opportunity, PACKAGE_CONFIG)[0][0]
        self.assertEqual("2026-02-01", package["service_start_date"])
        self.assertEqual("2026-08-01", package["service_end_date"])
        self.assertEqual("2027-01-01", package["expiration_date"])
        self.assertEqual("opportunity_explicit", package["service_period_source"])

        project = {
            "id": "P1", "name": "Acme", "customer_name": "Acme", "salesforce_account_id": "A1",
            "start_date": "2026-02-01", "due_date": "2026-08-01", "status": "In progress",
        }
        entry = {
            "id": "T1", "project_id": "P1", "date": "2026-03-01", "hours": 2, "billable": True,
            "approval_status": "APPROVED", "activity_name": "Workshop", "category": "Delivery", "user_id": "U1",
        }
        account = minimal_account(package=package, project=project, entry=entry)
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in (
            "sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours",
            "future_entitlement_hours", "overage_hours",
        )}, "accounts": [account]}
        attach_governance(report, project_match_evidence={"P1": {"basis": "salesforce_account_id"}})
        self.assertEqual("T1", report["accounts"][0]["governance"]["overall_tier"])
        self.assertEqual(20.0, report["governance"]["metrics"]["sold_hours"]["governed"])

    def test_weakest_dimension_makes_metrics_provisional(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        package = infer_packages(opportunity, PACKAGE_CONFIG)[0][0]
        account = minimal_account(package=package)
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in (
            "sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours",
            "future_entitlement_hours", "overage_hours",
        )}, "accounts": [account]}
        attach_governance(report)
        governance = report["accounts"][0]["governance"]
        self.assertEqual("T4", governance["overall_tier"])
        self.assertIn("project_linkage", governance["limiting_dimensions"])
        self.assertEqual(0.0, report["governance"]["metrics"]["sold_hours"]["governed"])
        self.assertEqual(20.0, report["governance"]["metrics"]["sold_hours"]["provisional"])

    def test_incomplete_source_coverage_caps_governance(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "service_start_date": "2026-01-01", "service_end_date": "2027-01-01",
            "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        package = infer_packages(opportunity, PACKAGE_CONFIG)[0][0]
        project = {
            "id": "P1", "salesforce_account_id": "A1", "start_date": "2026-01-01", "due_date": "2027-01-01",
            "status": "In progress",
        }
        entry = {
            "id": "T1", "project_id": "P1", "date": "2026-02-01", "hours": 1, "billable": True,
            "approval_status": "APPROVED", "activity_name": "Workshop", "category": "Delivery", "user_id": "U1",
        }
        account = minimal_account(package=package, project=project, entry=entry)
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(
            report,
            project_match_evidence={"P1": {"basis": "salesforce_account_id"}},
            source_coverage={"complete": False, "accounts": True, "opportunities": True, "projects": True, "time_entries": False, "pagination_complete": False},
        )
        self.assertEqual("T4", report["accounts"][0]["governance"]["overall_tier"])
        self.assertEqual(0.0, report["governance"]["metrics"]["sold_hours"]["governed"])

    def test_rejected_time_is_tier_four(self):
        package = {
            "id": "O1:L1", "opportunity_id": "O1", "opportunity_name": "Acme", "line_item_id": "L1",
            "line_item_source": "opportunity_line_item", "product_code": "Glean-Outcomes-Packages-Starter",
            "mapping_key": "Glean-Outcomes-Packages-Starter", "inference_source": "product_code", "service_period_source": "opportunity_explicit",
            "sold_hours": 20, "close_date": "2026-01-01", "service_start_date": "2026-01-01", "service_end_date": "2027-01-01",
            "expiration_date": "2027-01-01",
        }
        project = {"id": "P1", "start_date": "2026-01-01", "due_date": "2027-01-01", "status": "In progress"}
        entry = {"id": "T1", "project_id": "P1", "date": "2026-02-01", "hours": 1, "billable": True, "approval_status": "REJECTED", "activity_name": "Work", "category": "Delivery", "user_id": "U1"}
        account = minimal_account(package=package, project=project, entry=entry)
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(report, project_match_evidence={"P1": {"basis": "salesforce_account_id"}})
        self.assertEqual("T4", report["accounts"][0]["governance"]["dimensions"]["time_quality"]["tier"])

    def test_unresolved_package_cannot_be_masked_by_valid_package(self):
        package = {
            "id": "O1:L1", "opportunity_id": "O1", "opportunity_name": "Acme", "line_item_id": "L1",
            "line_item_source": "opportunity_line_item", "product_code": "Glean-Outcomes-Packages-Starter",
            "mapping_key": "Glean-Outcomes-Packages-Starter", "inference_source": "product_code", "service_period_source": "opportunity_explicit",
            "sold_hours": 20, "close_date": "2026-01-01", "service_start_date": "2026-01-01", "service_end_date": "2027-01-01", "expiration_date": "2027-01-01",
        }
        account = minimal_account(package=package)
        account["package_exceptions"] = [{"opportunity_id": "O2", "line_item_id": "L2"}]
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(report)
        self.assertEqual("T4", report["accounts"][0]["governance"]["dimensions"]["entitlement_source"]["tier"])
        self.assertEqual("T4", report["accounts"][0]["governance"]["dimensions"]["hours_mapping"]["tier"])

    def test_observe_only_explicit_service_dates_do_not_change_reported_totals(self):
        base_opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        sf = {"requester": {"id": "U1", "email": "u@example.com"}, "accounts": [{"id": "A1", "name": "Acme"}], "opportunities": [base_opportunity]}
        rl = {"projects": [{"id": "P1", "customer_name": "Acme"}], "entries": []}
        baseline = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 1))
        explicit_sf = copy.deepcopy(sf)
        explicit_sf["opportunities"][0].update({"service_start_date": "2026-06-01", "service_end_date": "2026-12-01"})
        observed = reconcile(explicit_sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 1))
        self.assertEqual(baseline["metrics"], observed["metrics"])
        self.assertEqual("opportunity_explicit", observed["accounts"][0]["packages"][0]["service_period_source"])

    def test_explicit_no_entitlement_disposition_is_governed_without_project(self):
        account = minimal_account()
        account["entitlement_disposition"] = "not_expected"
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(report)
        governance = report["accounts"][0]["governance"]
        self.assertEqual("T1", governance["overall_tier"])
        self.assertEqual([], governance["gaps"])

    def test_project_match_retains_basis(self):
        accounts = [{"id": "A1", "name": "Acme Inc."}]
        project = {"id": "P1", "customer_id": "C1", "customer_name": "Acme"}
        mapping, exceptions, evidence = match_projects_with_evidence(accounts, [project], {"aliases": {}})
        self.assertEqual({"P1": "A1"}, mapping)
        self.assertEqual([], exceptions)
        self.assertEqual("normalized_customer_name", evidence["P1"]["basis"])

        project["salesforce_account_id"] = "A1"
        _, _, evidence = match_projects_with_evidence(accounts, [project], {"aliases": {}})
        self.assertEqual("salesforce_account_id", evidence["P1"]["basis"])


class RemediationStoreTests(unittest.TestCase):
    def gap(self):
        return {
            "dimension": "service_period", "tier": "T3", "reason_code": "close_date_plus_one_year",
            "summary": "Service period is inferred.", "recommended_action": "Add explicit dates.",
            "refs": ["O1"], "details": {},
        }

    def test_fingerprints_are_stable(self):
        case_id = case_fingerprint("scope", "A1")
        self.assertEqual(case_id, case_fingerprint("scope", "A1"))
        self.assertNotEqual(case_id, case_fingerprint("scope", "A2"))
        self.assertEqual(gap_fingerprint(case_id, "service_period"), gap_fingerprint(case_id, "service_period"))

    def test_observation_is_idempotent_and_complete_retrieval_resolves_and_reopens(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "queue.sqlite3"
            store = RemediationStore(path)
            first = store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            repeat = store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            self.assertTrue(first["new_source_observation"])
            self.assertFalse(repeat["new_source_observation"])
            self.assertEqual(1, store.summary(scope_id="scope")["active_case_count"])

            clean = queue_report([], overall_tier="T2")
            resolved = store.observe(clean, retrieval_id="r2", scope_id="scope", coverage_complete=True)
            self.assertTrue(resolved["revalidation_performed"])
            self.assertEqual("resolved", store.list_cases(scope_id="scope")[0]["status"])

            store.observe(queue_report([self.gap()]), retrieval_id="r3", scope_id="scope", coverage_complete="true")
            gap = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("resolved", gap["status"])
            self.assertEqual(0, gap["regression_count"])

            store.observe(queue_report([self.gap()]), retrieval_id="r4", scope_id="scope", coverage_complete=True)
            gap = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("open", gap["status"])
            self.assertEqual(1, gap["regression_count"])

    def test_actions_use_optimistic_concurrency_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "queue.sqlite3"
            store = RemediationStore(path)
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            gap = store.list_cases(scope_id="scope")[0]["gaps"][0]
            updated = store.action(gap["fingerprint"], scope_id="scope", action="acknowledge", expected_version=gap["version"])
            self.assertEqual("acknowledged", updated["status"])
            with self.assertRaises(QueueConflict):
                store.action(gap["fingerprint"], scope_id="scope", action="start", expected_version=gap["version"])
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_incomplete_retrieval_cannot_fail_pending_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "private" / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            gap = store.list_cases(scope_id="scope")[0]["gaps"][0]
            gap = store.action(gap["fingerprint"], scope_id="scope", action="acknowledge", expected_version=gap["version"])
            gap = store.action(gap["fingerprint"], scope_id="scope", action="ready_for_validation", expected_version=gap["version"])
            self.assertEqual("pending_validation", gap["status"])
            store.observe(queue_report([self.gap()]), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            pending = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("pending_validation", pending["status"])
            store.observe(queue_report([self.gap()]), retrieval_id="r3", scope_id="scope", coverage_complete=True)
            failed = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("in_progress", failed["status"])

    def test_scope_isolation_and_scope_safe_retrieval_deduplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "private" / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="same", scope_id="scope-a", coverage_complete=False)
            store.observe(queue_report([self.gap()]), retrieval_id="same", scope_id="scope-b", coverage_complete=False)
            self.assertEqual(1, store.summary(scope_id="scope-a")["case_count"])
            self.assertEqual(1, store.summary(scope_id="scope-b")["case_count"])
            self.assertNotEqual(
                store.summary(scope_id="scope-a")["cases"][0]["fingerprint"],
                store.summary(scope_id="scope-b")["cases"][0]["fingerprint"],
            )
            gap_b = store.list_cases(scope_id="scope-b")[0]["gaps"][0]
            future = (date.today() + timedelta(days=5)).isoformat()
            store.action(
                gap_b["fingerprint"], scope_id="scope-b", action="snooze", expected_version=gap_b["version"],
                payload={"until": future},
            )
            with sqlite3.connect(str(store.path)) as connection:
                connection.execute("UPDATE gaps SET snoozed_until='2000-01-01' WHERE fingerprint=?", (gap_b["fingerprint"],))
                connection.commit()
            store.summary(scope_id="scope-a")
            with sqlite3.connect(str(store.path)) as connection:
                status_b = connection.execute("SELECT status FROM gaps WHERE fingerprint=?", (gap_b["fingerprint"],)).fetchone()[0]
            self.assertEqual("snoozed", status_b)
            self.assertEqual("open", store.summary(scope_id="scope-b")["cases"][0]["gaps"][0]["status"])

    def test_due_date_does_not_drift_and_history_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "private" / "queue.sqlite3")
            store.observe(queue_report([self.gap()], as_of="2026-01-01"), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            first = store.list_cases(scope_id="scope")[0]["gaps"][0]
            store.observe(queue_report([self.gap()], as_of="2026-02-01"), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            second = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual(first["due_on"], second["due_on"])
            case = store.get_case(store.list_cases(scope_id="scope")[0]["fingerprint"], scope_id="scope")
            self.assertGreaterEqual(len(case["events"]), 2)

    def test_expired_waiver_reopens_and_resolved_gap_cannot_be_snoozed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "queue.sqlite3"
            store = RemediationStore(path)
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            gap = store.list_cases(scope_id="scope")[0]["gaps"][0]
            future = (date.today() + timedelta(days=5)).isoformat()
            waived = store.action(
                gap["fingerprint"], scope_id="scope", action="waive", expected_version=gap["version"],
                payload={"reason": "Temporary source-system exception", "approved_by": "Governance Owner", "expires_on": future},
            )
            self.assertEqual("waived", waived["status"])
            with sqlite3.connect(str(path)) as connection:
                connection.execute("UPDATE gaps SET waiver_expires_on='2000-01-01' WHERE fingerprint=?", (gap["fingerprint"],))
                connection.commit()
            reopened = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("open", reopened["status"])

            store.observe(queue_report([], overall_tier="T2"), retrieval_id="r2", scope_id="scope", coverage_complete=True)
            resolved = store.list_cases(scope_id="scope")[0]["gaps"][0]
            self.assertEqual("resolved", resolved["status"])
            with self.assertRaises(QueueValidationError):
                store.action(
                    resolved["fingerprint"], scope_id="scope", action="snooze", expected_version=resolved["version"],
                    payload={"until": future},
                )


if __name__ == "__main__":
    unittest.main()
