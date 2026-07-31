"""Orchestrate data retrieval, reconciliation, governance, caching, and remediation."""

from __future__ import annotations

import json
import copy
import re
import secrets
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from .dates import business_today
from .demo import demo_report
from .evidence import attach_governance
from .freshness import describe_freshness
from .matching import match_projects
from .mcp_snapshot import McpSnapshotError, load_mcp_snapshot
from .reconcile import attach_attention, reconcile
from .remediation_execution import build_execution_workspace
from .remediation_store import QueueError, QueueValidationError, RemediationStore
from .rocketlane import RocketlaneClient
from .salesforce import SalesforceClient
from .storage import read_cache, write_cache
from .trend import advance as advance_trend_baseline
from .trend import attach_trend


class ReconciliationService:
    MIN_REFRESH_INTERVAL_SECONDS = 5

    def __init__(self, app_settings: Mapping[str, Any]) -> None:
        self.settings = dict(app_settings)
        self.lock = Lock()
        self.last_refresh_attempt = None
        self.remediation_store: Optional[RemediationStore] = None
        self.remediation_error: Optional[str] = None
        self.action_token = secrets.token_urlsafe(32)
        configured_mode = self.settings["mode"]
        remediation_mode = self.settings.get("remediation_mode", "off")
        if remediation_mode == "observe_only" and configured_mode != "demo":
            try:
                self.remediation_store = RemediationStore(self.settings["remediation_db_path"])
            except Exception as exc:
                self.remediation_error = f"{type(exc).__name__}: {exc}"

        cached = None
        if configured_mode != "demo":
            cached = read_cache(self.settings["cache_path"], self.settings["cache_max_age_days"])
            if cached and cached.get("meta", {}).get("mode") != configured_mode:
                cached = None
            if cached and configured_mode == "mcp":
                expected_email = str(self.settings.get("mcp_requester_email") or "").strip().lower()
                cached_email = str(cached.get("meta", {}).get("mcp_requester_email") or cached.get("meta", {}).get("requester", {}).get("email") or "").strip().lower()
                if not expected_email or not cached_email or not secrets.compare_digest(expected_email, cached_email):
                    cached = None
            if (
                cached
                and self.settings.get("governance_mode", "observe_only") == "observe_only"
                and "governance" not in cached
                and configured_mode == "mcp"
                and self.settings.get("mcp_snapshot_path")
                and self.settings["mcp_snapshot_path"].exists()
            ):
                cached = None
        self._data = cached
        if self._data is not None:
            self._downgrade_stale_cached_governance(self._data)
        if self._data is not None and self.remediation_store and "governance" in self._data:
            self._observe_source(self._data)
        mcp_snapshot_error = None
        if self._data is None and configured_mode == "mcp" and self.settings["mcp_snapshot_path"].exists():
            try:
                self._data = self._load_mcp_report()
            except McpSnapshotError as exc:
                mcp_snapshot_error = str(exc)
            else:
                self._observe_source(self._data)
                write_cache(self.settings["cache_path"], self._data)
        if self._data is None:
            self._data = demo_report(
                self.settings["packages"],
                self.settings["account_aliases"],
                as_of=business_today(self.settings["timezone"]),
            )
            if configured_mode == "mcp":
                self._data["meta"]["notice"] = (
                    mcp_snapshot_error
                    or "Demo data is shown until Glean Pi writes the first MCP snapshot."
                )
            else:
                self._data["meta"]["notice"] = "Demo data is shown until the first successful live refresh."
        self._record_trend(self._data)

    def _trend_path(self):
        configured = self.settings.get("trend_path")
        if configured:
            return configured
        cache_path = self.settings["cache_path"]
        return cache_path.with_name(f"{cache_path.stem}_trend.json")

    def _record_trend(self, result: Dict[str, Any]) -> None:
        """Attach movement since the previous report and advance the baseline.

        Sample data never touches the baseline, so switching to demo mode cannot
        corrupt the comparison for real data.
        """
        if not isinstance(result, dict):
            return
        if result.get("meta", {}).get("mode") == "demo":
            attach_trend(result, None)
            return
        try:
            baseline = read_cache(self._trend_path(), -1)
        except Exception:
            baseline = None
        attach_trend(result, baseline)
        try:
            write_cache(self._trend_path(), advance_trend_baseline(baseline, result))
        except OSError:
            # Movement is a convenience. Losing it must never break the report.
            pass

    @property
    def data(self) -> Dict[str, Any]:
        result = copy.deepcopy(self._data)
        # Derived presentation fields are recomputed on read so a report cached
        # by an earlier build never renders an empty hero or attention list.
        attach_attention(result)
        result.setdefault("meta", {})["freshness"] = describe_freshness(
            result.get("meta", {}), report_date=business_today(self.settings["timezone"]),
        )
        self._attach_remediation(result)
        return result

    def _downgrade_stale_cached_governance(self, result: Dict[str, Any]) -> None:
        meta = result.get("meta", {})
        if meta.get("mode") != "mcp" or "governance" not in result:
            return
        through_date = meta.get("mcp_through_date")
        try:
            current = bool(through_date) and date.fromisoformat(str(through_date)) == business_today(self.settings["timezone"])
        except ValueError:
            current = False
        if current:
            return
        project_evidence = {
            str(project.get("id")): dict(project.get("match_evidence") or {})
            for account in result.get("accounts", [])
            for project in account.get("projects", [])
            if project.get("id")
        }
        stale_coverage = dict(meta.get("mcp_coverage") or {})
        stale_coverage["complete"] = False
        stale_coverage["through_date_current"] = False
        attach_governance(
            result,
            project_match_evidence=project_evidence,
            mode=self.settings.get("governance_mode", "observe_only"),
            source_coverage=stale_coverage,
        )
        meta["mcp_coverage"] = stale_coverage
        meta["mcp_data_coverage_complete"] = False
        meta["mcp_coverage_complete"] = False
        meta["cache_stale_for_governance"] = True

    def _active_scope_id(self, result: Optional[Mapping[str, Any]] = None) -> str:
        source = result or self._data
        meta = source.get("meta", {}) if isinstance(source, Mapping) else {}
        return str(
            self.settings.get("remediation_scope_id")
            or meta.get("mcp_scope_id")
            or meta.get("source_scope_id")
            or "local-default"
        )

    def _active_portfolio_id(self, result: Optional[Mapping[str, Any]] = None) -> str:
        """Return the requester-bound portfolio identity used by planner v2.

        The connector scope identifies the shared Salesforce/Rocketlane tenant;
        it does not identify which AIOM's account portfolio is currently loaded.
        Keeping both identities prevents systemic workstreams from crossing a
        requester boundary while leaving room for explicit sharing in a future
        authenticated multiplayer deployment.
        """
        source = result or self._data
        meta = source.get("meta", {}) if isinstance(source, Mapping) else {}
        requester = meta.get("requester") if isinstance(meta.get("requester"), Mapping) else {}
        return str(
            meta.get("mcp_requester_email")
            or requester.get("email")
            or self.settings.get("mcp_requester_email")
            or self.settings.get("requester_email")
            or "local-default"
        ).strip().lower()

    def _owned_account_ids(self, result: Optional[Mapping[str, Any]] = None) -> List[str]:
        """Account IDs held by the current requester in the active report."""
        source = result if result is not None else self._data
        accounts = source.get("accounts", []) if isinstance(source, Mapping) else []
        return sorted({str(account.get("id")) for account in accounts if account.get("id")})

    def _source_execution_status(self) -> Dict[str, Any]:
        return {
            "mode": "glean_pi_source_action_outbox",
            "available": self.remediation_store is not None,
            "executor": "Authenticated Salesforce/Rocketlane tools via Glean Pi",
            "requires_pi_command": True,
            "command": "execute pending Hours Recon source actions",
            "final_confirmation_required": True,
        }

    def _slack_delivery_status(self) -> Dict[str, Any]:
        return {
            "mode": "glean_slack_mcp_outbox",
            "available": self.remediation_store is not None,
            "sender": "Your connected Slack identity via Glean Pi",
            "requires_pi_command": True,
            "command": "send pending Hours Recon messages",
            "supports": ["direct_message", "channel"],
        }

    def _unavailable_remediation_summary(self) -> Dict[str, Any]:
        configured_mode = self.settings["mode"]
        remediation_mode = self.settings.get("remediation_mode", "off")
        if configured_mode == "demo":
            reason = "demo"
            message = (
                "Data quality checks run on your own live data. Connect Salesforce and Rocketlane "
                "in Glean Pi, then run a refresh."
            )
        elif remediation_mode != "observe_only":
            reason = "disabled"
            message = "Data quality checks are turned off. Set HOURS_RECON_REMEDIATION_MODE=observe_only to enable them."
        else:
            reason = "error"
            message = "Data quality checks could not start. The dashboard numbers above are unaffected."
        return {
            "schema_version": 2,
            "mode": remediation_mode,
            "available": False,
            "unavailable_reason": reason,
            "unavailable_message": message,
            "error": self.remediation_error,
            "workstreams": [],
            "workstream_count": 0,
            "active_workstream_count": 0,
            "active_instance_count": 0,
            "governed_instance_count": 0,
            "slack_delivery": self._slack_delivery_status(),
            "source_execution": self._source_execution_status(),
        }

    def _attach_flagged_entries(self, result: Dict[str, Any], exclusions: Mapping[str, Any]) -> None:
        """Tag each account's entries so the UI can list and exclude the flagged ones."""
        from .evidence import entry_quality_signals
        for account in result.get("accounts", []):
            projects = {str(item.get("id")): item for item in account.get("projects", [])}
            flagged = 0
            excluded = 0
            for entry in account.get("entries", []):
                entry_id = str(entry.get("id") or "")
                signals = entry_quality_signals(entry, projects.get(str(entry.get("project_id"))))
                entry["quality_signals"] = signals
                record = exclusions.get(entry_id) if signals else None
                is_excluded = bool(record) and not (set(signals) - set(record.get("signals") or []))
                entry["excluded"] = is_excluded
                if is_excluded:
                    entry["exclusion_reason"] = record.get("reason")
                    excluded += 1
                elif signals:
                    flagged += 1
            account["flagged_entry_count"] = flagged
            account["excluded_entry_count"] = excluded

    def _attach_remediation(self, result: Dict[str, Any]) -> None:
        if not self.remediation_store:
            result["remediation_queue"] = self._unavailable_remediation_summary()
            return
        try:
            scope_id = self._active_scope_id(result)
            portfolio_id = self._active_portfolio_id(result)
            summary = self.remediation_store.summary(
                scope_id=scope_id,
                portfolio_id=portfolio_id,
                account_ids=self._owned_account_ids(result),
            )
            summary["available"] = True
            summary["action_token"] = self.action_token
            summary["slack_delivery"] = self._slack_delivery_status()
            summary["source_execution"] = self._source_execution_status()
            workstream_titles = {
                str(item.get("fingerprint")): str(item.get("title") or "Hours Recon handoff")
                for item in summary.get("workstreams", [])
            }
            outbox_rows = self.remediation_store.list_slack_outbox(
                scope_id=scope_id,
                portfolio_id=portfolio_id,
                status=None,
                account_ids=self._owned_account_ids(result),
            )
            visible_outbox = [
                {
                    key: item.get(key)
                    for key in (
                        "id", "workstream_fingerprint", "recipient_query", "status", "queued_at",
                        "claimed_at", "sent_at", "permalink", "error", "version",
                    )
                } | {"workstream_title": workstream_titles.get(str(item.get("workstream_fingerprint")), "Hours Recon handoff")}
                for item in outbox_rows
            ]
            summary["slack_outbox"] = visible_outbox
            # Exclusions are operator state, so they ship with the payload and stay
            # visible rather than silently shaping the numbers.
            exclusions = self.remediation_store.active_time_entry_exclusions(
                scope_id=scope_id, portfolio_id=portfolio_id,
            )
            summary["time_entry_exclusions"] = exclusions
            summary["time_entry_exclusion_count"] = len(exclusions)
            self._attach_flagged_entries(result, exclusions)
            summary["slack_outbox_counts"] = {
                status: sum(1 for item in visible_outbox if item.get("status") == status)
                for status in ("pending", "sending", "needs_review", "sent", "cancelled")
            }
            by_account: Dict[str, List[Dict[str, Any]]] = {}
            for workstream in summary.get("workstreams", []):
                for instance in workstream.get("instances", []):
                    by_account.setdefault(str(instance["account_id"]), []).append({
                        "workstream_fingerprint": workstream["fingerprint"],
                        "title": workstream["title"],
                        "status": workstream["status"],
                        "priority": workstream["priority"],
                        "route": workstream["route"],
                        "due_on": workstream["due_on"],
                        "dimension": instance["dimension"],
                        "current_tier": instance["validation_tier"],
                        "unverified_observed_tier": instance["unverified_observed_tier"],
                        "minimum_target_met": instance["minimum_target_met"],
                        "selected_target_met": instance["selected_target_met"],
                    })
            for account in result.get("accounts", []):
                linked = by_account.get(str(account.get("id")), [])
                active = [item for item in linked if item["status"] not in {"governed", "waived"}]
                account["remediation"] = {
                    "workstreams": linked,
                    "workstream_count": len(linked),
                    "active_workstream_count": len(active),
                    "highest_priority": min(
                        (item["priority"] for item in active if item.get("priority")),
                        key=lambda value: {"P0": 0, "P1": 1, "P2": 2}.get(str(value), 99),
                        default=None,
                    ),
                } if linked else None
            result["remediation_queue"] = summary
        except Exception as exc:
            self.remediation_error = f"{type(exc).__name__}: {exc}"
            result["remediation_queue"] = self._unavailable_remediation_summary()

    def status(self) -> Dict[str, Any]:
        queue_health: Dict[str, Any]
        if self.remediation_store:
            try:
                queue_health = self.remediation_store.health(
                    scope_id=self._active_scope_id(),
                    portfolio_id=self._active_portfolio_id(),
                )
            except Exception as exc:
                queue_health = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            queue_health = {"available": False, "error": self.remediation_error}
        return {
            "configured_mode": self.settings["mode"],
            "requester_email": self.settings["requester_email"],
            "has_live_cache": self.settings["cache_path"].exists() and self._data.get("meta", {}).get("mode") in {"live", "mcp"},
            "displayed_mode": self._data.get("meta", {}).get("mode"),
            "freshness": describe_freshness(
                self._data.get("meta", {}), report_date=business_today(self.settings["timezone"]),
            ),
            "governance_mode": self.settings.get("governance_mode", "observe_only"),
            "governance_policy_version": self._data.get("governance", {}).get("policy_version"),
            "remediation_mode": self.settings.get("remediation_mode", "off"),
            "remediation_queue": queue_health,
            "slack_delivery": self._slack_delivery_status(),
            "source_execution": self._source_execution_status(),
        }

    def _time_entry_exclusions(self) -> Dict[str, Dict[str, Any]]:
        """Operator-accepted time entries, reloaded on every refresh.

        Keyed on entry ID within the configured scope, so an exclusion survives a
        refresh. Demo mode is excluded on purpose: sample data must never be shaped
        by real operator state.
        """
        if not self.remediation_store or self.settings.get("mode") == "demo":
            return {}
        try:
            return self.remediation_store.active_time_entry_exclusions(
                scope_id=self._active_scope_id(),
                portfolio_id=self._active_portfolio_id(),
            )
        except Exception:
            # A degraded exclusion read must never block a refresh; the worst case is
            # that an accepted entry is flagged again, which is the safe direction.
            return {}

    def _exclusion_digest(self) -> str:
        """Fingerprint the exclusion state for the planner's retrieval identity.

        The planner dedupes observations on retrieval_id. Accepting or restoring an
        entry changes the derived evidence without changing the snapshot, so without
        this the change would show in the report but never reach the work queue.
        """
        if not self.remediation_store or self.settings.get("mode") == "demo":
            return ""
        try:
            return self.remediation_store.time_entry_exclusion_revision(
                scope_id=self._active_scope_id(),
                portfolio_id=self._active_portfolio_id(),
            )
        except Exception:
            return ""

    def _load_mcp_report(self) -> Dict[str, Any]:
        return load_mcp_snapshot(
            self.settings["mcp_snapshot_path"],
            package_config=self.settings["packages"],
            account_aliases=self.settings["account_aliases"],
            timezone_name=self.settings["timezone"],
            governance_mode=self.settings.get("governance_mode", "observe_only"),
            expected_requester_email=self.settings.get("mcp_requester_email", ""),
            time_entry_exclusions=self._time_entry_exclusions(),
        )

    def _observe_source(self, result: Dict[str, Any]) -> None:
        if not self.remediation_store or result.get("meta", {}).get("mode") == "demo":
            return
        meta = result.setdefault("meta", {})
        mode = str(meta.get("mode") or "")
        if mode == "mcp":
            retrieval_id = str(meta.get("mcp_retrieval_id") or "")
            configured_scope = str(self.settings.get("remediation_scope_id") or "").strip()
            source_scope = str(meta.get("mcp_scope_id") or "").strip()
            if configured_scope and source_scope and not secrets.compare_digest(configured_scope, source_scope):
                meta["remediation_observation"] = {
                    "new_source_observation": False,
                    "revalidation_performed": False,
                    "reason": "scope_mismatch_quarantined",
                    "configured_scope_id": configured_scope,
                    "source_scope_id": source_scope,
                }
                return
            scope_id = self._active_scope_id(result)
            coverage_complete = (
                meta.get("mcp_data_coverage_complete") is True
                and meta.get("mcp_scope_verified") is True
                and bool(configured_scope)
                and secrets.compare_digest(configured_scope, source_scope)
            )
            digest = str(meta.get("mcp_snapshot_digest") or "")
        else:
            retrieval_id = str(meta.get("source_retrieval_id") or f"live-{uuid4().hex}")
            scope_id = self._active_scope_id(result)
            coverage_complete = meta.get("source_coverage_complete") is True
            digest = sha256(str(meta).encode("utf-8")).hexdigest()
        exclusion_digest = self._exclusion_digest()
        if exclusion_digest:
            retrieval_id = f"{retrieval_id}:x{exclusion_digest}"
            digest = f"{digest}:x{exclusion_digest}"
            meta["time_entry_exclusion_digest"] = exclusion_digest
        try:
            observation = self.remediation_store.observe(
                result,
                retrieval_id=retrieval_id,
                scope_id=scope_id,
                portfolio_id=self._active_portfolio_id(result),
                coverage_complete=coverage_complete,
                report_digest=digest,
            )
            meta["remediation_observation"] = observation
        except Exception as exc:
            self.remediation_error = f"{type(exc).__name__}: {exc}"
            meta["remediation_observation"] = {
                "new_source_observation": False,
                "revalidation_performed": False,
                "reason": "queue_unavailable",
                "error": self.remediation_error,
            }

    def list_remediation_workstreams(self, filters: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        values = dict(filters or {})
        return self.remediation_store.list_workstreams(
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            account_ids=self._owned_account_ids(),
            status=values.get("status"),
            route=values.get("route"),
            priority=values.get("priority"),
            account_id=values.get("account_id"),
        )

    def get_remediation_workstream(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.get_workstream(
            fingerprint,
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            account_ids=self._owned_account_ids(),
        )

    def remediation_action(
        self,
        workstream_id: str,
        *,
        action: str,
        expected_version: int,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        action_payload = dict(payload or {})
        if action == "prepare_execution":
            workstream = self.get_remediation_workstream(workstream_id)
            if not workstream:
                raise QueueValidationError("Unknown remediation workstream.")
            # Never trust a browser-supplied plan. Build it from the current,
            # requester-scoped report and selected path on the server.
            action_payload["execution_plan"] = build_execution_workspace(
                workstream,
                self._data,
                salesforce_web_base_url=self.settings.get("salesforce_web_base_url", ""),
                rocketlane_web_base_url=self.settings.get("rocketlane_web_base_url", ""),
                mcp_workspace_url=self.settings.get("mcp_workspace_url", ""),
            )
        return self.remediation_store.action(
            workstream_id,
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            account_ids=self._owned_account_ids(),
            action=action,
            expected_version=expected_version,
            payload=action_payload,
        )

    @staticmethod
    def _reviewed_slack_message(value: Any) -> str:
        message = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in message:
            raise QueueValidationError("The Slack message contains an invalid null character.")
        message = "\n".join(line.rstrip() for line in message.split("\n")).strip()
        message = re.sub(r"\n{3,}", "\n\n", message)
        attribution = "— sent via Glean Pi"
        if attribution not in message:
            message = f"{message}\n\n{attribution}" if message else attribution
        if len(message) < len(attribution) + 2 or len(message) > 4000:
            raise QueueValidationError("Review a Slack message between 1 and 4,000 characters before queueing.")
        return message

    # ---------- time-entry exclusions ----------
    def reload(self) -> None:
        """Recompute the report from the stored snapshot after local state changes.

        An exclusion changes evidence, not source data, so this never touches
        Salesforce or Rocketlane. Trend is deliberately not advanced: an exclusion is
        not a new day of data.
        """
        if self.settings.get("mode") != "mcp":
            return
        path = self.settings.get("mcp_snapshot_path")
        if not path or not path.exists():
            return
        if not self.lock.acquire(timeout=10):
            raise QueueError("A refresh is in progress. Try again in a moment.")
        try:
            result = self._load_mcp_report()
            self._observe_source(result)
            write_cache(self.settings["cache_path"], result)
            self._data = result
        except McpSnapshotError as exc:
            raise QueueError(str(exc)) from exc
        finally:
            self.lock.release()


    def _flagged_entries_for_account(self, account_id: str) -> List[Dict[str, Any]]:
        """Every entry on an account that currently carries a data-quality signal."""
        from .evidence import entry_quality_signals
        data = self._data or {}
        account = next(
            (item for item in data.get("accounts", []) if str(item.get("id")) == str(account_id)),
            None,
        )
        if not account:
            raise QueueValidationError("Unknown account.")
        projects = {str(item.get("id")): item for item in account.get("projects", [])}
        flagged: List[Dict[str, Any]] = []
        for entry in account.get("entries", []):
            signals = entry_quality_signals(entry, projects.get(str(entry.get("project_id"))))
            if not signals:
                continue
            flagged.append({
                "entry_id": str(entry.get("id")),
                "entry_date": entry.get("date"),
                "signals": signals,
                "hours": entry.get("hours"),
                "user_name": entry.get("user_name") or entry.get("user_email") or "",
                "project_name": entry.get("project_name") or "",
            })
        flagged.sort(key=lambda item: (str(item.get("entry_date") or ""), item["entry_id"]))
        return flagged

    def preview_time_entry_exclusions(
        self, account_id: str, *, before_date: str = "", entry_ids: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """Resolve a bulk selection to concrete entry IDs without writing anything."""
        flagged = self._flagged_entries_for_account(account_id)
        already = set(self._time_entry_exclusions())
        candidates = [item for item in flagged if item["entry_id"] not in already]
        if before_date:
            try:
                cutoff = date.fromisoformat(str(before_date))
            except ValueError as exc:
                raise QueueValidationError("Enter a valid cutoff date.") from exc
            candidates = [
                item for item in candidates
                if item.get("entry_date") and date.fromisoformat(str(item["entry_date"])) < cutoff
            ]
        if entry_ids is not None:
            wanted = {str(value) for value in entry_ids}
            candidates = [item for item in candidates if item["entry_id"] in wanted]
        return {
            "account_id": str(account_id),
            "flagged_count": len(flagged),
            "already_excluded": len(already & {item["entry_id"] for item in flagged}),
            "entries": candidates,
            "count": len(candidates),
        }

    def exclude_time_entries(
        self,
        account_id: str,
        *,
        entry_ids: Optional[Sequence[str]] = None,
        before_date: str = "",
        reason: str,
        confirmed: bool,
        workstream_id: str = "",
    ) -> Dict[str, Any]:
        """Accept specific flagged entries as unfixable.

        Bulk-by-date is resolved here into an explicit list, so the stored record is
        always a concrete set of entries the operator could see at the time.
        """
        if confirmed is not True:
            raise QueueValidationError("Confirm the exclusion before applying it.")
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        if not entry_ids and not before_date:
            raise QueueValidationError("Choose entries or a cutoff date to exclude.")
        preview = self.preview_time_entry_exclusions(
            account_id, before_date=before_date, entry_ids=entry_ids
        )
        if not preview["entries"]:
            raise QueueValidationError("No flagged entries match that selection.")
        result = self.remediation_store.exclude_time_entries(
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            account_id=str(account_id),
            entries=preview["entries"],
            reason=reason,
            actor=self._exclusion_actor(),
            workstream_id=workstream_id or None,
        )
        self.reload()
        return {**result, "account_id": str(account_id)}

    def restore_time_entries(
        self, *, entry_ids: Sequence[str], confirmed: bool, workstream_id: str = ""
    ) -> Dict[str, Any]:
        if confirmed is not True:
            raise QueueValidationError("Confirm before restoring excluded entries.")
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        result = self.remediation_store.restore_time_entries(
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            entry_ids=entry_ids,
            actor=self._exclusion_actor(),
            workstream_id=workstream_id or None,
        )
        self.reload()
        return result

    def _exclusion_actor(self) -> str:
        meta = (self._data or {}).get("meta", {})
        requester = meta.get("requester") if isinstance(meta.get("requester"), Mapping) else {}
        return str(
            requester.get("email")
            or self.settings.get("mcp_requester_email")
            or "Hours Recon operator"
        )

    def queue_remediation_slack(
        self,
        workstream_id: str,
        *,
        expected_version: int,
        recipient_query: str,
        reviewed_message: str,
        confirmed: bool,
    ) -> Dict[str, Any]:
        """Persist a reviewed handoff for an authenticated Glean Pi Slack MCP send."""
        if confirmed is not True:
            raise QueueValidationError("Confirm the reviewed Slack message before queueing it for Glean Pi.")
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        workstream = self.get_remediation_workstream(workstream_id)
        if not workstream:
            raise QueueValidationError("Unknown remediation workstream.")
        plan = workstream.get("execution_plan")
        if not isinstance(plan, Mapping) or workstream.get("execution_path_id") != workstream.get("selected_path_id"):
            raise QueueValidationError("Open the selected remediation path and review its current Slack handoff before queueing.")
        message = self._reviewed_slack_message(reviewed_message)
        result = self.remediation_store.queue_slack_message(
            workstream_id,
            scope_id=self._active_scope_id(),
            portfolio_id=self._active_portfolio_id(),
            account_ids=self._owned_account_ids(),
            expected_version=expected_version,
            execution_id=str(plan.get("execution_id") or ""),
            path_id=str(workstream.get("selected_path_id") or ""),
            recipient_query=recipient_query,
            message=message,
        )
        result["slack_message"] = message
        result["next_step"] = "Tell Glean Pi: send pending Hours Recon messages"
        return result

    def queue_remediation_source_action(
        self,
        workstream_id: str,
        *,
        expected_version: int,
        operation_index: int,
        proposed_fields: Mapping[str, Any],
        confirmed: bool,
    ) -> Dict[str, Any]:
        if confirmed is not True:
            raise QueueValidationError("Confirm that you reviewed the proposed fields before queueing the source action.")
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        result = self.remediation_store.queue_source_action(
            workstream_id,
            scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            account_ids=self._owned_account_ids(), expected_version=expected_version,
            operation_index=operation_index, proposed_fields=proposed_fields,
        )
        result["next_step"] = "Tell Glean Pi: execute pending Hours Recon source actions"
        return result

    def list_source_actions(self, status: str = "pending") -> List[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.list_source_actions(
            scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(), status=status,
            account_ids=self._owned_account_ids(),
        )

    def claim_source_action(self, outbox_id: str, *, expected_version: int) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.claim_source_action(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, account_ids=self._owned_account_ids(),
        )

    def complete_source_action(
        self, outbox_id: str, *, expected_version: int, source_links: List[str], result_summary: str,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.complete_source_action(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, source_links=source_links, result_summary=result_summary,
            account_ids=self._owned_account_ids(),
        )

    def mark_source_action_uncertain(
        self, outbox_id: str, *, expected_version: int, error: str,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.mark_source_action_uncertain(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, error=error, account_ids=self._owned_account_ids(),
        )

    def retry_source_action(
        self, outbox_id: str, *, expected_version: int, confirmed_not_applied: bool,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.retry_source_action(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, confirmed_not_applied=confirmed_not_applied,
            account_ids=self._owned_account_ids(),
        )

    def list_slack_outbox(self, status: str = "pending") -> List[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.list_slack_outbox(
            scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(), status=status,
            account_ids=self._owned_account_ids(),
        )

    def claim_slack_outbox(self, outbox_id: str, *, expected_version: int) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.claim_slack_outbox(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, account_ids=self._owned_account_ids(),
        )

    def complete_slack_outbox(
        self, outbox_id: str, *, expected_version: int, recipient_id: str, permalink: str,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.complete_slack_outbox(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, recipient_id=recipient_id, permalink=permalink,
            account_ids=self._owned_account_ids(),
        )

    def mark_slack_outbox_uncertain(
        self, outbox_id: str, *, expected_version: int, error: str,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.mark_slack_outbox_uncertain(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, error=error, account_ids=self._owned_account_ids(),
        )

    def retry_slack_outbox(
        self, outbox_id: str, *, expected_version: int, confirmed_not_delivered: bool,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation planner is unavailable.")
        return self.remediation_store.retry_slack_outbox(
            outbox_id, scope_id=self._active_scope_id(), portfolio_id=self._active_portfolio_id(),
            expected_version=expected_version, confirmed_not_delivered=confirmed_not_delivered,
            account_ids=self._owned_account_ids(),
        )

    def refresh(self) -> Dict[str, Any]:
        now = monotonic()
        if self.last_refresh_attempt is not None and now - self.last_refresh_attempt < self.MIN_REFRESH_INTERVAL_SECONDS:
            raise RuntimeError("Please wait a few seconds before refreshing again.")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("A refresh is already in progress.")
        self.last_refresh_attempt = now
        try:
            report_date = business_today(self.settings["timezone"])
            if self.settings["mode"] == "demo":
                result = demo_report(self.settings["packages"], self.settings["account_aliases"], as_of=report_date)
            elif self.settings["mode"] == "mcp":
                result = self._load_mcp_report()
            else:
                salesforce_client = SalesforceClient()
                salesforce_data = salesforce_client.fetch(self.settings["requester_email"], as_of=report_date)
                rocketlane_client = RocketlaneClient()
                projects = rocketlane_client.fetch_projects()
                # Two-phase by design: fetch_projects() returns the whole
                # Rocketlane workspace, so match first and only pull time entries
                # for in-scope projects. reconcile() re-derives the same
                # deterministic map from identical inputs, so no drift results.
                project_map, _ = match_projects(salesforce_data["accounts"], projects, self.settings["account_aliases"])
                entries = rocketlane_client.fetch_time_entries(project_map.keys())
                result = reconcile(
                    salesforce_data,
                    {"projects": projects, "entries": entries},
                    package_config=self.settings["packages"],
                    account_aliases=self.settings["account_aliases"],
                    as_of=report_date,
                    mode="live",
                    governance_mode=self.settings.get("governance_mode", "observe_only"),
                    time_entry_exclusions=self._time_entry_exclusions(),
                    source_coverage={
                        "complete": False,
                        "accounts": True,
                        "opportunities": False,
                        "projects": True,
                        "time_entries": True,
                        "pagination_complete": True,
                    },
                )
                result["meta"]["source_metadata"] = {"salesforce": salesforce_data.get("metadata", {})}
                result["meta"]["source_retrieval_id"] = f"live-{uuid4().hex}"
                result["meta"]["source_scope_id"] = self.settings.get("remediation_scope_id") or salesforce_data.get("metadata", {}).get("instance_url") or "live-local"
                result["meta"]["source_coverage_complete"] = False
            result["meta"]["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            self._observe_source(result)
            self._record_trend(result)
            if result["meta"]["mode"] in {"live", "mcp"}:
                write_cache(self.settings["cache_path"], result)
            self._data = result
            return self.data
        finally:
            self.lock.release()
