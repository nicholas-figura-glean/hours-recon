"""Per-package consumption percentages and stable entitlement identity.

The reconciliation engine tracks hours in absolute terms: ``sold_hours``,
``consumed_hours``, ``remaining_hours``. Nothing in it expresses consumption as
a *percentage*, because no consumer needed one until threshold notifications.

Percentages live here rather than in ``reconcile`` for two reasons:

* ``reconcile`` is pure and its output is pinned by tests. Threshold alerting is
  a consumer of the reconciliation, not part of it.
* The interesting percentage is **per package**, not per account. An account
  holding one expired-unused package and one fully-consumed package is at 50%
  of total sold hours while the entitlement the customer is actually drawing
  down is exhausted. Alerting on the account-level ratio would systematically
  under-report the accounts that most need attention, so the package is the
  unit of measurement and the account is only a reporting rollup.

``attach_consumption`` follows the ``attach_attention`` contract exactly: it is
idempotent and safe to re-apply to a report loaded from cache, so a report
produced by an earlier build gains these fields on load instead of rendering
without them.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

# Ladder rungs are policy, not math, and live in config/notification_policy.json.
# This module only answers "what percentage is this entitlement at".


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except Exception:  # pragma: no cover - defensive against malformed input
        return Decimal("0")


def _round(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def package_sold_hours(package: Mapping[str, Any]) -> Decimal:
    return _decimal(package.get("sold_hours"))


def package_consumed_hours(package: Mapping[str, Any]) -> Decimal:
    """Hours drawn from this package.

    ``_allocate_account`` records the authoritative figure as ``consumed_hours``
    from the FIFO walk. ``sold - remaining`` is only a fallback for a package
    dict that has not been through allocation; the two agree after allocation,
    and preferring the recorded value avoids re-deriving allocation arithmetic
    that has already been validated.
    """
    if package.get("consumed_hours") is not None:
        return _decimal(package.get("consumed_hours"))
    sold = package_sold_hours(package)
    remaining = _decimal(package.get("remaining_hours"))
    return max(sold - remaining, Decimal("0"))


def consumption_pct(consumed: Decimal, sold: Decimal) -> Optional[float]:
    """Percent of an entitlement consumed, or None when undefined.

    A package with no sold hours has no meaningful percentage. Returning None
    rather than 0 or 100 keeps "unknown" distinguishable from "untouched", so
    threshold detection can skip it instead of alerting on a divide-by-zero
    artifact. Unresolved packages are already surfaced through the existing
    ``unresolved_package`` exception path.
    """
    if sold <= 0:
        return None
    return _round(Decimal("100") * consumed / sold)


def _fingerprint(*parts: Any) -> str:
    canonical = "\n".join(str(part) for part in parts)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _hours_token(value: Decimal) -> str:
    """Stable text for an hours figure so keys do not drift on float noise."""
    return f"{value.quantize(Decimal('0.0001')):f}"


def entitlement_key(package: Mapping[str, Any]) -> str:
    """Identity of the *economics* of an entitlement.

    Deliberately excludes the package ID. The key changes when sold hours, the
    service window, or the package family/tier change, which is what makes a
    renewal re-arm the threshold ladder: new entitlement, new key, thresholds
    fire again from 50%.
    """
    return _fingerprint(
        _hours_token(package_sold_hours(package)),
        package.get("close_date") or "",
        package.get("expiration_date") or "",
        package.get("family") or "",
        package.get("tier") or "",
    )


def economic_key(package: Mapping[str, Any]) -> str:
    """Identity used to follow an entitlement across a package-ID rotation.

    ``package["id"]`` is ``f"{opportunity_id}:{line_item_id}"``, and the
    line-item source is chosen by precedence: OpportunityLineItems beat the
    approved quote, which beats the primary quote. When an opportunity gains
    real line items, or its approved quote changes, the package ID rotates even
    though the customer's entitlement is unchanged. Without this key the ladder
    would re-arm and re-notify hours that were already reported, so
    ``resolve_prior`` uses it to carry the high-water mark forward.
    """
    return _fingerprint(package.get("opportunity_id") or "", entitlement_key(package))


def attach_consumption(report: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Derive consumption percentages and entitlement identity in place.

    Idempotent by construction: every value is recomputed from the hours already
    on the report, so repeated application (including to a cached report) yields
    an identical result.
    """
    for account in report.get("accounts", []) or []:
        packages = account.get("packages") or []
        for package in packages:
            sold = package_sold_hours(package)
            consumed = package_consumed_hours(package)
            package["consumption_pct"] = consumption_pct(consumed, sold)
            package["entitlement_key"] = entitlement_key(package)
            package["economic_key"] = economic_key(package)

        account_sold = _decimal(account.get("sold_hours"))
        account_consumed = _decimal(account.get("consumed_hours"))
        account["consumption_pct"] = consumption_pct(account_consumed, account_sold)
        rungs = [
            package["consumption_pct"]
            for package in packages
            if package.get("consumption_pct") is not None
        ]
        # The rollup a person reads is the account ratio; the figure that drives
        # alerting is the worst package, so both are recorded explicitly.
        account["max_package_consumption_pct"] = max(rungs) if rungs else None
    return report


def package_label(package: Mapping[str, Any]) -> str:
    """Human-readable package name for an email body.

    Prefers the product name over ``family``/``tier`` because the inferred tier
    is not always presentation-safe: ``infer_text`` builds it with
    ``Decimal.normalize()``, so a 20-hour growth package yields the tier string
    ``"2E+1 hours"``. That is a pre-existing cosmetic issue in inference which
    this module deliberately does not change, since ``tier`` also participates
    in entitlement identity and is rendered by the dashboard.
    """
    line_item_name = str(package.get("line_item_name") or "").strip()
    if line_item_name:
        return line_item_name
    composed = " ".join(
        part for part in [str(package.get("family") or ""), str(package.get("tier") or "")] if part
    ).strip()
    if composed:
        return composed
    return str(package.get("opportunity_name") or package.get("id") or "Unnamed package")


def package_rows(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Flatten to one row per package for threshold evaluation and rendering."""
    rows: List[Dict[str, Any]] = []
    for account in report.get("accounts", []) or []:
        for package in account.get("packages") or []:
            rows.append({
                "account_id": str(account.get("id") or ""),
                "account_name": str(account.get("name") or ""),
                "account_owner_email": str(account.get("owner_email") or "").strip().lower(),
                "account_owner_name": str(account.get("owner_name") or ""),
                "package_id": str(package.get("id") or ""),
                "package_label": package_label(package),
                "opportunity_id": str(package.get("opportunity_id") or ""),
                "entitlement_key": str(package.get("entitlement_key") or entitlement_key(package)),
                "economic_key": str(package.get("economic_key") or economic_key(package)),
                "consumption_pct": package.get("consumption_pct"),
                "consumed_hours": _round(package_consumed_hours(package)),
                "sold_hours": _round(package_sold_hours(package)),
                "remaining_hours": _round(_decimal(package.get("remaining_hours"))),
                "expiration_date": package.get("expiration_date"),
                "days_to_expiration": package.get("days_to_expiration"),
                "overage_hours": float(account.get("overage_hours") or 0.0),
                "unapplied_correction_hours": float(account.get("unapplied_correction_hours") or 0.0),
            })
    return rows
