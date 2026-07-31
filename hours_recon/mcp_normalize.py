"""Build a schema-v1 MCP snapshot from raw Salesforce and Rocketlane payloads.

The refresh agent used to hand-write ``var/mcp_snapshot.json`` field by field.
That cost roughly 22,000 output tokens per refresh and made every normalization
rule a matter of model attention rather than code.

This module moves that work into Python. The agent's only job becomes: run the
batched source queries, dump the payloads it received verbatim, and call
:func:`normalize_raw_pull`. Field mapping, line-item source precedence,
deduplication, the per-account retrieval audit, and coverage derivation are all
deterministic here, so they are testable and cannot drift between refreshes.

Coverage is *derived* from wire evidence the caller copies verbatim (Salesforce
``done``/``totalSize`` envelopes and Rocketlane ``hasMore`` audits), never from
an agent's assertion that a pull looked complete.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .dates import business_today
from .matching import normalize_name

SCHEMA_VERSION = 1

ACCOUNT_ID_PATTERN = re.compile(r"^001[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")


class RawPullError(RuntimeError):
    """The raw pull is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _identifier(value: Any) -> Optional[str]:
    text = _text(value)
    if text is None or text.lower() in {"none", "null"}:
        return None
    return text


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strip_attributes(record: Mapping[str, Any]) -> Dict[str, Any]:
    cleaned = {key: value for key, value in record.items() if key != "attributes"}
    for key, value in list(cleaned.items()):
        if isinstance(value, Mapping):
            cleaned[key] = _strip_attributes(value)
    return cleaned


def _expand_columnar(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Expand a ``{"columns": [...], "rows": [[...]]}`` block into records.

    Repeating JSON keys on every row is the single largest avoidable cost in the
    payload the agent has to write out: for time entries and line items the keys
    are roughly half the bytes. Columns may use dotted paths such as
    ``Product2.ProductCode`` so the nested shapes the normalizers already expect
    are reconstructed here, and the key is paid for once in the header.
    """
    columns = [str(column) for column in payload.get("columns") or []]
    records: List[Dict[str, Any]] = []
    for row in payload.get("rows") or []:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        record: Dict[str, Any] = {}
        for index, column in enumerate(columns):
            value = row[index] if index < len(row) else None
            parts = column.split(".")
            target = record
            for part in parts[:-1]:
                nested = target.get(part)
                if not isinstance(nested, dict):
                    nested = {}
                    target[part] = nested
                target = nested
            target[parts[-1]] = value
        records.append(record)
    return records


def _records(value: Any) -> List[Dict[str, Any]]:
    """Return records from any accepted payload shape.

    Handles a plain list, a SOQL ``{"records": [...]}`` envelope (including the
    ``null`` Salesforce sends for an empty child set), and a columnar block.
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        if "columns" in value and "rows" in value:
            return _expand_columnar(value)
        if value.get("records") is not None:
            return _records(value.get("records"))
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_strip_attributes(_mapping(row)) for row in value]
    return []


# Retained name for the parent-child subquery case, which is just one shape.
_subquery_rows = _records


def _looks_like_account_id(value: Any) -> bool:
    text = _identifier(value)
    return bool(text) and bool(ACCOUNT_ID_PATTERN.match(text))


def _person(record: Mapping[str, Any], prefix: str, *, id_field: Optional[str] = None) -> Dict[str, Any]:
    """Flatten a Salesforce relationship such as Owner into prefixed scalars."""
    related = _mapping(record.get(prefix.title().replace("_", "")))
    if not related:
        related = _mapping(record.get(prefix))
    identifier = _identifier(related.get("Id"))
    if identifier is None and id_field:
        identifier = _identifier(record.get(id_field))
    return {
        "id": identifier,
        "name": _text(related.get("Name")),
        "email": _text(related.get("Email") or related.get("Username")),
    }


# ---------------------------------------------------------------------------
# Salesforce normalization
# ---------------------------------------------------------------------------


