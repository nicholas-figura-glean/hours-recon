"""Infer sold-hour package grants from Salesforce opportunities and line items."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .dates import add_one_year, parse_date

PACKAGE_WORDS = re.compile(r"\b(outcomes?|aiom|growth|professional services|ps)\b", re.I)
EXPLICIT_HOURS = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:ps\s*)?hours?\b", re.I)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except InvalidOperation:
        return Decimal(default)


def _override(config: Mapping[str, Any], kind: str, identifier: Any) -> Optional[Decimal]:
    value = config.get("overrides", {}).get(kind, {}).get(str(identifier))
    if isinstance(value, Mapping):
        value = value.get("hours")
    return _decimal(value) if value is not None else None


def infer_text(
    text: str,
    *,
    unit_price: Any,
    list_price: Any,
    config: Mapping[str, Any],
) -> Optional[Tuple[Decimal, str, str, str]]:
    """Return hours, family, tier, source. None means this is not a recognized package."""
    normalized = " ".join((text or "").lower().split())
    if not PACKAGE_WORDS.search(normalized) and "package" not in normalized:
        return None

    explicit = EXPLICIT_HOURS.search(normalized)
    if explicit:
        hours = _decimal(explicit.group(1))
        family = "growth" if "growth" in normalized else "outcome"
        return hours, family, f"{hours.normalize()} hours", "explicit_hours"

    for tier, hours_value in config.get("outcome_tiers", {}).items():
        if re.search(rf"\b{re.escape(tier.lower())}\b", normalized):
            return _decimal(hours_value), "outcome", tier.title(), "tier_name"

    if "growth" in normalized:
        allowed = sorted((int(value) for value in config.get("growth_hours", [])), reverse=True)
        for hours_value in allowed:
            if re.search(rf"\b{hours_value}\b", normalized):
                return _decimal(hours_value), "growth", str(hours_value), "growth_tier"

    if "custom" in normalized:
        return Decimal("0"), "custom", "Custom", "unresolved_custom"

    price = _decimal(list_price or unit_price)
    price_map = config.get("outcome_list_prices", {})
    price_key = str(int(price)) if price == price.to_integral_value() else str(price)
    if price_key in price_map and ("outcome" in normalized or "aiom" in normalized):
        hours = _decimal(price_map[price_key])
        return hours, "outcome", f"Price ${price_key}", "list_price"
    return None


def infer_packages(opportunity: Mapping[str, Any], config: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    packages: List[Dict[str, Any]] = []
    exceptions: List[Dict[str, Any]] = []
    opportunity_id = str(opportunity["id"])
    close_date = parse_date(opportunity["close_date"])
    expiration = add_one_year(close_date)

    opportunity_override = _override(config, "opportunities", opportunity_id)
    if opportunity_override is not None:
        packages.append(_package(opportunity, None, opportunity_override, "custom", "Override", "opportunity_override", close_date, expiration))
        return packages, exceptions

    recognized_line_items = 0
    pending_exceptions: List[Dict[str, Any]] = []
    for line_item in opportunity.get("line_items", []):
        line_id = str(line_item.get("id", ""))
        override = _override(config, "line_items", line_id)
        if override is None:
            product_override = config.get("overrides", {}).get("product_names", {}).get(str(line_item.get("name", "")))
            override = _decimal(product_override) if product_override is not None else None
        if override is not None:
            result = (override, "custom", "Override", "line_item_override")
        else:
            # Infer each line from its own product evidence only. Appending the
            # opportunity name here can multiply one opportunity-level package
            # hint across unrelated subscription/support lines.
            result = infer_text(
                " ".join(filter(None, [str(line_item.get("name", "")), str(line_item.get("product_code", ""))])),
                unit_price=line_item.get("unit_price"),
                list_price=line_item.get("list_price"),
                config=config,
            )
        if result is None:
            continue
        recognized_line_items += 1
        hours, family, tier, source = result
        quantity = _decimal(line_item.get("quantity"), "1")
        sold = hours * quantity
        if source == "unresolved_custom" or sold <= 0:
            pending_exceptions.append(_package_exception(opportunity, line_item, "Custom package needs an hours override."))
            continue
        packages.append(_package(opportunity, line_item, sold, family, tier, source, close_date, expiration, quantity))

    # Opportunity names are a fallback only. They cover legacy records without
    # products and named custom packages such as "Custom 300 PS hours". If any
    # line item resolved successfully, do not also count the opportunity hint.
    if not packages:
        result = infer_text(str(opportunity.get("name", "")), unit_price=None, list_price=None, config=config)
        if result:
            hours, family, tier, source = result
            if source == "unresolved_custom" or hours <= 0:
                exceptions.extend(pending_exceptions or [_package_exception(opportunity, None, "Custom package needs an hours override.")])
            else:
                packages.append(_package(opportunity, None, hours, family, tier, f"opportunity_{source}", close_date, expiration))
        elif recognized_line_items:
            exceptions.extend(pending_exceptions)
    else:
        exceptions.extend(pending_exceptions)
    return packages, exceptions


def _package(
    opportunity: Mapping[str, Any],
    line_item: Optional[Mapping[str, Any]],
    sold: Decimal,
    family: str,
    tier: str,
    source: str,
    close_date: Any,
    expiration: Any,
    quantity: Decimal = Decimal("1"),
) -> Dict[str, Any]:
    line_id = str(line_item.get("id")) if line_item else "opportunity"
    return {
        "id": f"{opportunity['id']}:{line_id}",
        "opportunity_id": str(opportunity["id"]),
        "opportunity_name": opportunity.get("name"),
        "line_item_id": str(line_item.get("id")) if line_item else None,
        "line_item_name": line_item.get("name") if line_item else None,
        "family": family,
        "tier": tier,
        "quantity": float(quantity),
        "sold_hours": float(sold),
        "close_date": close_date.isoformat(),
        "expiration_date": expiration.isoformat(),
        "inference_source": source,
    }


def _package_exception(opportunity: Mapping[str, Any], line_item: Optional[Mapping[str, Any]], message: str) -> Dict[str, Any]:
    return {
        "type": "unresolved_package",
        "account_id": str(opportunity.get("account_id", "")),
        "account_name": opportunity.get("account_name"),
        "opportunity_id": str(opportunity.get("id", "")),
        "opportunity_name": opportunity.get("name"),
        "line_item_id": str(line_item.get("id")) if line_item else None,
        "line_item_name": line_item.get("name") if line_item else None,
        "message": message,
    }
