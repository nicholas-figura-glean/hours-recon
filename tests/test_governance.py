from __future__ import annotations

import copy
import json
import sqlite3
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from hours_recon.evidence import attach_governance
from hours_recon.freshness import describe_freshness
from hours_recon.inference import infer_packages
from hours_recon.matching import match_projects_with_evidence
from hours_recon.reconcile import reconcile
from hours_recon.remediation import (
    account_urgency,
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
from hours_recon.service import ReconciliationService


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

    def _covered_account(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme",
            "close_date": "2026-01-01", "service_start_date": "2026-01-01", "service_end_date": "2027-01-01",
            "line_items": [{
                "id": "L1", "source": "opportunity_line_item", "name": "Glean Outcomes Packages: Starter",
                "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1,
            }],
        }
        package = infer_packages(opportunity, PACKAGE_CONFIG)[0][0]
        project = {"id": "P1", "salesforce_account_id": "A1", "start_date": "2026-01-01", "due_date": "2027-01-01", "status": "In progress"}
        entry = {
            "id": "T1", "project_id": "P1", "date": "2026-02-01", "hours": 1, "billable": True,
            "approval_status": "APPROVED", "activity_name": "Workshop", "category": "Delivery", "user_id": "U1",
        }
        return minimal_account(package=package, project=project, entry=entry)

    def _governed(self, account, coverage):
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(
            report,
            project_match_evidence={"P1": {"basis": "salesforce_account_id"}},
            source_coverage=coverage,
        )
        return report

    def test_coverage_cap_never_leaks_an_internal_flag_name_into_copy(self):
        """The sentinel "complete" must never be presented as a dataset name."""
        report = self._governed(self._covered_account(), {
            "complete": False, "accounts": True, "opportunities": True, "projects": True,
            "time_entries": False, "pagination_complete": True, "through_date_current": False,
        })
        dimensions = report["accounts"][0]["governance"]["dimensions"]
        time_quality = dimensions["time_quality"]
        self.assertEqual("incomplete_source_coverage", time_quality["reason_code"])
        self.assertIn("Rocketlane time entries", time_quality["summary"])
        self.assertNotIn("complete,", time_quality["summary"])
        self.assertNotIn("for: complete", time_quality["summary"])
        for item in dimensions.values():
            self.assertNotIn("complete.", str(item["summary"]).replace("not confirmed complete.", ""))
        self.assertEqual(["Rocketlane time entries"], time_quality["details"]["missing_coverage_labels"])

    def test_a_stale_pull_caps_evidence_without_creating_an_account_backlog(self):
        """One retrieval problem must not become one work item per account and dimension."""
        report = self._governed(self._covered_account(), {
            "complete": False, "accounts": True, "opportunities": True, "projects": True,
            "time_entries": True, "pagination_complete": True, "through_date_current": False,
        })
        governance = report["accounts"][0]["governance"]
        self.assertEqual("T4", governance["overall_tier"])
        self.assertTrue(governance["coverage_capped"])
        self.assertEqual(0.0, report["governance"]["metrics"]["sold_hours"]["governed"])
        # The underlying evidence is strong, so there is nothing for a person to fix.
        self.assertEqual([], governance["gaps"])
        self.assertEqual(
            [], build_workstreams(
                {"meta": {"as_of": "2026-07-22"}, "accounts": report["accounts"]},
                scope_id="scope", portfolio_id="owner@example.com",
            ),
        )

    def test_a_real_weakness_survives_a_coverage_cap(self):
        """Suppressing the retrieval gap must not hide genuine evidence problems."""
        account = self._covered_account()
        account["projects"][0].pop("salesforce_account_id")
        fields = ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "expired_unused_hours", "future_entitlement_hours", "overage_hours")
        report = {"meta": {}, "metrics": {field: account.get(field, 0) for field in fields}, "accounts": [account]}
        attach_governance(
            report,
            project_match_evidence={"P1": {"basis": "normalized_customer_name"}},
            source_coverage={
                "complete": False, "accounts": True, "opportunities": True, "projects": True,
                "time_entries": True, "pagination_complete": True, "through_date_current": False,
            },
        )
        gaps = report["accounts"][0]["governance"]["gaps"]
        reasons = {gap["reason_code"] for gap in gaps}
        self.assertIn("normalized_customer_name", reasons)
        self.assertNotIn("incomplete_source_coverage", reasons)

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

    def test_priority_follows_hours_and_time_rather_than_evidence_tier(self):
        """Ranking on tier made every item P0. Money and dates carry the signal."""
        def account(**overrides):
            base = {
                "id": "A1", "name": "Acme", "sold_hours": 100, "billed_hours": 10,
                "remaining_hours": 90, "at_risk_hours": 0, "expired_unused_hours": 0,
                "overage_hours": 0, "packages": [],
            }
            base.update(overrides)
            return base

        expiring = lambda days: [{"remaining_hours": 10, "days_to_expiration": days}]
        self.assertEqual("P0", account_urgency(account(overage_hours=4)))
        self.assertEqual("P0", account_urgency(account(expired_unused_hours=10)))
        self.assertEqual("P0", account_urgency(account(at_risk_hours=10, packages=expiring(12))))
        self.assertEqual("P1", account_urgency(account(at_risk_hours=10, packages=expiring(75))))
        self.assertEqual("P1", account_urgency(account(sold_hours=0, billed_hours=8)))
        self.assertEqual("P2", account_urgency(account(packages=expiring(300))))

        # A weak mapping on a calm account is hygiene, not an emergency.
        calm = queue_report([{
            "dimension": "hours_mapping", "tier": "T4", "reason_code": "tier_name",
            "summary": "Name inference.", "recommended_action": "Use a governed mapping.",
            "refs": ["L1"], "details": {},
        }])
        calm["accounts"][0].update({"at_risk_hours": 0, "overage_hours": 0, "packages": expiring(300)})
        self.assertEqual("P2", build_workstreams(calm, scope_id="scope")[0]["priority"])

        # The same evidence on an account losing hours this month is urgent.
        urgent = copy.deepcopy(calm)
        urgent["accounts"][0].update({"at_risk_hours": 30, "packages": expiring(9)})
        self.assertEqual("P0", build_workstreams(urgent, scope_id="scope")[0]["priority"])

    def test_a_gap_that_hides_the_hours_is_escalated_one_band(self):
        """If usage cannot be measured, the account's apparent calm is not evidence."""
        report = queue_report([{
            "dimension": "project_linkage", "tier": "T4", "reason_code": "no_rocketlane_project",
            "summary": "No project is linked.", "recommended_action": "Link the project.",
            "refs": ["A1"], "details": {},
        }])
        report["accounts"][0].update({"at_risk_hours": 0, "overage_hours": 0, "sold_hours": 40, "packages": []})
        self.assertEqual("P1", build_workstreams(report, scope_id="scope")[0]["priority"])

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
        self.assertIn("What needs attention", message)
        self.assertIn("What to do", message)
        self.assertIn("— sent via Glean Pi", message)
        self.assertNotIn("*", message)
        self.assertIn("refresh Hours Recon and verify", message)

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
        opportunity_url = "https://glean.lightning.force.com/lightning/r/Opportunity/006OPP/view"
        # Only externalReferenceId is proposed. Rocketlane has no project field
        # labelled "Link to Salesforce Opportunity", so proposing one produced a
        # label that preflight could never resolve and that blocked the whole
        # write. The opportunity is already carried by the OppID custom field.
        self.assertEqual({"externalReferenceId": "001ABC"}, operation["proposed_fields"])
        self.assertEqual("ready_after_preflight", operation["status"])
        self.assertNotIn("Link to Salesforce Opportunity", json.dumps(operation))
        self.assertEqual("AISM / Rocketlane project owner", workspace["recipient_role"])
        self.assertEqual(["Alex AISM"], workspace["recipient_suggestions"])
        self.assertEqual("Alex AISM", workspace["default_recipient"])
        slack_message = workspace["slack_draft"]["message"]
        self.assertIn("read-only Hours Recon preflight for Acme", slack_message)
        self.assertIn("project 1379328 currently links to Acme only by normalized customer name", slack_message)
        self.assertIn("verified Salesforce Account ID is 001ABC", slack_message)
        self.assertIn("Set Rocketlane project 1379328 `externalReferenceId` to `001ABC`", slack_message)
        self.assertNotIn("Link to Salesforce Opportunity", slack_message)
        # The opportunity still appears as reference evidence, just not as a write.
        self.assertIn(f"Salesforce Opportunity 006OPP: {opportunity_url}", slack_message)
        self.assertIn("confirm whether you’re the right owner", slack_message)
        self.assertIn("point me to the correct owner", slack_message)
        self.assertIn("refresh Hours Recon and verify the direct ID match", slack_message)
        self.assertIn("https://glean.rocketlane.com/projects/1379328/overview", slack_message)
        self.assertNotIn("Identify or create", slack_message)
        self.assertIn("wait for my explicit confirmation", workspace["mcp_request"])
        self.assertFalse(workspace["source_write_performed"])

    def test_project_linkage_is_unambiguous_when_multiple_opportunities_exist(self):
        gap = {
            "dimension": "project_linkage", "tier": "T3", "reason_code": "normalized_customer_name",
            "summary": "Matched by name.", "recommended_action": "Store a stable ID.",
            "refs": ["001ABC", "1379328"], "details": {"match_bases": ["normalized_customer_name"]},
        }
        report = queue_report([gap])
        report["accounts"][0].update({
            "id": "001ABC", "name": "Acme",
            "packages": [{"opportunity_id": "006FIRST"}, {"opportunity_id": "006SECOND"}],
            "projects": [{"id": "1379328", "name": "Acme Outcomes", "owner_name": "Alex AISM"}],
            "entries": [],
        })
        workstream = build_workstreams(report, scope_id="scope")[0]
        selected = next(path for path in workstream["paths"] if path["id"] == "project_linkage.salesforce_account_id.t1")
        workstream["selected_path_id"] = selected["id"]
        workstream["selected_path"] = selected
        workspace = build_execution_workspace(workstream, report)
        operation = workspace["operations"][0]
        # Writing only the Account ID is unambiguous however many opportunities
        # exist, so several opportunities no longer stall the write.
        self.assertEqual("ready_after_preflight", operation["status"])
        self.assertEqual({"externalReferenceId": "001ABC"}, operation["proposed_fields"])
        self.assertEqual([], [item for item in workspace["required_inputs"] if "Opportunity" in item])
        self.assertNotIn("Link to Salesforce Opportunity", workspace["slack_draft"]["message"])
        # Both opportunities stay visible as evidence for the reviewer.
        self.assertIn("006FIRST/view", workspace["slack_draft"]["message"])
        self.assertIn("006SECOND/view", workspace["slack_draft"]["message"])

    def test_time_quality_workspace_splits_supported_updates_from_manual_approval(self):
        gap = {
            "dimension": "time_quality", "tier": "T3", "reason_code": "incomplete_time_or_project_metadata",
            "summary": "Time metadata is incomplete.", "recommended_action": "Correct Rocketlane metadata.",
            "refs": ["9001", "9002"],
            "details": {"approval_pending": 1, "missing_activity": 1, "stale_or_incomplete_projects": 1},
        }
        report = queue_report([gap])
        report["accounts"][0].update({
            "projects": [{"id": "77", "name": "Acme Outcomes", "customer_id": "88", "start_date": "2026-01-01", "due_date": "2026-12-31"}],
            "entries": [
                {"id": "9001", "project_id": "77", "date": "2026-07-01", "billable": True, "approval_status": "SUBMITTED", "activity_name": None, "category": "Delivery", "user_id": "11", "user_name": "Taylor Submitter", "user_email": "taylor@example.com"},
                {"id": "9002", "project_id": "77", "date": "2026-07-02", "billable": True, "approval_status": "APPROVED", "activity_name": "Discovery", "category": "Delivery", "user_id": "12", "user_name": "Healthy Author", "user_email": "healthy@example.com"},
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
        self.assertIn("What needs attention", workspace["slack_draft"]["message"])
        self.assertIn("— sent via Glean Pi", workspace["slack_draft"]["message"])
        self.assertEqual("Rocketlane time-entry submitter", workspace["recipient_role"])
        self.assertEqual(["taylor@example.com"], workspace["recipient_suggestions"])
        self.assertEqual(1, len(workspace["slack_handoffs"]))
        handoff = workspace["slack_handoffs"][0]
        self.assertEqual("taylor@example.com", handoff["recipient"])
        self.assertEqual(["9001"], handoff["entry_ids"])
        self.assertNotIn("9002", handoff["message"])
        self.assertIn("entries you submitted", handoff["message"])

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
                "execution_id": "stale-plan",
                "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]},
                "source_write_performed": False,
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            self.assertIsNotNone(prepared["execution_plan"])
            store.queue_slack_message(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=prepared["version"], execution_id="stale-plan",
                path_id=prepared["selected_path_id"], recipient_query="@owner",
                message="Please update this evidence.\n\n— sent via Glean Pi",
            )
            store.observe(queue_report([self.gap()], as_of="2026-07-23"), retrieval_id="r2", scope_id="scope", coverage_complete=False)
            refreshed = store.list_workstreams(scope_id="scope")[0]
            self.assertIsNone(refreshed["execution_plan"])
            self.assertIsNone(refreshed["execution_prepared_at"])
            self.assertEqual([], store.list_slack_outbox(scope_id="scope", status="pending"))
            self.assertEqual("cancelled", store.list_slack_outbox(scope_id="scope", status="cancelled")[0]["status"])

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

    def test_slack_mcp_outbox_queue_claim_and_confirmed_permalink_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = queue_report([self.gap()])
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(report, retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "execution_id": "hrex1_outbox_test", "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]},
                "execution_mode": "delegated", "source_write_performed": False,
                "slack_draft": {"message": "Hi {{recipient}} — please update the source."},
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            queued = store.queue_slack_message(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=prepared["version"], execution_id=plan["execution_id"],
                path_id=prepared["selected_path_id"], recipient_query="Alex Owner",
                message="Hi Alex — please update the linked record.\n\n— sent via Glean Pi",
            )
            self.assertEqual("queued_not_sent", queued["delivery"])
            self.assertEqual("pending", queued["outbox"]["status"])
            self.assertIsNone(queued["workstream"]["slack_sent_at"])
            self.assertEqual([], store.list_slack_outbox(scope_id="scope", account_ids=["OTHER"]))
            with self.assertRaises(QueueValidationError):
                store.claim_slack_outbox(
                    queued["outbox"]["id"], scope_id="scope", portfolio_id="local-default",
                    expected_version=queued["outbox"]["version"], account_ids=["OTHER"],
                )
            duplicate = store.queue_slack_message(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=prepared["version"], execution_id=plan["execution_id"],
                path_id=prepared["selected_path_id"], recipient_query="Alex Owner",
                message="Hi Alex — please update the linked record.\n\n— sent via Glean Pi",
            )
            self.assertEqual("already_pending", duplicate["delivery"])
            second = store.queue_slack_message(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=duplicate["workstream"]["version"], execution_id=plan["execution_id"],
                path_id=prepared["selected_path_id"], recipient_query="Taylor Submitter",
                message="Hi Taylor — please update your submitted entries.\n\n— sent via Glean Pi",
            )
            self.assertEqual("pending", second["outbox"]["status"])
            self.assertEqual(2, len(store.list_slack_outbox(scope_id="scope", status="pending")))
            claimed = store.claim_slack_outbox(
                queued["outbox"]["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=queued["outbox"]["version"],
            )
            self.assertEqual("sending", claimed["status"])
            sent = store.complete_slack_outbox(
                claimed["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=claimed["version"], recipient_id="U123ABC",
                permalink="https://example.slack.com/archives/D456DEF/p1770000000123456",
            )
            self.assertEqual("sent", sent["status"])
            self.assertEqual("", sent["message_text"])
            final = store.get_workstream(workstream["fingerprint"], scope_id="scope")
            self.assertEqual("https://example.slack.com/archives/D456DEF/p1770000000123456", final["slack_permalink"])
            self.assertEqual("D456DEF", final["slack_channel_id"])
            self.assertEqual("1770000000.123456", final["slack_message_ts"])
            self.assertEqual(sent["id"], final["slack_outbox_id"])
            self.assertEqual(2, len(final["slack_outboxes"]))
            self.assertEqual("sent", final["events"][0]["payload"]["delivery"])

    def test_slack_mcp_outbox_rejects_unsafe_permalink_and_blocks_uncertain_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = queue_report([self.gap()])
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(report, retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "execution_id": "hrex1_uncertain", "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]}, "source_write_performed": False,
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            queued = store.queue_slack_message(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=prepared["version"], execution_id=plan["execution_id"],
                path_id=prepared["selected_path_id"], recipient_query="@owner",
                message="Please update the source.\n\n— sent via Glean Pi",
            )["outbox"]
            claimed = store.claim_slack_outbox(
                queued["id"], scope_id="scope", portfolio_id="local-default", expected_version=queued["version"],
            )
            with self.assertRaisesRegex(QueueValidationError, "permalink"):
                store.complete_slack_outbox(
                    claimed["id"], scope_id="scope", portfolio_id="local-default",
                    expected_version=claimed["version"], recipient_id="U123ABC",
                    permalink="https://evil.example/message",
                )
            uncertain = store.mark_slack_outbox_uncertain(
                claimed["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=claimed["version"], error="Slack MCP response was uncertain",
            )
            self.assertEqual("needs_review", uncertain["status"])
            refreshed = store.get_workstream(workstream["fingerprint"], scope_id="scope")
            with self.assertRaisesRegex(QueueConflict, "reconciled"):
                store.queue_slack_message(
                    workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                    expected_version=refreshed["version"], execution_id=plan["execution_id"],
                    path_id=prepared["selected_path_id"], recipient_query="@different",
                    message="A different message.\n\n— sent via Glean Pi",
                )
            with self.assertRaisesRegex(QueueValidationError, "Confirm"):
                store.retry_slack_outbox(
                    uncertain["id"], scope_id="scope", portfolio_id="local-default",
                    expected_version=uncertain["version"], confirmed_not_delivered=False,
                )
            retried = store.retry_slack_outbox(
                uncertain["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=uncertain["version"], confirmed_not_delivered=True,
            )
            self.assertEqual("pending", retried["status"])

    def test_source_action_outbox_requires_concrete_server_scoped_fields_and_records_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = queue_report([self.gap()])
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(report, retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "execution_id": "hrex1_source_test", "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]}, "source_write_performed": False,
                "operations": [{
                    "system": "rocketlane", "tool": "update_project", "object": "Project",
                    "record_ids": ["77"], "proposed_fields": {"externalReferenceId": "<account id>"},
                    "preflight": ["Re-read the project."],
                }],
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            with self.assertRaisesRegex(QueueValidationError, "placeholder"):
                store.queue_source_action(
                    workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                    expected_version=prepared["version"], operation_index=0,
                    proposed_fields={"externalReferenceId": "<account id>"},
                )
            with self.assertRaisesRegex(QueueValidationError, "field names"):
                store.queue_source_action(
                    workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                    expected_version=prepared["version"], operation_index=0,
                    proposed_fields={"unsafeExtraField": "value"},
                )
            queued = store.queue_source_action(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=prepared["version"], operation_index=0,
                proposed_fields={"externalReferenceId": "001ABC"},
            )
            self.assertEqual("queued_not_executed", queued["execution"])
            self.assertEqual(["77"], queued["outbox"]["record_ids"])
            self.assertEqual({"externalReferenceId": "001ABC"}, queued["outbox"]["proposed_fields"])
            claimed = store.claim_source_action(
                queued["outbox"]["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=queued["outbox"]["version"],
            )
            self.assertEqual("executing", claimed["status"])
            with self.assertRaisesRegex(QueueValidationError, "trusted Salesforce or Rocketlane"):
                store.complete_source_action(
                    claimed["id"], scope_id="scope", portfolio_id="local-default", expected_version=claimed["version"],
                    source_links=["https://evil.example/record"], result_summary="Untrusted audit result.",
                )
            completed = store.complete_source_action(
                claimed["id"], scope_id="scope", portfolio_id="local-default", expected_version=claimed["version"],
                source_links=["https://glean.rocketlane.com/projects/77/overview"],
                result_summary="Observed externalReferenceId=001ABC after write.",
            )
            self.assertEqual("completed", completed["status"])
            self.assertEqual(1, len(store.list_source_actions(scope_id="scope", status="completed")))
            refreshed = store.get_workstream(workstream["fingerprint"], scope_id="scope")
            second = store.queue_source_action(
                workstream["fingerprint"], scope_id="scope", portfolio_id="local-default", account_ids=None,
                expected_version=refreshed["version"], operation_index=0,
                proposed_fields={"externalReferenceId": "001SECOND"},
            )["outbox"]
            second_claim = store.claim_source_action(
                second["id"], scope_id="scope", portfolio_id="local-default", expected_version=second["version"],
            )
            uncertain = store.mark_source_action_uncertain(
                second_claim["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=second_claim["version"], error="Connector response was uncertain",
            )
            with self.assertRaisesRegex(QueueValidationError, "fresh read"):
                store.retry_source_action(
                    uncertain["id"], scope_id="scope", portfolio_id="local-default",
                    expected_version=uncertain["version"], confirmed_not_applied=False,
                )
            retried = store.retry_source_action(
                uncertain["id"], scope_id="scope", portfolio_id="local-default",
                expected_version=uncertain["version"], confirmed_not_applied=True,
            )
            self.assertEqual("pending", retried["status"])
            store.observe(report, retrieval_id="r2", scope_id="scope", coverage_complete=False)
            self.assertEqual([], store.list_source_actions(scope_id="scope", status="pending"))
            self.assertTrue(any(
                item["id"] == retried["id"] and item["status"] == "cancelled"
                for item in store.list_source_actions(scope_id="scope", status="cancelled")
            ))

    def test_service_queues_reviewed_slack_text_without_sending(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = queue_report([self.gap()])
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            store.observe(report, retrieval_id="r1", scope_id="scope", coverage_complete=False)
            workstream = store.list_workstreams(scope_id="scope")[0]
            plan = {
                "execution_id": "hrex1_service_test", "workstream_id": workstream["fingerprint"],
                "selected_path": {"id": workstream["selected_path_id"]},
                "execution_mode": "delegated", "source_write_performed": False,
            }
            prepared = store.action(
                workstream["fingerprint"], scope_id="scope", action="prepare_execution",
                expected_version=workstream["version"], payload={"execution_plan": plan},
            )["workstream"]
            service = object.__new__(ReconciliationService)
            service.settings = {"remediation_scope_id": "scope", "requester_email": "", "mcp_requester_email": ""}
            service._data = report
            service.remediation_store = store
            with self.assertRaisesRegex(QueueValidationError, "Confirm"):
                service.queue_remediation_slack(
                    workstream["fingerprint"], expected_version=prepared["version"],
                    recipient_query="Alex Owner", reviewed_message="Do not queue", confirmed=False,
                )
            result = service.queue_remediation_slack(
                workstream["fingerprint"], expected_version=prepared["version"],
                recipient_query="Alex Owner", reviewed_message="Hi Alex — please update the linked record.", confirmed=True,
            )
            self.assertEqual("queued_not_sent", result["delivery"])
            self.assertIn("— sent via Glean Pi", result["slack_message"])
            self.assertEqual("pending", service.list_slack_outbox()[0]["status"])
            self.assertIn("send pending Hours Recon messages", result["next_step"])

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

    def test_existing_v2_database_adds_slack_delivery_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.sqlite3"
            with sqlite3.connect(str(path)) as connection:
                connection.execute("CREATE TABLE workstreams(fingerprint TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE instances(fingerprint TEXT PRIMARY KEY, last_governed_tier TEXT)")
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            RemediationStore(path)
            with sqlite3.connect(str(path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(workstreams)")}
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"slack_recipient_id", "slack_channel_id", "slack_message_ts", "slack_permalink", "slack_sent_at", "slack_outbox_id", "slack_message_sha256"} <= columns)
            self.assertIn("slack_outbox", tables)
            self.assertIn("source_action_outbox", tables)

    def test_v1_database_is_cleanly_reset_to_v4(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.sqlite3"
            with sqlite3.connect(str(path)) as connection:
                connection.execute("CREATE TABLE cases(fingerprint TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO cases VALUES('legacy-case')")
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            store = RemediationStore(path)
            self.assertEqual(4, store.health(scope_id="scope")["schema_version"])
            with sqlite3.connect(str(path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                workstream_columns = {row[1] for row in connection.execute("PRAGMA table_info(workstreams)")}
            self.assertNotIn("cases", tables)
            self.assertIn("workstreams", tables)
            self.assertTrue({"slack_sent_at", "slack_permalink", "slack_message_sha256"} <= workstream_columns)


if __name__ == "__main__":
    unittest.main()


class TimeEntryExclusionTests(unittest.TestCase):
    """Accepting an unfixable entry must suppress flagging and nothing else."""

    PROJECT = {
        "id": "P1", "name": "Delivery", "status": "completed",
        "start_date": "2026-01-01", "due_date": "2026-12-31",
    }

    def _entry(self, entry_id, **overrides):
        entry = {
            "id": entry_id, "project_id": "P1", "date": "2026-03-01", "billable": True,
            "approval_status": "APPROVED", "activity_name": "Workshop",
            "category": "Consulting", "user_id": "u1", "hours": 1.0,
        }
        entry.update(overrides)
        return entry

    def _account(self, entries, projects=None):
        return {
            "id": "001ABC", "name": "Acme", "sold_hours": 10.0,
            "projects": list(projects if projects is not None else [self.PROJECT]),
            "entries": entries,
        }

    def test_excluded_entry_reaches_t2_but_never_t1(self):
        from hours_recon.evidence import _time_quality_dimension
        account = self._account([self._entry("1", activity_name="")])
        self.assertEqual("T3", _time_quality_dimension(account)["tier"])
        governed = _time_quality_dimension(account, {"1": {"signals": ["missing_activity"]}})
        # T2 clears the work queue; T1 stays reserved for genuinely clean source data.
        self.assertEqual("T2", governed["tier"])
        self.assertEqual("accepted_time_exceptions", governed["reason_code"])
        self.assertEqual(1, governed["details"]["excluded_entries"])

    def test_exclusion_never_changes_the_hours_math(self):
        """The validator asserts billed hours equal source minutes over every matched
        in-window entry, so an exclusion that touched hours would fail every refresh."""
        from hours_recon.reconcile import reconcile
        salesforce = {
            "requester": {"id": "U1", "name": "Alex AIOM", "email": "alex@example.com"},
            "accounts": [{"id": "A1", "name": "Acme, Inc."}],
            "opportunities": [{
                "id": "O1", "account_id": "A1", "account_name": "Acme",
                "name": "Growth Package 20 hours", "close_date": "2025-12-01", "line_items": [],
            }],
        }
        rocketlane = {
            "projects": [{
                "id": "P1", "name": "Acme Project", "customer_name": "Acme", "status": "completed",
                "start_date": "2026-01-01", "due_date": "2026-12-31",
            }],
            "entries": [
                {"id": "T1", "project_id": "P1", "date": "2026-01-15", "minutes": 120,
                 "billable": True, "user_email": "alex@example.com"},
                {"id": "T2", "project_id": "P1", "date": "2026-01-16", "minutes": 60,
                 "billable": True, "user_email": "alex@example.com"},
            ],
        }
        common = dict(
            package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 1),
        )
        base = reconcile(copy.deepcopy(salesforce), copy.deepcopy(rocketlane), **common)
        excluded = reconcile(
            copy.deepcopy(salesforce), copy.deepcopy(rocketlane),
            time_entry_exclusions={
                "T1": {"signals": ["approval_unknown", "missing_activity", "missing_category"]},
                "T2": {"signals": ["approval_unknown", "missing_activity", "missing_category"]},
            },
            **common,
        )
        for field in ("sold_hours", "billed_hours", "remaining_hours", "at_risk_hours", "overage_hours"):
            self.assertEqual(
                base["metrics"][field], excluded["metrics"][field],
                f"{field} moved when an entry was excluded",
            )
        self.assertEqual(
            len(base["accounts"][0]["entries"]), len(excluded["accounts"][0]["entries"])
        )
        # The evidence tier is the only thing allowed to move.
        self.assertEqual(
            "incomplete_time_or_project_metadata",
            base["accounts"][0]["governance"]["dimensions"]["time_quality"]["reason_code"],
        )
        self.assertEqual(
            "accepted_time_exceptions",
            excluded["accounts"][0]["governance"]["dimensions"]["time_quality"]["reason_code"],
        )

    def test_a_new_problem_on_an_excluded_entry_reflags_it(self):
        from hours_recon.evidence import _time_quality_dimension
        accepted = {"1": {"signals": ["missing_activity"]}}
        same = self._account([self._entry("1", activity_name="")])
        self.assertEqual("T2", _time_quality_dimension(same, accepted)["tier"])
        # The entry later gets rejected: a problem the operator never reviewed.
        worse = self._account([self._entry("1", activity_name="", approval_status="REJECTED")])
        result = _time_quality_dimension(worse, accepted)
        self.assertEqual("T4", result["tier"])
        self.assertEqual(1, result["details"]["reflagged_entries"])

    def test_an_excluded_entry_fixed_at_source_earns_a_real_t1(self):
        from hours_recon.evidence import _time_quality_dimension
        fixed = self._account([self._entry("1")])
        result = _time_quality_dimension(fixed, {"1": {"signals": ["missing_activity"]}})
        self.assertEqual("T1", result["tier"])
        self.assertEqual("complete_approved_time", result["reason_code"])

    def test_excluding_every_entry_cannot_clear_a_project_level_problem(self):
        from hours_recon.evidence import _time_quality_dimension
        stale = [{"id": "P1", "name": "Delivery", "status": "planning", "start_date": None, "due_date": None}]
        account = self._account(
            [self._entry("1", activity_name=""), self._entry("2", activity_name="")], projects=stale
        )
        accepted = {"1": {"signals": ["missing_activity"]}, "2": {"signals": ["missing_activity"]}}
        result = _time_quality_dimension(account, accepted)
        self.assertEqual("T3", result["tier"])
        self.assertEqual(1, result["details"]["stale_or_incomplete_projects"])

    def test_an_account_with_no_time_is_unaffected_by_exclusions(self):
        from hours_recon.evidence import _time_quality_dimension
        empty = self._account([])
        self.assertEqual(
            _time_quality_dimension(empty)["reason_code"],
            _time_quality_dimension(empty, {"1": {"signals": []}})["reason_code"],
        )


class TimeEntryExclusionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = RemediationStore(Path(self.directory.name) / "remediation.sqlite3")
        self.entries = [
            {"entry_id": "5001", "entry_date": "2025-11-02", "signals": ["missing_activity"]},
            {"entry_id": "5002", "entry_date": "2025-11-03", "signals": ["approval_pending"]},
        ]

    def _exclude(self, entries=None, scope_id="scope"):
        return self.store.exclude_time_entries(
            scope_id=scope_id, portfolio_id="portfolio", account_id="001ABC",
            entries=entries if entries is not None else self.entries,
            reason="Closed period; Rocketlane is read-only.", actor="nick",
        )

    def test_exclusions_survive_and_are_scope_isolated(self):
        self._exclude()
        active = self.store.active_time_entry_exclusions(scope_id="scope", portfolio_id="portfolio")
        self.assertEqual({"5001", "5002"}, set(active))
        self.assertEqual(["missing_activity"], active["5001"]["signals"])
        # A different connector scope must never inherit another scope's exceptions.
        self.assertEqual({}, self.store.active_time_entry_exclusions(scope_id="other", portfolio_id="portfolio"))

    def test_restore_reverts_and_re_excluding_reactivates(self):
        self._exclude()
        self.assertEqual(1, self.store.restore_time_entries(
            scope_id="scope", portfolio_id="portfolio", entry_ids=["5001"], actor="nick",
        )["restored"])
        self.assertEqual({"5002"}, set(self.store.active_time_entry_exclusions(scope_id="scope", portfolio_id="portfolio")))
        self._exclude([self.entries[0]])
        self.assertEqual({"5001", "5002"}, set(self.store.active_time_entry_exclusions(scope_id="scope", portfolio_id="portfolio")))

    def test_revision_changes_on_every_mutation_and_never_repeats(self):
        # The planner dedupes observations on retrieval_id. If restoring everything
        # returned the original revision, the regression would be deduped away and the
        # work queue could never reopen.
        seen = [self.store.time_entry_exclusion_revision(scope_id="scope", portfolio_id="portfolio")]
        self._exclude()
        seen.append(self.store.time_entry_exclusion_revision(scope_id="scope", portfolio_id="portfolio"))
        self.store.restore_time_entries(
            scope_id="scope", portfolio_id="portfolio", entry_ids=["5001", "5002"], actor="nick",
        )
        seen.append(self.store.time_entry_exclusion_revision(scope_id="scope", portfolio_id="portfolio"))
        self._exclude()
        seen.append(self.store.time_entry_exclusion_revision(scope_id="scope", portfolio_id="portfolio"))
        self.assertEqual(len(seen), len(set(seen)), f"revision repeated across states: {seen}")

    def test_a_reason_and_at_least_one_entry_are_required(self):
        with self.assertRaises(QueueValidationError):
            self.store.exclude_time_entries(
                scope_id="scope", portfolio_id="portfolio", account_id="001ABC",
                entries=self.entries, reason="   ", actor="nick",
            )
        with self.assertRaises(QueueValidationError):
            self.store.exclude_time_entries(
                scope_id="scope", portfolio_id="portfolio", account_id="001ABC",
                entries=[], reason="valid", actor="nick",
            )


class ExclusionQueueLifecycleTests(unittest.TestCase):
    """The whole point: an accepted entry must leave the work queue and stay gone."""

    @staticmethod
    def _report(tier, reason):
        """A report whose time_quality dimension carries the given tier.

        The dimension must stay time_quality across every observation: the store
        matches stored instances by account and dimension, so resolving through a
        different dimension would leave the original instance untouched.
        """
        gaps = [] if tier in {"T1", "T2"} else [{
            "dimension": "time_quality", "tier": tier, "reason_code": reason,
            "summary": "Time evidence is incomplete.",
            "recommended_action": "Correct required metadata.",
            "refs": ["5001"], "details": {},
        }]
        report = queue_report(gaps, overall_tier=tier, current_tier=tier)
        report["accounts"][0]["governance"]["dimensions"] = {
            "time_quality": {
                "tier": tier, "rank": int(tier[1:]), "reason_code": reason,
                "summary": "Time evidence state.", "recommended_action": "Keep metadata complete.",
                "refs": ["5001"], "details": {},
            }
        }
        return report

    def test_accepting_clears_the_queue_and_restoring_reopens_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RemediationStore(Path(temporary) / "queue.sqlite3")
            scope = dict(scope_id="scope", portfolio_id="local-default")

            store.observe(
                self._report("T3", "incomplete_time_or_project_metadata"),
                retrieval_id="snap1", coverage_complete=True, **scope,
            )
            self.assertEqual(1, store.summary(**scope)["active_workstream_count"])

            # Accepting entries changes derived evidence, not the snapshot. The service
            # folds the exclusion revision into retrieval_id precisely so this lands.
            store.exclude_time_entries(
                account_id="001ABC",
                entries=[{"entry_id": "5001", "entry_date": "2025-11-02", "signals": ["missing_activity"]}],
                reason="Closed period.", actor="nick", **scope,
            )
            accepted_revision = store.time_entry_exclusion_revision(**scope)
            store.observe(
                self._report("T2", "accepted_time_exceptions"),
                retrieval_id=f"snap1:x{accepted_revision}", coverage_complete=True, **scope,
            )
            self.assertEqual(0, store.summary(**scope)["active_workstream_count"])
            self.assertEqual("governed", store.list_workstreams(**scope)[0]["status"])

            # Re-observing the SAME snapshot must not resurrect the item.
            store.observe(
                self._report("T2", "accepted_time_exceptions"),
                retrieval_id=f"snap2:x{accepted_revision}", coverage_complete=True, **scope,
            )
            self.assertEqual(0, store.summary(**scope)["active_workstream_count"])

            store.restore_time_entries(entry_ids=["5001"], actor="nick", **scope)
            restored_revision = store.time_entry_exclusion_revision(**scope)
            self.assertNotEqual(accepted_revision, restored_revision)
            store.observe(
                self._report("T3", "incomplete_time_or_project_metadata"),
                retrieval_id=f"snap3:x{restored_revision}", coverage_complete=True, **scope,
            )
            self.assertEqual(1, store.summary(**scope)["active_workstream_count"])
            self.assertEqual("open", store.list_workstreams(**scope)[0]["status"])


class RecordLabelTests(unittest.TestCase):
    """A bare Rocketlane project or Salesforce account ID tells an operator nothing."""

    def _report(self):
        return {
            "meta": {"as_of": "2026-07-22"},
            "metrics": {},
            "accounts": [{
                "id": "001PZ00000MqN8jYAF", "name": "Orthogonal Networks (DBA Jellyfish)",
                "sold_hours": 20, "billed_hours": 3, "remaining_hours": 17, "at_risk_hours": 0,
                "expired_unused_hours": 0, "future_entitlement_hours": 0, "overage_hours": 0,
                "packages": [{"opportunity_id": "O1", "opportunity_name": "Jellyfish Growth Package"}],
                "projects": [{
                    "id": "972680", "name": "Jellyfish | Implementation",
                    "customer_id": "539816", "customer_name": "Jellyfish",
                    "status": "completed", "start_date": "2026-01-01", "due_date": "2026-12-31",
                }],
                "entries": [{
                    "id": "30229909", "project_id": "972680", "date": "2026-02-20",
                    "hours": 1.0, "user_name": "Awaneendra Tiwari", "billable": True,
                    "approval_status": "APPROVED", "category": "Working Session",
                }],
                "governance": {"overall_tier": "T3", "policy_version": "evidence-v1", "dimensions": {}, "gaps": []},
            }],
        }

    def _workspace(self, path_id):
        report = self._report()
        workstream = {
            "fingerprint": "hrw2_" + "0" * 64,
            "selected_path_id": path_id,
            "paths": [{"id": path_id, "title": "Fix", "target_tier": "T1", "primary_owner": "AISM"}],
            "instances": [{
                "account_id": "001PZ00000MqN8jYAF",
                "account_name": "Orthogonal Networks (DBA Jellyfish)",
                "dimension": path_id.split(".")[0], "reason_code": "x",
                "evidence": {"refs": [], "details": {}},
            }],
        }
        return build_execution_workspace(workstream, report)

    def test_a_project_operation_names_the_project_and_the_account_it_points_at(self):
        workspace = self._workspace("project_linkage.salesforce_account_id.t1")
        operation = workspace["operations"][0]
        # The ID is still exactly what gets written.
        self.assertEqual(["972680"], operation["record_ids"])
        self.assertEqual("001PZ00000MqN8jYAF", operation["proposed_fields"]["externalReferenceId"])
        # ...and the operator can now tell what either number means.
        self.assertEqual("Jellyfish | Implementation", operation["record_labels"]["972680"])
        self.assertEqual(
            "Orthogonal Networks (DBA Jellyfish)",
            operation["field_value_labels"]["externalReferenceId"],
        )

    def test_a_time_entry_is_labelled_by_date_person_and_hours(self):
        workspace = self._workspace("time_quality.complete_required_metadata.t1")
        labels = workspace["record_labels"]
        self.assertEqual("Feb 20, 2026 · Awaneendra Tiwari · 1h", labels["30229909"])
        self.assertEqual("Jellyfish (Rocketlane customer)", labels["539816"])
        self.assertEqual("Jellyfish Growth Package", labels["O1"])

    def test_a_label_never_replaces_or_reorders_the_written_identifiers(self):
        for path_id in ("project_linkage.salesforce_account_id.t1", "time_quality.complete_required_metadata.t1"):
            for operation in self._workspace(path_id)["operations"]:
                self.assertTrue(all(isinstance(item, str) for item in operation["record_ids"]))
                # Labels are a lookup keyed by ID, so they cannot desynchronise the list.
                self.assertTrue(set(operation["record_labels"]).issubset(set(operation["record_ids"])))

    def test_an_unknown_or_malformed_date_degrades_to_the_raw_value(self):
        from hours_recon.remediation_execution import _entry_label, _short_date
        self.assertEqual("not-a-date", _short_date("not-a-date"))
        self.assertEqual("2026-13-01", _short_date("2026-13-01"))
        self.assertEqual("", _entry_label({}))
        self.assertEqual("Alex", _entry_label({"user_name": "Alex"}))