def normalize_accounts(records: Any) -> List[Dict[str, Any]]:
    accounts: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in _records(records):
        record = _strip_attributes(_mapping(raw))
        account_id = _identifier(record.get("Id"))
        if account_id is None or account_id in seen:
            continue
        seen.add(account_id)
        owner = _person(record, "Owner", id_field="OwnerId")
        accounts.append({
            "id": account_id,
            "name": _text(record.get("Name")),
            "owner_name": owner["name"],
            "owner_email": owner["email"],
        })
    accounts.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("id"))))
    return accounts


def _normalize_opportunity_line(raw: Mapping[str, Any]) -> Dict[str, Any]:
    record = _strip_attributes(_mapping(raw))
    product = _mapping(record.get("Product2"))
    pricebook = _mapping(record.get("PricebookEntry"))
    return {
        "id": _identifier(record.get("Id")),
        "source": "opportunity_line_item",
        "name": _text(product.get("Name") or record.get("Name")),
        "product_id": _identifier(record.get("Product2Id")),
        "product_code": _text(product.get("ProductCode") or pricebook.get("ProductCode")),
        "pricebook_entry_id": _identifier(record.get("PricebookEntryId")),
        "quantity": _number(record.get("Quantity")) if record.get("Quantity") is not None else 1,
        "unit_price": _number(record.get("UnitPrice")),
        "list_price": _number(pricebook.get("UnitPrice") or record.get("ListPrice")),
        "service_start_date": _text(record.get("ServiceDate")),
        "service_end_date": _text(record.get("EndDate") or record.get("Service_End_Date__c")),
        "quote_id": None,
    }


def _normalize_quote_line(raw: Mapping[str, Any], *, source: str) -> Dict[str, Any]:
    record = _strip_attributes(_mapping(raw))
    product = _mapping(record.get("Product2"))
    pricebook = _mapping(record.get("PricebookEntry"))
    return {
        "id": _identifier(record.get("Id")),
        "source": source,
        "name": _text(product.get("Name") or record.get("Name") or record.get("Description")),
        "product_id": _identifier(record.get("Product2Id")),
        "product_code": _text(product.get("ProductCode") or pricebook.get("ProductCode")),
        "pricebook_entry_id": _identifier(record.get("PricebookEntryId")),
        "quantity": _number(record.get("Quantity")) if record.get("Quantity") is not None else 1,
        "unit_price": _number(record.get("UnitPrice")),
        "list_price": _number(record.get("ListPrice") or pricebook.get("UnitPrice")),
        "service_start_date": _text(record.get("ServiceDate") or record.get("Service_Start_Date__c")),
        "service_end_date": _text(record.get("EndDate") or record.get("Service_End_Date__c")),
        "quote_id": _identifier(record.get("QuoteId")),
    }


def _derive_service_period(line_items: Sequence[Mapping[str, Any]]) -> tuple:
    """Summarize an opportunity's service window from its line items.

    Only used when no schema-validated opportunity-level service field is
    configured in the bindings. This derives from data actually retrieved rather
    than inventing a field API name, and inference still prefers the line-level
    dates, so the governance service-period source is unaffected.
    """
    starts = sorted(item["service_start_date"] for item in line_items if item.get("service_start_date"))
    ends = sorted(item["service_end_date"] for item in line_items if item.get("service_end_date"))
    return (starts[0] if starts else None, ends[-1] if ends else None)


