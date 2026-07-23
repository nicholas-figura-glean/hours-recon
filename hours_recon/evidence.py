"""Governance evidence scoring for reconciled Hours Recon accounts.

The evidence layer is intentionally additive during the observe-only rollout:
legacy reconciliation totals remain unchanged while each account receives a
five-dimension confidence vector and governed/provisional shadow metrics.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

POLICY_VERSION = "evidence-v1"
GOVERNED_TIERS = {"T1", "T2"}
TIER_RANK = {"T1": 1, "T2": 2, "T3": 3, "T4": 4}
DIMENSION_ORDER = (
    "entitlement_source",
    "hours_mapping",
    "service_period",
    "project_linkage",
    "time_quality",
)
METRIC_FIELDS = (
    "sold_hours",
    "billed_hours",
    "remaining_hours",
    "at_risk_hours",
    "expired_unused_hours",
    "future_entitlement_hours",
    "overage_hours",
)


def _dimension(
    tier: str,
    reason_code: str,
    summary: str,
    recommended_action: str,
    *,
    refs: Optional[Iterable[str]] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if tier not in TIER_RANK:
        raise ValueError(f"Unknown evidence tier: {tier}")
    return {
        "tier": tier,
        "rank": TIER_RANK[tier],
        "reason_code": reason_code,
        "summary": summary,
        "recommended_action": recommended_action,
        "refs": sorted({str(value) for value in (refs or []) if value not in (None, "")}),
        "details": dict(details or {}),
    }


def _worst(dimensions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not dimensions:
        raise ValueError("At least one evidence dimension is required.")
    return max(dimensions, key=lambda item: (int(item["rank"]), str(item.get("reason_code", ""))))


def _source_dimension(account: Mapping[str, Any]) -> Dict[str, Any]:
    packages = list(account.get("packages", []))
    unresolved = list(account.get("package_exceptions", []))
    if unresolved:
        return _dimension(
            "T4", "unresolved_entitlement_evidence",
            "At least one in-scope Opportunity or line item has unresolved entitlement evidence.",
            "Correct every unresolved package line or record an explicit no-entitlement disposition before governing the account.",
            refs=[item.get("line_item_id") or item.get("opportunity_id") for item in unresolved],
            details={"unresolved_count": len(unresolved)},
        )
    disposition = str(account.get("entitlement_disposition") or "").strip().lower()
    if not packages:
        if disposition in {"none", "not_expected", "not_applicable", "ended"}:
            return _dimension(
                "T1",
                "explicit_no_entitlement",
                "Salesforce explicitly records that no active hours entitlement is expected.",
                "Keep the disposition and effective dates current.",
                refs=[account.get("id")],
            )
        return _dimension(
            "T4",
            "no_recognized_entitlement",
            "No recognized sold-hours package or governed no-entitlement disposition was found.",
            "Populate a canonical hours product or an explicit no-entitlement disposition in Salesforce.",
            refs=[account.get("id")],
        )

    classifications: List[Dict[str, Any]] = []
    for package in packages:
        source = str(package.get("line_item_source") or "opportunity_fallback")
        refs = [package.get("opportunity_id"), package.get("line_item_id"), package.get("quote_id")]
        if source == "opportunity_line_item":
            classifications.append(_dimension(
                "T1", "opportunity_product", "Entitlement comes from a Closed Won Opportunity Product.",
                "Keep the Opportunity Product synchronized with the accepted commercial agreement.", refs=refs,
            ))
        elif source in {"approved_quote", "synced_quote"}:
            classifications.append(_dimension(
                "T2", f"{source}_fallback", "Entitlement comes from an approved or synced Quote Line because Opportunity Products were absent.",
                "Sync the accepted Quote Line to the Opportunity when the sales process permits.", refs=refs,
            ))
        elif source == "primary_quote":
            classifications.append(_dimension(
                "T3", "primary_quote_fallback", "Entitlement comes from a Primary Quote that is not recorded as approved or synced.",
                "Populate the Approved/Synced Quote reference or create the corresponding Opportunity Product.", refs=refs,
            ))
        else:
            classifications.append(_dimension(
                "T4", "opportunity_name_fallback", "Entitlement was inferred without a product or governed Quote Line source.",
                "Add a canonical hours product to the Closed Won Opportunity.", refs=refs,
            ))
    worst = dict(_worst(classifications))
    worst["details"] = {"package_sources": sorted({str(item.get("line_item_source") or "opportunity_fallback") for item in packages})}
    return worst


def _mapping_dimension(account: Mapping[str, Any]) -> Dict[str, Any]:
    packages = list(account.get("packages", []))
    unresolved = list(account.get("package_exceptions", []))
    if unresolved:
        return _dimension(
            "T4", "unresolved_hours_mapping",
            "At least one in-scope package cannot be mapped to sold hours.",
            "Add canonical ProductCode mappings or explicit contracted hours for every unresolved line.",
            refs=[item.get("line_item_id") or item.get("opportunity_id") for item in unresolved],
            details={"unresolved_count": len(unresolved)},
        )
    if not packages:
        disposition = str(account.get("entitlement_disposition") or "").strip().lower()
        if disposition in {"none", "not_expected", "not_applicable", "ended"}:
            return _dimension("T1", "mapping_not_applicable", "No hours mapping is required for the governed no-entitlement disposition.", "No action required.")
        return _dimension(
            "T4", "no_hours_mapping", "No package resolved to a sold-hours mapping.",
            "Add a canonical ProductCode-to-hours mapping or explicit contracted hours.", refs=[account.get("id")],
        )

    classifications: List[Dict[str, Any]] = []
    for package in packages:
        source = str(package.get("inference_source") or "")
        refs = [package.get("line_item_id"), package.get("product_code"), package.get("mapping_key")]
        if source == "product_code":
            classifications.append(_dimension(
                "T1", "canonical_product_code", "Sold hours use an exact, governed ProductCode mapping.",
                "Keep the versioned ProductCode catalog aligned with the Salesforce product catalog.", refs=refs,
            ))
        elif source in {"explicit_hours", "growth_tier"}:
            classifications.append(_dimension(
                "T2", source, "Sold hours are explicit in the product evidence or use a constrained numeric package tier.",
                "Prefer a canonical ProductCode or explicit contracted-hours field when available.", refs=refs,
            ))
        elif source in {"tier_name", "list_price", "line_item_override"}:
            classifications.append(_dimension(
                "T3", source, "Sold hours depend on tier-name, price, or local override inference.",
                "Replace the inference with a canonical ProductCode mapping or reviewed explicit-hours field.", refs=refs,
            ))
        else:
            classifications.append(_dimension(
                "T4", source or "unknown_mapping", "Sold hours depend on opportunity-name or manual opportunity-level inference.",
                "Correct the Salesforce product evidence and remove the opportunity-level fallback.", refs=refs,
            ))
    worst = dict(_worst(classifications))
    worst["details"] = {"mapping_sources": sorted({str(item.get("inference_source") or "unknown") for item in packages})}
    return worst


def _service_period_dimension(account: Mapping[str, Any]) -> Dict[str, Any]:
    packages = list(account.get("packages", []))
    if not packages:
        disposition = str(account.get("entitlement_disposition") or "").strip().lower()
        if disposition in {"none", "not_expected", "not_applicable", "ended"}:
            return _dimension("T1", "service_period_not_applicable", "No service period is required for the governed no-entitlement disposition.", "No action required.")
        return _dimension(
            "T4", "missing_service_period", "No governed entitlement service period is available.",
            "Populate explicit entitlement start and end dates in Salesforce.", refs=[account.get("id")],
        )

    classifications: List[Dict[str, Any]] = []
    for package in packages:
        source = str(package.get("service_period_source") or "close_date_plus_one_year")
        refs = [package.get("opportunity_id"), package.get("line_item_id")]
        if source in {"line_item_explicit", "opportunity_explicit"}:
            classifications.append(_dimension(
                "T1", source, "The entitlement has explicit Salesforce service start and end dates.",
                "Keep the contractual service dates current.", refs=refs,
            ))
        elif source == "partial_explicit":
            classifications.append(_dimension(
                "T2", source, "One service-period boundary is explicit and the other uses the governed one-year rule.",
                "Populate both contractual service-period boundaries.", refs=refs,
            ))
        elif source == "close_date_plus_one_year":
            classifications.append(_dimension(
                "T3", source, "Service validity assumes Opportunity CloseDate through CloseDate plus one year.",
                "Populate explicit contractual service start and end dates in Salesforce.", refs=refs,
            ))
        else:
            classifications.append(_dimension(
                "T4", source or "invalid_service_period", "The entitlement service period is missing or invalid.",
                "Correct the Salesforce service-period fields before using the entitlement operationally.", refs=refs,
            ))
    worst = dict(_worst(classifications))
    worst["details"] = {"period_sources": sorted({str(item.get("service_period_source") or "close_date_plus_one_year") for item in packages})}
    return worst


def _project_linkage_dimension(
    account: Mapping[str, Any],
    project_evidence: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    projects = list(account.get("projects", []))
    sold = float(account.get("sold_hours", 0) or 0)
    disposition = str(account.get("entitlement_disposition") or "").strip().lower()
    if not projects and sold <= 0 and disposition in {"none", "not_expected", "not_applicable", "ended"}:
        return _dimension(
            "T1", "project_linkage_not_applicable",
            "No Rocketlane project is required for the governed no-entitlement disposition.",
            "Keep the disposition and effective dates current.", refs=[account.get("id")],
        )
    if not projects:
        return _dimension(
            "T4" if sold > 0 else "T3",
            "no_rocketlane_project" if sold > 0 else "no_project_or_entitlement",
            "No Rocketlane project is linked to this account." if sold > 0 else "Neither an entitlement nor a Rocketlane project is currently linked.",
            "Create or identify the service project and store a stable Salesforce Account/Opportunity reference.",
            refs=[account.get("id")],
        )

    classifications: List[Dict[str, Any]] = []
    for project in projects:
        evidence = dict(project_evidence.get(str(project.get("id")), {}))
        basis = str(evidence.get("basis") or "legacy_name_match")
        refs = [account.get("id"), project.get("id"), project.get("customer_id")]
        if basis == "salesforce_account_id":
            classifications.append(_dimension(
                "T1", basis, "Rocketlane stores the matching Salesforce Account ID.",
                "Keep the cross-system identifier synchronized.", refs=refs,
            ))
        elif basis == "rocketlane_customer_id_crosswalk":
            classifications.append(_dimension(
                "T2", basis, "A governed Rocketlane customer-ID crosswalk links the project to Salesforce.",
                "Maintain the one-to-one crosswalk and its review history.", refs=refs,
            ))
        elif basis in {"normalized_customer_name", "configured_alias"}:
            classifications.append(_dimension(
                "T3", basis, "Rocketlane is linked through a normalized customer name or configured alias.",
                "Store the Salesforce Account ID on the Rocketlane customer/project or establish a governed customer-ID crosswalk.", refs=refs,
            ))
        else:
            classifications.append(_dimension(
                "T4", basis, "Rocketlane linkage depends on a project-name fallback or lacks auditable provenance.",
                "Add a stable cross-system account identifier.", refs=refs,
            ))
    worst = dict(_worst(classifications))
    worst["details"] = {"match_bases": sorted({str(project_evidence.get(str(item.get("id")), {}).get("basis") or "legacy_name_match") for item in projects})}
    return worst


def _time_quality_dimension(account: Mapping[str, Any]) -> Dict[str, Any]:
    entries = list(account.get("entries", []))
    projects = list(account.get("projects", []))
    project_by_id = {str(item.get("id")): item for item in projects}
    sold = float(account.get("sold_hours", 0) or 0)
    disposition = str(account.get("entitlement_disposition") or "").strip().lower()
    if not entries and sold <= 0 and disposition in {"none", "not_expected", "not_applicable", "ended"}:
        return _dimension(
            "T1", "time_quality_not_applicable",
            "No Rocketlane time is required for the governed no-entitlement disposition.",
            "Keep the disposition and effective dates current.", refs=[account.get("id")],
        )
    if not entries:
        completed_without_time = any(str(item.get("status") or "").lower() == "completed" for item in projects)
        if completed_without_time:
            return _dimension(
                "T3", "completed_project_without_billable_time", "A completed Rocketlane project has no billable time in the retrieved dataset.",
                "Confirm that time is complete and recorded on the intended service project.", refs=[item.get("id") for item in projects],
            )
        if sold > 0 and not projects:
            return _dimension(
                "T4", "usage_unobservable_without_project", "Usage cannot be verified because no Rocketlane project is linked.",
                "Link the service project and rerun the account pull.", refs=[account.get("id")],
            )
        return _dimension(
            "T2", "no_billable_entries_observed", "No billable entries were observed on the linked project set.",
            "Continue monitoring; verify extraction coverage before treating zero usage as authoritative.", refs=[item.get("id") for item in projects],
        )

    invalid = []
    approval_unknown = 0
    approval_pending = 0
    approval_rejected = 0
    missing_activity = 0
    missing_category = 0
    missing_user = 0
    outside_dates = 0
    stale_projects = set()
    for entry in entries:
        if not entry.get("id") or not entry.get("project_id") or not entry.get("date") or entry.get("billable") is not True:
            invalid.append(str(entry.get("id") or "missing-id"))
        approval_status = str(entry.get("approval_status") or "").strip().upper()
        if not approval_status:
            approval_unknown += 1
        elif approval_status not in {"APPROVED", "APPROVED_WITH_CHANGES"}:
            if approval_status in {"REJECTED", "DENIED", "DECLINED"}:
                approval_rejected += 1
            else:
                approval_pending += 1
        if not entry.get("activity_name"):
            missing_activity += 1
        if not entry.get("category"):
            missing_category += 1
        if not entry.get("user_id") and not entry.get("user_email"):
            missing_user += 1
        project = project_by_id.get(str(entry.get("project_id")))
        if project:
            start = project.get("start_date")
            due = project.get("due_date")
            entry_date = entry.get("date")
            if entry_date and ((start and entry_date < start) or (due and entry_date > due)):
                outside_dates += 1
            status = str(project.get("status") or "").lower()
            if status in {"proposed", "in planning", "planning"} or not start or not due:
                stale_projects.add(str(project.get("id")))

    refs = [entry.get("id") for entry in entries]
    details = {
        "entry_count": len(entries),
        "invalid_entries": len(invalid),
        "approval_unknown": approval_unknown,
        "approval_pending": approval_pending,
        "approval_rejected": approval_rejected,
        "missing_activity": missing_activity,
        "missing_category": missing_category,
        "missing_user": missing_user,
        "outside_project_dates": outside_dates,
        "stale_or_incomplete_projects": len(stale_projects),
    }
    if invalid or approval_rejected:
        return _dimension(
            "T4", "invalid_time_evidence", "One or more time entries are structurally invalid or explicitly rejected.",
            "Correct or exclude invalid/rejected entries and rerun the account pull.", refs=invalid or refs, details=details,
        )
    if approval_unknown or approval_pending or missing_activity or missing_category or missing_user or outside_dates or stale_projects:
        approval_not_required = all(bool(item.get("approval_not_required")) for item in projects) if projects else False
        only_unknown_approval = approval_unknown and not (approval_pending or missing_activity or missing_category or missing_user or outside_dates or stale_projects)
        tier = "T2" if only_unknown_approval and approval_not_required else "T3"
        reason = "approval_not_required" if tier == "T2" else "incomplete_time_or_project_metadata"
        return _dimension(
            tier,
            reason,
            "Billable time is structurally valid but approval, activity, category, contributor, lifecycle, or timing evidence is incomplete.",
            "Correct Rocketlane project lifecycle/dates and required time-entry metadata; document approval semantics.",
            refs=refs,
            details=details,
        )
    return _dimension(
        "T1", "complete_approved_time", "Billable time has complete identifiers, metadata, project dates, and approval evidence.",
        "Continue enforcing the Rocketlane data-entry policy.", refs=refs, details=details,
    )


def _apply_coverage_caps(
    dimensions: Dict[str, Dict[str, Any]],
    coverage: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    if coverage is None:
        return dimensions
    required = {
        "entitlement_source": ("accounts", "opportunities"),
        "hours_mapping": ("accounts", "opportunities"),
        "service_period": ("accounts", "opportunities"),
        "project_linkage": ("projects",),
        "time_quality": ("time_entries", "pagination_complete"),
    }
    complete = coverage.get("complete") is True and all(
        coverage.get(key) is True
        for keys in required.values()
        for key in keys
    )
    if complete:
        return dimensions
    result = dict(dimensions)
    for name, keys in required.items():
        missing = [key for key in keys if coverage.get(key) is not True]
        if coverage.get("complete") is not True:
            missing = ["complete", *missing]
        if not missing:
            continue
        current = dimensions[name]
        cap_tier = "T4" if any(coverage.get(key) is False for key in missing) else "T3"
        if TIER_RANK[current["tier"]] >= TIER_RANK[cap_tier]:
            continue
        result[name] = _dimension(
            cap_tier,
            "incomplete_source_coverage",
            f"Source retrieval coverage is incomplete or unverified for: {', '.join(missing)}.",
            "Run a new account-isolated retrieval with all coverage flags explicitly true.",
            refs=current.get("refs", []),
            details={"missing_coverage": missing, "underlying_tier": current.get("tier"), "coverage": dict(coverage)},
        )
    return result


def _package_governance(package: Mapping[str, Any]) -> Dict[str, Any]:
    source_account = {"packages": [package]}
    dimensions = {
        "entitlement_source": _source_dimension(source_account),
        "hours_mapping": _mapping_dimension(source_account),
        "service_period": _service_period_dimension(source_account),
    }
    worst_rank = max(int(item["rank"]) for item in dimensions.values())
    overall = f"T{worst_rank}"
    return {
        "overall_tier": overall,
        "status": "governed" if overall in GOVERNED_TIERS else "provisional",
        "limiting_dimensions": [name for name, item in dimensions.items() if int(item["rank"]) == worst_rank],
        "dimensions": dimensions,
        "evidence_chain": [
            {"step": "Opportunity", "id": package.get("opportunity_id"), "label": package.get("opportunity_name")},
            {"step": "Line source", "id": package.get("line_item_id") or package.get("quote_id"), "label": package.get("line_item_source") or "opportunity fallback"},
            {"step": "Hours mapping", "id": package.get("mapping_key") or package.get("product_code"), "label": package.get("inference_source")},
            {"step": "Service period", "id": None, "label": package.get("service_period_source") or "close_date_plus_one_year"},
            {"step": "Result", "id": None, "label": f"{package.get('sold_hours', 0)} sold hours"},
        ],
    }


def attach_governance(
    report: Dict[str, Any],
    *,
    project_match_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    mode: str = "observe_only",
    source_coverage: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach evidence tiers and governed/provisional shadow metrics in place."""
    project_evidence = project_match_evidence or {}
    portfolio_governed = {field: 0.0 for field in METRIC_FIELDS}
    portfolio_provisional = {field: 0.0 for field in METRIC_FIELDS}
    tier_counts = {tier: 0 for tier in TIER_RANK}
    remediation_accounts = 0

    for account in report.get("accounts", []):
        for package in account.get("packages", []):
            package["governance"] = _package_governance(package)

        dimensions = _apply_coverage_caps({
            "entitlement_source": _source_dimension(account),
            "hours_mapping": _mapping_dimension(account),
            "service_period": _service_period_dimension(account),
            "project_linkage": _project_linkage_dimension(account, project_evidence),
            "time_quality": _time_quality_dimension(account),
        }, source_coverage)
        worst_rank = max(int(item["rank"]) for item in dimensions.values())
        overall = f"T{worst_rank}"
        governed = overall in GOVERNED_TIERS
        gaps = []
        for dimension_name in DIMENSION_ORDER:
            item = dimensions[dimension_name]
            if int(item["rank"]) >= 3:
                gaps.append({
                    "dimension": dimension_name,
                    "tier": item["tier"],
                    "reason_code": item["reason_code"],
                    "summary": item["summary"],
                    "recommended_action": item["recommended_action"],
                    "refs": item["refs"],
                    "details": item["details"],
                })
        metric_partition = {}
        for field in METRIC_FIELDS:
            reported = round(float(account.get(field, 0) or 0), 2)
            governed_value = reported if governed else 0.0
            provisional_value = round(reported - governed_value, 2)
            metric_partition[field] = {
                "reported": reported,
                "governed": governed_value,
                "provisional": provisional_value,
            }
            portfolio_governed[field] += governed_value
            portfolio_provisional[field] += provisional_value

        account["governance"] = {
            "policy_version": POLICY_VERSION,
            "mode": mode,
            "overall_tier": overall,
            "status": "governed" if governed else "provisional",
            "limiting_dimensions": [name for name, item in dimensions.items() if int(item["rank"]) == worst_rank],
            "dimensions": dimensions,
            "gaps": gaps,
            "metrics": metric_partition,
        }
        tier_counts[overall] += 1
        if gaps:
            remediation_accounts += 1

    reported_metrics = report.get("metrics", {})
    governance_metrics = {}
    for field in METRIC_FIELDS:
        reported = round(float(reported_metrics.get(field, 0) or 0), 2)
        governed_value = round(portfolio_governed[field], 2)
        provisional_value = round(portfolio_provisional[field], 2)
        if round(governed_value + provisional_value, 2) != reported:
            raise ValueError(f"Governance partition does not conserve {field}.")
        governance_metrics[field] = {
            "reported": reported,
            "governed": governed_value,
            "provisional": provisional_value,
        }

    non_empty_tiers = [tier for tier, count in tier_counts.items() if count]
    overall_portfolio = max(non_empty_tiers, key=lambda tier: TIER_RANK[tier]) if non_empty_tiers else "T4"
    report["governance"] = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "mode": mode,
        "minimum_governed_tier": "T2",
        "overall_tier": overall_portfolio,
        "account_tier_counts": tier_counts,
        "remediation_account_count": remediation_accounts,
        "metrics": governance_metrics,
    }
    report.setdefault("meta", {})["governance_mode"] = mode
    report["meta"]["governance_policy_version"] = POLICY_VERSION
    return report
