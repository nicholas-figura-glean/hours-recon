"""Describe how current the loaded data is, once, in plain language.

Retrieval problems used to reach the user only as governance tier caps, which
meant a seven-day-old snapshot appeared as dozens of high-priority evidence
failures. The tier caps are still correct and unchanged; this module gives the
same condition a single, honest, actionable presentation.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional

from .dates import parse_date
from .evidence import COVERAGE_REQUIREMENTS, coverage_labels

REFRESH_COMMAND = "Run Hours Recon MCP refresh"
REQUIRED_COVERAGE = tuple(sorted({key for keys in COVERAGE_REQUIREMENTS.values() for key in keys}))


def _join(labels: List[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _day_phrase(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day old"
    return f"{days} days old"


def _missing_datasets(coverage: Mapping[str, Any]) -> List[str]:
    return coverage_labels(key for key in REQUIRED_COVERAGE if coverage.get(key) is not True)


def describe_freshness(meta: Mapping[str, Any], *, report_date: date) -> Dict[str, Any]:
    """Summarize data currency for the header banner and the data-quality panel."""
    mode = str(meta.get("mode") or "unknown")
    result: Dict[str, Any] = {
        "state": "unknown",
        "is_current": False,
        "covers_today": False,
        "blocks_verification": True,
        "mode": mode,
        "through_date": None,
        "days_behind": None,
        "missing_datasets": [],
        "headline": "Data currency is unknown",
        "detail": "Hours Recon could not determine how current this data is. Refresh before acting on these numbers.",
        "action_label": "Refresh",
        "action_hint": REFRESH_COMMAND,
    }

    if mode == "demo":
        result.update({
            "state": "demo",
            # Sample data is internally consistent through the report date, so
            # week-to-date figures are meaningful even though it is not real.
            "covers_today": True,
            "headline": "Sample data",
            "detail": (
                "These are built-in example accounts, not your portfolio. Connect Salesforce and "
                "Rocketlane in Glean Pi, then run a refresh to load your own data."
            ),
            "action_label": None,
        })
        return result

    if mode == "live":
        complete = meta.get("source_coverage_complete") is True
        result.update({
            "state": "current" if complete else "incomplete",
            "is_current": complete,
            "covers_today": True,
            "blocks_verification": not complete,
            "through_date": str(meta.get("as_of") or ""),
            "days_behind": 0,
            "headline": "Live connector data" if complete else "Live pull was not confirmed complete",
            "detail": (
                "Loaded directly from the Salesforce and Rocketlane connectors."
                if complete else
                "The direct connector pull does not confirm full opportunity coverage, so hours cannot be "
                "marked verified. Numbers below are still the best available view."
            ),
        })
        return result

    coverage = meta.get("mcp_coverage") if isinstance(meta.get("mcp_coverage"), Mapping) else {}
    through_date_text = str(meta.get("mcp_through_date") or "")
    days_behind: Optional[int] = None
    if through_date_text:
        try:
            days_behind = (report_date - parse_date(through_date_text)).days
        except (ValueError, TypeError):
            days_behind = None
    missing = _missing_datasets(coverage)
    scope_verified = meta.get("mcp_scope_verified") is True
    stale = days_behind is None or days_behind > 0

    result.update({
        "through_date": through_date_text or None,
        "days_behind": days_behind,
        "covers_today": not stale,
        "missing_datasets": missing,
    })

    if not stale and not missing and scope_verified:
        result.update({
            "state": "current",
            "is_current": True,
            "blocks_verification": False,
            "headline": "Data is current",
            "detail": f"Verified complete pull through {through_date_text}.",
            "action_label": None,
            "action_hint": None,
        })
        return result

    if missing:
        result.update({
            "state": "incomplete",
            "headline": "Last data pull was incomplete",
            "detail": (
                f"It did not return {_join(missing)}. At-risk hours and weekly activity may be wrong "
                "until a complete pull succeeds."
            ),
        })
        if stale and days_behind is not None:
            result["detail"] += f" The data is also {_day_phrase(days_behind)}."
        return result

    if not scope_verified:
        result.update({
            "state": "unverified_scope",
            "headline": "Data source could not be confirmed",
            "detail": (
                "Hours Recon could not confirm which Salesforce and Rocketlane workspace this data came "
                "from, so hours are shown but not marked verified."
            ),
        })
        return result

    result.update({
        "state": "stale",
        "headline": f"Data is {_day_phrase(days_behind or 0)}",
        "detail": (
            f"Everything below reflects data through {through_date_text}. Weekly activity and at-risk "
            "hours are unreliable until you refresh."
        ),
    })
    return result
