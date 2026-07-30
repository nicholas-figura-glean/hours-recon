"""Versioned remediation path catalog and deterministic recommendation policy."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

POLICY_VERSION = "remediation-v2"
MINIMUM_GOVERNED_TIER = "T2"
EFFORT_POINTS = {"XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8}
DURABILITY_POINTS = {"low": 0, "medium": 1, "high": 2}
VALIDATION_POINTS = {"manual": 0, "hybrid": 1, "automatic": 2}


def _path(
    path_id: str,
    title: str,
    target_tier: str,
    outcome: str,
    *,
    effort: str,
    durability: str,
    execution_scope: str,
    primary_owner: str,
    contributors: Sequence[str],
    steps: Sequence[str],
    dependencies: Sequence[str],
    validation_checks: Sequence[str],
    validation_mode: str = "automatic",
    detailed: bool = True,
) -> Dict[str, Any]:
    return {
        "id": path_id,
        "title": title,
        "target_tier": target_tier,
        "outcome": outcome,
        "effort": effort,
        "effort_points": EFFORT_POINTS[effort],
        "durability": durability,
        "execution_scope": execution_scope,
        "primary_owner": primary_owner,
        "contributors": list(contributors),
        "steps": list(steps),
        "dependencies": list(dependencies),
        "validation_checks": list(validation_checks),
        "validation_mode": validation_mode,
        "detailed": detailed,
    }


def route_for_dimension(dimension: str, reason_code: str = "") -> Dict[str, Any]:
    if reason_code == "incomplete_source_coverage":
        return {
            "route": "source_retrieval",
            "primary_owner": "Hours Recon operator",
            "required_partners": ["Salesforce / Rocketlane connector owner"],
        }
    routes = {
        "entitlement_source": {
            "route": "entitlement_data",
            "primary_owner": "Opportunity owner",
            "required_partners": ["Deal Desk / RevOps"],
        },
        "hours_mapping": {
            "route": "entitlement_data",
            "primary_owner": "Hours Recon owner",
            "required_partners": ["Opportunity owner", "Deal Desk / RevOps"],
        },
        "service_period": {
            "route": "entitlement_data",
            "primary_owner": "Opportunity owner",
            "required_partners": ["AIOM owner", "Deal Desk / RevOps"],
        },
        "project_linkage": {
            "route": "project_mapping",
            "primary_owner": "Rocketlane project owner",
            "required_partners": ["CS Ops"],
        },
        "time_quality": {
            "route": "time_quality",
            "primary_owner": "Rocketlane project owner / time-entry author",
            "required_partners": ["Rocketlane admin"],
        },
    }
    return routes.get(dimension, {
        "route": "data_governance",
        "primary_owner": "Hours Recon owner",
        "required_partners": ["Source-system owner"],
    })


def path_options(dimension: str, reason_code: str, details: Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Return all valid remediation paths for an evidence gap.

    The first three evidence dimensions have detailed, source-specific paths.
    Entitlement source and time quality intentionally use framework-safe paths
    until their operating policies are expanded.
    """
    details = dict(details or {})
    if reason_code == "incomplete_source_coverage":
        missing = ", ".join(str(value) for value in details.get("missing_coverage", []) if value) or "required source coverage"
        return [_path(
            "source_coverage.complete_verified_pull.t2",
            "Complete and verify a fresh source pull",
            "T2",
            "Remove the retrieval-coverage cap and re-evaluate every underlying evidence dimension against the T2 minimum.",
            effort="S", durability="medium", execution_scope="systemic",
            primary_owner="Hours Recon operator", contributors=["Salesforce / Rocketlane connector owner"],
            steps=[
                f"Identify why coverage is missing or unverified for: {missing}.",
                "Run an account-isolated pull through the report date and exhaust every pagination token.",
                "Verify connector scope identity and set coverage flags only from observed retrieval evidence.",
                "Publish the snapshot atomically and reload Hours Recon.",
            ],
            dependencies=["Authenticated Salesforce and Rocketlane connectors", "Verified tenant/workspace scope"],
            validation_checks=[
                "Every required coverage flag is literal true.",
                "The through-date equals the report date and scope verification succeeds.",
                "The refreshed underlying dimension reaches T2 or T1.",
            ],
        )]

    if dimension == "hours_mapping":
        return [
            _path(
                "hours_mapping.reviewed_explicit_hours.t2",
                "Record reviewed explicit contracted hours",
                "T2",
                "Replace name, price, or local-override inference with explicit hours evidence reviewed against the agreement.",
                effort="S", durability="medium", execution_scope="account",
                primary_owner="Opportunity owner", contributors=["Deal Desk / RevOps", "Hours Recon owner"],
                steps=[
                    "Confirm contracted hours against the accepted commercial agreement.",
                    "Populate the governed explicit-hours field or constrained numeric package evidence.",
                    "Remove conflicting name, price, or local override assumptions.",
                    "Run a complete refresh and inspect the resulting hours mapping.",
                ],
                dependencies=["Accepted agreement or approved commercial record", "Writable Salesforce hours evidence"],
                validation_checks=[
                    "The mapping source is explicit_hours or growth_tier.",
                    "Calculated sold hours match the accepted agreement.",
                    "A complete refresh scores hours_mapping at T2 or better.",
                ],
            ),
            _path(
                "hours_mapping.canonical_product_code.t1",
                "Establish a canonical ProductCode mapping",
                "T1",
                "Resolve the root cause with an exact, versioned ProductCode-to-hours mapping reusable across affected accounts.",
                effort="M", durability="high", execution_scope="systemic",
                primary_owner="Hours Recon owner", contributors=["Deal Desk / RevOps", "Salesforce product catalog owner"],
                steps=[
                    "Identify the canonical Salesforce ProductCode and contracted hours per unit.",
                    "Confirm the code is unique and semantically stable in the product catalog.",
                    "Add or correct the versioned ProductCode mapping and remove superseded overrides.",
                    "Ensure affected Opportunities carry the canonical product evidence.",
                    "Run a complete refresh and reconcile sold hours for every affected account.",
                ],
                dependencies=["Canonical Salesforce product record", "Approved hours-per-unit definition"],
                validation_checks=[
                    "Every affected package resolves through product_code.",
                    "No conflicting override or unresolved package exception remains.",
                    "A complete refresh scores hours_mapping at T1.",
                ],
            ),
        ]

    if dimension == "service_period":
        return [
            _path(
                "service_period.one_explicit_boundary.t2",
                "Record one contractual service boundary",
                "T2",
                "Use one explicit contractual boundary with the governed one-year rule for the other boundary.",
                effort="S", durability="medium", execution_scope="account",
                primary_owner="Opportunity owner", contributors=["AIOM owner", "Deal Desk / RevOps"],
                steps=[
                    "Review the accepted agreement and determine the authoritative start or end date.",
                    "Populate the governed Salesforce boundary field.",
                    "Confirm the derived opposite boundary follows the approved one-year rule.",
                    "Run a complete refresh and inspect entitlement dates.",
                ],
                dependencies=["Accepted agreement or approved service schedule"],
                validation_checks=[
                    "The service period source is partial_explicit.",
                    "The explicit boundary matches the agreement.",
                    "A complete refresh scores service_period at T2 or better.",
                ],
            ),
            _path(
                "service_period.both_explicit_boundaries.t1",
                "Record both contractual service boundaries",
                "T1",
                "Store explicit start and end dates so entitlement validity no longer depends on a duration assumption.",
                effort="M", durability="high", execution_scope="account",
                primary_owner="Opportunity owner", contributors=["AIOM owner", "Deal Desk / RevOps"],
                steps=[
                    "Review the accepted agreement and authoritative service schedule.",
                    "Populate explicit service start and end dates on the governed Salesforce record.",
                    "Resolve conflicting or invalid date values and confirm start is not after end.",
                    "Run a complete refresh and compare the entitlement window to the agreement.",
                ],
                dependencies=["Accepted agreement or approved service schedule"],
                validation_checks=[
                    "The service period source is line_item_explicit or opportunity_explicit.",
                    "Both boundaries match the agreement and form a valid interval.",
                    "A complete refresh scores service_period at T1.",
                ],
            ),
        ]

    if dimension == "project_linkage":
        return [
            _path(
                "project_linkage.customer_id_crosswalk.t2",
                "Create a governed customer-ID crosswalk",
                "T2",
                "Link Salesforce and Rocketlane through a reviewed one-to-one Rocketlane customer-ID crosswalk.",
                effort="S", durability="high", execution_scope="account",
                primary_owner="Rocketlane project owner", contributors=["CS Ops", "Hours Recon owner"],
                steps=[
                    "Identify the authoritative Rocketlane customer and service project.",
                    "Confirm the customer ID maps to exactly one Salesforce Account.",
                    "Add the reviewed customer-ID crosswalk and remove conflicting aliases.",
                    "Run a complete refresh and inspect project match provenance.",
                ],
                dependencies=["Existing or newly created Rocketlane customer/project", "One-to-one identity review"],
                validation_checks=[
                    "The match basis is rocketlane_customer_id_crosswalk.",
                    "No duplicate or conflicting crosswalk exists.",
                    "A complete refresh scores project_linkage at T2 or better.",
                ],
            ),
            _path(
                "project_linkage.salesforce_account_id.t1",
                "Store the Salesforce Account ID in Rocketlane",
                "T1",
                "Create a direct cross-system identity link on the Rocketlane customer or project.",
                effort="M", durability="high", execution_scope="account",
                primary_owner="Rocketlane project owner", contributors=["CS Ops"],
                steps=[
                    "Identify or create the authoritative Rocketlane customer and service project.",
                    "Confirm the intended Salesforce Account ID with the account owner.",
                    "Populate the governed Salesforce Account ID field in Rocketlane.",
                    "Remove conflicting identity values or ambiguous aliases.",
                    "Run a complete refresh and inspect project match provenance.",
                ],
                dependencies=["Writable Rocketlane identity field", "Confirmed Salesforce Account"],
                validation_checks=[
                    "The Rocketlane record stores the exact Salesforce Account ID.",
                    "The match basis is salesforce_account_id with no collision.",
                    "A complete refresh scores project_linkage at T1.",
                ],
            ),
        ]

    if dimension == "entitlement_source":
        return [
            _path(
                "entitlement_source.approved_quote.t2",
                "Govern the accepted Quote source",
                "T2",
                "Use an approved or synced Quote Line as authoritative entitlement evidence when Opportunity Products are absent.",
                effort="S", durability="medium", execution_scope="account",
                primary_owner="Opportunity owner", contributors=["Deal Desk / RevOps"],
                steps=[
                    "Confirm the accepted Quote and its entitlement lines.",
                    "Record the approved or synced Quote relationship in Salesforce.",
                    "Resolve ambiguous primary-quote or opportunity-name fallbacks.",
                    "Run a complete refresh and verify the selected source.",
                ],
                dependencies=["Accepted Quote with complete entitlement lines"],
                validation_checks=["The source is approved_quote or synced_quote.", "A complete refresh scores entitlement_source at T2 or better."],
                detailed=False,
            ),
            _path(
                "entitlement_source.opportunity_product.t1",
                "Create canonical Opportunity Product evidence",
                "T1",
                "Represent entitlement directly on the Closed Won Opportunity with canonical product records.",
                effort="M", durability="high", execution_scope="account",
                primary_owner="Opportunity owner", contributors=["Deal Desk / RevOps"],
                steps=[
                    "Confirm the accepted entitlement and canonical product.",
                    "Create or synchronize the Opportunity Product record.",
                    "Remove unresolved or contradictory fallback evidence.",
                    "Run a complete refresh and verify the product source.",
                ],
                dependencies=["Canonical Salesforce product", "Accepted commercial agreement"],
                validation_checks=["The source is opportunity_line_item.", "A complete refresh scores entitlement_source at T1."],
                detailed=False,
            ),
        ]

    if dimension == "time_quality" and reason_code == "usage_unobservable_without_project":
        return [_path(
            "time_quality.restore_project_observability.t2",
            "Restore usage observability",
            "T2",
            "Link the intended project and confirm complete extraction so observed usage or governed zero usage is auditable.",
            effort="M", durability="medium", execution_scope="account",
            primary_owner="Rocketlane project owner", contributors=["CS Ops", "Hours Recon operator"],
            steps=[
                "Identify and link the authoritative service project.",
                "Retrieve all billable entries through the report date.",
                "Verify pagination and project coverage.",
                "Run a complete refresh and inspect time evidence.",
            ],
            dependencies=["Authoritative Rocketlane project", "Complete time-entry extraction"],
            validation_checks=["A linked project makes usage observable.", "A complete refresh scores time_quality at T2 or better."],
            detailed=False,
        )]

    if dimension == "time_quality":
        return [_path(
            "time_quality.complete_required_metadata.t1",
            "Correct required project and time metadata",
            "T1",
            "Make billable usage structurally complete, attributable, in-period, and compliant with approval policy.",
            effort="M", durability="high", execution_scope="account",
            primary_owner="Rocketlane project owner / time-entry author", contributors=["Rocketlane admin"],
            steps=[
                "Review the flagged project and time-entry evidence.",
                "Correct identifiers, contributor, activity, category, lifecycle dates, and approval state where required.",
                "Exclude rejected or invalid records only through an auditable source-system correction.",
                "Run a complete refresh and verify all billable entries.",
            ],
            dependencies=["Writable Rocketlane records", "Documented time approval policy"],
            validation_checks=["No invalid or rejected entry remains.", "Required project and time metadata is complete.", "A complete refresh scores time_quality at T1."],
            detailed=False,
        )]

    return [_path(
        f"{dimension or 'data_governance'}.review_source_evidence.t1",
        "Review and correct authoritative source evidence",
        "T1",
        "Replace provisional evidence with complete, directly attributable source-system evidence.",
        effort="M", durability="high", execution_scope="account",
        primary_owner="Hours Recon owner", contributors=["Source-system owner"],
        steps=["Review the evidence references.", "Correct the authoritative source record.", "Run a complete refresh and verify the resulting tier."],
        dependencies=["Authoritative source-system access"],
        validation_checks=[f"A complete refresh scores {dimension or 'the dimension'} at T1."],
        detailed=False,
    )]


