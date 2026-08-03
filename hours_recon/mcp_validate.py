"""Deterministic validation of an MCP snapshot and the report derived from it.

The refresh skill used to ask the agent to re-derive a dozen assertions by
reading the snapshot: sum every line item, sum every time entry, confirm each
opportunity used one line-item source, confirm governed plus provisional equals
reported. That is slow, consumes context, and is exactly the kind of arithmetic
a language model should not be trusted to repeat identically every run.

These checks are pure functions over the snapshot and report. The agent calls
them once and reads a short pass/fail list.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .dates import business_today, parse_date
from .inference import infer_packages
from .matching import match_projects

TOLERANCE = Decimal("0.02")

# Rocketlane's approvalStatus enum, plus the variant hours_recon.evidence treats
# as equivalent to approved.
KNOWN_APPROVAL_STATUSES = frozenset({
    "NOT_SUBMITTED", "SUBMITTED", "APPROVED", "REJECTED", "APPROVED_WITH_CHANGES",
})


def _finding(check: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"check": check, "ok": bool(ok), "detail": detail}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001 - any unparsable metric is a failure, not a crash
        return Decimal("0")


def _hours(entry: Mapping[str, Any]) -> Decimal:
    if entry.get("hours") is not None:
        return _decimal(entry["hours"])
    return _decimal(entry.get("minutes")) / Decimal("60")


def _pagination_terminal(entry: Mapping[str, Any]) -> bool:
    if entry.get("has_more") is True:
        return bool(entry.get("followed_next_page"))
    if entry.get("done") is False:
        return bool(entry.get("followed_next_page"))
    return True


def validate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    expected_requester_email: str = "",
    expected_scope_id: str = "",
    timezone_name: str = "America/Denver",
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Structural and source-level checks that need no reconciliation run."""
    findings: List[Dict[str, Any]] = []
    report_date = today or business_today(timezone_name)

    findings.append(_finding(
        "schema_version",
        snapshot.get("schema_version") == 1,
        f"found {snapshot.get('schema_version')!r}",
    ))

    salesforce = snapshot.get("salesforce") if isinstance(snapshot.get("salesforce"), Mapping) else {}
    rocketlane = snapshot.get("rocketlane") if isinstance(snapshot.get("rocketlane"), Mapping) else {}
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}

    accounts = list(salesforce.get("accounts") or [])
    opportunities = list(salesforce.get("opportunities") or [])
    projects = list(rocketlane.get("projects") or [])
    entries = list(rocketlane.get("entries") or [])

    actual_email = str((salesforce.get("requester") or {}).get("email") or "").strip().lower()
    if expected_requester_email:
        findings.append(_finding(
            "requester_matches_configuration",
            actual_email == expected_requester_email.strip().lower(),
            f"snapshot={actual_email or '(none)'} expected={expected_requester_email.strip().lower()}",
        ))

    through_date = str(meta.get("through_date") or "")
    findings.append(_finding(
        "through_date_is_report_date",
        through_date == report_date.isoformat(),
        f"through_date={through_date or '(none)'} report_date={report_date.isoformat()}",
    ))

    scope_id = str(meta.get("scope_id") or "")
    findings.append(_finding(
        "scope_verified",
        meta.get("scope_verified") is True and bool(scope_id),
        f"scope_verified={meta.get('scope_verified')!r} scope_id={scope_id or '(none)'}",
    ))
    if expected_scope_id:
        findings.append(_finding(
            "scope_matches_configuration",
            scope_id == expected_scope_id,
            f"snapshot={scope_id or '(none)'} expected={expected_scope_id}",
        ))

    # Every in-scope account must have its own audit row, including accounts
    # with zero Closed Won opportunities.
    audit_ids = {str(row.get("account_id")) for row in meta.get("account_retrieval_audit") or []}
    account_ids = {str(item.get("id")) for item in accounts}
    missing_audit = sorted(account_ids - audit_ids)
    findings.append(_finding(
        "every_account_has_retrieval_audit",
        not missing_audit and bool(account_ids),
        f"missing={missing_audit}" if missing_audit else f"{len(account_ids)} accounts audited",
    ))

    orphan_opportunities = sorted(
        str(item.get("id")) for item in opportunities if str(item.get("account_id")) not in account_ids
    )
    findings.append(_finding(
        "opportunities_are_in_scope",
        not orphan_opportunities,
        f"out_of_scope={orphan_opportunities[:5]}" if orphan_opportunities else "all opportunities map to in-scope accounts",
    ))

    # One line-item source per opportunity, no duplicated line IDs anywhere.
    mixed_sources: List[str] = []
    seen_line_ids: Dict[str, str] = {}
    duplicate_lines: List[str] = []
    for opportunity in opportunities:
        sources = {str(line.get("source")) for line in opportunity.get("line_items") or []}
        if len(sources) > 1:
            mixed_sources.append(f"{opportunity.get('id')}:{sorted(sources)}")
        for line in opportunity.get("line_items") or []:
            line_id = str(line.get("id"))
            if line_id in seen_line_ids:
                duplicate_lines.append(f"{line_id} on {seen_line_ids[line_id]} and {opportunity.get('id')}")
            else:
                seen_line_ids[line_id] = str(opportunity.get("id"))
    findings.append(_finding(
        "one_line_item_source_per_opportunity",
        not mixed_sources,
        f"mixed={mixed_sources[:5]}" if mixed_sources else "no opportunity mixes line-item sources",
    ))
    findings.append(_finding(
        "no_duplicate_line_items",
        not duplicate_lines,
        f"duplicates={duplicate_lines[:5]}" if duplicate_lines else f"{len(seen_line_ids)} unique line items",
    ))

    entry_ids = [str(item.get("id")) for item in entries]
    findings.append(_finding(
        "no_duplicate_time_entries",
        len(entry_ids) == len(set(entry_ids)),
        f"{len(entry_ids)} entries, {len(set(entry_ids))} unique",
    ))

    # Approval state is absent from Rocketlane's time-entry search payload, so a
    # refresh that skips the approvalStatus filter partition looks successful
    # while reporting every entry as UNKNOWN. Fail loudly instead.
    missing_approval = sorted(
        str(item.get("id"))
        for item in entries
        if not str(item.get("approval_status") or "").strip()
    )
    findings.append(_finding(
        "approval_status_present_for_every_entry",
        not missing_approval,
        f"{len(missing_approval)} entries missing approval status, e.g. {missing_approval[:5]}"
        if missing_approval
        else f"all {len(entries)} entries carry an approval status",
    ))

    unknown_approval = sorted({
        str(item.get("approval_status")).strip().upper()
        for item in entries
        if str(item.get("approval_status") or "").strip()
        and str(item.get("approval_status")).strip().upper() not in KNOWN_APPROVAL_STATUSES
    })
    findings.append(_finding(
        "approval_status_values_are_known",
        not unknown_approval,
        f"unrecognized={unknown_approval}" if unknown_approval else "all approval statuses are recognized",
    ))

    project_ids = {str(item.get("id")) for item in projects}
    stray_entries = sorted({str(item.get("project_id")) for item in entries} - project_ids)
    findings.append(_finding(
        "entries_belong_to_retrieved_projects",
        not stray_entries,
        f"unknown_projects={stray_entries[:5]}" if stray_entries else "all entries map to retrieved projects",
    ))

    counts = meta.get("source_counts") or {}
    expected_counts = {
        "accounts": len(accounts),
        "opportunities": len(opportunities),
        "line_items": sum(len(item.get("line_items") or []) for item in opportunities),
        "projects": len(projects),
        "time_entries": len(entries),
    }
    mismatched = {
        key: (counts.get(key), value) for key, value in expected_counts.items() if counts.get(key) != value
    }
    findings.append(_finding(
        "source_counts_match_payload",
        not mismatched,
        f"mismatched={mismatched}" if mismatched else str(expected_counts),
    ))

    non_terminal = [
        entry
        for group in ("salesforce_pagination_audit", "project_search_audit", "time_pagination_audit")
        for entry in meta.get(group) or []
        if not _pagination_terminal(entry)
    ]
    findings.append(_finding(
        "no_pagination_page_skipped",
        not non_terminal,
        f"non_terminal={non_terminal[:3]}" if non_terminal else "all pagination reached a terminal page",
    ))

    unaudited = sorted(project_ids - {str(row.get("project_id")) for row in meta.get("time_pagination_audit") or []})
    findings.append(_finding(
        "time_entries_audited_for_every_project",
        not unaudited,
        f"unaudited={unaudited}" if unaudited else f"{len(project_ids)} projects audited",
    ))

    coverage = meta.get("coverage") or {}
    required = ("accounts", "opportunities", "projects", "time_entries", "pagination_complete", "complete")
    failed_coverage = [key for key in required if coverage.get(key) is not True]
    findings.append(_finding(
        "coverage_complete",
        not failed_coverage,
        f"false={failed_coverage}" if failed_coverage else "all coverage flags true",
    ))

    return findings


