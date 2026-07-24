"""Configuration loading without third-party dependencies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

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
        "cache_path": ROOT / "var" / "reconciliation.json",
        "mcp_snapshot_path": ROOT / os.getenv("HOURS_RECON_MCP_SNAPSHOT_PATH", "var/mcp_snapshot.json"),
        "governance_mode": os.getenv("HOURS_RECON_GOVERNANCE_MODE", "observe_only").lower(),
        "remediation_mode": os.getenv("HOURS_RECON_REMEDIATION_MODE", "observe_only").lower(),
        "remediation_db_path": ROOT / os.getenv("HOURS_RECON_REMEDIATION_DB_PATH", "var/remediation.sqlite3"),
        "remediation_scope_id": os.getenv("HOURS_RECON_REMEDIATION_SCOPE_ID", "").strip(),
    }