def _path_score(path: Mapping[str, Any], affected_accounts: int) -> int:
    effort = EFFORT_POINTS[str(path["effort"])]
    durability = DURABILITY_POINTS[str(path["durability"])]
    validation = VALIDATION_POINTS[str(path["validation_mode"])]
    breadth = min(15, max(0, affected_accounts - 1) * 3) if path.get("execution_scope") == "systemic" else 0
    quality = 3 if path.get("target_tier") == "T1" else 0
    return 100 - effort * 8 + durability * 6 + validation * 4 + breadth + quality


def rank_paths(
    paths: Iterable[Mapping[str, Any]],
    *,
    affected_accounts: int,
    priority: str,
    impact: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], str, str]:
    """Rank paths and explain the deterministic T2-versus-T1 decision."""
    decorated: List[Dict[str, Any]] = []
    for source in paths:
        item = dict(source)
        item["recommendation_score"] = _path_score(item, affected_accounts)
        item["recommended"] = False
        decorated.append(item)
    if not decorated:
        raise ValueError("At least one remediation path is required.")

    def best(target: str) -> Dict[str, Any] | None:
        choices = [item for item in decorated if item.get("target_tier") == target]
        return max(choices, key=lambda item: (int(item["recommendation_score"]), str(item["id"]))) if choices else None

    t2 = best("T2")
    t1 = best("T1")
    chosen = t2 or t1 or max(decorated, key=lambda item: (int(item["recommendation_score"]), str(item["id"])))
    reason = "This is the lowest-effort durable path that reaches the governed T2 minimum."
    if t2 is None and t1 is not None:
        chosen = t1
        reason = "No policy-safe T2 shortcut exists for this evidence; the direct T1 correction is the minimum valid path."
    elif t1 is not None and t2 is not None:
        effort_delta = int(t1["effort_points"]) - int(t2["effort_points"])
        broader = affected_accounts >= 3 and t1.get("execution_scope") == "systemic" and t2.get("execution_scope") != "systemic"
        high_impact = priority == "P0" or any(float(impact.get(key, 0) or 0) > 0 for key in ("at_risk_hours", "overage_hours", "expired_unused_hours"))
        more_durable = DURABILITY_POINTS[str(t1["durability"])] > DURABILITY_POINTS[str(t2["durability"])]
        if effort_delta <= 0:
            chosen = t1
            reason = "The T1 path requires no more effort than the T2 option and produces stronger evidence."
        elif broader and effort_delta <= 2:
            chosen = t1
            reason = "The systemic T1 path fixes a shared root cause across multiple accounts, outweighing its modest added effort."
        elif high_impact and more_durable and effort_delta <= 1:
            chosen = t1
            reason = "The affected hours are high-impact and the more durable T1 outcome requires only one additional effort band."

    for item in decorated:
        item["recommended"] = item["id"] == chosen["id"]
    decorated.sort(key=lambda item: (not bool(item["recommended"]), -int(item["recommendation_score"]), str(item["id"])))
    return decorated, str(chosen["id"]), reason


def validate_paths(paths: Iterable[Mapping[str, Any]]) -> None:
    seen = set()
    required = {
        "id", "title", "target_tier", "outcome", "effort", "durability", "execution_scope",
        "primary_owner", "contributors", "steps", "dependencies", "validation_checks", "validation_mode",
    }
    for path in paths:
        missing = required - set(path)
        if missing:
            raise ValueError(f"Remediation path is missing fields: {sorted(missing)}")
        path_id = str(path["id"])
        if path_id in seen:
            raise ValueError(f"Duplicate remediation path ID: {path_id}")
        seen.add(path_id)
        if path["target_tier"] not in {"T1", "T2"}:
            raise ValueError(f"Invalid target tier for {path_id}")
        if path["effort"] not in EFFORT_POINTS or path["durability"] not in DURABILITY_POINTS:
            raise ValueError(f"Invalid effort or durability for {path_id}")
        if not path["steps"] or not path["validation_checks"]:
            raise ValueError(f"Remediation path {path_id} requires steps and validation checks")
