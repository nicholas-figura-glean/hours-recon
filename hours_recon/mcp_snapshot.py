"""Import normalized snapshots produced through authenticated MCP tool calls.

MCP authentication belongs to the Glean Pi session, not the local HTTP server.
The agent writes a normalized source snapshot, and this module runs the same
reconciliation engine used by the direct connectors.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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

    report = reconcile(
        salesforce,
        rocketlane,
        package_config=package_config,
        account_aliases=account_aliases,
        as_of=business_today(timezone_name),
        mode="mcp",
    )
    source_meta = snapshot.get("meta", {})
    report["meta"].update({
        "source": "Salesforce MCP + Rocketlane MCP",
        "mcp_snapshot_created_at": source_meta.get("created_at"),
        "mcp_scope": source_meta.get("scope"),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "notice": "Live data imported from authenticated Salesforce and Rocketlane MCP tools.",
    })
    return report
