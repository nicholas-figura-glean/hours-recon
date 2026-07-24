"""Import normalized snapshots produced through authenticated MCP tool calls.

MCP authentication belongs to the Glean Pi session, not the local HTTP server.
The agent writes a normalized source snapshot, and this module runs the same
reconciliation engine used by the direct connectors.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from .dates import business_today
from .storage import write_cache
from .reconcile import reconcile


class McpSnapshotError(RuntimeError):
    pass


def publish_mcp_snapshot(
    path: Path,
    snapshot: Mapping[str, Any],
    *,
    expected_requester_email: str,
    expected_scope_id: str,
    timezone_name: str,
) -> None:
    """Atomically replace the active snapshot only with a complete verified pull.

    The caller is responsible for assembling the source evidence. This boundary
    prevents an incomplete pull, a different requester's data, or data from a
    different connector tenant from replacing the last known-good snapshot.
    """
    expected_email = expected_requester_email.strip().lower()
    actual_email = str((snapshot.get("salesforce") or {}).get("requester", {}).get("email") or "").strip().lower()
    if not expected_email or not actual_email or not secrets.compare_digest(expected_email, actual_email):
        raise McpSnapshotError("Refusing to publish an MCP snapshot for a different requester.")

    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}
    coverage = meta.get("coverage") if isinstance(meta.get("coverage"), Mapping) else {}
    required_coverage = ("accounts", "opportunities", "projects", "time_entries", "pagination_complete")
    if coverage.get("complete") is not True or not all(coverage.get(key) is True for key in required_coverage):
        raise McpSnapshotError("Refusing to publish an MCP snapshot with incomplete source coverage.")
    if meta.get("scope_verified") is not True or not expected_scope_id or not secrets.compare_digest(str(meta.get("scope_id") or ""), expected_scope_id):
        raise McpSnapshotError("Refusing to publish an MCP snapshot with an unverified or mismatched scope.")
    try:
        current_through_date = date.fromisoformat(str(meta.get("through_date") or "")) == business_today(timezone_name)
    except ValueError:
        current_through_date = False
    if not current_through_date:
        raise McpSnapshotError("Refusing to publish an MCP snapshot whose through_date is not the report date.")
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("rocketlane"), Mapping):
        raise McpSnapshotError("Refusing to publish an invalid MCP snapshot schema.")

    # write_cache creates a 0600 temporary file and atomically replaces path.
    write_cache(path, dict(snapshot))


def load_mcp_snapshot(
    path: Path,
    *,
    package_config: Mapping[str, Any],
    account_aliases: Mapping[str, Any],
    timezone_name: str,
    governance_mode: str = "observe_only",
    expected_requester_email: str = "",
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

    expected_email = expected_requester_email.strip().lower()
    snapshot_email = str((salesforce.get("requester") or {}).get("email") or "").strip().lower()
    if not expected_email:
        raise McpSnapshotError(
            "HOURS_RECON_MCP_REQUESTER_EMAIL is required in MCP mode so the dashboard can verify the snapshot belongs to its requester."
        )
    if not snapshot_email or snapshot_email != expected_email:
        raise McpSnapshotError(
            "The MCP snapshot requester does not match HOURS_RECON_MCP_REQUESTER_EMAIL. "
            "Ask Glean Pi to run an Hours Recon MCP refresh for the authenticated requester."
        )

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
        "mcp_requester_email": snapshot_email,
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
