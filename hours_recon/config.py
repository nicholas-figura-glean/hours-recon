"""Configuration loading without third-party dependencies."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=VALUE pairs without replacing exported variables."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_json_optional(path: Path) -> Dict[str, Any]:
    """Load an optional config file, returning {} when it is absent or unreadable.

    Pinned MCP bindings are a cache, never a source of truth. A missing or
    corrupt file must degrade to full rediscovery rather than fail the refresh.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def binding_freshness(bindings: Mapping[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Report whether pinned MCP bindings may be reused without rediscovery.

    Bindings only remove *discovery* round trips. Scope is still corroborated
    against live connector identity on every refresh, so a stale binding costs
    an extra verification pass rather than admitting unverified data.
    """
    reference = today or date.today()
    if not bindings:
        return {"fresh": False, "reason": "no pinned bindings are configured"}
    if bindings.get("schema_version") != 1:
        return {"fresh": False, "reason": "unsupported bindings schema version"}
    raw_verified_on = str(bindings.get("verified_on") or "")
    try:
        verified_on = date.fromisoformat(raw_verified_on)
    except ValueError:
        return {"fresh": False, "reason": "bindings have no valid verified_on date"}
    try:
        ttl_days = int(bindings.get("ttl_days", 7))
    except (TypeError, ValueError):
        ttl_days = 7
    if verified_on > reference:
        return {"fresh": False, "reason": "bindings verified_on is in the future"}
    age_days = (reference - verified_on).days
    required = (
        ("salesforce", "mcp_server"),
        ("salesforce", "account_aiom_field"),
        ("rocketlane", "mcp_server"),
    )
    for section, key in required:
        if not str((bindings.get(section) or {}).get(key) or "").strip():
            return {"fresh": False, "reason": f"bindings are missing {section}.{key}"}
    if age_days > ttl_days:
        return {
            "fresh": False,
            "age_days": age_days,
            "expires_on": (verified_on + timedelta(days=ttl_days)).isoformat(),
            "reason": f"bindings are {age_days} days old and exceed the {ttl_days}-day TTL",
        }
    return {
        "fresh": True,
        "age_days": age_days,
        "expires_on": (verified_on + timedelta(days=ttl_days)).isoformat(),
        "reason": "pinned bindings are within TTL",
    }


def settings() -> Dict[str, Any]:
    load_dotenv()
    return {
        "mode": os.getenv("HOURS_RECON_MODE", "demo").lower(),
        "host": os.getenv("HOURS_RECON_HOST", "127.0.0.1"),
        "port": int(os.getenv("HOURS_RECON_PORT", "8765")),
        "requester_email": os.getenv("HOURS_RECON_REQUESTER_EMAIL", ""),
        # MCP snapshots are produced outside this process. Bind them to the
        # expected requester so a previous user's private snapshot is never
        # displayed as the current user's dashboard.
        "mcp_requester_email": os.getenv("HOURS_RECON_MCP_REQUESTER_EMAIL", os.getenv("HOURS_RECON_REQUESTER_EMAIL", "")).strip().lower(),
        "timezone": os.getenv("HOURS_RECON_TIMEZONE", "America/Denver"),
        "cache_max_age_days": int(os.getenv("HOURS_RECON_CACHE_MAX_AGE_DAYS", "30")),
        "packages": load_json(ROOT / "config" / "packages.json"),
        "account_aliases": load_json(ROOT / "config" / "account_aliases.json"),
        # Cached MCP discovery results. Optional by design: an absent file just
        # means the refresh skill rediscovers servers, tools, and field names.
        "mcp_bindings": load_json_optional(ROOT / os.getenv("HOURS_RECON_MCP_BINDINGS_PATH", "config/mcp_bindings.json")),
        # Overridable so a second instance (verification, a scratch run against a
        # fixture) cannot overwrite the real portfolio's cached report. Without
        # this, any non-demo run silently clobbers var/reconciliation.json.
        "cache_path": ROOT / os.getenv("HOURS_RECON_CACHE_PATH", "var/reconciliation.json"),
        "mcp_snapshot_path": ROOT / os.getenv("HOURS_RECON_MCP_SNAPSHOT_PATH", "var/mcp_snapshot.json"),
        "governance_mode": os.getenv("HOURS_RECON_GOVERNANCE_MODE", "observe_only").lower(),
        "remediation_mode": os.getenv("HOURS_RECON_REMEDIATION_MODE", "observe_only").lower(),
        "remediation_db_path": ROOT / os.getenv("HOURS_RECON_REMEDIATION_DB_PATH", "var/remediation.sqlite3"),
        "remediation_scope_id": os.getenv("HOURS_RECON_REMEDIATION_SCOPE_ID", "").strip(),
        "salesforce_web_base_url": os.getenv("HOURS_RECON_SALESFORCE_WEB_BASE_URL", "https://glean.lightning.force.com").strip(),
        "rocketlane_web_base_url": os.getenv("HOURS_RECON_ROCKETLANE_WEB_BASE_URL", "https://glean.rocketlane.com").strip(),
        "mcp_workspace_url": os.getenv("HOURS_RECON_MCP_WORKSPACE_URL", "https://app.glean.com/chat").strip(),
    }
