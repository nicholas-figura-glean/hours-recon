"""Tests for the batched MCP pull: normalization, coverage, and validation."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from hours_recon.config import ROOT, binding_freshness, load_json, load_json_optional
from hours_recon.dates import business_today
from hours_recon.mcp_normalize import (
    RawPullError,
    normalize_raw_pull,
    normalize_time_entries,
    _records,
    _subquery_rows,
)
from hours_recon.mcp_snapshot import McpSnapshotError, publish_mcp_snapshot
from hours_recon.reconcile import reconcile
from hours_recon.mcp_validate import validate_refresh, validate_snapshot

TIMEZONE = "America/Denver"
REQUESTER = "nick.figura@glean.com"
SCOPE_ID = "salesforce:00D4S000000Ghf0UAC|rocketlane:glean.rocketlane.com"

PACKAGES = load_json(ROOT / "config" / "packages.json")
ALIASES = {"aliases": {"Quote Only Inc": ["QuoteOnly"]}, "rocketlane_customer_ids": {}}


def _today():
    return business_today(TIMEZONE)


def _iso(offset_days: int) -> str:
    return (_today() + timedelta(days=offset_days)).isoformat()


def raw_pull() -> dict:
    """A raw pull shaped exactly like the batched SOQL and Rocketlane payloads."""
    return {
        "meta": {
            "report_date": _today().isoformat(),
            "retrieval_id": "test-retrieval-0001",
            "scope": "test portfolio",
            "scope_id": SCOPE_ID,
            "scope_verified": True,
            "salesforce_org_id": "00D4S000000Ghf0UAC",
            "salesforce_mcp_server": "sf-server",
            "rocketlane_mcp_server": "rl-server",
            "bindings_source": "pinned",
            "identity_evidence": {"salesforce_org_id": "00D4S000000Ghf0UAC"},
        },
        "salesforce": {
            "requester": {"id": "005TEST", "name": "Nick Figura", "email": REQUESTER},
            "aiom_field": "AIOM__c",
            "account_records": [
                {
                    "attributes": {"type": "Account"},
                    "Id": "001000000000001AAA",
                    "Name": "Cprime Test",
                    "OwnerId": "005OWNER1",
                    "Owner": {"attributes": {}, "Id": "005OWNER1", "Name": "Owner One", "Email": "one@glean.com"},
                },
                {"Id": "001000000000002AAA", "Name": "Zero Opp Co"},
                {"Id": "001000000000003AAA", "Name": "Quote Only Inc"},
            ],
            "opportunity_records": [
                {
                    "attributes": {"type": "Opportunity"},
                    "Id": "006000000000001AAA",
                    "AccountId": "001000000000001AAA",
                    "Account": {"Name": "Cprime Test"},
                    "Name": "Cprime Outcomes",
                    "StageName": "Closed Won",
                    "IsWon": True,
                    "IsClosed": True,
                    "CloseDate": _iso(-30),
                    "Amount": 50000,
                    # Parent-child subquery envelope, exactly as SOQL returns it.
                    "OpportunityLineItems": {
                        "totalSize": 1,
                        "done": True,
                        "records": [
                            {
                                "attributes": {"type": "OpportunityLineItem"},
                                "Id": "00k000000000001AAA",
                                "Quantity": 1,
                                "UnitPrice": 50000,
                                "TotalPrice": 50000,
                                "Product2Id": "01t0001",
                                "Product2": {"Name": "Outcomes Select", "ProductCode": "Glean-Outcomes-Packages-Select"},
                                "PricebookEntryId": "01u0001",
                                "PricebookEntry": {"Name": "Select", "UnitPrice": 50000},
                            }
                        ],
                    },
                },
                {
                    "Id": "006000000000002AAA",
                    "AccountId": "001000000000003AAA",
                    "Account": {"Name": "Quote Only Inc"},
                    "Name": "Quote Only Renewal",
                    "StageName": "Closed Won",
                    "IsWon": True,
                    "CloseDate": _iso(-60),
                    # Salesforce returns null, not an empty envelope, for no children.
                    "OpportunityLineItems": None,
                    "Approved_Quote__c": "0Q0APPROVED",
                    "Ruby__PrimaryQuote__c": "0Q0PRIMARY",
                },
            ],
            "quote_line_records": [
                {
                    "attributes": {"type": "QuoteLineItem"},
                    "Id": "0QL000000000001AAA",
                    "QuoteId": "0Q0APPROVED",
                    "Quantity": 2,
                    "UnitPrice": 10000,
                    "ListPrice": 10000,
                    "Product2Id": "01t0002",
                    "Product2": {"Name": "Outcomes Starter", "ProductCode": "Glean-Outcomes-Packages-Starter"},
                },
                {
                    "Id": "0QL000000000002AAA",
                    "QuoteId": "0Q0PRIMARY",
                    "Quantity": 9,
                    "UnitPrice": 10000,
                    "Product2": {"Name": "Outcomes Starter", "ProductCode": "Glean-Outcomes-Packages-Starter"},
                },
            ],
            "quote_records": [
                {"Id": "0Q0APPROVED", "Name": "Approved Quote", "AccountId": "001000000000003AAA", "Status": "Approved"},
            ],
            "pagination": [
                {"label": "accounts", "done": True, "total_size": 3, "returned": 3},
                {"label": "opportunities", "done": True, "total_size": 2, "returned": 2},
                {"label": "quote_lines", "done": True, "total_size": 2, "returned": 2},
            ],
        },
        "rocketlane": {
            "requester": {"id": "752101", "name": "Nick Figura", "email": REQUESTER},
            "project_records": [
                {
                    "projectId": 964197,
                    "projectName": "Cprime Test Onboarding",
                    "customer": {"companyId": 445682, "companyName": "Cprime Test"},
                    "archived": False,
                    "status": {"label": "In Progress"},
                    "startDate": _iso(-25),
                    "externalReferenceId": "001000000000001AAA",
                    "owner": {"userId": 9001, "firstName": "Pat", "lastName": "Owner", "emailId": "pat@glean.com"},
                    "fields": [{"fieldLabel": "Account Name", "value": "Cprime Test"}],
                },
                {
                    "projectId": 970816,
                    "projectName": "QuoteOnly Deployment",
                    "customer": {"companyId": 458270, "companyName": "Quote Only Inc"},
                    "archived": True,
                    "status": {"label": "Completed"},
                },
            ],
            "time_entry_records": [
                {
                    "timeEntryId": 5001,
                    "project": {"projectId": 964197, "projectName": "Cprime Test Onboarding"},
                    "date": _iso(-3),
                    "minutes": 120,
                    "billable": True,
                    "approvalStatus": "APPROVED",
                    "user": {"userId": 752101, "firstName": "Nick", "lastName": "Figura", "emailId": REQUESTER},
                    "category": {"categoryId": 7, "categoryName": "Consulting"},
                },
                {
                    "timeEntryId": 5002,
                    "project": {"projectId": 964197},
                    "date": _iso(-2),
                    "minutes": 90,
                    "billable": True,
                    "approvalStatus": "SUBMITTED",
                    "user": {"userId": 752101, "emailId": REQUESTER},
                },
                # Duplicate delivered by an overlapping page; must be dropped.
                {
                    "timeEntryId": 5002,
                    "project": {"projectId": 964197},
                    "date": _iso(-2),
                    "minutes": 90,
                    "billable": True,
                },
                {
                    "timeEntryId": 5003,
                    "project": {"projectId": 970816},
                    "date": _iso(-10),
                    "minutes": 60,
                    "billable": True,
                    "approvalStatus": "APPROVED",
                    "user": {"userId": 888, "emailId": "someone.else@glean.com"},
                },
            ],
            "project_search_audit": [
                {"query": "Cprime Test", "count": 1, "has_more": False},
                {"query": "Zero Opp Co", "count": 0, "has_more": False},
                {"query": "Quote Only Inc", "count": 1, "has_more": False},
                {"query": "QuoteOnly", "count": 1, "has_more": False},
            ],
            "time_pagination_audit": [
                {"project_id": "964197", "count": 2, "has_more": False},
                {"project_id": "970816", "count": 1, "has_more": False},
            ],
        },
    }


def build_snapshot(raw=None):
    return normalize_raw_pull(
        raw or raw_pull(),
        account_aliases=ALIASES,
        timezone_name=TIMEZONE,
        bindings=load_json_optional(ROOT / "config" / "mcp_bindings.json"),
    )


def _columnar_raw() -> dict:
    """The same pull expressed in the compact columnar form."""
    raw = raw_pull()
    sf = raw["salesforce"]
    rl = raw["rocketlane"]

    sf["account_records"] = {
        "columns": ["Id", "Name", "OwnerId", "Owner.Id", "Owner.Name", "Owner.Email"],
        "rows": [
            ["001000000000001AAA", "Cprime Test", "005OWNER1", "005OWNER1", "Owner One", "one@glean.com"],
            ["001000000000002AAA", "Zero Opp Co", None, None, None, None],
            ["001000000000003AAA", "Quote Only Inc", None, None, None, None],
        ],
    }
    sf["opportunity_records"] = {
        "columns": [
            "Id", "AccountId", "Account.Name", "Name", "StageName", "IsWon", "IsClosed", "CloseDate",
            "Amount", "Approved_Quote__c", "Ruby__PrimaryQuote__c",
        ],
        "rows": [
            ["006000000000001AAA", "001000000000001AAA", "Cprime Test", "Cprime Outcomes",
             "Closed Won", True, True, _iso(-30), 50000, None, None],
            ["006000000000002AAA", "001000000000003AAA", "Quote Only Inc", "Quote Only Renewal",
             "Closed Won", True, None, _iso(-60), None, "0Q0APPROVED", "0Q0PRIMARY"],
        ],
    }
    # Line items travel as one flat block keyed by OpportunityId instead of a
    # nested envelope repeated inside every parent record.
    sf["line_item_records"] = {
        "columns": [
            "Id", "OpportunityId", "Quantity", "UnitPrice", "TotalPrice", "Product2Id",
            "Product2.Name", "Product2.ProductCode", "PricebookEntryId", "PricebookEntry.Name",
            "PricebookEntry.UnitPrice",
        ],
        "rows": [[
            "00k000000000001AAA", "006000000000001AAA", 1, 50000, 50000, "01t0001",
            "Outcomes Select", "Glean-Outcomes-Packages-Select", "01u0001", "Select", 50000,
        ]],
    }
    rl["time_entry_records"] = {
        "columns": [
            "timeEntryId", "project.projectId", "project.projectName", "date", "minutes", "billable",
            "approvalStatus", "category.categoryId", "category.categoryName",
            "user.userId", "user.firstName", "user.lastName", "user.emailId",
        ],
        "rows": [
            [5001, 964197, "Cprime Test Onboarding", _iso(-3), 120, True, "APPROVED", 7, "Consulting",
             752101, "Nick", "Figura", REQUESTER],
            [5002, 964197, None, _iso(-2), 90, True, "SUBMITTED", None, None, 752101, None, None, REQUESTER],
            [5002, 964197, None, _iso(-2), 90, True, None, None, None, None, None, None, None],
            [5003, 970816, None, _iso(-10), 60, True, "APPROVED", None, None, 888, None, None,
             "someone.else@glean.com"],
        ],
    }
    return raw


def _validated(snapshot, report=None):
    return validate_refresh(
        snapshot,
        report,
        package_config=PACKAGES,
        account_aliases=ALIASES,
        expected_requester_email=REQUESTER,
        expected_scope_id=SCOPE_ID,
        timezone_name=TIMEZONE,
    )


def _failed_checks(result):
    return {item["check"] for item in result["failures"]}


class NormalizationTests(unittest.TestCase):
    def test_subquery_envelope_shapes_are_equivalent(self):
        assert _subquery_rows(None) == []
        assert _subquery_rows({"records": []}) == []
        assert _subquery_rows({"records": [{"Id": "a", "attributes": {"type": "x"}}]}) == [{"Id": "a"}]
        assert _subquery_rows([{"Id": "a"}]) == [{"Id": "a"}]

    def test_columnar_block_expands_dotted_paths(self):
        expanded = _records({
            "columns": ["Id", "Product2.Name", "Product2.ProductCode"],
            "rows": [["00k1", "Outcomes Select", "Glean-Outcomes-Packages-Select"]],
        })
        assert expanded == [
            {"Id": "00k1", "Product2": {"Name": "Outcomes Select", "ProductCode": "Glean-Outcomes-Packages-Select"}}
        ]

    def test_short_columnar_rows_pad_with_none(self):
        assert _records({"columns": ["a", "b"], "rows": [[1]]}) == [{"a": 1, "b": None}]

    def test_columnar_and_verbose_pulls_produce_identical_snapshots(self):
        verbose = build_snapshot()
        columnar = build_snapshot(_columnar_raw())
        for snapshot in (verbose, columnar):
            snapshot["meta"].pop("created_at")
        assert columnar == verbose

    def test_columnar_pull_passes_validation(self):
        assert _validated(build_snapshot(_columnar_raw()))["ok"]

    def test_normalizes_batched_pull_into_publishable_snapshot(self):
        snapshot = build_snapshot()
        assert snapshot["schema_version"] == 1
        assert snapshot["meta"]["source_counts"] == {
            "accounts": 3,
            "opportunities": 2,
            "line_items": 2,
            "projects": 2,
            "time_entries": 3,
        }
        assert snapshot["meta"]["coverage"]["complete"] is True
        assert snapshot["salesforce"]["metadata"]["aiom_field"] == "AIOM__c"

    def test_opportunity_line_items_take_precedence_over_quote_lines(self):
        snapshot = build_snapshot()
        by_id = {item["id"]: item for item in snapshot["salesforce"]["opportunities"]}
        with_lines = by_id["006000000000001AAA"]
        assert with_lines["line_item_source"] == "opportunity_line_item"
        assert [line["product_code"] for line in with_lines["line_items"]] == ["Glean-Outcomes-Packages-Select"]

    def test_approved_quote_wins_over_primary_and_never_combines(self):
        snapshot = build_snapshot()
        by_id = {item["id"]: item for item in snapshot["salesforce"]["opportunities"]}
        fallback = by_id["006000000000002AAA"]
        assert fallback["line_item_source"] == "approved_quote"
        # Exactly the approved quote's line, never merged with the primary quote's.
        assert [line["id"] for line in fallback["line_items"]] == ["0QL000000000001AAA"]
        assert {line["source"] for line in fallback["line_items"]} == {"approved_quote"}
        assert fallback["line_items"][0]["quote_id"] == "0Q0APPROVED"

    def test_primary_quote_used_when_no_approved_quote(self):
        raw = raw_pull()
        raw["salesforce"]["opportunity_records"][1].pop("Approved_Quote__c")
        snapshot = build_snapshot(raw)
        fallback = snapshot["salesforce"]["opportunities"][0]
        assert fallback["line_item_source"] == "primary_quote"
        assert fallback["line_items"][0]["quantity"] == 9

    def test_time_entries_are_deduplicated_by_id(self):
        snapshot = build_snapshot()
        ids = [entry["id"] for entry in snapshot["rocketlane"]["entries"]]
        assert ids == sorted(set(ids), key=ids.index)
        assert len(ids) == 3

    def test_snapshot_schema_is_locked_to_consumed_fields(self):
        """Pin the emitted field set.

        Every field below is read by the reconciliation engine, the remediation
        workflow, the validator, or the dashboard. Adding one here means the
        agent pays to write it on every refresh, so additions should be
        deliberate: add the consumer first, then widen this test.
        """
        snapshot = build_snapshot()

        def union(rows):
            keys = set()
            for row in rows:
                keys |= set(row)
            return keys

        accounts = snapshot["salesforce"]["accounts"]
        opportunities = snapshot["salesforce"]["opportunities"]
        line_items = [line for item in opportunities for line in item["line_items"]]
        projects = snapshot["rocketlane"]["projects"]
        entries = snapshot["rocketlane"]["entries"]

        self.assertEqual(union(accounts), {"id", "name", "owner_name", "owner_email"})
        self.assertEqual(union(opportunities), {
            "id", "account_id", "account_name", "name", "close_date", "owner_name", "owner_email",
            "service_start_date", "service_end_date", "entitlement_disposition",
            "line_item_source", "line_items",
        })
        self.assertEqual(union(line_items), {
            "id", "source", "name", "product_id", "product_code", "pricebook_entry_id",
            "quantity", "unit_price", "list_price", "service_start_date", "service_end_date", "quote_id",
        })
        self.assertEqual(union(projects), {
            "id", "name", "customer_id", "customer_name", "account_name", "archived", "status",
            "start_date", "due_date", "salesforce_account_id", "opportunity_id",
            "owner_name", "owner_email",
        })
        self.assertEqual(union(entries), {
            "id", "project_id", "project_name", "date", "minutes", "billable", "approval_status",
            "activity_name", "category", "user_id", "user_name", "user_email",
        })
        # Nothing reads a quote record, so quotes are not emitted at all; they
        # are still normalized internally for service-period inheritance.
        self.assertNotIn("quotes", snapshot["salesforce"])

    def test_quote_lines_still_inherit_the_quote_service_window(self):
        raw = raw_pull()
        # Strip the line's own dates so inheritance is the only source.
        raw["salesforce"]["quote_records"] = [{
            "Id": "0Q0APPROVED", "Ruby__StartDate__c": "2026-01-01", "Ruby__EndDate__c": "2026-12-31",
        }]
        snapshot = build_snapshot(raw)
        fallback = next(
            item for item in snapshot["salesforce"]["opportunities"]
            if item["line_item_source"] == "approved_quote"
        )
        self.assertEqual(fallback["line_items"][0]["service_start_date"], "2026-01-01")
        self.assertEqual(fallback["line_items"][0]["service_end_date"], "2026-12-31")
        # And the opportunity window is derived from those line dates.
        self.assertEqual(fallback["service_start_date"], "2026-01-01")
        self.assertEqual(fallback["service_end_date"], "2026-12-31")

    def test_project_external_reference_is_promoted_to_account_id(self):
        snapshot = build_snapshot()
        by_id = {item["id"]: item for item in snapshot["rocketlane"]["projects"]}
        assert by_id["964197"]["salesforce_account_id"] == "001000000000001AAA"
        # A project with no Account-shaped reference must not be promoted.
        assert by_id["970816"]["salesforce_account_id"] is None
        # The raw reference itself is not retained; only the derived link is read.
        assert "external_reference_id" not in by_id["964197"]

    def test_account_with_zero_opportunities_is_audited_explicitly(self):
        snapshot = build_snapshot()
        audit = {row["account_id"]: row for row in snapshot["meta"]["account_retrieval_audit"]}
        assert set(audit) == {"001000000000001AAA", "001000000000002AAA", "001000000000003AAA"}
        # The distinction the per-account query loop used to provide: an account
        # with no Closed Won opportunities is recorded, not merely absent.
        assert audit["001000000000002AAA"]["opportunity_count"] == 0
        assert audit["001000000000002AAA"]["opportunities_done"] is True
        assert audit["001000000000003AAA"]["quote_fallbacks_audited"] == 1

    def test_stale_report_date_is_rejected(self):
        raw = raw_pull()
        raw["meta"]["report_date"] = _iso(-1)
        with self.assertRaisesRegex(RawPullError, "does not match the current report date"):
            build_snapshot(raw)

    def test_opportunity_outside_requested_scope_is_rejected(self):
        raw = raw_pull()
        raw["salesforce"]["opportunity_records"][0]["AccountId"] = "001999999999999AAA"
        with self.assertRaisesRegex(RawPullError, "outside the requested scope"):
            build_snapshot(raw)

    def test_missing_requester_email_is_rejected(self):
        raw = raw_pull()
        raw["salesforce"]["requester"] = {"id": "005TEST"}
        with self.assertRaisesRegex(RawPullError, "requester email"):
            build_snapshot(raw)

    def test_normalize_time_entries_falls_back_to_project_id(self):
        entries = normalize_time_entries([{"timeEntryId": 1, "minutes": 30, "billable": True}], fallback_project_id="42")
        assert entries[0]["project_id"] == "42"


class CoverageTests(unittest.TestCase):
    def test_unfollowed_pagination_blocks_completeness(self):
        raw = raw_pull()
        raw["rocketlane"]["time_pagination_audit"][0] = {
            "project_id": "964197", "count": 2, "has_more": True, "followed_next_page": False,
        }
        snapshot = build_snapshot(raw)
        assert snapshot["meta"]["coverage"]["pagination_complete"] is False
        assert snapshot["meta"]["coverage"]["complete"] is False

    def test_followed_pagination_still_counts_as_terminal(self):
        raw = raw_pull()
        raw["rocketlane"]["time_pagination_audit"][0] = {
            "project_id": "964197", "count": 2, "has_more": True, "followed_next_page": True,
        }
        assert build_snapshot(raw)["meta"]["coverage"]["complete"] is True

    def test_unsearched_alias_blocks_project_coverage(self):
        raw = raw_pull()
        raw["rocketlane"]["project_search_audit"] = [
            entry for entry in raw["rocketlane"]["project_search_audit"] if entry["query"] != "QuoteOnly"
        ]
        snapshot = build_snapshot(raw)
        assert snapshot["meta"]["coverage"]["projects"] is False
        assert snapshot["meta"]["coverage"]["unsearched_account_queries"] == ["quoteonly"]

    def test_project_without_time_audit_blocks_coverage(self):
        raw = raw_pull()
        raw["rocketlane"]["time_pagination_audit"].pop()
        snapshot = build_snapshot(raw)
        assert snapshot["meta"]["coverage"]["time_entries"] is False
        assert snapshot["meta"]["coverage"]["unaudited_project_ids"] == ["970816"]

    def test_incomplete_coverage_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as _tmp:
            tmp_path = Path(_tmp)
            raw = raw_pull()
            raw["rocketlane"]["time_pagination_audit"].pop()
            snapshot = build_snapshot(raw)
            with self.assertRaisesRegex(McpSnapshotError, "incomplete source coverage"):
                publish_mcp_snapshot(
                    tmp_path / "snapshot.json",
                    snapshot,
                    expected_requester_email=REQUESTER,
                    expected_scope_id=SCOPE_ID,
                    timezone_name=TIMEZONE,
                )

    def test_complete_pull_publishes(self):
        with tempfile.TemporaryDirectory() as _tmp:
            tmp_path = Path(_tmp)
            target = tmp_path / "nested" / "snapshot.json"
            publish_mcp_snapshot(
                target,
                build_snapshot(),
                expected_requester_email=REQUESTER,
                expected_scope_id=SCOPE_ID,
                timezone_name=TIMEZONE,
            )
            assert target.exists()
            assert oct(target.stat().st_mode)[-3:] == "600"


class ValidationTests(unittest.TestCase):
    def test_normalized_pull_passes_every_check_end_to_end(self):
        snapshot = build_snapshot()
        report = reconcile(
            snapshot["salesforce"],
            snapshot["rocketlane"],
            package_config=PACKAGES,
            account_aliases=ALIASES,
            as_of=_today(),
            mode="mcp",
            source_coverage=snapshot["meta"]["coverage"],
        )
        result = _validated(snapshot, report)
        assert result["ok"], result["failures"]
        # 100h from the Select line item plus 2 x 20h from the approved quote lines.
        assert report["metrics"]["sold_hours"] == 140.0
        assert report["metrics"]["billed_hours"] == 4.5

    def test_validator_catches_duplicated_line_items(self):
        snapshot = build_snapshot()
        opportunity = snapshot["salesforce"]["opportunities"][1]
        opportunity["line_items"].append(copy.deepcopy(opportunity["line_items"][0]))
        assert "no_duplicate_line_items" in _failed_checks(_validated(snapshot))

    def test_validator_catches_mixed_line_item_sources(self):
        snapshot = build_snapshot()
        opportunity = next(
            item for item in snapshot["salesforce"]["opportunities"] if item["line_item_source"] == "approved_quote"
        )
        extra = copy.deepcopy(opportunity["line_items"][0])
        extra["id"] = "0QLOTHER"
        extra["source"] = "opportunity_line_item"
        opportunity["line_items"].append(extra)
        assert "one_line_item_source_per_opportunity" in _failed_checks(_validated(snapshot))

    def test_validator_catches_count_drift(self):
        snapshot = build_snapshot()
        snapshot["meta"]["source_counts"]["time_entries"] = 99
        assert "source_counts_match_payload" in _failed_checks(_validated(snapshot))

    def test_validator_catches_wrong_requester_and_scope(self):
        snapshot = build_snapshot()
        snapshot["salesforce"]["requester"]["email"] = "someone.else@glean.com"
        snapshot["meta"]["scope_id"] = "salesforce:OTHER"
        failed = _failed_checks(_validated(snapshot))
        assert {"requester_matches_configuration", "scope_matches_configuration"} <= failed

    def test_validator_catches_unverified_scope(self):
        snapshot = build_snapshot()
        snapshot["meta"]["scope_verified"] = False
        assert "scope_verified" in _failed_checks(_validated(snapshot))

    def test_validator_catches_sold_and_billed_drift(self):
        snapshot = build_snapshot()
        report = reconcile(
            snapshot["salesforce"],
            snapshot["rocketlane"],
            package_config=PACKAGES,
            account_aliases=ALIASES,
            as_of=_today(),
            mode="mcp",
            source_coverage=snapshot["meta"]["coverage"],
        )
        report["metrics"]["sold_hours"] = 999.0
        report["metrics"]["billed_hours"] = 999.0
        failed = _failed_checks(_validated(snapshot, report))
        assert {"sold_hours_equal_inferred_packages", "billed_hours_equal_source_minutes"} <= failed

    def test_validator_catches_governance_decomposition_drift(self):
        snapshot = build_snapshot()
        report = reconcile(
            snapshot["salesforce"],
            snapshot["rocketlane"],
            package_config=PACKAGES,
            account_aliases=ALIASES,
            as_of=_today(),
            mode="mcp",
            governance_mode="observe_only",
            source_coverage=snapshot["meta"]["coverage"],
        )
        report["governance"]["metrics"]["sold_hours"]["governed"] += 5
        assert "governed_plus_provisional_equals_reported" in _failed_checks(_validated(snapshot, report))

    def test_validator_flags_stale_snapshot_date(self):
        snapshot = build_snapshot()
        snapshot["meta"]["through_date"] = _iso(-2)
        assert "through_date_is_report_date" in _failed_checks(_validated(snapshot))

    def test_validate_snapshot_accepts_no_expectations(self):
        findings = validate_snapshot(build_snapshot(), timezone_name=TIMEZONE)
        assert all(item["ok"] for item in findings), [i for i in findings if not i["ok"]]


class BindingsTests(unittest.TestCase):
    def test_committed_bindings_are_loadable_and_shaped(self):
        bindings = load_json_optional(ROOT / "config" / "mcp_bindings.json")
        assert bindings["schema_version"] == 1
        assert bindings["salesforce"]["account_aiom_field"] == "AIOM__c"
        assert bindings["rocketlane"]["tools"]["time_entries"] == "get_time_entries"

    def test_missing_bindings_file_degrades_to_rediscovery(self):
        with tempfile.TemporaryDirectory() as _tmp:
            tmp_path = Path(_tmp)
            assert load_json_optional(tmp_path / "absent.json") == {}
            assert binding_freshness({})["fresh"] is False

    def test_corrupt_bindings_file_degrades_to_rediscovery(self):
        with tempfile.TemporaryDirectory() as _tmp:
            tmp_path = Path(_tmp)
            corrupt = tmp_path / "bindings.json"
            corrupt.write_text("{not json", encoding="utf-8")
            assert load_json_optional(corrupt) == {}

    def test_bindings_expire_after_ttl(self):
        today = _today()
        base = {
            "schema_version": 1,
            "ttl_days": 7,
            "salesforce": {"mcp_server": "a", "account_aiom_field": "AIOM__c"},
            "rocketlane": {"mcp_server": "b"},
        }
        fresh = dict(base, verified_on=(today - timedelta(days=3)).isoformat())
        stale = dict(base, verified_on=(today - timedelta(days=30)).isoformat())
        assert binding_freshness(fresh, today)["fresh"] is True
        assert binding_freshness(stale, today)["fresh"] is False
        assert "TTL" in binding_freshness(stale, today)["reason"]

    def test_bindings_missing_required_keys_are_not_fresh(self):
        today = _today()
        incomplete = {
            "schema_version": 1,
            "verified_on": today.isoformat(),
            "salesforce": {"mcp_server": "a"},
            "rocketlane": {"mcp_server": "b"},
        }
        status = binding_freshness(incomplete, today)
        assert status["fresh"] is False
        assert "account_aiom_field" in status["reason"]


if __name__ == "__main__":
    unittest.main()