def validate_report(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    package_config: Mapping[str, Any],
    account_aliases: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Cross-check the derived report against the snapshot it came from."""
    findings: List[Dict[str, Any]] = []
    salesforce = snapshot.get("salesforce") or {}
    rocketlane = snapshot.get("rocketlane") or {}
    accounts = list(salesforce.get("accounts") or [])
    opportunities = list(salesforce.get("opportunities") or [])
    projects = list(rocketlane.get("projects") or [])
    entries = list(rocketlane.get("entries") or [])
    metrics = report.get("metrics") or {}

    as_of_text = str((report.get("meta") or {}).get("as_of") or "")
    findings.append(_finding(
        "report_as_of_matches_snapshot",
        as_of_text == str((snapshot.get("meta") or {}).get("through_date") or ""),
        f"report={as_of_text} snapshot={(snapshot.get('meta') or {}).get('through_date')}",
    ))

    findings.append(_finding(
        "account_count_matches",
        metrics.get("account_count") == len(accounts),
        f"report={metrics.get('account_count')} snapshot={len(accounts)}",
    ))

    account_ids = {str(item.get("id")) for item in accounts}
    expected_sold = Decimal("0")
    for opportunity in opportunities:
        if str(opportunity.get("account_id")) not in account_ids:
            continue
        packages, _ = infer_packages(opportunity, package_config)
        for package in packages:
            expected_sold += _decimal(package.get("sold_hours"))
    reported_sold = _decimal(metrics.get("sold_hours"))
    findings.append(_finding(
        "sold_hours_equal_inferred_packages",
        abs(reported_sold - expected_sold) <= TOLERANCE,
        f"report={reported_sold} inferred={expected_sold}",
    ))

    # Billed hours only counts billable, dated, in-window entries on projects
    # that matched an in-scope account, which is exactly what reconcile sums.
    try:
        as_of = date.fromisoformat(as_of_text)
    except ValueError:
        as_of = None
    project_map, _ = match_projects(accounts, projects, account_aliases)
    expected_billed = Decimal("0")
    counted = 0
    for entry in entries:
        if not entry.get("billable", True):
            continue
        raw_date = entry.get("date")
        if not raw_date:
            continue
        if as_of is not None and parse_date(str(raw_date)) > as_of:
            continue
        if project_map.get(str(entry.get("project_id"))) not in account_ids:
            continue
        expected_billed += _hours(entry)
        counted += 1
    reported_billed = _decimal(metrics.get("billed_hours"))
    findings.append(_finding(
        "billed_hours_equal_source_minutes",
        abs(reported_billed - expected_billed) <= TOLERANCE,
        f"report={reported_billed} source={expected_billed} over {counted} entries",
    ))

    exceptions = list(report.get("exceptions") or [])
    unmatched_types = {
        "unmatched_project", "account_collision", "customer_id_collision",
        "explicit_account_out_of_scope", "invalid_project_id", "project_id_collision",
    }
    unmatched_count = sum(1 for item in exceptions if item.get("type") in unmatched_types)
    matched_count = sum(1 for item in projects if project_map.get(str(item.get("id"))) in account_ids)
    findings.append(_finding(
        "every_project_matched_or_surfaced",
        matched_count + unmatched_count >= len(projects),
        f"projects={len(projects)} matched={matched_count} surfaced={unmatched_count}",
    ))

    pre_entitlement_accounts = [
        str(item.get("id")) for item in report.get("accounts") or []
        if float(item.get("pre_entitlement_hours") or 0) > 0
    ]
    surfaced_pre_entitlement = {
        str(item.get("account_id")) for item in exceptions if item.get("type") == "pre_entitlement_activity"
    }
    missing_pre_entitlement = sorted(set(pre_entitlement_accounts) - surfaced_pre_entitlement)
    findings.append(_finding(
        "pre_entitlement_activity_surfaced",
        not missing_pre_entitlement,
        f"missing={missing_pre_entitlement}" if missing_pre_entitlement else
        f"{len(pre_entitlement_accounts)} accounts with pre-entitlement activity surfaced",
    ))

    governance = report.get("governance") or {}
    drift: List[str] = []
    for name, values in (governance.get("metrics") or {}).items():
        governed = _decimal(values.get("governed"))
        provisional = _decimal(values.get("provisional"))
        reported = _decimal(values.get("reported"))
        if abs(governed + provisional - reported) > TOLERANCE:
            drift.append(f"{name}: {governed}+{provisional}!={reported}")
        portfolio = metrics.get(name)
        if portfolio is not None and abs(_decimal(portfolio) - reported) > TOLERANCE:
            drift.append(f"{name}: governance reported {reported} != portfolio {portfolio}")
    findings.append(_finding(
        "governed_plus_provisional_equals_reported",
        not drift,
        f"drift={drift[:5]}" if drift else "governance decomposition is exact",
    ))

    return findings


def validate_refresh(
    snapshot: Mapping[str, Any],
    report: Optional[Mapping[str, Any]] = None,
    *,
    package_config: Optional[Mapping[str, Any]] = None,
    account_aliases: Optional[Mapping[str, Any]] = None,
    expected_requester_email: str = "",
    expected_scope_id: str = "",
    timezone_name: str = "America/Denver",
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Run every available check and return a compact machine-readable result."""
    findings = validate_snapshot(
        snapshot,
        expected_requester_email=expected_requester_email,
        expected_scope_id=expected_scope_id,
        timezone_name=timezone_name,
        today=today,
    )
    if report is not None:
        findings.extend(validate_report(
            snapshot,
            report,
            package_config=package_config or {},
            account_aliases=account_aliases or {},
        ))
    failures = [item for item in findings if not item["ok"]]
    return {
        "ok": not failures,
        "checks_run": len(findings),
        "failed": len(failures),
        "failures": failures,
        "findings": findings,
    }


def format_findings(result: Mapping[str, Any]) -> str:
    """Render a validation result as a short text block for the agent to read."""
    lines = []
    for item in result.get("findings", []):
        marker = "PASS" if item["ok"] else "FAIL"
        lines.append(f"[{marker}] {item['check']}: {item['detail']}")
    summary = "OK" if result.get("ok") else f"{result.get('failed')} FAILED"
    lines.append(f"-- {summary} ({result.get('checks_run')} checks) --")
    return "\n".join(lines)
