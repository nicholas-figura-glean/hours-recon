"""Threshold detection, recipient resolution, and weekly digest rendering.

Pure functions over a reconciled report plus stored high-water marks. Nothing
here touches the database, the network, or the clock beyond the report date it
is handed, so every decision is reproducible from its inputs.

Semantics, in one place:

* The unit of measurement is a **package**, not an account. See
  ``hours_recon.consumption`` for why.
* A rung fires **once ever** per entitlement. The high-water mark is monotonic,
  so usage that falls and rises again does not re-notify.
* A changed entitlement (renewal, resize) is a **new** entitlement and re-arms
  the ladder. A rotated package ID for an *unchanged* entitlement is not.
* Detection never runs on incomplete or stale source data. Missing usage is
  never treated as zero usage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .consumption import package_rows
from .dates import monday_of, parse_date

DEFAULT_POLICY: Dict[str, Any] = {
    "ladder": [50, 75, 90, 100],
    # Guards against a package resting exactly on a rung and flapping on float
    # noise. A rung must be exceeded by this margin before it counts.
    "epsilon_pp": 0.5,
    # Suppresses a rung that is only reached because of a rounding-scale change
    # rather than real new activity.
    "min_delta_hours": 0.25,
    "roles": ["salesforce_account_owner", "aiom"],
    "allowed_domains": ["glean.com"],
    "account_overrides": {},
}

APPROVED_STATUSES = {"APPROVED", "APPROVED_WITH_CHANGES"}


def _rung_floor(rung: int, epsilon_pp: float) -> float:
    """Percentage a package must reach for ``rung`` to count as crossed.

    The epsilon margin exists so a package resting exactly on a rung cannot flap
    on float noise. It must be capped at 100 or the top rung becomes
    unreachable: consumption is bounded by the entitlement, so a fully exhausted
    package reports exactly 100.0 and would never clear ``100 + epsilon``. That
    would silently downgrade the single most important alert to 90%.
    """
    return min(float(rung) + float(epsilon_pp), 100.0)


def load_policy(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Merge a configured policy over the defaults, validating the ladder."""
    policy = dict(DEFAULT_POLICY)
    for key, value in dict(raw or {}).items():
        if value is not None:
            policy[key] = value
    ladder = sorted({int(value) for value in policy.get("ladder") or [] if 0 < int(value) <= 100})
    if not ladder:
        raise ValueError("notification policy ladder must contain at least one rung in 1..100")
    policy["ladder"] = ladder
    policy["epsilon_pp"] = float(policy.get("epsilon_pp") or 0.0)
    policy["min_delta_hours"] = float(policy.get("min_delta_hours") or 0.0)
    policy["allowed_domains"] = [
        str(value).strip().lower().lstrip("@") for value in policy.get("allowed_domains") or [] if str(value).strip()
    ]
    policy["roles"] = [str(value).strip() for value in policy.get("roles") or [] if str(value).strip()]
    overrides = policy.get("account_overrides")
    policy["account_overrides"] = dict(overrides) if isinstance(overrides, Mapping) else {}
    return policy


def account_unapproved_hours(account: Mapping[str, Any]) -> float:
    """Billable hours counted toward usage that Rocketlane has not approved.

    Reported rather than excluded: the agreed semantics count these hours, and
    the digest discloses them so a recipient can see that a figure may still
    move. Rocketlane's time-entry search omits approval status entirely, so an
    entry with no recorded status is treated as unapproved here.
    """
    total = Decimal("0")
    for entry in account.get("entries") or []:
        status = str(entry.get("approval_status") or "").strip().upper()
        if status in APPROVED_STATUSES:
            continue
        hours = entry.get("hours")
        if hours is None:
            hours = Decimal(str(entry.get("minutes") or 0)) / Decimal("60")
        total += Decimal(str(hours))
    return float(total.quantize(Decimal("0.01")))


def _state_indices(state_rows: Sequence[Mapping[str, Any]]) -> Tuple[Dict[Tuple[str, str, str], Mapping[str, Any]], Dict[Tuple[str, str], List[Mapping[str, Any]]]]:
    exact: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
    by_economic: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for row in state_rows:
        account_id = str(row.get("account_id") or "")
        exact[(account_id, str(row.get("package_id") or ""), str(row.get("entitlement_key") or ""))] = row
        by_economic.setdefault((account_id, str(row.get("economic_key") or "")), []).append(row)
    return exact, by_economic


