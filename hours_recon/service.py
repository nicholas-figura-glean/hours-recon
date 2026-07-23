"""Orchestrate data retrieval, reconciliation, governance, caching, and remediation."""

from __future__ import annotations

import copy
import secrets
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from .dates import business_today
from .demo import demo_report
from .evidence import attach_governance
from .matching import match_projects
from .mcp_snapshot import load_mcp_snapshot
from .reconcile import reconcile
from .remediation_store import QueueError, RemediationStore
from .rocketlane import RocketlaneClient
from .salesforce import SalesforceClient
from .storage import read_cache, write_cache


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
        if self._data is None and configured_mode == "mcp" and self.settings["mcp_snapshot_path"].exists():
            self._data = self._load_mcp_report()
            self._observe_source(self._data)
            write_cache(self.settings["cache_path"], self._data)
        if self._data is None:
            self._data = demo_report(
                self.settings["packages"],
                self.settings["account_aliases"],
                as_of=business_today(self.settings["timezone"]),
            )
            if configured_mode == "mcp":
                self._data["meta"]["notice"] = "Demo data is shown until Glean Pi writes the first MCP snapshot."
            else:
                self._data["meta"]["notice"] = "Demo data is shown until the first successful live refresh."

    @property
    def data(self) -> Dict[str, Any]:
        result = copy.deepcopy(self._data)
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

    def _attach_remediation(self, result: Dict[str, Any]) -> None:
        if not self.remediation_store:
            result["remediation_queue"] = {
                "schema_version": 1,
                "mode": self.settings.get("remediation_mode", "off"),
                "available": False,
                "error": self.remediation_error,
                "cases": [],
                "active_case_count": 0,
                "active_gap_count": 0,
            }
            return
        try:
            scope_id = self._active_scope_id(result)
            summary = self.remediation_store.summary(scope_id=scope_id)
            summary["available"] = True
            summary["action_token"] = self.action_token
            by_account = {str(item["account_id"]): item for item in summary.get("cases", [])}
            for account in result.get("accounts", []):
                case = by_account.get(str(account.get("id")))
                if case:
                    account["remediation"] = {
                        "case_fingerprint": case["fingerprint"],
                        "status": case["status"],
                        "priority": case["priority"],
                        "primary_route": case["primary_route"],
                        "due_on": case["due_on"],
                        "active_gap_count": case["active_gap_count"],
                        "version": case["version"],
                    }
                else:
                    account["remediation"] = None
            result["remediation_queue"] = summary
        except Exception as exc:
            self.remediation_error = f"{type(exc).__name__}: {exc}"
            result["remediation_queue"] = {
                "schema_version": 1,
                "mode": "observe_only",
                "available": False,
                "error": self.remediation_error,
                "cases": [],
                "active_case_count": 0,
                "active_gap_count": 0,
            }

    def status(self) -> Dict[str, Any]:
        queue_health: Dict[str, Any]
        if self.remediation_store:
            try:
                queue_health = self.remediation_store.health(scope_id=self._active_scope_id())
            except Exception as exc:
                queue_health = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            queue_health = {"available": False, "error": self.remediation_error}
        return {
            "configured_mode": self.settings["mode"],
            "requester_email": self.settings["requester_email"],
            "has_live_cache": self.settings["cache_path"].exists() and self._data.get("meta", {}).get("mode") in {"live", "mcp"},
            "displayed_mode": self._data.get("meta", {}).get("mode"),
            "governance_mode": self.settings.get("governance_mode", "observe_only"),
            "governance_policy_version": self._data.get("governance", {}).get("policy_version"),
            "remediation_mode": self.settings.get("remediation_mode", "off"),
            "remediation_queue": queue_health,
        }

    def _load_mcp_report(self) -> Dict[str, Any]:
        return load_mcp_snapshot(
            self.settings["mcp_snapshot_path"],
            package_config=self.settings["packages"],
            account_aliases=self.settings["account_aliases"],
            timezone_name=self.settings["timezone"],
            governance_mode=self.settings.get("governance_mode", "observe_only"),
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
        try:
            observation = self.remediation_store.observe(
                result,
                retrieval_id=retrieval_id,
                scope_id=scope_id,
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

    def list_remediation_cases(self, filters: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation queue is unavailable.")
        values = dict(filters or {})
        return self.remediation_store.list_cases(
            scope_id=self._active_scope_id(),
            status=values.get("status"),
            route=values.get("route"),
            priority=values.get("priority"),
            account_id=values.get("account_id"),
        )

    def get_remediation_case(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        if not self.remediation_store:
            raise QueueError("The remediation queue is unavailable.")
        return self.remediation_store.get_case(fingerprint, scope_id=self._active_scope_id())

    def remediation_action(
        self,
        gap_id: str,
        *,
        action: str,
        expected_version: int,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.remediation_store:
            raise QueueError("The remediation queue is unavailable.")
        return self.remediation_store.action(
            gap_id,
            scope_id=self._active_scope_id(),
            action=action,
            expected_version=expected_version,
            payload=payload,
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
                salesforce_data = salesforce_client.fetch(self.settings["requester_email"])
                rocketlane_client = RocketlaneClient()
                projects = rocketlane_client.fetch_projects()
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
            if result["meta"]["mode"] in {"live", "mcp"}:
                write_cache(self.settings["cache_path"], result)
            self._data = result
            return self.data
        finally:
            self.lock.release()
