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


def _product_code_mapping(line_item: Mapping[str, Any], config: Mapping[str, Any]) -> Optional[Tuple[Decimal, str, str, str]]:
    product_code = str(line_item.get("product_code") or "")
    configured = config.get("product_codes", {}).get(product_code)
    if configured is None:
        return None
    if isinstance(configured, Mapping):
        hours = _decimal(configured.get("hours_per_unit", configured.get("hours")))
        family = str(configured.get("family") or "outcome")
        tier = str(configured.get("tier") or product_code)
    else:
        hours = _decimal(configured)
        family = "outcome"
        tier = product_code
    if hours <= 0:
        return None
    return hours, family, tier, "product_code"


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
        packages.append(_package(
            opportunity, None, opportunity_override, "custom", "Override", "opportunity_override",
            close_date, expiration, mapping_key=f"opportunity:{opportunity_id}",
        ))
        return packages, exceptions

    recognized_line_items = 0
    pending_exceptions: List[Dict[str, Any]] = []
    for line_item in opportunity.get("line_items", []):
        line_id = str(line_item.get("id", ""))
        mapping_key: Optional[str] = None
        result = _product_code_mapping(line_item, config)
        if result is not None:
            mapping_key = str(line_item.get("product_code") or "")
        else:
            override = _override(config, "line_items", line_id)
            if override is not None:
                mapping_key = f"line_item:{line_id}"
            else:
                product_name = str(line_item.get("name", ""))
                product_override = config.get("overrides", {}).get("product_names", {}).get(product_name)
                override = _decimal(product_override) if product_override is not None else None
                if override is not None:
                    mapping_key = f"product_name:{product_name}"
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
        packages.append(_package(
            opportunity, line_item, sold, family, tier, source, close_date, expiration, quantity,
            mapping_key=mapping_key,
        ))

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


def _service_period(
    opportunity: Mapping[str, Any],
    line_item: Optional[Mapping[str, Any]],
    close_date: Any,
    default_expiration: Any,
) -> Tuple[Any, Any, str]:
    line_start = line_item.get("service_start_date") if line_item else None
    line_end = line_item.get("service_end_date") if line_item else None
    opportunity_start = opportunity.get("service_start_date")
    opportunity_end = opportunity.get("service_end_date")
    start_raw = line_start or opportunity_start
    end_raw = line_end or opportunity_end
    if not start_raw and not end_raw:
        return close_date, default_expiration, "close_date_plus_one_year"
    try:
        start = parse_date(start_raw) if start_raw else close_date
        end = parse_date(end_raw) if end_raw else add_one_year(start)
    except (TypeError, ValueError):
        return close_date, default_expiration, "invalid_service_period"
    if end < start:
        return close_date, default_expiration, "invalid_service_period"
    if start_raw and end_raw:
        source = "line_item_explicit" if line_start and line_end else "opportunity_explicit"
    else:
        source = "partial_explicit"
    return start, end, source


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
    *,
    mapping_key: Optional[str] = None,
) -> Dict[str, Any]:
    line_id = str(line_item.get("id")) if line_item else "opportunity"
    service_start, service_end, service_period_source = _service_period(opportunity, line_item, close_date, expiration)
    return {
        "id": f"{opportunity['id']}:{line_id}",
        "opportunity_id": str(opportunity["id"]),
        "opportunity_name": opportunity.get("name"),
        "line_item_id": str(line_item.get("id")) if line_item else None,
        "line_item_name": line_item.get("name") if line_item else None,
        "line_item_source": str(line_item.get("source") or "opportunity_line_item") if line_item else None,
        "quote_id": str(line_item.get("quote_id")) if line_item and line_item.get("quote_id") else None,
        "product_id": str(line_item.get("product_id")) if line_item and line_item.get("product_id") else None,
        "product_code": str(line_item.get("product_code")) if line_item and line_item.get("product_code") else None,
        "pricebook_entry_id": str(line_item.get("pricebook_entry_id")) if line_item and line_item.get("pricebook_entry_id") else None,
        "family": family,
        "tier": tier,
        "quantity": float(quantity),
        "sold_hours": float(sold),
        "close_date": close_date.isoformat(),
        "service_start_date": service_start.isoformat(),
        "service_end_date": service_end.isoformat(),
        "expiration_date": expiration.isoformat(),
        "service_period_source": service_period_source,
        "inference_source": source,
        "mapping_key": mapping_key,
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
        "line_item_source": str(line_item.get("source") or "opportunity_line_item") if line_item else None,
        "quote_id": str(line_item.get("quote_id")) if line_item and line_item.get("quote_id") else None,
        "message": message,
    }
