from __future__ import annotations

import copy
import unittest
from datetime import date

from hours_recon.inference import infer_packages
from hours_recon.matching import match_projects, normalize_name
from hours_recon.reconcile import package_risk, reconcile

PACKAGE_CONFIG = {
    "outcome_tiers": {"starter": 20, "standard": 50, "select": 100, "advanced": 200, "strategic": 300},
    "outcome_list_prices": {"10000": 20, "25000": 50, "50000": 100, "100000": 200, "150000": 300},
    "growth_hours": [20, 50, 100, 300],
    "overrides": {"opportunities": {}, "line_items": {}, "product_names": {}},
}


def sources(opportunities, entries, projects=None):
    sf = {
        "requester": {"id": "U1", "name": "Alex AIOM", "email": "alex@example.com"},
        "accounts": [{"id": "A1", "name": "Acme, Inc."}],
        "opportunities": opportunities,
    }
    rl = {
        "projects": projects or [{"id": "P1", "name": "Acme Project", "customer_name": "Acme"}],
        "entries": entries,
    }
    return sf, rl


class InferenceTests(unittest.TestCase):
    def test_outcome_tier_and_quantity(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme outcomes",
            "close_date": "2026-01-01", "line_items": [{"id": "L1", "name": "AI Outcomes - Standard", "quantity": 2, "list_price": 25000}],
        }
        packages, exceptions = infer_packages(opportunity, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(100.0, packages[0]["sold_hours"])
        self.assertEqual("tier_name", packages[0]["inference_source"])

    def test_real_salesforce_product_name_and_unrelated_lines_do_not_double_count(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme - AI Outcomes Package",
            "close_date": "2026-01-01", "line_items": [
                {"id": "L1", "name": "Acme Glean Enterprise Flex: Glean Universal Model Key + Glean Hosted", "product_code": "Glean-Enterprise-Flex-Glean-Universal-Model-Key-Glean-Hosted", "quantity": 100, "unit_price": 600},
                {"id": "L2", "name": "Acme Premium Support", "product_code": "Premium-Support", "quantity": 100, "unit_price": 60},
                {"id": "L3", "name": "Acme Glean Outcomes Packages: Starter", "product_code": "Glean-Outcomes-Packages-Starter", "quantity": 1, "unit_price": 10000, "list_price": 10000},
            ],
        }
        packages, exceptions = infer_packages(opportunity, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(1, len(packages))
        self.assertEqual(20.0, packages[0]["sold_hours"])

    def test_opportunity_hint_does_not_multiply_across_unrelated_lines(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Acme Growth Package 100 hours",
            "close_date": "2026-01-01", "line_items": [
                {"id": "L1", "name": "Enterprise Subscription", "quantity": 100, "unit_price": 600},
                {"id": "L2", "name": "Premium Support", "quantity": 100, "unit_price": 60},
            ],
        }
        packages, exceptions = infer_packages(opportunity, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(1, len(packages))
        self.assertEqual(100.0, packages[0]["sold_hours"])
        self.assertEqual("opportunity_explicit_hours", packages[0]["inference_source"])

    def test_explicit_growth_hours_from_opportunity_without_line_items(self):
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme",
            "name": "Acme - Growth Package (10 hours)", "close_date": "2026-01-01", "line_items": [],
        }
        packages, exceptions = infer_packages(opportunity, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(10.0, packages[0]["sold_hours"])
        self.assertEqual("opportunity_explicit_hours", packages[0]["inference_source"])

    def test_custom_is_unresolved_but_explicit_custom_hours_are_valid(self):
        unresolved = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Custom Outcomes Package",
            "close_date": "2026-01-01", "line_items": [],
        }
        packages, exceptions = infer_packages(unresolved, PACKAGE_CONFIG)
        self.assertEqual([], packages)
        self.assertEqual("unresolved_package", exceptions[0]["type"])

        explicit = copy.deepcopy(unresolved)
        explicit["name"] = "Custom 300 PS hours (Growth Package)"
        packages, exceptions = infer_packages(explicit, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(300.0, packages[0]["sold_hours"])

        explicit["line_items"] = [{"id": "L1", "name": "Glean Outcomes Packages: Custom", "quantity": 1}]
        packages, exceptions = infer_packages(explicit, PACKAGE_CONFIG)
        self.assertEqual([], exceptions)
        self.assertEqual(300.0, packages[0]["sold_hours"])
        self.assertEqual("opportunity_explicit_hours", packages[0]["inference_source"])

    def test_override_precedes_inference(self):
        config = copy.deepcopy(PACKAGE_CONFIG)
        config["overrides"]["opportunities"]["O1"] = 77
        opportunity = {
            "id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Starter Outcomes",
            "close_date": "2026-01-01", "line_items": [],
        }
        packages, _ = infer_packages(opportunity, config)
        self.assertEqual(77.0, packages[0]["sold_hours"])
        self.assertEqual("opportunity_override", packages[0]["inference_source"])


class MatchingTests(unittest.TestCase):
    def test_normalization_and_alias(self):
        self.assertEqual("acme", normalize_name(" Acme, Inc. "))
        accounts = [{"id": "A1", "name": "Orthogonal Networks (DBA Jellyfish)"}]
        projects = [{"id": "P1", "name": "Jellyfish Outcomes", "customer_name": "Jellyfish"}]
        mapping, exceptions = match_projects(accounts, projects, {"aliases": {accounts[0]["name"]: ["Jellyfish"]}})
        self.assertEqual({"P1": "A1"}, mapping)
        self.assertEqual([], exceptions)

    def test_normalization_collision_is_not_auto_matched(self):
        accounts = [{"id": "A1", "name": "Acme Inc."}, {"id": "A2", "name": "Acme LLC"}]
        mapping, exceptions = match_projects(accounts, [{"id": "P1", "customer_name": "Acme"}], {"aliases": {}})
        self.assertEqual({}, mapping)
        self.assertEqual("account_collision", exceptions[0]["type"])

    def test_fuzzy_match_is_suggestion_only(self):
        accounts = [{"id": "A1", "name": "Northstar Analytics"}]
        mapping, exceptions = match_projects(accounts, [{"id": "P1", "customer_name": "North Star Analytic"}], {"aliases": {}})
        self.assertEqual({}, mapping)
        self.assertEqual("unmatched_project", exceptions[0]["type"])
        self.assertEqual("Northstar Analytics", exceptions[0]["suggested_account"])


class ReconciliationTests(unittest.TestCase):
    def test_fifo_by_expiration_and_conservation(self):
        opportunities = [
            {"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2025-09-01", "line_items": []},
            {"id": "O2", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 50 hours", "close_date": "2025-12-01", "line_items": []},
        ]
        entries = [{"id": "T1", "project_id": "P1", "date": "2026-01-15", "minutes": 30 * 60, "billable": True, "user_email": "alex@example.com"}]
        sf, rl = sources(opportunities, entries)
        result = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 1, 20))
        account = result["accounts"][0]
        self.assertEqual([20.0, 10.0], [item["consumed_hours"] for item in account["packages"]])
        self.assertEqual(40.0, account["remaining_hours"])
        self.assertEqual(30.0, account["billed_hours"])
        self.assertEqual(account["billed_hours"], account["consumed_hours"] + account["overage_hours"])

    def test_pre_entitlement_activity_consumes_later_closed_package(self):
        opportunities = [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2025-11-11", "line_items": []}]
        entries = [
            {"id": "T1", "project_id": "P1", "date": "2025-11-10", "minutes": 30, "billable": True, "user_email": "alex@example.com"},
            {"id": "T2", "project_id": "P1", "date": "2025-11-12", "minutes": 240, "billable": True, "user_email": "alex@example.com"},
        ]
        sf, rl = sources(opportunities, entries)
        result = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 1, 1))
        account = result["accounts"][0]
        self.assertEqual(4.5, account["billed_hours"])
        self.assertEqual(4.5, account["consumed_hours"])
        self.assertEqual(15.5, account["remaining_hours"])
        self.assertEqual(0.0, account["overage_hours"])
        self.assertEqual(0.5, account["pre_entitlement_hours"])
        self.assertEqual(1, account["pre_entitlement_entry_count"])
        self.assertTrue(account["entries"][0]["pre_entitlement_hours"] > 0)
        self.assertTrue(any(item["type"] == "pre_entitlement_activity" for item in result["exceptions"]))

    def test_entry_after_expiration_becomes_overage(self):
        opportunities = [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2025-01-01", "line_items": []}]
        entries = [{"id": "T1", "project_id": "P1", "date": "2026-01-02", "minutes": 5 * 60, "billable": True, "user_email": "alex@example.com"}]
        sf, rl = sources(opportunities, entries)
        account = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 1, 3))["accounts"][0]
        self.assertEqual(5.0, account["overage_hours"])
        self.assertEqual(20.0, account["expired_unused_hours"])
        self.assertEqual("overage", account["risk"])

    def test_expiration_date_is_inclusive(self):
        opportunities = [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2025-01-01", "line_items": []}]
        entries = [{"id": "T1", "project_id": "P1", "date": "2026-01-01", "minutes": 5 * 60, "billable": True, "user_email": "alex@example.com"}]
        sf, rl = sources(opportunities, entries)
        account = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 1, 1))["accounts"][0]
        self.assertEqual(0.0, account["overage_hours"])
        self.assertEqual(15.0, account["remaining_hours"])

    def test_weekly_account_and_aiom_flags_are_separate(self):
        opportunities = [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2026-01-01", "line_items": []}]
        entries = [{"id": "T1", "project_id": "P1", "date": "2026-07-21", "minutes": 60, "billable": True, "user_email": "someone@example.com"}]
        sf, rl = sources(opportunities, entries)
        weekly = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 7, 22))["accounts"][0]["weekly"]
        self.assertTrue(weekly["account_active_current"])
        self.assertFalse(weekly["aiom_active_current"])

    def test_non_billable_entries_are_excluded(self):
        opportunities = [{"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2026-01-01", "line_items": []}]
        entries = [{"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 600, "billable": False, "user_email": "alex@example.com"}]
        sf, rl = sources(opportunities, entries)
        account = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 2))["accounts"][0]
        self.assertEqual(0.0, account["billed_hours"])
        self.assertEqual(20.0, account["remaining_hours"])

    def test_input_order_does_not_change_result(self):
        opportunities = [
            {"id": "O1", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 20 hours", "close_date": "2025-09-01", "line_items": []},
            {"id": "O2", "account_id": "A1", "account_name": "Acme", "name": "Growth Package 50 hours", "close_date": "2025-12-01", "line_items": []},
        ]
        entries = [
            {"id": "T2", "project_id": "P1", "date": "2026-02-01", "minutes": 600, "billable": True, "user_email": "alex@example.com"},
            {"id": "T1", "project_id": "P1", "date": "2026-01-01", "minutes": 1200, "billable": True, "user_email": "alex@example.com"},
        ]
        sf, rl = sources(opportunities, entries)
        first = reconcile(sf, rl, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 2))["accounts"][0]
        sf2, rl2 = sources(list(reversed(opportunities)), list(reversed(entries)))
        second = reconcile(sf2, rl2, package_config=PACKAGE_CONFIG, account_aliases={"aliases": {}}, as_of=date(2026, 2, 2))["accounts"][0]
        self.assertEqual(first["packages"], second["packages"])
        self.assertEqual(first["allocations"], second["allocations"])

    def test_risk_boundaries(self):
        as_of = date(2026, 1, 1)
        self.assertEqual("critical", package_risk(__import__('decimal').Decimal('1'), date(2026, 1, 31), as_of)[0])  # 30
        self.assertEqual("high", package_risk(__import__('decimal').Decimal('1'), date(2026, 2, 1), as_of)[0])       # 31
        self.assertEqual("high", package_risk(__import__('decimal').Decimal('1'), date(2026, 3, 2), as_of)[0])       # 60
        self.assertEqual("medium", package_risk(__import__('decimal').Decimal('1'), date(2026, 3, 3), as_of)[0])     # 61
        self.assertEqual("medium", package_risk(__import__('decimal').Decimal('1'), date(2026, 4, 1), as_of)[0])     # 90
        self.assertEqual("healthy", package_risk(__import__('decimal').Decimal('1'), date(2026, 4, 2), as_of)[0])    # 91
        self.assertEqual("expired", package_risk(__import__('decimal').Decimal('1'), date(2025, 12, 31), as_of)[0])
        self.assertEqual("exhausted", package_risk(__import__('decimal').Decimal('0'), date(2025, 12, 31), as_of)[0])


if __name__ == "__main__":
    unittest.main()
