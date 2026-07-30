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
from hours_recon.remediation import (
    build_workstreams,
    case_fingerprint,
    format_slack_followup,
    gap_fingerprint,
    instance_fingerprint,
    workstream_fingerprint,
)
from hours_recon.remediation_execution import build_execution_workspace
from hours_recon.remediation_policy import path_options, rank_paths, validate_paths
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


def queue_report(gaps, *, overall_tier="T3", as_of="2026-07-22", current_tier=None):
    dimensions = {
        str(gap["dimension"]): {
            "tier": gap["tier"], "rank": int(str(gap["tier"])[1:]),
            "reason_code": gap.get("reason_code"), "summary": gap.get("summary"),
            "recommended_action": gap.get("recommended_action"), "refs": gap.get("refs", []),
            "details": gap.get("details", {}),
        }
        for gap in gaps
    }
    if not dimensions:
        tier = current_tier or overall_tier
        dimensions["service_period"] = {
            "tier": tier, "rank": int(str(tier)[1:]), "reason_code": "partial_explicit" if tier == "T2" else "opportunity_explicit",
            "summary": "Current service-period evidence.", "recommended_action": "Keep dates current.", "refs": ["O1"], "details": {},
        }
    return {
        "meta": {"as_of": as_of},
        "metrics": {},
        "accounts": [{
            "id": "A1", "name": "Acme", "sold_hours": 20, "billed_hours": 3,
            "remaining_hours": 17, "at_risk_hours": 0, "expired_unused_hours": 0,
            "future_entitlement_hours": 0, "overage_hours": 0, "packages": [],
            "governance": {
                "overall_tier": overall_tier, "policy_version": "evidence-v1",
                "dimensions": dimensions, "gaps": gaps,
            },
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


class RemediationPlannerTests(unittest.TestCase):
    def test_detailed_dimensions_offer_valid_t2_and_t1_paths(self):
        for dimension, reason in (
            ("hours_mapping", "tier_name"),
            ("service_period", "close_date_plus_one_year"),
            ("project_linkage", "normalized_customer_name"),
        ):
            paths = path_options(dimension, reason)
            validate_paths(paths)
            self.assertEqual({"T1", "T2"}, {item["target_tier"] for item in paths})
            self.assertTrue(all(item["detailed"] for item in paths))

    def test_t2_is_default_but_systemic_breadth_can_justify_t1(self):
        paths = path_options("hours_mapping", "tier_name")
        ranked, recommended, reason = rank_paths(
            paths, affected_accounts=1, priority="P1",
            impact={"at_risk_hours": 0, "overage_hours": 0, "expired_unused_hours": 0},
        )
        self.assertEqual("T2", next(item for item in ranked if item["id"] == recommended)["target_tier"])
        self.assertIn("T2 minimum", reason)
        _, systemic, systemic_reason = rank_paths(
            paths, affected_accounts=3, priority="P1",
            impact={"at_risk_hours": 0, "overage_hours": 0, "expired_unused_hours": 0},
        )
        self.assertEqual("T1", next(item for item in paths if item["id"] == systemic)["target_tier"])
        self.assertIn("shared root cause", systemic_reason)

    def test_shared_product_root_cause_groups_accounts_but_retains_instances(self):
        accounts = []
        for number in (1, 2):
            gap = {
                "dimension": "hours_mapping", "tier": "T3", "reason_code": "tier_name",
                "summary": "Name inference.", "recommended_action": "Use governed mapping.",
                "refs": [f"L{number}"], "details": {"mapping_sources": ["tier_name"]},
            }
            accounts.append({
                "id": f"A{number}", "name": f"Account {number}", "sold_hours": 20,
                "billed_hours": 0, "remaining_hours": 20, "at_risk_hours": 0,
                "expired_unused_hours": 0, "future_entitlement_hours": 0, "overage_hours": 0,
                "packages": [{
                    "opportunity_id": f"O{number}", "line_item_id": f"L{number}",
                    "line_item_name": "Shared Outcomes SKU", "product_code": "SHARED-SKU",
                    "inference_source": "tier_name",
                }],
                "governance": {
                    "overall_tier": "T3", "policy_version": "evidence-v1",
                    "dimensions": {"hours_mapping": {**gap, "rank": 3}}, "gaps": [gap],
                },
            })
        workstreams = build_workstreams(
            {"meta": {"as_of": "2026-07-22"}, "accounts": accounts},
            scope_id="scope", portfolio_id="owner@example.com",
        )
        self.assertEqual(1, len(workstreams))
        self.assertEqual(2, workstreams[0]["affected_account_count"])
        self.assertEqual(2, len(workstreams[0]["instances"]))
        self.assertTrue(workstreams[0]["group_key"].startswith("product:"))
        self.assertEqual(2, len({item["fingerprint"] for item in workstreams[0]["instances"]}))

    def test_unresolved_shared_product_codes_also_group_systemically(self):
        accounts = []
        for number in (1, 2):
            gap = {
                "dimension": "hours_mapping", "tier": "T4", "reason_code": "unresolved_hours_mapping",
                "summary": "Package is unresolved.", "recommended_action": "Map the product.",
                "refs": [f"L{number}"], "details": {"unresolved_count": 1},
            }
            accounts.append({
                "id": f"A{number}", "name": f"Account {number}", "sold_hours": 0,
                "billed_hours": 0, "remaining_hours": 0, "at_risk_hours": 0,
                "expired_unused_hours": 0, "future_entitlement_hours": 0, "overage_hours": 0,
                "packages": [], "package_exceptions": [{
                    "line_item_id": f"L{number}", "line_item_name": "Unmapped package",
                    "product_code": "UNMAPPED-SHARED-SKU",
                }],
                "governance": {
                    "overall_tier": "T4", "policy_version": "evidence-v1",
                    "dimensions": {"hours_mapping": {**gap, "rank": 4}}, "gaps": [gap],
                },
            })
        workstreams = build_workstreams(
            {"meta": {"as_of": "2026-07-22"}, "accounts": accounts}, scope_id="scope",
        )
        self.assertEqual(1, len(workstreams))
        self.assertEqual("product:unmapped-shared-sku", workstreams[0]["group_key"])
        self.assertEqual(2, workstreams[0]["affected_account_count"])

    def test_fingerprints_and_slack_draft_are_stable_and_explicit(self):
        self.assertEqual(
            workstream_fingerprint("scope", "owner", "hours_mapping", "product:x"),
            workstream_fingerprint("scope", "owner", "hours_mapping", "product:x"),
        )
        self.assertNotEqual(
            instance_fingerprint("scope", "owner", "A1", "service_period"),
            instance_fingerprint("scope", "owner", "A2", "service_period"),
        )
        # Legacy identity helpers remain recognizable during the clean reset.
        case_id = case_fingerprint("scope", "A1")
        self.assertEqual(gap_fingerprint(case_id, "service_period"), gap_fingerprint(case_id, "service_period"))
        workstream = build_workstreams(queue_report([RemediationStoreTests.gap_value()]), scope_id="scope")[0]
        message = format_slack_followup(workstream, "@revops")
        self.assertIn("Hi @revops", message)
        self.assertIn("T2 is the minimum governed outcome", message)
        self.assertIn("fresh, complete pull", message)

    def test_project_linkage_t1_builds_exact_rocketlane_mcp_write_and_owner_handoff(self):
        gap = {
            "dimension": "project_linkage", "tier": "T3", "reason_code": "normalized_customer_name",
            "summary": "Matched by name.", "recommended_action": "Store a stable ID.",
            "refs": ["001ABC", "1379328", "313397"], "details": {"match_bases": ["normalized_customer_name"]},
        }
        report = queue_report([gap])
        account = report["accounts"][0]
        account.update({
            "id": "001ABC", "name": "Acme", "packages": [{"opportunity_id": "006OPP"}],
            "projects": [{"id": "1379328", "name": "Acme Outcomes", "customer_id": "313397", "owner_name": "Alex AISM"}],
            "entries": [],
        })
        workstream = build_workstreams(report, scope_id="scope")[0]
        selected = next(path for path in workstream["paths"] if path["id"] == "project_linkage.salesforce_account_id.t1")
        workstream["selected_path_id"] = selected["id"]
        workstream["selected_path"] = selected
        workspace = build_execution_workspace(workstream, report)
        self.assertEqual("mcp_write", workspace["execution_mode"])
        self.assertTrue(workspace["mcp_write_available"])
        operation = workspace["operations"][0]
        self.assertEqual("update_project", operation["tool"])
        self.assertEqual(["1379328"], operation["record_ids"])
        self.assertEqual({"externalReferenceId": "001ABC"}, operation["proposed_fields"])
        self.assertEqual("AISM / Rocketlane project owner", workspace["recipient_role"])
        self.assertEqual(["Alex AISM"], workspace["recipient_suggestions"])
        self.assertEqual("Alex AISM", workspace["default_recipient"])
        self.assertIn("https://glean.rocketlane.com/projects/1379328/overview", workspace["slack_draft"]["message"])
        self.assertIn("wait for my explicit confirmation", workspace["mcp_request"])
        self.assertFalse(workspace["source_write_performed"])

    def test_time_quality_workspace_splits_supported_updates_from_manual_approval(self):
        gap = {
            "dimension": "time_quality", "tier": "T3", "reason_code": "incomplete_time_or_project_metadata",
            "summary": "Time metadata is incomplete.", "recommended_action": "Correct Rocketlane metadata.",
            "refs": ["9001", "9002"],
            "details": {"approval_pending": 1, "missing_activity": 1, "stale_or_incomplete_projects": 1},
        }
        report = queue_report([gap])
        report["accounts"][0].update({
            "projects": [{"id": "77", "name": "Acme Outcomes", "customer_id": "88"}],
            "entries": [
                {"id": "9001", "approval_status": "SUBMITTED", "activity_name": None},
                {"id": "9002", "approval_status": "APPROVED", "activity_name": "Discovery"},
            ],
        })
        workstream = build_workstreams(report, scope_id="scope")[0]
        workstream["selected_path_id"] = workstream["recommended_path_id"]
        workstream["selected_path"] = next(path for path in workstream["paths"] if path["id"] == workstream["recommended_path_id"])
        workspace = build_execution_workspace(workstream, report)
        tools = [operation["tool"] for operation in workspace["operations"]]
        self.assertIn("update_time_entry", tools)
        self.assertIn("update_project", tools)
        approval = next(operation for operation in workspace["operations"] if operation["object"] == "Time Entry approval workflow")
        self.assertIsNone(approval["tool"])
        self.assertEqual(["9001"], approval["record_ids"])
        self.assertIn("does not expose approvalStatus", approval["limitation"])
        self.assertIn("AISM", workspace["slack_draft"]["message"])

    def test_execution_slack_template_is_recipient_specific_and_never_claims_delivery(self):
        workstream = build_workstreams(queue_report([RemediationStoreTests.gap_value()]), scope_id="scope")[0]
        workstream["execution_plan"] = {
            "slack_draft": {"message": "Hi {{recipient}} — update Salesforce here: https://example.test/record. Not validated."}
        }
        message = format_slack_followup(workstream, "@ae-owner")
        self.assertEqual("Hi @ae-owner — update Salesforce here: https://example.test/record. Not validated.", message)
        self.assertNotIn("sent", message.lower())


class RemediationStoreTests(unittest.TestCase):
    @staticmethod
    def gap_value():
        return {
            "dimension": "service_period", "tier": "T3", "reason_code": "close_date_plus_one_year",
            "summary": "Service period is inferred.", "recommended_action": "Add explicit dates.",
            "refs": ["O1"], "details": {},
        }

    def gap(self):
        return self.gap_value()

    def test_observation_is_idempotent_and_complete_t2_retrieval_resolves_and_reopens(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "private" / "queue.sqlite3")
            first = store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            repeat = store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            self.assertTrue(first["new_source_observation"])
            self.assertFalse(repeat["new_source_observation"])
            self.assertEqual(1, store.summary(scope_id="scope")["active_workstream_count"])

            resolved = store.observe(
                queue_report([], overall_tier="T2", current_tier="T2"),
                retrieval_id="r2", scope_id="scope", coverage_complete=True,
            )
            self.assertTrue(resolved["revalidation_performed"])
            workstream = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual("governed", workstream["status"])
            self.assertTrue(workstream["minimum_target_met"])

            store.observe(queue_report([self.gap()]), retrieval_id="r3", scope_id="scope", coverage_complete="true")
            preserved = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual("governed", preserved["status"])
            self.assertTrue(preserved["minimum_target_met"])
            self.assertEqual("T2", preserved["instances"][0]["validation_tier"])
            self.assertEqual("T3", preserved["instances"][0]["unverified_observed_tier"])
            self.assertEqual(0, preserved["instances"][0]["regression_count"])

            store.observe(queue_report([self.gap()]), retrieval_id="r4", scope_id="scope", coverage_complete=True)
            reopened = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual("open", reopened["status"])
            self.assertEqual(1, reopened["instances"][0]["regression_count"])

    def test_selected_t1_goal_remains_optional_after_t2_governance(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            t1 = next(item for item in workstream["paths"] if item["target_tier"] == "T1")
            store.action(
                workstream["fingerprint"], scope_id="scope", action="select_path",
                expected_version=workstream["version"], payload={"path_id": t1["id"]},
            )
            store.observe(
                queue_report([], overall_tier="T2", current_tier="T2"),
                retrieval_id="r2", scope_id="scope", coverage_complete=True,
            )
            governed = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual("governed", governed["status"])
            self.assertTrue(governed["minimum_target_met"])
            self.assertEqual("T1", governed["selected_target_tier"])
            self.assertFalse(governed["selected_target_met"])

    def test_actions_use_optimistic_concurrency_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "queue.sqlite3"
            store = RemediationStore(path)
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            updated = store.action(
                workstream["fingerprint"], scope_id="scope", action="acknowledge",
                expected_version=workstream["version"],
            )["workstream"]
            self.assertEqual("acknowledged", updated["status"])
            with self.assertRaises(QueueConflict):
                store.action(
                    workstream["fingerprint"], scope_id="scope", action="start",
                    expected_version=workstream["version"],
                )
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_incomplete_retrieval_cannot_fail_pending_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            acknowledged = store.action(
                workstream["fingerprint"], scope_id="scope", action="acknowledge",
                expected_version=workstream["version"],
            )["workstream"]
            pending = store.action(
                workstream["fingerprint"], scope_id="scope", action="ready_for_validation",
                expected_version=acknowledged["version"],
            )["workstream"]
            self.assertEqual("pending_validation", pending["status"])
            store.observe(queue_report([self.gap()]), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            self.assertEqual("pending_validation", store.list_workstreams(scope_id="scope")[0]["status"])
            store.observe(queue_report([self.gap()]), retrieval_id="r3", scope_id="scope", coverage_complete=True)
            self.assertEqual("in_progress", store.list_workstreams(scope_id="scope")[0]["status"])

    def test_scope_and_portfolio_isolation_include_temporary_state_expiration(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="same", scope_id="scope-a", portfolio_id="owner", coverage_complete=False)
            store.observe(queue_report([self.gap()]), retrieval_id="same", scope_id="scope-b", portfolio_id="owner", coverage_complete=False)
            store.observe(queue_report([self.gap()]), retrieval_id="same", scope_id="scope-a", portfolio_id="other", coverage_complete=False)
            first = store.summary(scope_id="scope-a", portfolio_id="owner")["workstreams"][0]
            second = store.summary(scope_id="scope-b", portfolio_id="owner")["workstreams"][0]
            third = store.summary(scope_id="scope-a", portfolio_id="other")["workstreams"][0]
            self.assertEqual(3, len({first["fingerprint"], second["fingerprint"], third["fingerprint"]}))
            future = (date.today() + timedelta(days=5)).isoformat()
            store.action(
                second["fingerprint"], scope_id="scope-b", portfolio_id="owner", action="snooze",
                expected_version=second["version"], payload={"until": future},
            )
            with sqlite3.connect(str(store.path)) as connection:
                connection.execute(
                    "UPDATE workstreams SET snoozed_until='2000-01-01' WHERE fingerprint=?", (second["fingerprint"],),
                )
                connection.commit()
            store.summary(scope_id="scope-a", portfolio_id="owner")
            with sqlite3.connect(str(store.path)) as connection:
                status = connection.execute(
                    "SELECT status FROM workstreams WHERE fingerprint=?", (second["fingerprint"],),
                ).fetchone()[0]
            self.assertEqual("snoozed", status)
            self.assertEqual("open", store.summary(scope_id="scope-b", portfolio_id="owner")["workstreams"][0]["status"])

    def test_due_date_does_not_drift_and_history_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()], as_of="2026-01-01"), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            first = store.list_workstreams(scope_id="scope")[0]
            store.observe(queue_report([self.gap()], as_of="2026-02-01"), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            second = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual(first["due_on"], second["due_on"])
            detail = store.get_workstream(second["fingerprint"], scope_id="scope")
            self.assertGreaterEqual(len(detail["events"]), 4)

    def test_expired_waiver_reopens_and_governed_workstream_cannot_be_snoozed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.sqlite3"
            store = RemediationStore(path)
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            future = (date.today() + timedelta(days=5)).isoformat()
            waived = store.action(
                workstream["fingerprint"], scope_id="scope", action="waive", expected_version=workstream["version"],
                payload={"reason": "Temporary source-system exception", "approved_by": "Governance Owner", "expires_on": future},
            )["workstream"]
            self.assertEqual("waived", waived["status"])
            with sqlite3.connect(str(path)) as connection:
                connection.execute(
                    "UPDATE workstreams SET waiver_expires_on='2000-01-01' WHERE fingerprint=?", (workstream["fingerprint"],),
                )
                connection.commit()
            self.assertEqual("open", store.list_workstreams(scope_id="scope")[0]["status"])

            store.observe(
                queue_report([], overall_tier="T2", current_tier="T2"),
                retrieval_id="r2", scope_id="scope", coverage_complete=True,
            )
            governed = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual("governed", governed["status"])
            with self.assertRaises(QueueValidationError):
                store.action(
                    governed["fingerprint"], scope_id="scope", action="snooze", expected_version=governed["version"],
                    payload={"until": future},
                )

    def test_execution_preparation_and_copy_are_distinct_from_source_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "execution_id": "hrex1_test", "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]},
                "execution_mode": "mcp_write", "source_write_performed": False,
                "slack_draft": {"message": "Hi {{recipient}} — please update the source."},
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )
            self.assertEqual("prepared_not_executed", prepared["execution"])
            self.assertEqual(plan, prepared["execution_workspace"])
            self.assertIsNone(prepared["workstream"]["mcp_request_copied_at"])
            copied = store.action(
                workstream["fingerprint"], scope_id="scope", action="record_mcp_request_copy",
                expected_version=prepared["workstream"]["version"],
            )
            self.assertEqual("copied_not_executed", copied["execution"])
            self.assertTrue(copied["workstream"]["mcp_request_copied_at"])
            self.assertFalse(copied["workstream"]["execution_plan"]["source_write_performed"])
            alternate = next(path for path in copied["workstream"]["paths"] if path["id"] != copied["workstream"]["selected_path_id"])
            changed = store.action(
                workstream["fingerprint"], scope_id="scope", action="select_path",
                expected_version=copied["workstream"]["version"], payload={"path_id": alternate["id"]},
            )["workstream"]
            self.assertIsNone(changed["execution_plan"])
            self.assertIsNone(changed["mcp_request_copied_at"])

    def test_execution_plan_must_match_selected_path_and_cannot_claim_a_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            base = {
                "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": "wrong.path"},
                "source_write_performed": False,
            }
            with self.assertRaisesRegex(QueueValidationError, "does not match"):
                store.action(
                    workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                    expected_version=workstream["version"], payload={"execution_plan": base},
                )
            base["selected_path"]["id"] = workstream["selected_path_id"]
            base["source_write_performed"] = True
            with self.assertRaisesRegex(QueueValidationError, "cannot claim"):
                store.action(
                    workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                    expected_version=workstream["version"], payload={"execution_plan": base},
                )

    def test_new_source_observation_invalidates_prepared_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]},
                "source_write_performed": False,
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            self.assertIsNotNone(prepared["execution_plan"])
            store.observe(queue_report([self.gap()], as_of="2026-07-23"), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            refreshed = store.list_workstreams(scope_id="scope")[0]
            self.assertIsNone(refreshed["execution_plan"])
            self.assertIsNone(refreshed["execution_prepared_at"])

    def test_slack_preparation_and_successful_copy_are_distinct_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_slack",
                expected_version=workstream["version"], payload={"recipient": "#revops"},
            )
            self.assertEqual("prepared_not_sent", prepared["delivery"])
            self.assertIsNone(prepared["workstream"]["slack_copied_at"])
            self.assertIn("Hi #revops", prepared["slack_message"])
            copied = store.action(
                workstream["fingerprint"], scope_id="scope", action="record_slack_copy",
                expected_version=prepared["workstream"]["version"],
            )
            self.assertEqual("copied_not_sent", copied["delivery"])
            self.assertTrue(copied["workstream"]["slack_copied_at"])
            second_draft = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_slack",
                expected_version=copied["workstream"]["version"], payload={"recipient": "@new-owner"},
            )
            self.assertIsNone(second_draft["workstream"]["slack_copied_at"])
            self.assertEqual("prepared_not_sent", second_draft["delivery"])

    def test_account_visibility_filters_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(queue_report([self.gap()]), retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            self.assertEqual([], store.list_workstreams(scope_id="scope", account_ids=["OTHER"]))
            with self.assertRaises(QueueValidationError):
                store.action(
                    workstream["fingerprint"], scope_id="scope", account_ids=["OTHER"],
                    action="acknowledge", expected_version=workstream["version"],
                )

    def test_v1_database_is_cleanly_reset_to_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.sqlite3"
            with sqlite3.connect(str(path)) as connection:
                connection.execute("CREATE TABLE cases(fingerprint TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO cases VALUES('legacy-case')")
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            store = RemediationStore(path)
            self.assertEqual(2, store.health(scope_id="scope")["schema_version"])
            with sqlite3.connect(str(path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("cases", tables)
            self.assertIn("workstreams", tables)


if __name__ == "__main__":
    unittest.main()
