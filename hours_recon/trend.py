"""Week-over-week movement for a weekly-cadence report.

The dashboard is opened on a cadence, so the most useful fact about a number is
usually how it moved. This keeps a private two-slot baseline (the latest report
and the one before it) so deltas stay stable across page reloads and only
advance when the underlying data actually changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

TREND_FIELDS = (
    "remaining_hours",
    "at_risk_hours",
    "expired_unused_hours",
    "overage_hours",
    "sold_hours",
    "billed_hours",
)
SCHEMA_VERSION = 1


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def report_signature(report: Mapping[str, Any]) -> str:
    """Stable digest of the figures a person would notice changing."""
    payload = {
        "as_of": str(report.get("meta", {}).get("as_of") or ""),
        "accounts": sorted(
            [str(account.get("id") or "")] + [_number(account.get(field)) for field in TREND_FIELDS]
            for account in report.get("accounts", [])
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact record of one report, used only to compute the next report's deltas."""
    metrics = report.get("metrics", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "signature": report_signature(report),
        "as_of": str(report.get("meta", {}).get("as_of") or ""),
        "refreshed_at": str(report.get("meta", {}).get("refreshed_at") or ""),
        "metrics": {field: _number(metrics.get(field)) for field in TREND_FIELDS},
        "accounts": {
            str(account.get("id")): {field: _number(account.get(field)) for field in TREND_FIELDS}
            for account in report.get("accounts", [])
            if account.get("id")
        },
    }


def _deltas(current: Mapping[str, Any], previous: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    result: Dict[str, Dict[str, float]] = {}
    for field in TREND_FIELDS:
        before = _number(previous.get(field))
        now = _number(current.get(field))
        result[field] = {"previous": before, "delta": round(now - before, 2)}
    return result


def advance(baseline: Optional[Mapping[str, Any]], report: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the baseline file contents after observing ``report``.

    The file keeps the latest report and the one before it. Re-reading the same
    report never advances the window, so deltas do not silently collapse to zero
    when the page is reloaded.
    """
    current = snapshot(report)
    if not isinstance(baseline, Mapping) or not baseline.get("current"):
        return {"schema_version": SCHEMA_VERSION, "current": current, "previous": None}
    existing = baseline.get("current") or {}
    if str(existing.get("signature")) == current["signature"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "current": {**existing, **{"refreshed_at": current["refreshed_at"]}},
            "previous": baseline.get("previous"),
        }
    return {"schema_version": SCHEMA_VERSION, "current": current, "previous": existing}


def comparison_for(baseline: Optional[Mapping[str, Any]], report: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Pick which stored snapshot ``report`` should be compared against."""
    if not isinstance(baseline, Mapping):
        return None
    current = baseline.get("current") or {}
    if str(current.get("signature")) == report_signature(report):
        previous = baseline.get("previous")
        return previous if isinstance(previous, Mapping) and previous.get("metrics") else None
    return current if current.get("metrics") else None


def attach_trend(report: Dict[str, Any], baseline: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Attach portfolio and per-account movement since the previous report."""
    reference = comparison_for(baseline, report)
    if not reference:
        report["trend"] = {"available": False, "compared_to": None, "metrics": {}}
        for account in report.get("accounts", []):
            account["trend"] = None
        return report

    report["trend"] = {
        "available": True,
        "compared_to": {
            "as_of": reference.get("as_of") or None,
            "refreshed_at": reference.get("refreshed_at") or None,
        },
        "metrics": _deltas(report.get("metrics", {}), reference.get("metrics", {})),
    }
    reference_accounts = reference.get("accounts") or {}
    for account in report.get("accounts", []):
        before = reference_accounts.get(str(account.get("id")))
        if not isinstance(before, Mapping):
            account["trend"] = {"new": True, "fields": {}}
            continue
        account["trend"] = {"new": False, "fields": _deltas(account, before)}
    return report
