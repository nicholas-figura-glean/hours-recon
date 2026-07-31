"""Build deterministic remediation workstreams from Tier 3/4 evidence gaps."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from .evidence import coverage_labels
from .remediation_policy import (
    MINIMUM_GOVERNED_TIER,
    POLICY_VERSION,
    path_options,
    rank_paths,
    route_for_dimension,
    validate_paths,
)

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
PRIORITY_ORDER = ("P0", "P1", "P2")
DEFAULT_SLA_DAYS = {"P0": 5, "P1": 10, "P2": 20}
# Gaps that make the hours themselves unmeasurable. When one of these is at T4
# the account's apparent risk cannot be trusted, so the gap outranks the
# account's currently visible exposure.
MEASUREMENT_BLOCKING_REASONS = frozenset({
    "no_recognized_entitlement",
    "unresolved_entitlement_evidence",
    "no_hours_mapping",
    "unresolved_hours_mapping",
    "no_rocketlane_project",
    "usage_unobservable_without_project",
    "invalid_time_evidence",
})
METRIC_FIELDS = (
    "sold_hours",
    "billed_hours",
    "remaining_hours",
    "at_risk_hours",
    "expired_unused_hours",
    "future_entitlement_hours",
    "overage_hours",
)


def stable_fingerprint(prefix: str, identity: Mapping[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Retained as deterministic v1 identity helpers for callers that need to
# recognize old local records. The v2 store does not write these identities.
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


def workstream_fingerprint(scope_id: str, portfolio_id: str, family: str, group_key: str) -> str:
    return stable_fingerprint("hrw2_", {
        "schema": "hours-recon-workstream/v2",
        "scope_id": scope_id,
        "portfolio_id": portfolio_id,
        "family": family,
        "group_key": group_key,
    })


def instance_fingerprint(scope_id: str, portfolio_id: str, account_id: str, dimension: str) -> str:
    return stable_fingerprint("hri2_", {
        "schema": "hours-recon-instance/v2",
        "scope_id": scope_id,
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "dimension": dimension,
    })


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    return stable_fingerprint("hre2_", evidence)


def add_business_days(start: date, days: int) -> date:
    current = start
    remaining = max(0, int(days))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def days_to_soonest_expiration(account: Mapping[str, Any]) -> Any:
    """Days until the first package holding usable hours expires, if any."""
    days = [
        int(item["days_to_expiration"])
        for item in account.get("packages", [])
        if float(item.get("remaining_hours", 0) or 0) > 0 and item.get("days_to_expiration") is not None
    ]
    return min(days) if days else None


def account_urgency(account: Mapping[str, Any]) -> str:
    """Rank an account by hours at stake and time remaining.

    Evidence tier is deliberately not an input. An unverified mapping on an
    account with no expiring hours is hygiene; a verified mapping on an account
    losing hours this month is not. Ranking on tier made every item P0 and
    destroyed the signal the priority column exists to carry.
    """
    overage = float(account.get("overage_hours", 0) or 0)
    expired = float(account.get("expired_unused_hours", 0) or 0)
    at_risk = float(account.get("at_risk_hours", 0) or 0)
    sold = float(account.get("sold_hours", 0) or 0)
    billed = float(account.get("billed_hours", 0) or 0)
    days = days_to_soonest_expiration(account)
    if overage > 0 or expired > 0:
        return "P0"
    if at_risk > 0 and days is not None and days <= 30:
        return "P0"
    if billed > 0 and sold <= 0:
        return "P1"
    if at_risk > 0 and days is not None and days <= 90:
        return "P1"
    return "P2"


def _escalate(priority: str) -> str:
    return PRIORITY_ORDER[max(0, PRIORITY_ORDER.index(priority) - 1)]


def _priority(account: Mapping[str, Any], gap: Mapping[str, Any]) -> str:
    priority = account_urgency(account)
    reason = str(gap.get("reason_code") or "")
    dimension = str(gap.get("dimension") or "")
    blocks_measurement = reason in MEASUREMENT_BLOCKING_REASONS or (
        dimension == "project_linkage" and float(account.get("sold_hours", 0) or 0) > 0
    )
    if blocks_measurement and str(gap.get("tier") or "") == "T4":
        return _escalate(priority)
    return priority


def _clean_key(value: Any, maximum: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    return text[:maximum]


def _weak_package_keys(account: Mapping[str, Any], dimension: str) -> List[Tuple[str, str]]:
    values: List[Tuple[str, str]] = []
    for package in account.get("packages", []):
        if dimension == "hours_mapping" and str(package.get("inference_source") or "") in {"product_code", "explicit_hours", "growth_tier"}:
            continue
        if dimension == "entitlement_source" and str(package.get("line_item_source") or "") in {"opportunity_line_item", "approved_quote", "synced_quote"}:
            continue
        raw = package.get("product_code") or package.get("mapping_key") or package.get("line_item_name")
        clean = _clean_key(raw)
        if clean and not clean.startswith(("line_item:", "opportunity:")):
            label = str(package.get("product_code") or package.get("line_item_name") or package.get("mapping_key"))
            values.append((clean, label))
    for unresolved in account.get("package_exceptions", []):
        raw = unresolved.get("product_code") or unresolved.get("line_item_name")
        clean = _clean_key(raw)
        if clean:
            label = str(unresolved.get("product_code") or unresolved.get("line_item_name"))
            values.append((clean, label))
    return sorted(set(values))


def _group_identity(account: Mapping[str, Any], gap: Mapping[str, Any]) -> Tuple[str, str, str]:
    account_id = str(account.get("id") or "")
    account_name = str(account.get("name") or account_id)
    dimension = str(gap.get("dimension") or "data_governance")
    reason = str(gap.get("reason_code") or "unknown")
    details = dict(gap.get("details") or {})

    if reason == "incomplete_source_coverage":
        missing = sorted({str(value) for value in details.get("missing_coverage", []) if value})
        labels = coverage_labels(missing) or ["the full dataset"]
        return "source_coverage", "coverage:" + "|".join(missing or ["unverified"]), f"Re-pull {', '.join(labels)}"

    if dimension in {"hours_mapping", "entitlement_source"}:
        keys = _weak_package_keys(account, dimension)
        if len(keys) == 1:
            key, label = keys[0]
            action = "Govern hours mapping" if dimension == "hours_mapping" else "Govern entitlement source"
            return dimension, f"product:{key}", f"{action} for {label}"

    if dimension == "service_period":
        weak_opportunities = sorted({
            str(package.get("opportunity_id"))
            for package in account.get("packages", [])
            if package.get("opportunity_id")
            and str(package.get("service_period_source") or "close_date_plus_one_year") not in {"line_item_explicit", "opportunity_explicit", "partial_explicit"}
        })
        suffix = f":opportunity:{weak_opportunities[0]}" if len(weak_opportunities) == 1 else ""
        return dimension, f"account:{account_id}{suffix}", f"Record contractual service periods for {account_name}"

    titles = {
        "project_linkage": f"Establish stable Rocketlane linkage for {account_name}",
        "time_quality": f"Correct Rocketlane time evidence for {account_name}",
        "entitlement_source": f"Govern entitlement evidence for {account_name}",
        "hours_mapping": f"Govern hours mapping for {account_name}",
    }
    return dimension, f"account:{account_id}", titles.get(dimension, f"Correct {dimension.replace('_', ' ')} for {account_name}")


def _impact_for_instances(instances: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    # A coverage workstream can have several dimensions for one account. Count
    # each account's report metrics only once so impact is never inflated.
    by_account: Dict[str, Mapping[str, Any]] = {}
    for instance in instances:
        evidence = instance.get("evidence") or {}
        by_account.setdefault(str(instance.get("account_id")), evidence.get("metric_impact") or {})
    return {
        field: round(sum(float(metrics.get(field, 0) or 0) for metrics in by_account.values()), 2)
        for field in METRIC_FIELDS
    }


def build_workstreams(
    report: Mapping[str, Any],
    *,
    scope_id: str,
    portfolio_id: str = "local-default",
) -> List[Dict[str, Any]]:
    """Build root-cause workstreams with stable account/dimension instances."""
    raw_as_of = str(report.get("meta", {}).get("as_of") or "")
    try:
        as_of = date.fromisoformat(raw_as_of)
    except ValueError as exc:
        raise ValueError("The report requires a valid meta.as_of date for remediation planning.") from exc

    grouped: MutableMapping[Tuple[str, str], Dict[str, Any]] = {}
    for account in sorted(report.get("accounts", []), key=lambda item: str(item.get("id"))):
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        governance = dict(account.get("governance") or {})
        for raw_gap in sorted(governance.get("gaps") or [], key=lambda item: str(item.get("dimension"))):
            gap = dict(raw_gap)
            dimension = str(gap.get("dimension") or "data_governance")
            reason = str(gap.get("reason_code") or "unknown")
            family, group_key, title = _group_identity(account, gap)
            priority = _priority(account, gap)
            routing = route_for_dimension(dimension, reason)
            evidence = {
                "account_id": account_id,
                "account_name": account.get("name"),
                "overall_tier": governance.get("overall_tier"),
                "dimension": dimension,
                "tier": gap.get("tier"),
                "reason_code": reason,
                "summary": gap.get("summary"),
                "recommended_action": gap.get("recommended_action"),
                "refs": gap.get("refs") or [],
                "details": gap.get("details") or {},
                "metric_impact": {field: account.get(field, 0) for field in METRIC_FIELDS},
                "report_as_of": as_of.isoformat(),
                "evidence_policy_version": governance.get("policy_version"),
                "remediation_policy_version": POLICY_VERSION,
                "grouping": {"family": family, "group_key": group_key, "title": title},
            }
            instance = {
                "fingerprint": instance_fingerprint(scope_id, portfolio_id, account_id, dimension),
                "account_id": account_id,
                "account_name": account.get("name"),
                "dimension": dimension,
                "current_tier": gap.get("tier"),
                "reason_code": reason,
                "summary": gap.get("summary"),
                "priority": priority,
                "minimum_target_tier": MINIMUM_GOVERNED_TIER,
                "due_on": add_business_days(as_of, DEFAULT_SLA_DAYS[priority]).isoformat(),
                "evidence": evidence,
                "evidence_hash": evidence_hash(evidence),
            }
            key = (family, group_key)
            group = grouped.setdefault(key, {
                "family": family,
                "group_key": group_key,
                "title": title,
                "routes": [],
                "instances": [],
            })
            group["routes"].append(routing)
            group["instances"].append(instance)

    workstreams: List[Dict[str, Any]] = []
    for (family, group_key), group in sorted(grouped.items()):
        instances = sorted(group["instances"], key=lambda item: (str(item["account_name"]), str(item["dimension"])))
        priorities = [str(item["priority"]) for item in instances]
        priority = min(priorities, key=lambda value: PRIORITY_RANK.get(value, 99))
        impact = _impact_for_instances(instances)
        account_count = len({str(item["account_id"]) for item in instances})
        dimensions = sorted({str(item["dimension"]) for item in instances})
        reasons = sorted({str(item["reason_code"]) for item in instances})
        representative = instances[0]
        paths = path_options(
            str(representative["dimension"]),
            str(representative["reason_code"]),
            (representative.get("evidence") or {}).get("details") or {},
        )
        validate_paths(paths)
        ranked, recommended_id, recommendation_reason = rank_paths(
            paths,
            affected_accounts=account_count,
            priority=priority,
            impact=impact,
        )
        # Coverage may combine dimensions, but all instances share the same
        # source-retrieval route. Other groups use their representative route.
        route = group["routes"][0]
        workstreams.append({
            "fingerprint": workstream_fingerprint(scope_id, portfolio_id, family, group_key),
            "scope_id": scope_id,
            "portfolio_id": portfolio_id,
            "policy_version": POLICY_VERSION,
            "family": family,
            "group_key": group_key,
            "title": group["title"],
            "dimensions": dimensions,
            "reason_codes": reasons,
            "priority": priority,
            "route": route["route"],
            "primary_owner": route["primary_owner"],
            "required_partners": route["required_partners"],
            "minimum_target_tier": MINIMUM_GOVERNED_TIER,
            "due_on": min(str(item["due_on"]) for item in instances),
            "affected_account_count": account_count,
            "impact": impact,
            "paths": ranked,
            "recommended_path_id": recommended_id,
            "recommendation_reason": recommendation_reason,
            "instances": instances,
        })
    return sorted(workstreams, key=lambda item: (PRIORITY_RANK.get(str(item["priority"]), 99), str(item["due_on"]), str(item["title"])))


def summarize_workstreams(workstreams: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    rows = list(workstreams)
    instances = [item for workstream in rows for item in workstream.get("instances", [])]
    return {
        "workstream_count": len(rows),
        "instance_count": len(instances),
        "p0_workstream_count": sum(1 for item in rows if item.get("priority") == "P0"),
        "p1_workstream_count": sum(1 for item in rows if item.get("priority") == "P1"),
        "p2_workstream_count": sum(1 for item in rows if item.get("priority") == "P2"),
    }


def _slack_safe(value: Any, maximum: int = 500) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return text.replace("<", "‹").replace(">", "›")[:maximum]


def selected_path(workstream: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = workstream.get("selected_path")
    if isinstance(snapshot, Mapping) and snapshot.get("id"):
        return dict(snapshot)
    selected_id = str(workstream.get("selected_path_id") or workstream.get("recommended_path_id") or "")
    paths = [dict(item) for item in workstream.get("paths", [])]
    return next((item for item in paths if str(item.get("id")) == selected_id), paths[0] if paths else {})


def format_slack_followup(workstream: Mapping[str, Any], recipient: str) -> str:
    """Create a concise reviewed Slack handoff for copying or Slack MCP queueing."""
    recipient = _slack_safe(recipient, 200)
    if not recipient:
        raise ValueError("A Slack recipient or team label is required.")
    execution_plan = workstream.get("execution_plan")
    if isinstance(execution_plan, Mapping):
        draft = execution_plan.get("slack_draft")
        template = draft.get("message") if isinstance(draft, Mapping) else None
        if isinstance(template, str) and "{{recipient}}" in template:
            return template.replace("{{recipient}}", recipient)
    path = selected_path(workstream)
    if not path:
        raise ValueError("The workstream does not have a selected remediation path.")
    instances = list(workstream.get("instances", []))
    account_names = sorted({_slack_safe(item.get("account_name") or item.get("account_id"), 120) for item in instances})
    shown = account_names[:8]
    affected = ", ".join(shown) + (f" (+{len(account_names) - len(shown)} more)" if len(account_names) > len(shown) else "")
    steps = [
        _slack_safe(step, 500) for step in path.get("steps", [])
        if _slack_safe(step, 500)
    ]
    step_text = "\n".join(f"{index}. {step}" for index, step in enumerate(steps[:4], 1))
    due_line = f"\nDue: {_slack_safe(workstream.get('due_on'), 40)}" if workstream.get("due_on") else ""
    return (
        f"Hi {recipient} — could you help with {_slack_safe(path.get('title') or workstream.get('title'), 300)} "
        f"for {affected or 'the affected account'}?\n\n"
        f"What needs attention\n- {_slack_safe(workstream.get('recommendation_reason'), 600)}\n\n"
        f"What to do\n{step_text or '1. Review and correct the source evidence.'}"
        f"{due_line}\n\n"
        "Reply here when it’s done so I can refresh Hours Recon and verify the change.\n\n"
        "— sent via Glean Pi"
    )
