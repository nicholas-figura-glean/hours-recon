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
from hours_recon.freshness import describe_freshness
from hours_recon.http_client import ApiError, request_json
from hours_recon.mcp_snapshot import McpSnapshotError, publish_mcp_snapshot
from hours_recon.reconcile import reconcile
from hours_recon.rocketlane import RocketlaneClient
from hours_recon.sample_data import build_demo_sources
from hours_recon.service import ReconciliationService
from hours_recon.storage import write_cache
from hours_recon.trend import advance as advance_trend_baseline
from hours_recon.trend import attach_trend

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
        self.assertIn("Rocketlane projects matched to this account", html)
        self.assertIn("No matched Rocketlane projects.", html)

    def test_dashboard_renders_governance_and_remediation_planner(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function renderRemediation()", html)
        self.assertIn("function renderPathOption(path, workstream)", html)
        self.assertIn("function applyRemediationAction(button)", html)
        self.assertIn("The weakest check sets the account status.", html)
        self.assertIn("Governed ${fmt(split.governed)}h · Provisional ${fmt(split.provisional)}h", html)
        self.assertIn("Other ways to fix this", html)
        self.assertIn("Next steps", html)
        self.assertIn("Use this instead", html)
        # The chosen fix is one bar with one button; alternates collapse behind a
        # disclosure, and no path renders its step list inline any more.
        self.assertIn('<div class="fix-bar">', html)
        # Accepting unfixable older entries: bulk selection only ticks boxes, and the
        # request always carries explicit IDs so nothing is suppressed unseen.
        self.assertIn("function renderExclusionPanel(workstream)", html)
        self.assertIn("function selectEntriesBefore(button)", html)
        self.assertIn("data-action=\"exclusion-apply\"", html)
        self.assertIn("data-action=\"exclusion-restore\"", html)
        self.assertIn("/api/remediation/time-exclusions/", html)
        self.assertIn("It never changes billed hours", html)
        self.assertIn("entry_ids: entryIds", html)
        self.assertIn('data-remediation-action="prepare_execution" type="button">Open next steps<', html)
        self.assertNotIn('class="path-steps"', html)
        self.assertNotIn('class="path-list"', html)
        self.assertNotIn('class="planner-summary"', html)
        self.assertIn("data-remediation-action=\"prepare_execution\"", html)
        self.assertIn("data-remediation-action=\"select_path\"", html)
        self.assertIn("function renderExecutionWorkspace(workspace, workstream)", html)
        # The next-steps modal must stay a numbered flow with reference detail collapsed:
        # the previous flat two-column dump gave no cue about what to actually do.
        self.assertIn('<div class="do-now">', html)
        self.assertIn('Do this next', html)
        self.assertIn('<ol class="flow">', html)
        self.assertIn('class="flow-step ${step.optional ? \'optional\' : \'\'}"', html)
        self.assertIn('<span class="step-num">${index + 1}</span>', html)
        self.assertIn('Nothing has been written to Salesforce or Rocketlane yet.', html)
        for summary in ('Affected records', 'What you need, limits, and safety', 'Drive it yourself in Glean'):
            self.assertIn(f'<summary>{summary}', html)
        self.assertNotIn('execution-banner', html)
        self.assertNotIn('execution-grid', html)
        self.assertIn("function copyMcpRequest()", html)
        self.assertIn("function queueExecutionSlackDraft()", html)
        self.assertIn("function queueReviewedSourceActions()", html)
        self.assertIn("Queue ${plural(sourceFieldEditors, 'change')}", html)
        self.assertIn("id=\"openGleanExecution\" type=\"button\">Open Glean<", html)
        self.assertIn("function pendingSourceActionsForWorkspace(workspace, workstream)", html)
        self.assertIn("function buildGleanSourceExecutionPrompt(workspace, sourceActions)", html)
        self.assertIn("outbox_id: action.id", html)
        self.assertIn("record_ids: action.record_ids || []", html)
        self.assertIn("proposed_fields: action.proposed_fields || {}", html)
        self.assertIn("executionUrl.searchParams.set('message', buildGleanSourceExecutionPrompt(workspace, state.execution?.sourceActions))", html)
        self.assertIn("Auto-send from an external link is blocked by Glean security.", html)
        self.assertIn("execute pending Hours Recon source actions", html)
        self.assertIn("function queueAllSlackHandoffs()", html)
        self.assertIn("function renderSlackQueue(queue)", html)
        self.assertIn("function selectRemediationTab(tab, focus = false)", html)
        self.assertIn("role=\"tablist\"", html)
        self.assertIn("aria-controls=\"remediationPlannerPane\"", html)
        self.assertIn("aria-controls=\"slackQueuePane\"", html)
        self.assertIn("Slack delivery queue", html)
        self.assertIn("renderSection('Queued', queued", html)
        self.assertIn("renderSection('Delivered', delivered", html)
        self.assertIn("String(b.queued_at || '').localeCompare", html)
        self.assertIn("String(b.sent_at || b.queued_at || '').localeCompare", html)
        self.assertIn("Each person receives only their own flagged entries.", html)
        self.assertIn("/slack/queue", html)
        self.assertIn("Queue message", html)
        self.assertIn("Copy instead", html)
        self.assertIn("data-remediation-action=\"snooze\"", html)
        self.assertIn("data-remediation-action=\"waive\"", html)
        self.assertIn("function loadWorkstreamHistory(button)", html)
        self.assertIn("const workstreams = queue.workstreams || []", html)
        self.assertIn("record_mcp_request_copy", html)
        self.assertIn("record_slack_copy", html)
        self.assertIn("Nothing was sent by the dashboard", html)
        self.assertIn("no source write is implied", html)
        self.assertIn("X-Hours-Recon-Action-Token", html)
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/remediation/workstreams", app_source)
        self.assertIn("hrw2_[a-f0-9]{64}", app_source)
        self.assertIn("X-Frame-Options", app_source)
        self.assertIn("Invalid remediation action token", app_source)
        self.assertIn("hro1_[a-f0-9]{64}", app_source)
        self.assertIn("/api/remediation/slack/outbox", app_source)
        self.assertIn("/api/remediation/source/outbox", app_source)
        self.assertIn("hsa1_[a-f0-9]{64}", app_source)

    def test_dashboard_presents_one_clear_answer_before_the_governance_detail(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        # A single hero figure, a short act-on-these list, and movement.
        self.assertIn('id="heroValue"', html)
        self.assertIn("At risk in the next 90 days", html)
        self.assertIn('id="attentionSection"', html)
        self.assertIn("Needs attention", html)
        self.assertIn("function renderAttention()", html)
        self.assertIn("function deltaChip(entry, betterWhen)", html)
        self.assertIn("since last refresh", html)
        # Staleness is one banner with an action, not a governance backlog.
        self.assertIn('id="freshnessBanner"', html)
        self.assertIn("function renderFreshnessBanner(freshness)", html)
        self.assertIn("These counts are unreliable until you refresh", html)
        # The governance apparatus is secondary, and its empty state explains itself.
        self.assertIn('id="dataQuality"', html)
        self.assertIn("queue.unavailable_message", html)
        self.assertIn("Checks run on your own data", html)

    def test_dashboard_uses_plain_language_instead_of_internal_vocabulary(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for label in ("Where the hours came from", "How hours were counted", "Rocketlane link", "Time entry quality"):
            self.assertIn(label, html)
        # Effort bands are shown as time, and tiers as a checked/not-checked state.
        self.assertIn("const EFFORT_LABELS", html)
        self.assertIn("about 30 minutes", html)
        self.assertIn("const verificationChip = (tier, extra = '')", html)
        self.assertIn("Not checked", html)
        self.assertIn("const URGENCY_LABELS = { P0: 'Act now'", html)
        # The raw fingerprint no longer occupies a summary column.
        self.assertNotIn("item.fingerprint.slice(0, 17)", html)

    def test_dashboard_patches_the_dom_and_delegates_events(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        # Keyed patching is what preserves open cards, scroll, and focus.
        self.assertIn("function patchList(container, items, { key, tag = 'div', className = '', render })", html)
        self.assertIn("const actionHandlers = {", html)
        self.assertIn("event.target.closest('[data-action]')", html)
        self.assertIn("state.searchTimer = setTimeout(renderAccounts, 140)", html)
        # Native prompt/confirm are gone; every input is a dialog form.
        self.assertNotIn("window.prompt(", html)
        self.assertNotIn("window.confirm(", html)
        self.assertIn("function openFormDialog({ title, description = '', fields = [], confirmLabel = 'Confirm' })", html)
        self.assertIn("async function confirmAction({ title, description, confirmLabel = 'Confirm' })", html)
        self.assertIn('id="promptDialog"', html)
        # The account detail is a full-screen modal dialog, not a side rail or an
        # aria-live blob: <dialog> gives us the focus trap, backdrop, and Escape.
        self.assertIn('<dialog class="detail-dialog" id="detail"', html)
        self.assertIn("function closeDetail()", html)
        self.assertIn("function selectDetailTab(tab, focus = false)", html)
        self.assertIn("const DETAIL_TABS = ['entitlements', 'projects', 'time', 'evidence'];", html)
        self.assertIn("detail.showModal()", html)
        self.assertNotIn('id="detail" aria-live="polite"', html)
        # The old translate-X sheet and its hand-rolled scrim must be gone.
        self.assertNotIn('class="sheet"', html)
        self.assertNotIn("sheetScrim", html)

    def test_source_action_editor_replaces_raw_json_but_keeps_the_placeholder_guard(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function fieldEditorRow(operationIndex, name, value, fieldIndex)", html)
        self.assertIn("function collectProposedFields(editor)", html)
        self.assertIn('data-field-name=', html)
        self.assertIn("data-source-operation-index=", html)
        # Typed values must survive the round trip, and placeholders must not.
        self.assertIn("const PLACEHOLDER = /^<[^>]+>$/", html)
        self.assertIn("Replace every <placeholder> with a real value before queueing.", html)
        self.assertIn("fields[name] = Number(raw)", html)
        self.assertIn("fields[name] = input.checked", html)
        self.assertNotIn("textarea class=\"execution-fields source-fields\"", html)

    def test_source_action_outbox_cli_and_skill_require_preflight_and_confirmation(self):
        script = (ROOT / "scripts" / "hours_recon_source_outbox.py").read_text(encoding="utf-8")
        skill = (ROOT / ".glean" / "skills" / "hours-recon-source-execute" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('subparsers.add_parser("claim"', script)
        self.assertIn('subparsers.add_parser("completed"', script)
        self.assertIn('subparsers.add_parser("uncertain"', script)
        self.assertIn('subparsers.add_parser("retry"', script)
        self.assertIn("must be a loopback HTTP URL", script)
        self.assertIn("Re-read every `record_id`", skill)
        self.assertIn("ask_clarifying_question", skill)
        self.assertIn("final write confirmation", skill)
        self.assertIn("Never parallelize writes. Retry an uncertain write only after a fresh read", skill)

    def test_slack_mcp_outbox_cli_and_skill_enforce_claim_before_send(self):
        script = (ROOT / "scripts" / "hours_recon_slack_outbox.py").read_text(encoding="utf-8")
        skill = (ROOT / ".glean" / "skills" / "hours-recon-slack-send" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('subparsers.add_parser("claim"', script)
        self.assertIn('subparsers.add_parser("sent"', script)
        self.assertIn('subparsers.add_parser("uncertain"', script)
        self.assertIn('subparsers.add_parser("retry"', script)
        self.assertIn("must be a loopback HTTP URL", script)
        self.assertIn("glean_Slack_MCP_slack_search_users", skill)
        self.assertIn("glean_Slack_MCP_slack_send_message", skill)
        self.assertIn("Process items **sequentially**, never in parallel", skill)
        self.assertIn("do **not** retry", skill)


class CacheSafetyTests(unittest.TestCase):
    def test_cache_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "cache.json"
            write_cache(path, {"meta": {"mode": "live"}})
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_publish_mcp_snapshot_atomically_replaces_a_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "mcp_snapshot.json"
            snapshot = {
                "schema_version": 1,
                "meta": {
                    "scope_id": "sf:tenant|rl:workspace", "scope_verified": True,
                    "through_date": business_today("America/Denver").isoformat(),
                    "coverage": {"complete": True, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True},
                },
                "salesforce": {"requester": {"email": "nick.figura@glean.com"}},
                "rocketlane": {"projects": [], "entries": []},
            }
            publish_mcp_snapshot(path, snapshot, expected_requester_email="nick.figura@glean.com", expected_scope_id="sf:tenant|rl:workspace", timezone_name="America/Denver")
            self.assertEqual(snapshot, json.loads(path.read_text()))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_publish_mcp_snapshot_rejects_stale_report_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp_snapshot.json"
            path.write_text('{"existing": true}')
            snapshot = {
                "schema_version": 1,
                "meta": {
                    "scope_id": "sf:tenant|rl:workspace", "scope_verified": True,
                    "through_date": "2000-01-01",
                    "coverage": {"complete": True, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True},
                },
                "salesforce": {"requester": {"email": "nick.figura@glean.com"}},
                "rocketlane": {"projects": [], "entries": []},
            }
            with self.assertRaisesRegex(McpSnapshotError, "through_date is not the report date"):
                publish_mcp_snapshot(path, snapshot, expected_requester_email="nick.figura@glean.com", expected_scope_id="sf:tenant|rl:workspace", timezone_name="America/Denver")
            self.assertEqual('{"existing": true}', path.read_text())

    def test_publish_mcp_snapshot_preserves_active_file_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp_snapshot.json"
            path.write_text('{"existing": true}')
            with self.assertRaisesRegex(McpSnapshotError, "different requester"):
                publish_mcp_snapshot(path, {"schema_version": 1, "salesforce": {"requester": {"email": "jason.fleming@glean.com"}}, "rocketlane": {}, "meta": {}}, expected_requester_email="nick.figura@glean.com", expected_scope_id="scope", timezone_name="America/Denver")
            self.assertEqual('{"existing": true}', path.read_text())

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

    def test_mcp_observe_mode_creates_idempotent_local_remediation_workstreams(self):
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
            self.assertGreater(first["remediation_queue"]["active_workstream_count"], 0)
            first_count = first["remediation_queue"]["workstream_count"]
            refreshed = service.refresh()
            self.assertEqual(first_count, refreshed["remediation_queue"]["workstream_count"])
            self.assertFalse(refreshed["meta"]["remediation_observation"]["new_source_observation"])

    def test_remediation_planner_excludes_accounts_not_held_by_requester(self):
        from hours_recon.remediation_store import QueueValidationError, RemediationStore

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_path = root / "mcp_snapshot.json"
            db_path = root / "private" / "queue.sqlite3"
            salesforce, rocketlane = build_demo_sources(date(2026, 2, 2))
            snapshot_path.write_text(json.dumps({
                "schema_version": 1,
                "meta": {
                    "created_at": "2026-02-02T12:00:00Z", "scope": "test", "retrieval_id": "retrieval-mine",
                    "scope_id": "test-tenant", "through_date": "2026-02-02",
                    "coverage": {
                        "complete": True, "accounts": True, "opportunities": True,
                        "projects": True, "time_entries": True, "pagination_complete": True,
                    },
                },
                "salesforce": salesforce,
                "rocketlane": rocketlane,
            }))
            # Simulate a shared local database containing a workstream for an
            # account this requester does not own under the same scope and
            # portfolio identity. Account filtering remains a final boundary.
            foreign_report = {
                "meta": {"as_of": "2026-02-02"},
                "accounts": [{
                    "id": "FOREIGN-JASON-1", "name": "Jason Test Corp",
                    "sold_hours": 20, "billed_hours": 0, "remaining_hours": 20,
                    "at_risk_hours": 0, "expired_unused_hours": 0, "future_entitlement_hours": 0, "overage_hours": 0,
                    "packages": [],
                    "governance": {
                        "overall_tier": "T4", "policy_version": "test",
                        "dimensions": {"entitlement_source": {
                            "tier": "T4", "rank": 4, "reason_code": "missing", "summary": "foreign gap",
                            "recommended_action": "fix", "refs": [], "details": {},
                        }},
                        "gaps": [{
                            "dimension": "entitlement_source", "tier": "T4",
                            "reason_code": "missing", "summary": "foreign gap",
                            "recommended_action": "fix", "refs": [], "details": {},
                        }],
                    },
                }],
            }
            store = RemediationStore(db_path)
            store.observe(
                foreign_report, retrieval_id="jason-suite-1", scope_id="test-tenant",
                portfolio_id="demo.aiom@example.com", coverage_complete=True, report_digest="jason-digest",
            )
            foreign = store.list_workstreams(scope_id="test-tenant", portfolio_id="demo.aiom@example.com")
            self.assertTrue(any(
                instance["account_id"] == "FOREIGN-JASON-1"
                for workstream in foreign for instance in workstream["instances"]
            ))
            foreign_workstream = foreign[0]

            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "demo.aiom@example.com", "packages": PACKAGES,
                "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
                "cache_path": root / "cache.json", "mcp_snapshot_path": snapshot_path,
                "cache_max_age_days": 30, "governance_mode": "observe_only",
                "remediation_mode": "observe_only", "remediation_db_path": db_path,
                "remediation_scope_id": "test-tenant",
            })
            queue = service.data["remediation_queue"]
            queue_accounts = {
                instance["account_id"]
                for workstream in queue["workstreams"] for instance in workstream["instances"]
            }
            self.assertNotIn("FOREIGN-JASON-1", queue_accounts)
            self.assertTrue(queue_accounts)  # the requester's own workstreams still show
            listed_accounts = {
                instance["account_id"]
                for workstream in service.list_remediation_workstreams() for instance in workstream["instances"]
            }
            self.assertNotIn("FOREIGN-JASON-1", listed_accounts)
            # The requester must not be able to act on an unowned workstream either.
            with self.assertRaises(QueueValidationError):
                service.remediation_action(
                    foreign_workstream["fingerprint"], action="acknowledge",
                    expected_version=foreign_workstream["version"],
                )

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
            expected_workstreams = first.data["remediation_queue"]["workstream_count"]
            settings["remediation_db_path"].unlink()
            second = ReconciliationService(settings)
            self.assertEqual(expected_workstreams, second.data["remediation_queue"]["workstream_count"])

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
            self.assertEqual(0, service.data["remediation_queue"]["workstream_count"])

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


class FreshnessTests(unittest.TestCase):
    """A retrieval problem must be stated once, in words, with an action."""

    def test_current_mcp_pull_reports_no_banner(self):
        today = date(2026, 7, 31)
        result = describe_freshness({
            "mode": "mcp", "mcp_through_date": "2026-07-31", "mcp_scope_verified": True,
            "mcp_coverage": {"complete": True, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True},
        }, report_date=today)
        self.assertEqual("current", result["state"])
        self.assertTrue(result["is_current"])
        self.assertFalse(result["blocks_verification"])
        self.assertIsNone(result["action_label"])

    def test_stale_pull_is_described_in_days_not_as_governance_failures(self):
        result = describe_freshness({
            "mode": "mcp", "mcp_through_date": "2026-07-24", "mcp_scope_verified": True,
            "mcp_coverage": {"complete": False, "accounts": True, "opportunities": True, "projects": True, "time_entries": True, "pagination_complete": True, "through_date_current": False},
        }, report_date=date(2026, 7, 31))
        self.assertEqual("stale", result["state"])
        self.assertEqual(7, result["days_behind"])
        self.assertIn("7 days old", result["headline"])
        self.assertIn("2026-07-24", result["detail"])
        self.assertEqual("Run Hours Recon MCP refresh", result["action_hint"])

    def test_incomplete_pull_names_the_missing_datasets_in_english(self):
        result = describe_freshness({
            "mode": "mcp", "mcp_through_date": "2026-07-31", "mcp_scope_verified": True,
            "mcp_coverage": {"complete": False, "accounts": True, "opportunities": False, "projects": True, "time_entries": False, "pagination_complete": True},
        }, report_date=date(2026, 7, 31))
        self.assertEqual("incomplete", result["state"])
        self.assertIn("Salesforce opportunities", result["detail"])
        self.assertIn("Rocketlane time entries", result["detail"])
        self.assertNotIn("complete,", result["detail"])

    def test_demo_mode_explains_itself_rather_than_looking_broken(self):
        result = describe_freshness({"mode": "demo"}, report_date=date(2026, 7, 31))
        self.assertEqual("demo", result["state"])
        self.assertIn("example accounts", result["detail"])


class TrendTests(unittest.TestCase):
    """Deltas must stay stable on reload and only advance when data changes."""

    @staticmethod
    def _report(as_of, at_risk):
        return {
            "meta": {"as_of": as_of, "refreshed_at": f"{as_of}T09:00:00Z"},
            "metrics": {
                "at_risk_hours": at_risk, "remaining_hours": 100.0, "sold_hours": 200.0,
                "billed_hours": 100.0, "expired_unused_hours": 0.0, "overage_hours": 0.0,
            },
            "accounts": [{
                "id": "A1", "at_risk_hours": at_risk, "remaining_hours": 100.0, "sold_hours": 200.0,
                "billed_hours": 100.0, "expired_unused_hours": 0.0, "overage_hours": 0.0,
            }],
        }

    def test_first_report_has_no_movement_and_reload_does_not_invent_one(self):
        first = self._report("2026-07-01", 10)
        attach_trend(first, None)
        self.assertFalse(first["trend"]["available"])
        baseline = advance_trend_baseline(None, first)

        reloaded = self._report("2026-07-01", 10)
        attach_trend(reloaded, baseline)
        self.assertFalse(reloaded["trend"]["available"])
        unchanged = advance_trend_baseline(baseline, reloaded)
        self.assertIsNone(unchanged["previous"])

    def test_changed_data_produces_movement_that_survives_a_reload(self):
        first = self._report("2026-07-01", 10)
        baseline = advance_trend_baseline(None, first)
        second = self._report("2026-07-08", 25)
        attach_trend(second, baseline)
        self.assertTrue(second["trend"]["available"])
        self.assertEqual({"previous": 10.0, "delta": 15.0}, second["trend"]["metrics"]["at_risk_hours"])
        self.assertEqual({"previous": 10.0, "delta": 15.0}, second["accounts"][0]["trend"]["fields"]["at_risk_hours"])

        baseline = advance_trend_baseline(baseline, second)
        reloaded = self._report("2026-07-08", 25)
        attach_trend(reloaded, baseline)
        self.assertEqual({"previous": 10.0, "delta": 15.0}, reloaded["trend"]["metrics"]["at_risk_hours"])

    def test_a_report_cached_by_an_earlier_build_still_renders_the_hero(self):
        """Derived presentation fields must be recomputed, not assumed present."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sf, rl = base_sources([
                {"id": "T1", "project_id": "P1", "date": "2026-02-01", "minutes": 60, "billable": True},
            ])
            report = reconcile(sf, rl, package_config=PACKAGES, account_aliases=ALIASES, as_of=date(2026, 12, 20), mode="mcp")
            # Simulate a cache written before the attention list existed.
            report.pop("attention")
            for key in ("at_risk_account_count", "overage_account_count", "attention_account_count", "soonest_expiration_days", "soonest_expiration_date"):
                report["metrics"].pop(key, None)
            report["meta"].update({"mcp_requester_email": "alex@example.com", "mcp_through_date": "2026-12-20"})
            cache = root / "cache.json"
            write_cache(cache, report)
            service = ReconciliationService({
                "mode": "mcp", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "alex@example.com",
                "packages": PACKAGES, "account_aliases": ALIASES, "cache_path": cache,
                "mcp_snapshot_path": root / "missing.json", "cache_max_age_days": 30,
                "governance_mode": "observe_only", "remediation_mode": "off",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "",
            })
            data = service.data
            self.assertTrue(data["attention"])
            self.assertEqual(1, data["metrics"]["at_risk_account_count"])
            self.assertIsNotNone(data["metrics"]["soonest_expiration_days"])

    def test_service_attaches_freshness_and_movement_and_never_baselines_demo_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = {
                "mode": "demo", "timezone": "America/Denver", "requester_email": "", "mcp_requester_email": "",
                "packages": PACKAGES, "account_aliases": ALIASES, "cache_path": root / "cache.json",
                "mcp_snapshot_path": root / "missing.json", "cache_max_age_days": 30,
                "governance_mode": "observe_only", "remediation_mode": "off",
                "remediation_db_path": root / "private" / "queue.sqlite3", "remediation_scope_id": "",
            }
            data = ReconciliationService(settings).data
            self.assertEqual("demo", data["meta"]["freshness"]["state"])
            self.assertFalse(data["trend"]["available"])
            self.assertFalse((root / "cache_trend.json").exists())
            self.assertEqual("demo", data["remediation_queue"]["unavailable_reason"])
            self.assertIn("Connect Salesforce", data["remediation_queue"]["unavailable_message"])


if __name__ == "__main__":
    unittest.main()


class CachePathOverrideTests(unittest.TestCase):
    """The report cache must be redirectable.

    A hard-coded cache path means any second instance -- a verification run, a
    scratch run against a fixture -- silently overwrites the real portfolio's
    cached report with someone else's accounts.
    """

    def _settings(self, env):
        import importlib
        from hours_recon import config as config_module
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
        try:
            importlib.reload(config_module)
            return config_module.settings()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(config_module)

    def test_cache_path_defaults_to_the_real_report(self):
        resolved = self._settings({"HOURS_RECON_CACHE_PATH": None})
        self.assertEqual("reconciliation.json", resolved["cache_path"].name)
        self.assertEqual("var", resolved["cache_path"].parent.name)

    def test_cache_path_can_be_redirected(self):
        resolved = self._settings({"HOURS_RECON_CACHE_PATH": "var/scratch/verify.json"})
        self.assertTrue(str(resolved["cache_path"]).endswith("var/scratch/verify.json"))
        self.assertNotEqual("reconciliation.json", resolved["cache_path"].name)

    def test_trend_baseline_follows_the_redirected_cache(self):
        """Otherwise a scratch run poisons the real week-over-week baseline."""
        from hours_recon.service import ReconciliationService
        resolved = self._settings({"HOURS_RECON_CACHE_PATH": "var/scratch/verify.json"})
        service = ReconciliationService.__new__(ReconciliationService)
        service.settings = resolved
        self.assertTrue(str(service._trend_path()).endswith("var/scratch/verify_trend.json"))
