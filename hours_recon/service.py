"""Orchestrate data retrieval, matching scope, reconciliation, and caching."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any, Dict, Mapping

from .dates import business_today
from .demo import demo_report
from .matching import match_projects
from .mcp_snapshot import load_mcp_snapshot
from .reconcile import reconcile
from .rocketlane import RocketlaneClient
from .salesforce import SalesforceClient
from .storage import read_cache, write_cache


class ReconciliationService:
    MIN_REFRESH_INTERVAL_SECONDS = 5

    def __init__(self, app_settings: Mapping[str, Any]) -> None:
        self.settings = dict(app_settings)
        self.lock = Lock()
        self.last_refresh_attempt = None
        cached = None
        configured_mode = self.settings["mode"]
        if configured_mode != "demo":
            cached = read_cache(self.settings["cache_path"], self.settings["cache_max_age_days"])
            if cached and cached.get("meta", {}).get("mode") != configured_mode:
                cached = None
        self._data = cached
        if self._data is None and configured_mode == "mcp" and self.settings["mcp_snapshot_path"].exists():
            self._data = self._load_mcp_report()
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
        return self._data

    def status(self) -> Dict[str, Any]:
        return {
            "configured_mode": self.settings["mode"],
            "requester_email": self.settings["requester_email"],
            "has_live_cache": self.settings["cache_path"].exists() and self._data.get("meta", {}).get("mode") in {"live", "mcp"},
            "displayed_mode": self._data.get("meta", {}).get("mode"),
        }

    def _load_mcp_report(self) -> Dict[str, Any]:
        return load_mcp_snapshot(
            self.settings["mcp_snapshot_path"],
            package_config=self.settings["packages"],
            account_aliases=self.settings["account_aliases"],
            timezone_name=self.settings["timezone"],
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
                )
                result["meta"]["source_metadata"] = {"salesforce": salesforce_data.get("metadata", {})}
            result["meta"]["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            if result["meta"]["mode"] in {"live", "mcp"}:
                write_cache(self.settings["cache_path"], result)
            self._data = result
            return result
        finally:
            self.lock.release()