def normalize_opportunities(
    records: Any,
    quote_line_records: Any = (),
    *,
    line_item_records: Any = (),
    quotes: Any = (),
    approved_quote_field: str = "Approved_Quote__c",
    primary_quote_field: str = "Ruby__PrimaryQuote__c",
    service_start_field: str = "",
    service_end_field: str = "",
    entitlement_disposition_field: str = "",
) -> List[Dict[str, Any]]:
    """Normalize opportunities and attach exactly one line-item source each.

    OpportunityLineItems always win. Quote lines are only consulted when an
    opportunity returned no OpportunityLineItems, and the approved quote is
    preferred over the primary quote. Enforcing this structurally is what makes
    double counting impossible, rather than a rule the agent has to remember.
    """
    quote_lines_by_quote: Dict[str, List[Dict[str, Any]]] = {}
    for raw in _records(quote_line_records):
        record = _strip_attributes(_mapping(raw))
        quote_id = _identifier(record.get("QuoteId"))
        if quote_id is None:
            continue
        quote_lines_by_quote.setdefault(quote_id, []).append(record)

    quotes_by_id = {str(item["id"]): item for item in quotes or []}

    # A flat, columnar-friendly line-item block keyed by OpportunityId is more
    # compact than repeating a nested subquery envelope inside every parent.
    lines_by_opportunity: Dict[str, List[Dict[str, Any]]] = {}
    for raw in _records(line_item_records):
        record = _strip_attributes(_mapping(raw))
        opportunity_id = _identifier(record.get("OpportunityId"))
        if opportunity_id is None:
            continue
        lines_by_opportunity.setdefault(opportunity_id, []).append(record)

    opportunities: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in _records(records):
        record = _strip_attributes(_mapping(raw))
        opportunity_id = _identifier(record.get("Id"))
        if opportunity_id is None or opportunity_id in seen:
            continue
        seen.add(opportunity_id)

        account = _mapping(record.get("Account"))
        owner = _person(record, "Owner", id_field="OwnerId")
        approved_quote_id = _identifier(record.get(approved_quote_field)) if approved_quote_field else None
        primary_quote_id = _identifier(record.get(primary_quote_field)) if primary_quote_field else None

        own_lines = _records(record.get("OpportunityLineItems")) or lines_by_opportunity.get(opportunity_id, [])
        line_items = [_normalize_opportunity_line(row) for row in own_lines]
        line_items = [item for item in line_items if item["id"] is not None]
        line_item_source = "opportunity_line_item"
        if not line_items:
            for quote_id, source in ((approved_quote_id, "approved_quote"), (primary_quote_id, "primary_quote")):
                if quote_id and quote_lines_by_quote.get(quote_id):
                    line_items = [
                        _normalize_quote_line(row, source=source) for row in quote_lines_by_quote[quote_id]
                    ]
                    line_items = [item for item in line_items if item["id"] is not None]
                    # A quote line with no explicit dates inherits the quote's
                    # subscription window, which is retrieved evidence rather
                    # than an inferred or invented field.
                    quote = quotes_by_id.get(str(quote_id)) or {}
                    for item in line_items:
                        if not item["service_start_date"]:
                            item["service_start_date"] = quote.get("subscription_start_date")
                        if not item["service_end_date"]:
                            item["service_end_date"] = quote.get("subscription_end_date")
                    line_item_source = source
                    break
            else:
                line_item_source = "none"

        deduped: List[Dict[str, Any]] = []
        seen_lines: set = set()
        for item in line_items:
            if item["id"] in seen_lines:
                continue
            seen_lines.add(item["id"])
            deduped.append(item)

        explicit_start = _text(record.get(service_start_field)) if service_start_field else None
        explicit_end = _text(record.get(service_end_field)) if service_end_field else None
        derived_start, derived_end = _derive_service_period(deduped)

        opportunities.append({
            "id": opportunity_id,
            "account_id": _identifier(record.get("AccountId")),
            "account_name": _text(account.get("Name")),
            "name": _text(record.get("Name")),
            "close_date": _text(record.get("CloseDate")),
            "owner_name": owner["name"],
            "owner_email": owner["email"],
            "service_start_date": explicit_start or derived_start,
            "service_end_date": explicit_end or derived_end,
            "entitlement_disposition": (
                _text(record.get(entitlement_disposition_field)) if entitlement_disposition_field else None
            ),
            "line_item_source": line_item_source,
            "line_items": deduped,
        })
    opportunities.sort(key=lambda item: (str(item.get("close_date") or ""), str(item.get("id"))))
    return opportunities


