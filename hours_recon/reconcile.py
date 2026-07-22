"""Deterministic reconciliation, FIFO allocation, risk, and weekly compliance."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple

from .dates import monday_of, parse_date
from .inference import infer_packages
from .matching import match_projects

RISK_ORDER = {"overage": 0, "expired": 1, "critical": 2, "high": 3, "medium": 4, "healthy": 5, "exhausted": 6, "none": 7}


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _hours(entry: Mapping[str, Any]) -> Decimal:
    if entry.get("hours") is not None:
        return _d(entry["hours"])
    return _d(entry.get("minutes")) / Decimal("60")


def _round(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def package_risk(remaining: Decimal, expiration: date, as_of: date) -> Tuple[str, int]:
    days = (expiration - as_of).days
    if remaining <= 0:
        return "exhausted", days
    if days < 0:
        return "expired", days
    if days <= 30:
        return "critical", days
    if days <= 60:
        return "high", days
    if days <= 90:
        return "medium", days
    return "healthy", days


def reconcile(
    salesforce: Mapping[str, Any],
    rocketlane: Mapping[str, Any],
    *,
    package_config: Mapping[str, Any],
    account_aliases: Mapping[str, Any],
    as_of: Optional[date] = None,
    mode: str = "live",
) -> Dict[str, Any]:
    report_date = as_of or date.today()
    requester = dict(salesforce.get("requester", {}))
    accounts = [dict(item) for item in salesforce.get("accounts", [])]
    opportunities = [dict(item) for item in salesforce.get("opportunities", [])]
    projects = [dict(item) for item in rocketlane.get("projects", [])]
    billable_entries = [dict(item) for item in rocketlane.get("entries", []) if item.get("billable", True)]
    entries = [item for item in billable_entries if item.get("date") and parse_date(item["date"]) <= report_date]
    future_entry_count = len(billable_entries) - len(entries)

    project_map, match_exceptions = match_projects(accounts, projects, account_aliases)
    exceptions: List[Dict[str, Any]] = list(match_exceptions)
    if future_entry_count:
        exceptions.append({
            "type": "future_entries_excluded",
            "message": f"{future_entry_count} future-dated billable time entries were excluded from this as-of report.",
            "count": future_entry_count,
        })
    account_results: Dict[str, Dict[str, Any]] = {}
    for account in accounts:
        account_id = str(account["id"])
        account_results[account_id] = {
            "id": account_id,
            "name": account["name"],
            "packages": [],
            "projects": [],
            "entries": [],
            "allocations": [],
        }

    for project in projects:
        account_id = project_map.get(str(project.get("id", "")))
        if account_id in account_results:
            account_results[account_id]["projects"].append(project)

    for opportunity in opportunities:
        account_id = str(opportunity.get("account_id", ""))
        if account_id not in account_results:
            continue
        packages, package_exceptions = infer_packages(opportunity, package_config)
        account_results[account_id]["packages"].extend(packages)
        exceptions.extend(package_exceptions)

    unmatched_entry_count = 0
    for entry in entries:
        project_id = str(entry.get("project_id", ""))
        account_id = project_map.get(project_id)
        if account_id in account_results:
            account_results[account_id]["entries"].append(entry)
        else:
            unmatched_entry_count += 1
    if unmatched_entry_count:
        exceptions.append({
            "type": "unmatched_entries_excluded",
            "message": f"{unmatched_entry_count} loaded time entries belonged to unmatched projects and were excluded.",
            "count": unmatched_entry_count,
        })

    current_week = monday_of(report_date)
    previous_week = current_week - timedelta(days=7)
    requester_email = str(requester.get("email", "")).lower()

    for account in account_results.values():
        _allocate_account(account, report_date)
        if account.get("unapplied_correction_hours", 0) > 0:
            exceptions.append({
                "type": "unapplied_negative_correction",
                "account_id": account["id"],
                "account_name": account["name"],
                "message": f"{account['unapplied_correction_hours']} correction hours exceeded prior allocated usage and need review.",
            })
        account_entries = account["entries"]
        for entry in account_entries:
            entry["hours"] = _round(_hours(entry))
        current_entries = [item for item in account_entries if current_week <= parse_date(item["date"]) <= report_date and _hours(item) != 0]
        previous_entries = [item for item in account_entries if previous_week <= parse_date(item["date"]) < current_week and _hours(item) != 0]
        account["weekly"] = {
            "current_week_start": current_week.isoformat(),
            "previous_week_start": previous_week.isoformat(),
            "account_active_current": bool(current_entries),
            "account_active_previous": bool(previous_entries),
            "aiom_active_current": any(str(item.get("user_email", "")).lower() == requester_email for item in current_entries),
            "aiom_active_previous": any(str(item.get("user_email", "")).lower() == requester_email for item in previous_entries),
        }
        account["project_count"] = len(account["projects"])
        account["entry_count"] = len(account_entries)
        account["risk"] = _account_risk(account)

    ordered_accounts = sorted(account_results.values(), key=lambda item: (RISK_ORDER.get(item["risk"], 99), item["name"].lower()))
    metrics = _portfolio_metrics(ordered_accounts)
    metrics["unmatched_projects"] = sum(1 for item in exceptions if item.get("type") in {"unmatched_project", "account_collision"})
    metrics["unresolved_packages"] = sum(1 for item in exceptions if item.get("type") == "unresolved_package")

    return {
        "meta": {
            "as_of": report_date.isoformat(),
            "generated_at": report_date.isoformat(),
            "mode": mode,
            "requester": requester,
            "allocation_method": "FIFO by package expiration, evaluated on each time-entry date",
            "expiration_rule": "Close date + 1 year; expiration date is inclusive",
            "risk_thresholds": {"critical_days": 30, "high_days": 60, "medium_days": 90},
        },
        "metrics": metrics,
        "risk_distribution": _risk_distribution(ordered_accounts),
        "accounts": ordered_accounts,
        "exceptions": sorted(exceptions, key=lambda item: (item.get("type", ""), item.get("account_name") or item.get("rocketlane_customer") or "")),
    }


def _allocate_account(account: MutableMapping[str, Any], as_of: date) -> None:
    packages = sorted(
        account["packages"],
        key=lambda item: (item["expiration_date"], item["close_date"], item["id"]),
    )
    balances: Dict[str, Decimal] = {item["id"]: _d(item["sold_hours"]) for item in packages}
    consumed: Dict[str, Decimal] = {item["id"]: Decimal("0") for item in packages}
    overage = Decimal("0")
    allocations: List[Dict[str, Any]] = []

    entries = sorted(account["entries"], key=lambda item: (item.get("date", ""), str(item.get("id", ""))))
    for entry in entries:
        amount = _hours(entry)
        entry_date = parse_date(entry["date"])
        if amount > 0:
            remaining = amount
            for package in packages:
                if remaining <= 0:
                    break
                package_id = package["id"]
                if not (parse_date(package["close_date"]) <= entry_date <= parse_date(package["expiration_date"])):
                    continue
                applied = min(remaining, balances[package_id])
                if applied <= 0:
                    continue
                balances[package_id] -= applied
                consumed[package_id] += applied
                remaining -= applied
                allocations.append({"entry_id": str(entry.get("id", "")), "package_id": package_id, "hours": _round(applied)})
            if remaining > 0:
                overage += remaining
                allocations.append({"entry_id": str(entry.get("id", "")), "package_id": None, "hours": _round(remaining), "reason": "No active package capacity on entry date"})
        elif amount < 0:
            credit = -amount
            reduce_overage = min(credit, overage)
            overage -= reduce_overage
            credit -= reduce_overage
            for package in reversed(packages):
                if credit <= 0:
                    break
                package_id = package["id"]
                adjustment = min(credit, consumed[package_id])
                if adjustment <= 0:
                    continue
                consumed[package_id] -= adjustment
                balances[package_id] += adjustment
                credit -= adjustment
            allocations.append({"entry_id": str(entry.get("id", "")), "package_id": None, "hours": _round(amount), "reason": "Negative correction reversed latest consumption"})

    sold = Decimal("0")
    used = Decimal("0")
    available = Decimal("0")
    expired_unused = Decimal("0")
    at_risk = Decimal("0")
    future_entitlement = Decimal("0")
    unapplied_correction = Decimal("0")
    for package in packages:
        package_id = package["id"]
        package_remaining = balances[package_id]
        expiration = parse_date(package["expiration_date"])
        risk, days = package_risk(package_remaining, expiration, as_of)
        package["consumed_hours"] = _round(consumed[package_id])
        package["remaining_hours"] = _round(package_remaining)
        package["risk"] = risk
        package["days_to_expiration"] = days
        sold += _d(package["sold_hours"])
        used += consumed[package_id]
        close_date = parse_date(package["close_date"])
        if expiration < as_of:
            expired_unused += package_remaining
        elif close_date > as_of:
            future_entitlement += package_remaining
        else:
            available += package_remaining
            if days <= 90:
                at_risk += package_remaining

    # A correction can exceed all previous overage and consumed capacity. Keep
    # that residual explicit so signed billed totals remain explainable.
    if entries:
        total_negative = sum((-_hours(item) for item in entries if _hours(item) < 0), Decimal("0"))
        # Allocation rows record the full correction; derive the residual from
        # net billed versus remaining capacity.
        positive = sum((_hours(item) for item in entries if _hours(item) > 0), Decimal("0"))
        net_capacity_usage = sum(consumed.values(), Decimal("0")) + overage
        unapplied_correction = max(total_negative - positive + net_capacity_usage, Decimal("0"))

    billed = sum((_hours(item) for item in account["entries"]), Decimal("0"))
    account["packages"] = packages
    account["allocations"] = allocations
    account["sold_hours"] = _round(sold)
    account["billed_hours"] = _round(billed)
    account["consumed_hours"] = _round(used)
    account["remaining_hours"] = _round(available)
    account["expired_unused_hours"] = _round(expired_unused)
    account["future_entitlement_hours"] = _round(future_entitlement)
    account["at_risk_hours"] = _round(at_risk)
    account["overage_hours"] = _round(max(overage, Decimal("0")))
    account["unapplied_correction_hours"] = _round(unapplied_correction)


def _account_risk(account: Mapping[str, Any]) -> str:
    if _d(account.get("overage_hours")) > 0:
        return "overage"
    package_risks = [item["risk"] for item in account.get("packages", []) if item["risk"] != "exhausted"]
    if not package_risks:
        return "none" if not account.get("packages") else "exhausted"
    return min(package_risks, key=lambda risk: RISK_ORDER.get(risk, 99))


def _portfolio_metrics(accounts: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(accounts)
    sum_field = lambda field: round(sum(float(item.get(field, 0)) for item in rows), 2)
    return {
        "account_count": len(rows),
        "sold_hours": sum_field("sold_hours"),
        "billed_hours": sum_field("billed_hours"),
        "remaining_hours": sum_field("remaining_hours"),
        "at_risk_hours": sum_field("at_risk_hours"),
        "expired_unused_hours": sum_field("expired_unused_hours"),
        "future_entitlement_hours": sum_field("future_entitlement_hours"),
        "overage_hours": sum_field("overage_hours"),
        "unapplied_correction_hours": sum_field("unapplied_correction_hours"),
        "weekly_account_gaps": sum(1 for item in rows if not item["weekly"]["account_active_current"]),
        "weekly_aiom_gaps": sum(1 for item in rows if not item["weekly"]["aiom_active_current"]),
    }


def _risk_distribution(accounts: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows = list(accounts)
    return [
        {"risk": risk, "accounts": sum(1 for item in rows if item["risk"] == risk), "remaining_hours": round(sum(float(item.get("remaining_hours", 0)) for item in rows if item["risk"] == risk), 2)}
        for risk in ["overage", "expired", "critical", "high", "medium", "healthy", "exhausted", "none"]
    ]
