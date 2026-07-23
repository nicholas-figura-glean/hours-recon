"""Build deterministic remediation candidates from Tier 3/4 evidence gaps."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
DEFAULT_SLA_DAYS = {"P0": 5, "P1": 10, "P2": 20}


def stable_fingerprint(prefix: str, identity: Mapping[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def case_fingerprint(scope_id: str, account_id: str) -> str:
    return stable_fingerprint("hrc1_", {
        "schema": "hours-recon-case/v1",
        "scope_id": scope_id,
        "subject_type": "salesforce_account",
        "subject_id": account_id,
    })


def gap_fingerprint(case_id: str, dimension: str) -> str:
    return stable_fingerprint("hrg1_", {
        "schema": "hours-recon-gap/v1",
        "case_fingerprint": case_id,
        "dimension": dimension,
    })


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    return stable_fingerprint("hre1_", evidence)


def add_business_days(start: date, days: int) -> date:
    current = start
    remaining = max(0, int(days))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _has_expiring_package(account: Mapping[str, Any], maximum_days: int = 30) -> bool:
    return any(
        float(item.get("remaining_hours", 0) or 0) > 0
        and 0 <= int(item.get("days_to_expiration", maximum_days + 1)) <= maximum_days
        for item in account.get("packages", [])
    )


def _priority(account: Mapping[str, Any], gap: Mapping[str, Any]) -> str:
    tier = str(gap.get("tier") or "T4")
    dimension = str(gap.get("dimension") or "")
    if (
        tier == "T4"
        or float(account.get("overage_hours", 0) or 0) > 0
        or (dimension == "project_linkage" and float(account.get("sold_hours", 0) or 0) > 0)
        or _has_expiring_package(account)
    ):
        return "P0"
    if dimension in {"entitlement_source", "hours_mapping", "service_period", "project_linkage"}:
        return "P1"
    return "P2"


def _route(dimension: str) -> Dict[str, str]:
    routes = {
        "entitlement_source": {
            "route": "entitlement_data",
            "primary_owner": "Opportunity owner",
            "required_partner": "Deal Desk / RevOps",
        },
        "hours_mapping": {
            "route": "entitlement_data",
            "primary_owner": "Opportunity owner",
            "required_partner": "Deal Desk / RevOps + Hours Recon owner",
        },
        "service_period": {
            "route": "entitlement_data",
            "primary_owner": "Opportunity owner",
            "required_partner": "AIOM owner + Deal Desk / RevOps",
        },
        "project_linkage": {
            "route": "project_mapping",
            "primary_owner": "Rocketlane project owner",
            "required_partner": "CS Ops",
        },
        "time_quality": {
            "route": "time_quality",
            "primary_owner": "Rocketlane project owner / time-entry author",
            "required_partner": "Rocketlane admin when policy or connector-related",
        },
    }
    return routes.get(dimension, {
        "route": "data_governance",
        "primary_owner": "Hours Recon owner",
        "required_partner": "Source-system owner",
    })


def build_candidates(report: Mapping[str, Any], *, scope_id: str) -> List[Dict[str, Any]]:
    """Return one account case candidate containing one item per weak dimension."""
    as_of = date.fromisoformat(str(report.get("meta", {}).get("as_of")))
    candidates: List[Dict[str, Any]] = []
    for account in sorted(report.get("accounts", []), key=lambda item: str(item.get("id"))):
        governance = account.get("governance") or {}
        gaps = list(governance.get("gaps") or [])
        if not gaps:
            continue
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        case_id = case_fingerprint(scope_id, account_id)
        gap_candidates = []
        for raw_gap in sorted(gaps, key=lambda item: str(item.get("dimension"))):
            dimension = str(raw_gap.get("dimension") or "unknown")
            priority = _priority(account, raw_gap)
            routing = _route(dimension)
            evidence = {
                "account_id": account_id,
                "account_name": account.get("name"),
                "overall_tier": governance.get("overall_tier"),
                "dimension": dimension,
                "tier": raw_gap.get("tier"),
                "reason_code": raw_gap.get("reason_code"),
                "summary": raw_gap.get("summary"),
                "recommended_action": raw_gap.get("recommended_action"),
                "refs": raw_gap.get("refs") or [],
                "details": raw_gap.get("details") or {},
                "metric_impact": {
                    key: account.get(key, 0)
                    for key in (
                        "sold_hours", "billed_hours", "remaining_hours", "at_risk_hours",
                        "expired_unused_hours", "overage_hours",
                    )
                },
                "report_as_of": as_of.isoformat(),
                "policy_version": governance.get("policy_version"),
            }
            gap_candidates.append({
                "fingerprint": gap_fingerprint(case_id, dimension),
                "dimension": dimension,
                "tier": raw_gap.get("tier"),
                "reason_code": raw_gap.get("reason_code"),
                "summary": raw_gap.get("summary"),
                "recommended_action": raw_gap.get("recommended_action"),
                "priority": priority,
                "route": routing["route"],
                "primary_owner": routing["primary_owner"],
                "required_partner": routing["required_partner"],
                "due_on": add_business_days(as_of, DEFAULT_SLA_DAYS[priority]).isoformat(),
                "evidence": evidence,
                "evidence_hash": evidence_hash(evidence),
            })
        candidates.append({
            "fingerprint": case_id,
            "scope_id": scope_id,
            "account_id": account_id,
            "account_name": account.get("name"),
            "overall_tier": governance.get("overall_tier"),
            "gaps": gap_candidates,
        })
    return candidates


def priority_sort_key(value: str) -> int:
    return PRIORITY_RANK.get(str(value), 99)


def summarize_candidates(candidates: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    rows = list(candidates)
    gaps = [gap for case in rows for gap in case.get("gaps", [])]
    return {
        "case_count": len(rows),
        "gap_count": len(gaps),
        "p0_count": sum(1 for item in gaps if item.get("priority") == "P0"),
        "p1_count": sum(1 for item in gaps if item.get("priority") == "P1"),
        "p2_count": sum(1 for item in gaps if item.get("priority") == "P2"),
    }