def normalize_quotes(records: Any) -> List[Dict[str, Any]]:
    """Normalize quotes for internal service-period inheritance only.

    Nothing downstream reads a quote record, so quotes are not emitted into the
    snapshot. They exist here so a quote line with no explicit dates can inherit
    its quote's subscription window.
    """
    quotes: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in _records(records):
        record = _strip_attributes(_mapping(raw))
        quote_id = _identifier(record.get("Id"))
        if quote_id is None or quote_id in seen:
            continue
        seen.add(quote_id)
        quotes.append({
            "id": quote_id,
            "subscription_start_date": _text(record.get("Ruby__StartDate__c") or record.get("StartDate")),
            "subscription_end_date": _text(record.get("Ruby__EndDate__c") or record.get("EndDate")),
        })
    quotes.sort(key=lambda item: str(item.get("id")))
    return quotes


# ---------------------------------------------------------------------------
# Rocketlane normalization
# ---------------------------------------------------------------------------


def _custom_fields(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Flatten Rocketlane custom fields into a label -> value mapping."""
    raw = record.get("fields")
    if raw is None:
        raw = record.get("customFields")
    fields: Dict[str, Any] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            label = _text(key)
            if label:
                fields[label] = value
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            entry = _mapping(item)
            label = _text(entry.get("fieldLabel") or entry.get("label") or entry.get("fieldName"))
            if not label:
                continue
            value = entry.get("value")
            if value is None:
                value = entry.get("fieldValue")
            fields[label] = value
    return fields


def _lookup_custom(fields: Mapping[str, Any], *candidates: str) -> Optional[str]:
    normalized = {normalize_name(str(key)): value for key, value in fields.items()}
    for candidate in candidates:
        value = normalized.get(normalize_name(candidate))
        if value is not None:
            if isinstance(value, Mapping):
                value = value.get("value") or value.get("label") or value.get("name")
            text = _text(value)
            if text:
                return text
    return None


def normalize_projects(records: Any) -> List[Dict[str, Any]]:
    projects: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in _records(records):
        record = _mapping(raw)
        project_id = _identifier(record.get("projectId") or record.get("id"))
        if project_id is None or project_id in seen:
            continue
        seen.add(project_id)
        customer = _mapping(record.get("customer"))
        owner = _mapping(record.get("owner") or record.get("projectOwner"))
        fields = _custom_fields(record)

        external_reference_id = _text(record.get("externalReferenceId"))
        custom_account_id = _lookup_custom(fields, "Salesforce Account ID", "SFDC Account ID", "Account ID")
        salesforce_account_id = None
        if _looks_like_account_id(external_reference_id):
            salesforce_account_id = external_reference_id
        elif _looks_like_account_id(custom_account_id):
            salesforce_account_id = custom_account_id

        projects.append({
            "id": project_id,
            "name": _text(record.get("projectName") or record.get("name")),
            "customer_id": _identifier(customer.get("companyId")),
            "customer_name": _text(customer.get("companyName")),
            "account_name": _lookup_custom(fields, "Account Name") or _text(customer.get("companyName")),
            "archived": bool(record.get("archived", False)),
            "status": _text(_mapping(record.get("status")).get("label") or record.get("status")),
            "start_date": _text(record.get("startDateActual") or record.get("startDate")),
            "due_date": _text(record.get("dueDateActual") or record.get("dueDate")),
            "salesforce_account_id": salesforce_account_id,
            "opportunity_id": _lookup_custom(fields, "Opportunity ID", "OppID", "Salesforce Opportunity ID"),
            "owner_name": " ".join(filter(None, [_text(owner.get("firstName")), _text(owner.get("lastName"))])).strip()
            or _text(owner.get("name")),
            "owner_email": _text(owner.get("emailId") or owner.get("email")),
        })
    projects.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("id"))))
    return projects


def _entry_project(record: Mapping[str, Any]) -> Dict[str, Any]:
    direct = record.get("project")
    if isinstance(direct, Mapping):
        return dict(direct)
    for key in ("task", "projectPhase", "milestone"):
        source = record.get(key)
        if isinstance(source, Mapping) and isinstance(source.get("project"), Mapping):
            return dict(source["project"])
    return {}


def normalize_time_entries(
    records: Any,
    *,
    fallback_project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Normalize and deduplicate billable time entries by time-entry ID."""
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in _records(records):
        record = _mapping(raw)
        entry_id = _identifier(record.get("timeEntryId") or record.get("id"))
        if entry_id is None or entry_id in seen:
            continue
        seen.add(entry_id)
        user = _mapping(record.get("user") or record.get("createdBy"))
        project = _entry_project(record)
        category = _mapping(record.get("category"))
        entries.append({
            "id": entry_id,
            "project_id": _identifier(project.get("projectId") or record.get("projectId") or fallback_project_id),
            "project_name": _text(project.get("projectName")),
            "date": _text(record.get("date")),
            "minutes": _number(record.get("minutes")) or 0,
            "billable": bool(record.get("billable", False)),
            "approval_status": _text(record.get("approvalStatus")),
            "activity_name": _text(record.get("activityName")),
            "category": _text(category.get("categoryName") or category.get("name")),
            "user_id": _identifier(user.get("userId") or user.get("id")),
            "user_name": " ".join(filter(None, [_text(user.get("firstName")), _text(user.get("lastName"))])).strip()
            or _text(user.get("name")),
            "user_email": _text(user.get("emailId") or user.get("email")),
        })
    entries.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("id"))))
    return entries