def detect(
    report: Mapping[str, Any],
    state_rows: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    coverage_complete: bool,
    freshness: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate every package against the ladder.

    Returns observations (one per package, always), crossings (rungs newly
    reached), migrations (package-ID rotations to carry forward), and
    diagnostics. Observations are returned even when nothing crossed so the
    store can record last-seen values and regressions.
    """
    if not coverage_complete:
        return {"skipped": True, "reason": "source_coverage_incomplete",
                "observations": [], "crossings": [], "migrations": [], "diagnostics": []}
    state = str((freshness or {}).get("state") or "")
    if freshness is not None and state not in {"current", "demo"}:
        return {"skipped": True, "reason": f"source_not_current:{state or 'unknown'}",
                "observations": [], "crossings": [], "migrations": [], "diagnostics": []}

    ladder = list(policy["ladder"])
    epsilon = float(policy["epsilon_pp"])
    min_delta = float(policy["min_delta_hours"])
    unapproved_by_account = {
        str(account.get("id") or ""): account_unapproved_hours(account)
        for account in report.get("accounts", []) or []
    }

    exact, by_economic = _state_indices(state_rows)
    rows = package_rows(report)
    live_ids_by_account: Dict[str, set] = {}
    for row in rows:
        live_ids_by_account.setdefault(row["account_id"], set()).add(row["package_id"])

    observations: List[Dict[str, Any]] = []
    crossings: List[Dict[str, Any]] = []
    migrations: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []

    for row in rows:
        account_id = row["account_id"]
        pct = row["consumption_pct"]
        if pct is None:
            diagnostics.append({"package_id": row["package_id"], "reason": "no_entitlement"})
            continue

        exact_key = (account_id, row["package_id"], row["entitlement_key"])
        prior = exact.get(exact_key)
        migration: Optional[Dict[str, Any]] = None

        if prior is None:
            # No state for this package ID. Either a genuinely new entitlement,
            # or the same entitlement whose package ID rotated because the
            # line-item source changed. Only carry the mark forward when the
            # mapping is unambiguous: exactly one orphaned state row with the
            # same economics, and exactly one live package claiming it.
            orphans = [
                candidate for candidate in by_economic.get((account_id, row["economic_key"]), [])
                if str(candidate.get("package_id")) not in live_ids_by_account.get(account_id, set())
            ]
            claimants = [
                other for other in rows
                if other["account_id"] == account_id
                and other["economic_key"] == row["economic_key"]
                and (account_id, other["package_id"], other["entitlement_key"]) not in exact
            ]
            if len(orphans) == 1 and len(claimants) == 1:
                prior = orphans[0]
                migration = {
                    "account_id": account_id,
                    "from_package_id": str(prior.get("package_id") or ""),
                    "from_entitlement_key": str(prior.get("entitlement_key") or ""),
                    "to_package_id": row["package_id"],
                    "to_entitlement_key": row["entitlement_key"],
                    "economic_key": row["economic_key"],
                }
                migrations.append(migration)
            elif len(orphans) > 1 or len(claimants) > 1:
                diagnostics.append({
                    "package_id": row["package_id"],
                    "reason": "ambiguous_entitlement_rotation",
                    "orphans": len(orphans), "claimants": len(claimants),
                })

        prior_high_water = int(prior.get("high_water_threshold") or 0) if prior else 0
        prior_high_pct = float(prior.get("high_water_pct") or 0.0) if prior else 0.0
        prior_consumed = float(prior.get("last_consumed_hours") or 0.0) if prior else 0.0
        regressed = bool(prior) and pct < prior_high_pct - epsilon

        newly = [
            rung for rung in ladder
            if rung > prior_high_water
            and pct >= _rung_floor(rung, epsilon)
            and (row["consumed_hours"] - prior_consumed) >= min_delta
        ]
        reached = max(newly) if newly else prior_high_water

        observations.append({
            "account_id": account_id,
            "package_id": row["package_id"],
            "entitlement_key": row["entitlement_key"],
            "economic_key": row["economic_key"],
            "pct": pct,
            "consumed_hours": row["consumed_hours"],
            "sold_hours": row["sold_hours"],
            "reached_threshold": reached,
            "regressed": regressed,
        })
        if regressed:
            diagnostics.append({"package_id": row["package_id"], "reason": "usage_regressed", "pct": pct})
        if not newly:
            continue

        crossings.append({
            "account_id": account_id,
            "account_name": row["account_name"],
            "account_owner_email": row["account_owner_email"],
            "package_id": row["package_id"],
            "package_label": row["package_label"],
            "entitlement_key": row["entitlement_key"],
            # A jump past several rungs is one notification at the highest rung;
            # the rungs it passed are recorded for explainability.
            "threshold": max(newly),
            "skipped_rungs": sorted(newly)[:-1],
            "pct": pct,
            "consumed_hours": row["consumed_hours"],
            "sold_hours": row["sold_hours"],
            "remaining_hours": row["remaining_hours"],
            "expiration_date": row["expiration_date"],
            "days_to_expiration": row["days_to_expiration"],
            "overage_hours": row["overage_hours"],
            "unapproved_hours": unapproved_by_account.get(account_id, 0.0),
            "entitlement_changed": bool(prior is None and not migration and by_economic.get((account_id, row["economic_key"]))),
            "prior_threshold": prior_high_water,
        })

    return {
        "skipped": False,
        "reason": None,
        "observations": observations,
        "crossings": crossings,
        "migrations": migrations,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# recipients
# ---------------------------------------------------------------------------


def _allowed(address: str, policy: Mapping[str, Any]) -> bool:
    text = str(address or "").strip().lower()
    if "@" not in text or text.startswith("@") or text.endswith("@"):
        return False
    domains = policy.get("allowed_domains") or []
    if not domains:
        return False
    return text.rsplit("@", 1)[1] in set(domains)


def assert_allowlisted(addresses: Sequence[str], policy: Mapping[str, Any]) -> None:
    blocked = sorted({str(value).strip().lower() for value in addresses if not _allowed(str(value), policy)})
    if blocked:
        raise ValueError(f"Refusing to queue a digest to non-allowlisted recipients: {', '.join(blocked)}")


def resolve_recipients(
    crossing: Mapping[str, Any], *, policy: Mapping[str, Any], aiom_email: str,
) -> List[Tuple[str, List[str]]]:
    """Recipient groups for one crossing.

    Each group receives its own digest containing only the crossings it resolves
    to, so an account owner never sees another owner's accounts. A per-account
    override replaces the role defaults entirely for that account.
    """
    account_id = str(crossing.get("account_id") or "")
    override = (policy.get("account_overrides") or {}).get(account_id)
    groups: List[Tuple[str, List[str]]] = []

    if isinstance(override, Mapping) and override.get("recipients"):
        addresses = sorted({str(value).strip().lower() for value in override["recipients"] if str(value).strip()})
        if addresses:
            groups.append((f"override:{account_id}", addresses))
        return groups

    roles = policy.get("roles") or []
    if "salesforce_account_owner" in roles:
        owner = str(crossing.get("account_owner_email") or "").strip().lower()
        if owner and _allowed(owner, policy):
            groups.append((f"account_owner:{owner}", [owner]))
    if "aiom" in roles:
        aiom = str(aiom_email or "").strip().lower()
        if aiom and _allowed(aiom, policy):
            groups.append((f"aiom:{aiom}", [aiom]))
    return groups


def group_for_digest(
    crossings: Sequence[Mapping[str, Any]], *, policy: Mapping[str, Any], aiom_email: str,
) -> List[Dict[str, Any]]:
    """Bucket crossings into one digest per recipient group."""
    buckets: Dict[str, Dict[str, Any]] = {}
    unroutable: List[Mapping[str, Any]] = []
    for crossing in crossings:
        groups = resolve_recipients(crossing, policy=policy, aiom_email=aiom_email)
        if not groups:
            unroutable.append(crossing)
            continue
        for group_key, addresses in groups:
            bucket = buckets.setdefault(group_key, {"group_key": group_key, "recipients": addresses, "crossings": []})
            bucket["crossings"].append(crossing)
    ordered = sorted(buckets.values(), key=lambda item: item["group_key"])
    for bucket in ordered:
        bucket["crossings"].sort(key=lambda item: (str(item.get("account_name") or ""), str(item.get("package_label") or "")))
    if unroutable:
        ordered.append({"group_key": "__unroutable__", "recipients": [], "crossings": list(unroutable)})
    return ordered


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def digest_window_key(report_date: date) -> str:
    return monday_of(report_date).isoformat()


def _hours(value: Any) -> str:
    return f"{float(value or 0):.2f}h"


def render_subject(crossings: Sequence[Mapping[str, Any]], window_key: str) -> str:
    accounts = sorted({str(item.get("account_name") or "") for item in crossings})
    if len(accounts) == 1 and len(crossings) == 1:
        item = crossings[0]
        return (
            f"[Hours] {accounts[0]} at {int(item['threshold'])}% of professional services hours "
            f"({_hours(item['consumed_hours'])} of {_hours(item['sold_hours'])}) — week of {window_key}"
        )
    if len(accounts) == 1:
        return f"[Hours] {accounts[0]} crossed {len(crossings)} usage thresholds — week of {window_key}"
    return f"[Hours] {len(accounts)} accounts crossed usage thresholds — week of {window_key}"


def render_digest(
    crossings: Sequence[Mapping[str, Any]],
    *,
    window_key: str,
    source_note: str = "",
) -> str:
    """Render a self-contained plain-text digest.

    No links by design: the dashboard is bound to localhost and is meaningless
    to a recipient, so every figure a reader needs is stated inline.
    """
    lines: List[str] = [f"Weekly professional services hours digest — week of {window_key}", ""]
    by_account: Dict[str, List[Mapping[str, Any]]] = {}
    for item in crossings:
        by_account.setdefault(str(item.get("account_name") or "Unknown account"), []).append(item)

    for account_name in sorted(by_account):
        items = sorted(by_account[account_name], key=lambda item: -int(item["threshold"]))
        headline = max(int(item["threshold"]) for item in items)
        lines.append(f"{account_name} — crossed {headline}% of package hours")
        for item in items:
            lines.append(f"  Package:    {item['package_label']} ({_hours(item['sold_hours'])} sold)")
            lines.append(
                f"  Used:       {_hours(item['consumed_hours'])} ({float(item['pct']):.1f}%)"
                f"          Remaining: {_hours(item['remaining_hours'])}"
            )
            if float(item.get("overage_hours") or 0) > 0:
                lines.append(f"  Overage:    {_hours(item['overage_hours'])}")
            if item.get("expiration_date"):
                days = item.get("days_to_expiration")
                suffix = f" ({int(days)} days)" if days is not None else ""
                lines.append(f"  Expires:    {item['expiration_date']}{suffix}")
            crossed = f"  Crossed:    {int(item['threshold'])}% this week"
            if int(item.get("prior_threshold") or 0):
                crossed += f" (last reported at {int(item['prior_threshold'])}%)"
            if item.get("skipped_rungs"):
                passed = ", ".join(f"{int(rung)}%" for rung in item["skipped_rungs"])
                crossed += f"; passed {passed} in the same period"
            if item.get("entitlement_changed"):
                crossed += "; entitlement changed since the last notification"
            lines.append(crossed)
            lines.append("")

    unapproved = sum(float(item.get("unapproved_hours") or 0) for item in _first_per_account(crossings))
    lines.append("Notes")
    lines.append("  - Only thresholds newly crossed this week are listed.")
    if unapproved > 0:
        lines.append(
            f"  - Usage includes {_hours(unapproved)} of billable time not yet approved in "
            "Rocketlane, which may still change."
        )
    if source_note:
        lines.append(f"  - {source_note}")
    lines.append("  - Figures are reconciled from Salesforce entitlements and Rocketlane billable time.")
    lines.append("  - Questions: reply to this email.")
    lines.append("")
    lines.append("— sent via Glean Pi")
    return "\n".join(lines)


def _first_per_account(crossings: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """One crossing per account, so account-level figures are not double counted."""
    seen: Dict[str, Mapping[str, Any]] = {}
    for item in crossings:
        seen.setdefault(str(item.get("account_id") or ""), item)
    return list(seen.values())


def body_digest(body: str) -> str:
    return sha256(str(body).encode("utf-8")).hexdigest()


def source_note_from_meta(meta: Mapping[str, Any]) -> str:
    retrieval = str(meta.get("mcp_retrieval_id") or meta.get("source_retrieval_id") or "").strip()
    through = str(meta.get("mcp_through_date") or meta.get("as_of") or "").strip()
    parts = []
    if retrieval:
        parts.append(f"retrieval {retrieval[:8]}")
    if through:
        parts.append(f"verified complete pull through {through}")
    return "Source: " + ", ".join(parts) if parts else ""
