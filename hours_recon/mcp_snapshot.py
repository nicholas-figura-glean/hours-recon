"""Import normalized snapshots produced through authenticated MCP tool calls.

MCP authentication belongs to the Glean Pi session, not the local HTTP server.
The agent writes a normalized source snapshot, and this module runs the same
reconciliation engine used by the direct connectors.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from .dates import business_today
from .reconcile import reconcile


class McpSnapshotError(RuntimeError):
    pass


def load_mcp_snapshot(
    path: Path,
    *,
    package_config: Mapping[str, Any],
    account_aliases: Mapping[str, Any],
    timezone_name: str,
    governance_mode: str = "observe_only",
) -> Dict[str, Any]:
    if not path.exists():
        raise McpSnapshotError(
            f"No MCP snapshot exists at {path}. Ask Glean Pi to run an Hours Recon MCP refresh first."
        )
    try:
        with path.open(encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        raise McpSnapshotError(f"The MCP snapshot could not be read: {exc}") from exc

    if snapshot.get("schema_version") != 1:
        raise McpSnapshotError("Unsupported MCP snapshot schema version.")
    salesforce = snapshot.get("salesforce")
    rocketlane = snapshot.get("rocketlane")
    if not isinstance(salesforce, dict) or not isinstance(rocketlane, dict):
        raise McpSnapshotError("The MCP snapshot must contain Salesforce and Rocketlane source objects.")

    source_meta = snapshot.get("meta", {})
    snapshot_digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    coverage = source_meta.get("coverage") if isinstance(source_meta.get("coverage"), dict) else {}
    required_coverage = ("accounts", "opportunities", "projects", "time_entries", "pagination_complete")
    report_date = business_today(timezone_name)
    through_date = source_meta.get("through_date")
    try:
        through_date_current = bool(through_date) and date.fromisoformat(str(through_date)) == report_date
    except ValueError:
        through_date_current = False
    data_coverage_complete = (
        coverage.get("complete") is True
        and all(coverage.get(key) is True for key in required_coverage)
        and through_date_current
    )
    explicit_scope_id = str(source_meta.get("scope_id") or "").strip()
    scope_verified = bool(explicit_scope_id) and source_meta.get("scope_verified") is True
    coverage_complete = data_coverage_complete and scope_verified
    effective_coverage = dict(coverage)
    effective_coverage["complete"] = data_coverage_complete
    effective_coverage["through_date_current"] = through_date_current
    scope_parts = [
        str(source_meta.get("salesforce_mcp_server") or ""),
        str(source_meta.get("rocketlane_mcp_server") or ""),
    ]
    scope_parts = [value for value in scope_parts if value]
    fallback_scope = "mcp:" + ":".join(scope_parts) if scope_parts else "mcp-local"
    report = reconcile(
        salesforce,
        rocketlane,
        package_config=package_config,
        account_aliases=account_aliases,
        as_of=report_date,
        mode="mcp",
        governance_mode=governance_mode,
        source_coverage=effective_coverage,
    )
    report["meta"].update({
        "source": "Salesforce MCP + Rocketlane MCP",
        "mcp_snapshot_created_at": source_meta.get("created_at"),
        "mcp_scope": source_meta.get("scope"),
        "mcp_retrieval_id": source_meta.get("retrieval_id") or f"legacy-{snapshot_digest}",
        "mcp_scope_id": explicit_scope_id or fallback_scope,
        "mcp_scope_verified": scope_verified,
        "mcp_data_coverage_complete": data_coverage_complete,
        "mcp_coverage": effective_coverage,
        "mcp_coverage_complete": coverage_complete,
        "mcp_through_date": through_date,
        "mcp_snapshot_digest": snapshot_digest,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "notice": "Live data imported from authenticated Salesforce and Rocketlane MCP tools.",
    })
    return report