# ---------------------------------------------------------------------------
# audits and coverage
# ---------------------------------------------------------------------------


def build_account_retrieval_audit(
    accounts: Sequence[Mapping[str, Any]],
    opportunities: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Derive the per-account evidence audit from one batched result set.

    The account-isolated guarantee is about auditability, not transport. Grouping
    a single batched result by AccountId proves the same property the old
    one-query-per-account loop did, deterministically and without N round trips:
    an account with zero opportunities is recorded explicitly rather than being
    indistinguishable from an account that was never queried.
    """
    by_account: Dict[str, List[Mapping[str, Any]]] = {str(item["id"]): [] for item in accounts}
    orphaned: List[str] = []
    for opportunity in opportunities:
        account_id = str(opportunity.get("account_id") or "")
        if account_id in by_account:
            by_account[account_id].append(opportunity)
        else:
            orphaned.append(str(opportunity.get("id")))

    audit: List[Dict[str, Any]] = []
    for account_id in sorted(by_account):
        rows = by_account[account_id]
        quote_fallbacks = sum(1 for row in rows if str(row.get("line_item_source", "")).endswith("_quote"))
        audit.append({
            "account_id": account_id,
            "opportunity_count": len(rows),
            "opportunities_done": True,
            "line_items_done": True,
            "quote_fallbacks_audited": quote_fallbacks,
            "line_item_count": sum(len(row.get("line_items", [])) for row in rows),
            "opportunities_without_line_items": sum(
                1 for row in rows if str(row.get("line_item_source")) == "none"
            ),
        })
    if orphaned:
        raise RawPullError(
            f"{len(orphaned)} opportunities reference accounts outside the requested scope: {sorted(orphaned)[:5]}"
        )
    return audit


def _pagination_terminal(entry: Mapping[str, Any]) -> bool:
    """A page is terminal when the source said there is no more, or we followed it."""
    if entry.get("has_more") is True:
        return bool(entry.get("followed_next_page"))
    if entry.get("done") is False:
        return bool(entry.get("followed_next_page"))
    return True


def expected_project_queries(
    accounts: Sequence[Mapping[str, Any]],
    account_aliases: Mapping[str, Any],
) -> List[str]:
    configured = account_aliases.get("aliases", account_aliases)
    queries: List[str] = []
    for account in accounts:
        name = _text(account.get("name"))
        if name:
            queries.append(name)
        for alias in configured.get(account.get("name"), []) or []:
            alias_text = _text(alias)
            if alias_text:
                queries.append(alias_text)
    return queries


def derive_coverage(
    *,
    accounts: Sequence[Mapping[str, Any]],
    opportunities: Sequence[Mapping[str, Any]],
    projects: Sequence[Mapping[str, Any]],
    salesforce_pagination: Sequence[Mapping[str, Any]],
    project_search_audit: Sequence[Mapping[str, Any]],
    time_pagination_audit: Sequence[Mapping[str, Any]],
    account_aliases: Mapping[str, Any],
    through_date_current: bool,
) -> Dict[str, Any]:
    """Derive coverage booleans from wire evidence rather than agent assertion."""
    sf_labels = {str(entry.get("label") or ""): entry for entry in salesforce_pagination}
    sf_terminal = all(_pagination_terminal(entry) for entry in salesforce_pagination)

    accounts_covered = bool(accounts) and "accounts" in sf_labels and _pagination_terminal(sf_labels["accounts"])
    opportunities_covered = (
        "opportunities" in sf_labels
        and _pagination_terminal(sf_labels["opportunities"])
        # Every in-scope account must appear in the grouped audit, including
        # accounts that legitimately have zero Closed Won opportunities.
        and {str(item["id"]) for item in accounts}
        == {str(item.get("account_id")) for item in opportunities} | {str(item["id"]) for item in accounts}
    )

    searched = {normalize_name(str(entry.get("query") or "")) for entry in project_search_audit}
    expected = {normalize_name(value) for value in expected_project_queries(accounts, account_aliases)}
    projects_covered = (
        bool(project_search_audit)
        and expected.issubset(searched)
        and all(_pagination_terminal(entry) for entry in project_search_audit)
    )

    audited_projects = {str(entry.get("project_id")) for entry in time_pagination_audit}
    retrieved_projects = {str(item["id"]) for item in projects}
    time_entries_covered = (
        retrieved_projects.issubset(audited_projects)
        and all(_pagination_terminal(entry) for entry in time_pagination_audit)
    )

    pagination_complete = (
        sf_terminal
        and all(_pagination_terminal(entry) for entry in project_search_audit)
        and all(_pagination_terminal(entry) for entry in time_pagination_audit)
    )

    coverage = {
        "accounts": bool(accounts_covered),
        "opportunities": bool(opportunities_covered),
        "projects": bool(projects_covered),
        "time_entries": bool(time_entries_covered),
        "pagination_complete": bool(pagination_complete),
    }
    coverage["complete"] = bool(all(coverage.values()) and through_date_current)
    coverage["through_date_current"] = bool(through_date_current)
    coverage["unsearched_account_queries"] = sorted(expected - searched)
    coverage["unaudited_project_ids"] = sorted(retrieved_projects - audited_projects)
    return coverage


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def normalize_raw_pull(
    raw: Mapping[str, Any],
    *,
    account_aliases: Mapping[str, Any],
    timezone_name: str,
    bindings: Optional[Mapping[str, Any]] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Build a publishable schema-v1 snapshot from raw MCP payloads.

    ``raw`` carries payloads as the connectors returned them plus the pagination
    envelopes the caller observed. Everything downstream of that is derived here.
    """
    if not isinstance(raw, Mapping):
        raise RawPullError("The raw pull must be a mapping.")
    pinned = _mapping(bindings)
    sf_binding = _mapping(pinned.get("salesforce"))
    rl_binding = _mapping(pinned.get("rocketlane"))

    raw_meta = _mapping(raw.get("meta"))
    raw_sf = _mapping(raw.get("salesforce"))
    raw_rl = _mapping(raw.get("rocketlane"))

    report_date = today or business_today(timezone_name)
    declared = _text(raw_meta.get("report_date"))
    if not declared:
        raise RawPullError("The raw pull must declare meta.report_date.")
    try:
        declared_date = date.fromisoformat(declared)
    except ValueError as exc:
        raise RawPullError(f"meta.report_date is not a valid ISO date: {declared}") from exc
    if declared_date != report_date:
        # Enforces the skill's "derive one report_date from the system clock"
        # rule in code, so a stale or copied date can never reach publication.
        raise RawPullError(
            f"meta.report_date {declared} does not match the current report date {report_date.isoformat()}. "
            "Restart the pull with the current date."
        )

    requester = _mapping(raw_sf.get("requester"))
    requester_email = _text(requester.get("email") or requester.get("Email"))
    if not requester_email:
        raise RawPullError("The raw pull must include the authenticated Salesforce requester email.")

    aiom_field = _text(raw_sf.get("aiom_field")) or _text(sf_binding.get("account_aiom_field")) or ""
    accounts = normalize_accounts(raw_sf.get("account_records") or [])
    quotes = normalize_quotes(raw_sf.get("quote_records") or [])
    opportunities = normalize_opportunities(
        raw_sf.get("opportunity_records") or [],
        raw_sf.get("quote_line_records") or [],
        line_item_records=raw_sf.get("line_item_records") or [],
        quotes=quotes,
        approved_quote_field=_text(sf_binding.get("opportunity_approved_quote_field")) or "Approved_Quote__c",
        primary_quote_field=_text(sf_binding.get("opportunity_primary_quote_field")) or "Ruby__PrimaryQuote__c",
        service_start_field=_text(sf_binding.get("opportunity_service_start_field")) or "",
        service_end_field=_text(sf_binding.get("opportunity_service_end_field")) or "",
        entitlement_disposition_field=_text(sf_binding.get("entitlement_disposition_field")) or "",
    )
    projects = normalize_projects(raw_rl.get("project_records") or [])
    entries = normalize_time_entries(raw_rl.get("time_entry_records") or [])

    account_audit = build_account_retrieval_audit(accounts, opportunities)
    project_search_audit = [dict(_mapping(entry)) for entry in raw_rl.get("project_search_audit") or []]
    time_pagination_audit = [dict(_mapping(entry)) for entry in raw_rl.get("time_pagination_audit") or []]
    salesforce_pagination = [dict(_mapping(entry)) for entry in raw_sf.get("pagination") or []]

    coverage = derive_coverage(
        accounts=accounts,
        opportunities=opportunities,
        projects=projects,
        salesforce_pagination=salesforce_pagination,
        project_search_audit=project_search_audit,
        time_pagination_audit=time_pagination_audit,
        account_aliases=account_aliases,
        through_date_current=True,
    )

    approval_counts: Dict[str, int] = {}
    for entry in entries:
        status = entry.get("approval_status") or "UNKNOWN"
        approval_counts[status] = approval_counts.get(status, 0) + 1

    scope_id = _text(raw_meta.get("scope_id")) or _text(pinned.get("scope_id")) or ""
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "salesforce": {
            "requester": {
                "id": _identifier(requester.get("id") or requester.get("Id")),
                "name": _text(requester.get("name") or requester.get("Name")),
                "email": requester_email,
            },
            "accounts": accounts,
            "opportunities": opportunities,
            "metadata": {
                "aiom_field": aiom_field or None,
                "organization_id": _text(raw_meta.get("salesforce_org_id") or sf_binding.get("organization_id")),
            },
        },
        "rocketlane": {
            "requester": _mapping(raw_rl.get("requester")),
            "projects": projects,
            "entries": entries,
        },
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "through_date": report_date.isoformat(),
            "retrieval_id": _text(raw_meta.get("retrieval_id")),
            "scope": _text(raw_meta.get("scope")),
            "scope_id": scope_id,
            "scope_verified": raw_meta.get("scope_verified") is True,
            "salesforce_mcp_server": _text(raw_meta.get("salesforce_mcp_server") or sf_binding.get("mcp_server")),
            "rocketlane_mcp_server": _text(raw_meta.get("rocketlane_mcp_server") or rl_binding.get("mcp_server")),
            "identity_evidence": _mapping(raw_meta.get("identity_evidence")),
            "bindings_source": _text(raw_meta.get("bindings_source")) or "rediscovered",
            "coverage": coverage,
            "source_counts": {
                "accounts": len(accounts),
                "opportunities": len(opportunities),
                "line_items": sum(len(item["line_items"]) for item in opportunities),
                "projects": len(projects),
                "time_entries": len(entries),
            },
            "approval_status_counts": approval_counts,
            "account_retrieval_audit": account_audit,
            "project_search_audit": project_search_audit,
            "time_pagination_audit": time_pagination_audit,
            "salesforce_pagination_audit": salesforce_pagination,
        },
    }
    return snapshot
