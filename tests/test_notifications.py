"""Threshold notification detection, dedup, digest assembly, and rendering."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from hours_recon.consumption import (
    attach_consumption,
    economic_key,
    entitlement_key,
    package_label,
    package_rows,
)
from hours_recon.notifications import (
    DEFAULT_POLICY,
    account_unapproved_hours,
    assert_allowlisted,
    detect,
    digest_window_key,
    group_for_digest,
    load_policy,
    render_digest,
    render_subject,
    resolve_recipients,
)
from hours_recon.remediation_store import (
    QueueConflict,
    QueueValidationError,
    RemediationStore,
)

SCOPE = "salesforce:00D|rocketlane:ws"
PORTFOLIO = "aiom@glean.com"
POLICY = load_policy(None)


def _package(package_id="006A:00kA", sold=20.0, consumed=0.0, *, family="outcome", tier="Starter",
             close="2026-01-01", expiration="2027-01-01", opportunity_id="006A", line_item_name=None):
    return {
        "id": package_id,
        "opportunity_id": opportunity_id,
        "opportunity_name": "Renewal FY26",
        "line_item_name": line_item_name,
        "family": family,
        "tier": tier,
        "sold_hours": sold,
        "consumed_hours": consumed,
        "remaining_hours": round(sold - consumed, 2),
        "close_date": close,
        "expiration_date": expiration,
        "days_to_expiration": 120,
        "risk": "healthy",
    }


def _report(packages, *, account_id="001A", name="Acme", owner="owner@glean.com",
            entries=None, overage=0.0):
    sold = sum(float(item["sold_hours"]) for item in packages)
    consumed = sum(float(item["consumed_hours"]) for item in packages)
    report = {
        "meta": {"as_of": "2026-08-03", "mcp_retrieval_id": "retr-1", "mcp_through_date": "2026-08-03"},
        "accounts": [{
            "id": account_id, "name": name, "owner_email": owner, "owner_name": "Owner",
            "packages": packages, "entries": entries or [],
            "sold_hours": sold, "consumed_hours": consumed,
            "remaining_hours": round(sold - consumed, 2), "overage_hours": overage,
            "unapplied_correction_hours": 0.0,
        }],
    }
    attach_consumption(report)
    return report


def _detect(report, state=(), **kwargs):
    options = {"policy": POLICY, "coverage_complete": True, "freshness": {"state": "current"}}
    options.update(kwargs)
    return detect(report, list(state), **options)


def _state_row(report, *, high_water, pct=None, consumed=None, package_index=0):
    row = package_rows(report)[package_index]
    return {
        "account_id": row["account_id"], "package_id": row["package_id"],
        "entitlement_key": row["entitlement_key"], "economic_key": row["economic_key"],
        "high_water_threshold": high_water,
        "high_water_pct": pct if pct is not None else float(row["consumption_pct"] or 0),
        "last_consumed_hours": consumed if consumed is not None else row["consumed_hours"],
        "last_sold_hours": row["sold_hours"],
    }


class ConsumptionTests(unittest.TestCase):
    def test_percentage_is_per_package(self):
        report = _report([_package("006A:1", sold=100.0, consumed=10.0),
                          _package("006A:2", sold=20.0, consumed=20.0)])
        rows = {item["package_id"]: item["consumption_pct"] for item in package_rows(report)}
        self.assertEqual(10.0, rows["006A:1"])
        self.assertEqual(100.0, rows["006A:2"])
        # The account rollup dilutes the two, which is exactly why alerting is
        # per package: the exhausted entitlement would otherwise be invisible.
        self.assertEqual(25.0, report["accounts"][0]["consumption_pct"])
        self.assertEqual(100.0, report["accounts"][0]["max_package_consumption_pct"])

    def test_zero_sold_hours_has_no_percentage(self):
        report = _report([_package(sold=0.0, consumed=0.0)])
        self.assertIsNone(package_rows(report)[0]["consumption_pct"])

    def test_consumed_hours_preferred_over_sold_minus_remaining(self):
        package = _package(sold=20.0, consumed=5.0)
        package["remaining_hours"] = 99.0
        report = _report([package])
        self.assertEqual(25.0, package_rows(report)[0]["consumption_pct"])

    def test_attach_consumption_is_idempotent(self):
        report = _report([_package(sold=20.0, consumed=11.5)])
        first = package_rows(report)
        attach_consumption(report)
        attach_consumption(report)
        self.assertEqual(first, package_rows(report))

    def test_entitlement_key_changes_only_with_economics(self):
        base = _package(sold=20.0)
        self.assertEqual(entitlement_key(base), entitlement_key(_package(sold=20.0, consumed=9.0)))
        self.assertNotEqual(entitlement_key(base), entitlement_key(_package(sold=40.0)))
        self.assertNotEqual(entitlement_key(base), entitlement_key(_package(expiration="2028-01-01")))
        self.assertEqual(entitlement_key(base), entitlement_key(_package("006A:00kZ")))
        self.assertEqual(economic_key(base), economic_key(_package("006A:00kZ")))

    def test_package_label_avoids_unformatted_tier(self):
        ugly = _package(family="growth", tier="2E+1 hours",
                        line_item_name="Glean Growth Packages: 20 hours")
        self.assertEqual("Glean Growth Packages: 20 hours", package_label(ugly))
        self.assertEqual("outcome Starter", package_label(_package()))


class DetectionTests(unittest.TestCase):
    def test_crossing_fires_once_and_not_again(self):
        report = _report([_package(sold=20.0, consumed=11.0)])
        first = _detect(report)
        self.assertEqual([50], [item["threshold"] for item in first["crossings"]])
        again = _detect(report, [_state_row(report, high_water=50)])
        self.assertEqual([], again["crossings"])

    def test_multi_rung_jump_collapses_to_highest(self):
        report = _report([_package(sold=20.0, consumed=19.0)])
        result = _detect(report)
        self.assertEqual(1, len(result["crossings"]))
        self.assertEqual(90, result["crossings"][0]["threshold"])
        self.assertEqual([50, 75], result["crossings"][0]["skipped_rungs"])

    def test_one_hundred_percent_is_reachable(self):
        report = _report([_package(sold=20.0, consumed=20.0)])
        self.assertEqual(100, _detect(report)["crossings"][0]["threshold"])

    def test_overage_is_reported_on_the_hundred_percent_crossing(self):
        report = _report([_package(sold=20.0, consumed=20.0)], overage=3.5)
        crossing = _detect(report)["crossings"][0]
        self.assertEqual(100, crossing["threshold"])
        self.assertEqual(3.5, crossing["overage_hours"])

    def test_full_consumption_is_never_downgraded_below_one_hundred(self):
        """Regression: epsilon hysteresis must not make the top rung unreachable.

        Consumption is bounded by the entitlement, so an exhausted package
        reports exactly 100.0 and can never clear 100 + epsilon.
        """
        for consumed in (20.0, 19.95):
            report = _report([_package(sold=20.0, consumed=consumed)])
            crossings = _detect(report)["crossings"]
            self.assertEqual(100 if consumed == 20.0 else 90, crossings[0]["threshold"])

    def test_ninety_nine_percent_does_not_reach_the_hundred_rung(self):
        report = _report([_package(sold=100.0, consumed=99.0)])
        self.assertEqual(90, _detect(report)["crossings"][0]["threshold"])

    def test_exactly_on_a_rung_does_not_fire(self):
        report = _report([_package(sold=20.0, consumed=10.0)])
        self.assertEqual([], _detect(report)["crossings"])

    def test_min_delta_hours_suppresses_a_rung_without_new_activity(self):
        report = _report([_package(sold=20.0, consumed=11.0)])
        prior = _state_row(report, high_water=0, pct=0.0, consumed=10.95)
        self.assertEqual([], _detect(report, [prior])["crossings"])

    def test_falling_usage_is_recorded_but_never_emailed(self):
        report = _report([_package(sold=20.0, consumed=8.0)])
        prior = _state_row(report, high_water=50, pct=60.0, consumed=12.0)
        result = _detect(report, [prior])
        self.assertEqual([], result["crossings"])
        self.assertTrue(result["observations"][0]["regressed"])
        self.assertIn("usage_regressed", [item["reason"] for item in result["diagnostics"]])

    def test_renewal_rearms_the_ladder(self):
        old = _report([_package(sold=20.0, consumed=11.0)])
        prior = _state_row(old, high_water=50)
        renewed = _report([_package("006B:1", sold=40.0, consumed=22.0, opportunity_id="006B")])
        self.assertEqual([50], [item["threshold"] for item in _detect(renewed, [prior])["crossings"]])

    def test_package_id_rotation_carries_the_high_water_mark(self):
        quote = _report([_package("006A:0QL1", sold=20.0, consumed=11.0)])
        prior = _state_row(quote, high_water=50)
        rotated = _report([_package("006A:00k9", sold=20.0, consumed=11.0)])
        result = _detect(rotated, [prior])
        self.assertEqual([], result["crossings"], "rotation must not re-notify")
        self.assertEqual(1, len(result["migrations"]))
        self.assertEqual("006A:0QL1", result["migrations"][0]["from_package_id"])
        self.assertEqual("006A:00k9", result["migrations"][0]["to_package_id"])

    def test_ambiguous_rotation_is_not_migrated(self):
        old = _report([_package("006A:0QL1", sold=20.0, consumed=11.0),
                       _package("006A:0QL2", sold=20.0, consumed=11.0)])
        priors = [_state_row(old, high_water=50, package_index=0),
                  _state_row(old, high_water=50, package_index=1)]
        rotated = _report([_package("006A:00k1", sold=20.0, consumed=11.0),
                           _package("006A:00k2", sold=20.0, consumed=11.0)])
        result = _detect(rotated, priors)
        self.assertEqual([], result["migrations"])
        self.assertIn("ambiguous_entitlement_rotation",
                      [item["reason"] for item in result["diagnostics"]])

    def test_incomplete_coverage_evaluates_nothing(self):
        report = _report([_package(sold=20.0, consumed=20.0)])
        result = _detect(report, coverage_complete=False)
        self.assertTrue(result["skipped"])
        self.assertEqual("source_coverage_incomplete", result["reason"])
        self.assertEqual([], result["crossings"])

    def test_stale_source_evaluates_nothing(self):
        report = _report([_package(sold=20.0, consumed=20.0)])
        result = _detect(report, freshness={"state": "stale"})
        self.assertTrue(result["skipped"])
        self.assertEqual([], result["crossings"])

    def test_package_without_entitlement_is_skipped(self):
        result = _detect(_report([_package(sold=0.0)]))
        self.assertEqual([], result["crossings"])
        self.assertEqual([], result["observations"])
        self.assertIn("no_entitlement", [item["reason"] for item in result["diagnostics"]])

    def test_unapproved_hours_are_counted_and_reported(self):
        entries = [
            {"id": "t1", "hours": 6.0, "approval_status": "APPROVED"},
            {"id": "t2", "hours": 5.0, "approval_status": "SUBMITTED"},
            {"id": "t3", "minutes": 30, "approval_status": ""},
        ]
        report = _report([_package(sold=20.0, consumed=11.5)], entries=entries)
        self.assertEqual(5.5, account_unapproved_hours(report["accounts"][0]))
        self.assertEqual(5.5, _detect(report)["crossings"][0]["unapproved_hours"])


class RecipientTests(unittest.TestCase):
    def _crossing(self, **kwargs):
        base = {"account_id": "001A", "account_name": "Acme",
                "account_owner_email": "owner@glean.com", "package_label": "Starter",
                "threshold": 50}
        base.update(kwargs)
        return base

    def test_owner_and_aiom_each_get_a_group(self):
        groups = resolve_recipients(self._crossing(), policy=POLICY, aiom_email="aiom@glean.com")
        self.assertEqual([("account_owner:owner@glean.com", ["owner@glean.com"]),
                          ("aiom:aiom@glean.com", ["aiom@glean.com"])], groups)

    def test_owner_outside_the_allowlist_is_dropped(self):
        groups = resolve_recipients(self._crossing(account_owner_email="partner@example.com"),
                                    policy=POLICY, aiom_email="aiom@glean.com")
        self.assertEqual([("aiom:aiom@glean.com", ["aiom@glean.com"])], groups)

    def test_account_override_replaces_role_defaults(self):
        policy = load_policy({"account_overrides": {"001A": {"recipients": ["lead@glean.com"]}}})
        groups = resolve_recipients(self._crossing(), policy=policy, aiom_email="aiom@glean.com")
        self.assertEqual([("override:001A", ["lead@glean.com"])], groups)

    def test_allowlist_assertion_rejects_foreign_domains(self):
        with self.assertRaisesRegex(ValueError, "non-allowlisted"):
            assert_allowlisted(["someone@notglean.com"], POLICY)

    def test_each_owner_sees_only_their_own_accounts(self):
        crossings = [
            self._crossing(account_id="001A", account_name="Acme", account_owner_email="a@glean.com"),
            self._crossing(account_id="001B", account_name="Beta", account_owner_email="b@glean.com"),
        ]
        groups = {item["group_key"]: item for item
                  in group_for_digest(crossings, policy=POLICY, aiom_email="aiom@glean.com")}
        self.assertEqual(["Acme"],
                         [c["account_name"] for c in groups["account_owner:a@glean.com"]["crossings"]])
        self.assertEqual(["Beta"],
                         [c["account_name"] for c in groups["account_owner:b@glean.com"]["crossings"]])
        self.assertEqual(["Acme", "Beta"],
                         [c["account_name"] for c in groups["aiom:aiom@glean.com"]["crossings"]])

    def test_unroutable_crossings_are_surfaced_not_dropped(self):
        policy = load_policy({"roles": ["salesforce_account_owner"]})
        groups = group_for_digest([self._crossing(account_owner_email="")],
                                 policy=policy, aiom_email="aiom@glean.com")
        self.assertEqual(["__unroutable__"], [item["group_key"] for item in groups])


class RenderingTests(unittest.TestCase):
    def _crossings(self):
        report = _report([_package(sold=50.0, consumed=27.0, line_item_name="Outcomes Standard")],
                         name="Five Below")
        return _detect(report)["crossings"]

    def test_subject_states_account_and_figures(self):
        subject = render_subject(self._crossings(), "2026-08-03")
        self.assertIn("Five Below", subject)
        self.assertIn("50%", subject)
        self.assertIn("27.00h of 50.00h", subject)

    def test_body_is_self_contained_with_no_links(self):
        body = render_digest(self._crossings(), window_key="2026-08-03",
                             source_note="Source: retrieval abc")
        self.assertIn("Five Below", body)
        self.assertIn("Outcomes Standard", body)
        self.assertIn("27.00h", body)
        self.assertIn("Remaining: 23.00h", body)
        self.assertIn("sent via Glean Pi", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)
        self.assertNotIn("127.0.0.1", body)

    def test_multi_account_subject_counts_accounts(self):
        crossings = self._crossings() + [dict(self._crossings()[0], account_id="001B",
                                              account_name="Beta")]
        self.assertIn("2 accounts", render_subject(crossings, "2026-08-03"))

    def test_weekly_window_is_the_monday(self):
        self.assertEqual("2026-08-03", digest_window_key(date(2026, 8, 6)))
        self.assertEqual("2026-08-03", digest_window_key(date(2026, 8, 3)))

    def test_policy_requires_a_usable_ladder(self):
        with self.assertRaises(ValueError):
            load_policy({"ladder": []})
        self.assertEqual(DEFAULT_POLICY["ladder"], load_policy(None)["ladder"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        private = Path(self._tmp.name) / "private"
        private.mkdir()
        self.store = RemediationStore(private / "remediation.sqlite3")
        self.report = _report([_package(sold=20.0, consumed=11.0)])
        self.evaluation = _detect(self.report)

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, evaluation=None, *, retrieval="retr-1", queue=True):
        evaluation = evaluation or self.evaluation
        return self.store.apply_notification_cycle(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, retrieval_id=retrieval,
            observations=evaluation["observations"], crossings=evaluation["crossings"],
            migrations=evaluation["migrations"], queue_crossings=queue,
        )

    def _pending(self, scope=SCOPE, portfolio=PORTFOLIO, state="pending"):
        return self.store.list_threshold_crossings(
            scope_id=scope, portfolio_id=portfolio, delivery_state=state)

    def _queue_digest(self):
        self._apply()
        return self.store.queue_email_digest(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, recipient_group_key="aiom:aiom@glean.com",
            recipients=["aiom@glean.com"], digest_window_key="2026-08-03",
            crossing_ids=[item["id"] for item in self._pending()],
            subject="[Hours] digest", body_text="body", sender_email="aiom@glean.com",
        )

    def test_crossing_is_recorded_once_even_if_detected_twice(self):
        self.assertEqual(1, len(self._apply()["recorded"]))
        second = self._apply(retrieval="retr-2")
        self.assertEqual(0, len(second["recorded"]))
        self.assertEqual("crossing_exists", second["suppressed"][0]["reason"])
        self.assertEqual(1, len(self._pending()))

    def test_high_water_mark_is_persisted_and_blocks_redetection(self):
        self._apply()
        state = self.store.load_threshold_state(scope_id=SCOPE, portfolio_id=PORTFOLIO)
        self.assertEqual(50, state[0]["high_water_threshold"])
        self.assertEqual([], _detect(self.report, state)["crossings"])

    def test_observe_only_records_state_without_deliverable_crossings(self):
        result = self._apply(queue=False)
        self.assertEqual([], result["recorded"])
        self.assertEqual("observe_only", result["suppressed"][0]["reason"])
        self.assertEqual([], self._pending())
        self.assertEqual(50, self.store.load_threshold_state(
            scope_id=SCOPE, portfolio_id=PORTFOLIO)[0]["high_water_threshold"])

    def test_high_water_mark_never_decreases(self):
        self._apply()
        lower = _report([_package(sold=20.0, consumed=2.0)])
        self.store.apply_notification_cycle(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, retrieval_id="retr-3",
            observations=[{**_state_row(lower, high_water=0), "pct": 10.0,
                           "consumed_hours": 2.0, "sold_hours": 20.0,
                           "reached_threshold": 0, "regressed": True}],
            crossings=[], migrations=[],
        )
        state = self.store.load_threshold_state(scope_id=SCOPE, portfolio_id=PORTFOLIO)
        self.assertEqual(50, state[0]["high_water_threshold"])
        self.assertEqual(1, state[0]["regression_count"])

    def test_queueing_a_digest_moves_crossings_to_queued(self):
        row = self._queue_digest()
        self.assertEqual("pending", row["status"])
        self.assertEqual(1, row["crossing_count"])
        self.assertEqual([], self._pending())
        self.assertEqual(1, len(self._pending(state="queued")))

    def test_second_active_digest_for_the_same_week_is_rejected(self):
        self._queue_digest()
        with self.assertRaises(QueueConflict):
            self.store.queue_email_digest(
                scope_id=SCOPE, portfolio_id=PORTFOLIO,
                recipient_group_key="aiom:aiom@glean.com", recipients=["aiom@glean.com"],
                digest_window_key="2026-08-03", crossing_ids=["hnc1_x"],
                subject="dup", body_text="other", sender_email="aiom@glean.com",
            )

    def test_claim_then_send_records_delivery_and_marks_crossings_sent(self):
        row = self._queue_digest()
        claimed = self.store.claim_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        self.assertEqual("sending", claimed["status"])
        sent = self.store.complete_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
            expected_version=claimed["version"],
            provider_message_id="CAHk=abc123@mail.gmail.com")
        self.assertEqual("sent", sent["status"])
        self.assertEqual(1, len(self._pending(state="sent")))

    def test_stale_version_cannot_claim(self):
        row = self._queue_digest()
        with self.assertRaises(QueueConflict):
            self.store.claim_email_outbox(
                row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
                expected_version=row["version"] + 5)

    def test_claimed_digest_leaves_the_pending_list(self):
        row = self._queue_digest()
        self.store.claim_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        self.assertEqual([], self.store.list_email_outbox(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, status="pending"))

    def test_uncertain_delivery_requires_explicit_confirmation_to_retry(self):
        row = self._queue_digest()
        claimed = self.store.claim_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        flagged = self.store.mark_email_outbox_uncertain(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
            expected_version=claimed["version"], error="timeout")
        self.assertEqual("needs_review", flagged["status"])
        with self.assertRaisesRegex(QueueValidationError, "Confirm the digest was not delivered"):
            self.store.retry_email_outbox(
                row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
                expected_version=flagged["version"], confirmed_not_delivered=False)
        armed = self.store.retry_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
            expected_version=flagged["version"], confirmed_not_delivered=True)
        self.assertEqual("pending", armed["status"])

    def test_needs_review_can_be_completed_without_resending(self):
        row = self._queue_digest()
        claimed = self.store.claim_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        flagged = self.store.mark_email_outbox_uncertain(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
            expected_version=claimed["version"], error="uncertain")
        sent = self.store.complete_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
            expected_version=flagged["version"], provider_message_id="msg-found-in-mailbox")
        self.assertEqual("sent", sent["status"])

    def test_cancelling_a_digest_returns_crossings_to_pending(self):
        row = self._queue_digest()
        self.store.cancel_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        self.assertEqual(1, len(self._pending()))

    def test_delivery_requires_a_real_provider_message_id(self):
        row = self._queue_digest()
        claimed = self.store.claim_email_outbox(
            row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO, expected_version=row["version"])
        with self.assertRaisesRegex(QueueValidationError, "provider message ID"):
            self.store.complete_email_outbox(
                row["id"], scope_id=SCOPE, portfolio_id=PORTFOLIO,
                expected_version=claimed["version"], provider_message_id="  ")

    def test_empty_digest_is_refused(self):
        with self.assertRaises(QueueValidationError):
            self.store.queue_email_digest(
                scope_id=SCOPE, portfolio_id=PORTFOLIO, recipient_group_key="g",
                recipients=["aiom@glean.com"], digest_window_key="2026-08-03",
                crossing_ids=[], subject="s", body_text="b", sender_email="aiom@glean.com")

    def test_rotation_migration_moves_state_instead_of_duplicating(self):
        self._apply()
        rotated = _report([_package("006A:00k9", sold=20.0, consumed=11.0)])
        state = self.store.load_threshold_state(scope_id=SCOPE, portfolio_id=PORTFOLIO)
        result = self._apply(_detect(rotated, state), retrieval="retr-rot")
        self.assertEqual(1, len(result["migrated"]))
        self.assertEqual([], result["recorded"])
        rows = self.store.load_threshold_state(scope_id=SCOPE, portfolio_id=PORTFOLIO)
        self.assertEqual(1, len(rows), "rotation must not leave two state rows")
        self.assertEqual("006A:00k9", rows[0]["package_id"])
        self.assertEqual(50, rows[0]["high_water_threshold"])

    def test_baseline_cancel_retires_pending_crossings(self):
        self._apply()
        self.assertEqual(1, self.store.cancel_pending_crossings(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, reason="seeded_baseline"))
        self.assertEqual([], self._pending())

    def test_baseline_seeding_is_audited(self):
        self.store.record_baseline_seeded(
            scope_id=SCOPE, portfolio_id=PORTFOLIO, retrieval_id="retr-1",
            seeded_thresholds={"006A:1": 50, "006A:2": 0}, cancelled_crossings=3)
        events = [item for item in self.store.notification_events(
            scope_id=SCOPE, portfolio_id=PORTFOLIO) if item["event_type"] == "baseline_seeded"]
        self.assertEqual(1, len(events))
        payload = events[0]["payload"]
        self.assertEqual(2, payload["package_count"])
        self.assertEqual([50], payload["suppressed_rungs"])
        self.assertEqual(3, payload["cancelled_pending_crossings"])

    def test_events_record_the_audit_trail(self):
        self._queue_digest()
        types = [item["event_type"] for item
                 in self.store.notification_events(scope_id=SCOPE, portfolio_id=PORTFOLIO)]
        self.assertIn("threshold_detected", types)
        self.assertIn("digest_queued", types)

    def test_another_scope_cannot_see_these_crossings(self):
        self._apply()
        self.assertEqual([], self._pending(scope="other-scope"))
        self.assertEqual([], self.store.list_email_outbox(
            scope_id=SCOPE, portfolio_id="someone.else@glean.com", status=None))


if __name__ == "__main__":
    unittest.main()
