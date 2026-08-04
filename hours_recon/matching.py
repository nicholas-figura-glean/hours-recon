"""Conservative Salesforce-to-Rocketlane account matching."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company"}

# Salesforce key prefixes. Rocketlane custom fields are populated by humans and by
# integrations, so a field labelled "OppID" cannot be trusted to hold an
# Opportunity ID; validate the shape before using it as a join key.
ACCOUNT_ID_PREFIX = "001"
OPPORTUNITY_ID_PREFIX = "006"


def looks_like_salesforce_id(value: Any, prefix: str) -> bool:
    """True when value is a 15/18 character Salesforce ID with the given prefix."""
    text = str(value or "").strip()
    return len(text) in {15, 18} and text.startswith(prefix) and text.isalnum()


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\([^)]*\)", " ", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def leading_name_token(value: str) -> str:
    """First significant token of a normalized account name.

    Rocketlane names projects `<short name> | <package>` while Salesforce stores
    the legal entity name, and the Rocketlane project filter is a substring
    match. "Aderant North America, Inc." therefore never matches
    "Aderant | Standard Package", but its leading token does. Used as an
    escalation query when an account's own name finds nothing.
    """
    tokens = normalize_name(value).split()
    return tokens[0] if tokens else ""


def account_search_queries(name: str, aliases: Iterable[str] = ()) -> List[str]:
    """Ordered, de-duplicated Rocketlane project searches for one account.

    The account name and every configured alias are always searched. The leading
    token is the documented escalation when those return nothing; coverage
    requires it before an account with no project can be called covered.
    """
    queries: List[str] = []
    for candidate in [name, *aliases, leading_name_token(name)]:
        text = str(candidate or "").strip()
        if text and all(normalize_name(text) != normalize_name(existing) for existing in queries):
            queries.append(text)
    return queries


def _configured_aliases(aliases: Mapping[str, Any]) -> Mapping[str, Any]:
    return aliases.get("aliases", aliases)


def _opportunity_crosswalk(opportunities: Iterable[Mapping[str, Any]], account_ids: set) -> Dict[str, str]:
    """Map in-scope Opportunity ID -> Account ID.

    Rocketlane projects provisioned from Salesforce carry the originating
    Opportunity ID, which is an exact identifier for the account that owns it.
    Joining on it removes name matching entirely for those projects.
    """
    crosswalk: Dict[str, str] = {}
    conflicting: set = set()
    for opportunity in opportunities:
        opportunity_id = str(opportunity.get("id") or "").strip()
        account_id = str(opportunity.get("account_id") or "").strip()
        if not opportunity_id or account_id not in account_ids:
            continue
        existing = crosswalk.get(opportunity_id)
        if existing and existing != account_id:
            conflicting.add(opportunity_id)
            continue
        crosswalk[opportunity_id] = account_id
    for opportunity_id in conflicting:
        crosswalk.pop(opportunity_id, None)
    return crosswalk


def build_account_index(accounts: Iterable[Mapping[str, Any]], aliases: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Compatibility index used by existing callers."""
    configured = _configured_aliases(aliases)
    index: Dict[str, List[Dict[str, Any]]] = {}
    for raw_account in accounts:
        account = dict(raw_account)
        names = [str(account.get("name", ""))]
        names.extend(configured.get(account.get("name"), []))
        for name in names:
            key = normalize_name(name)
            if key:
                bucket = index.setdefault(key, [])
                if all(str(existing.get("id")) != str(account.get("id")) for existing in bucket):
                    bucket.append(account)
    return index


def _build_provenance_index(accounts: Iterable[Mapping[str, Any]], aliases: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    configured = _configured_aliases(aliases)
    index: Dict[str, List[Dict[str, Any]]] = {}
    for raw_account in accounts:
        account = dict(raw_account)
        candidates = [(str(account.get("name", "")), "normalized_customer_name")]
        candidates.extend((str(value), "configured_alias") for value in configured.get(account.get("name"), []))
        for name, basis in candidates:
            key = normalize_name(name)
            if not key:
                continue
            bucket = index.setdefault(key, [])
            if all(str(existing["account"].get("id")) != str(account.get("id")) for existing in bucket):
                bucket.append({"account": account, "basis": basis, "matched_value": name})
    return index


def _customer_id_crosswalk(
    accounts: Iterable[Mapping[str, Any]], aliases: Mapping[str, Any]
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    configured = aliases.get("rocketlane_customer_ids", {})
    account_ids_by_name: Dict[str, set] = {}
    account_ids = {str(item.get("id")) for item in accounts}
    for item in accounts:
        account_ids_by_name.setdefault(str(item.get("name")), set()).add(str(item.get("id")))
    candidates: Dict[str, set] = {}
    for account_key, customer_ids in configured.items():
        target_ids = {str(account_key)} if str(account_key) in account_ids else account_ids_by_name.get(str(account_key), set())
        if not target_ids:
            continue
        values = customer_ids if isinstance(customer_ids, list) else [customer_ids]
        for customer_id in values:
            key = str(customer_id)
            if key:
                candidates.setdefault(key, set()).update(target_ids)
    collisions = {key: sorted(values) for key, values in candidates.items() if len(values) > 1}
    result = {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}
    return result, collisions


def match_projects_with_evidence(
    accounts: Iterable[Mapping[str, Any]],
    projects: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, Any],
    opportunities: Iterable[Mapping[str, Any]] = (),
) -> Tuple[Dict[str, str], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Match projects and retain the exact basis used for governance scoring.

    Tier order, strongest first. Identifier joins are attempted before any name
    comparison so that name normalization and configured aliases are a fallback
    for records that carry no cross-system identifier, never the primary join:

    1. ``salesforce_account_id``      - Account ID stored on the Rocketlane record
    2. ``salesforce_opportunity_id``  - in-scope Opportunity ID resolved to its account
    3. ``rocketlane_customer_id_crosswalk`` - governed customer-ID mapping
    4. ``governed_account_name_field``      - Rocketlane "Account Name" custom field
    5. ``normalized_customer_name`` / ``configured_alias``
    6. ``project_name_fallback``
    """
    account_list = [dict(account) for account in accounts]
    account_by_id = {str(item.get("id")): item for item in account_list}
    index = _build_provenance_index(account_list, aliases)
    customer_crosswalk, customer_crosswalk_collisions = _customer_id_crosswalk(account_list, aliases)
    opportunity_crosswalk = _opportunity_crosswalk(opportunities, set(account_by_id))
    project_to_account: Dict[str, str] = {}
    match_evidence: Dict[str, Dict[str, Any]] = {}
    exceptions: List[Dict[str, Any]] = []
    candidate_keys = list(index)
    project_groups: Dict[str, List[Dict[str, Any]]] = {}
    for raw_project in projects:
        project = dict(raw_project)
        project_id = str(project.get("id") or "").strip()
        if not project_id or project_id.lower() in {"none", "null"}:
            exceptions.append({
                "type": "invalid_project_id",
                "project_id": None,
                "project_name": project.get("name"),
                "rocketlane_customer": project.get("customer_name"),
                "message": "Rocketlane project is missing a stable project ID; automatic matching was blocked.",
            })
            continue
        project_groups.setdefault(project_id, []).append(project)

    prepared_projects: List[Dict[str, Any]] = []
    for project_id, rows in sorted(project_groups.items()):
        fingerprints = {
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            for row in rows
        }
        if len(rows) > 1:
            exceptions.append({
                "type": "project_id_collision",
                "project_id": project_id,
                "project_name": rows[0].get("name"),
                "rocketlane_customer": rows[0].get("customer_name"),
                "message": "Duplicate Rocketlane project records share the same project ID; automatic matching was blocked.",
                "source_record_count": len(rows),
                "conflicting_payloads": len(fingerprints) > 1,
            })
            continue
        prepared_projects.append(rows[0])

    for project in prepared_projects:
        project_id = str(project["id"])
        explicit_account_id = str(project.get("salesforce_account_id") or "")
        if explicit_account_id:
            if explicit_account_id in account_by_id:
                project_to_account[project_id] = explicit_account_id
                match_evidence[project_id] = {
                    "basis": "salesforce_account_id",
                    "account_id": explicit_account_id,
                    "project_id": project_id,
                    "matched_value": explicit_account_id,
                }
            else:
                exceptions.append({
                    "type": "explicit_account_out_of_scope",
                    "project_id": project_id,
                    "project_name": project.get("name"),
                    "rocketlane_customer": project.get("customer_name"),
                    "salesforce_account_id": explicit_account_id,
                    "message": "Rocketlane carries an explicit Salesforce Account ID that is not in the current scope; name fallback was blocked.",
                })
            continue

        # Tier 2: the Opportunity ID Rocketlane stores for the project. Only a
        # correctly shaped Opportunity ID is trusted, and only when it resolves
        # to an in-scope opportunity. An unresolved value falls through to the
        # remaining tiers rather than blocking: the in-scope opportunity set is
        # restricted to Closed Won on or before the report date, so a project
        # may legitimately reference an opportunity outside that window.
        opportunity_id = str(project.get("opportunity_id") or "").strip()
        if looks_like_salesforce_id(opportunity_id, OPPORTUNITY_ID_PREFIX):
            crosswalk_account_id = opportunity_crosswalk.get(opportunity_id)
            if crosswalk_account_id:
                project_to_account[project_id] = crosswalk_account_id
                match_evidence[project_id] = {
                    "basis": "salesforce_opportunity_id",
                    "account_id": crosswalk_account_id,
                    "project_id": project_id,
                    "matched_value": opportunity_id,
                }
                continue

        customer_id = str(project.get("customer_id") or "")
        if customer_id in customer_crosswalk_collisions:
            exceptions.append({
                "type": "customer_id_collision",
                "project_id": project_id,
                "project_name": project.get("name"),
                "rocketlane_customer": project.get("customer_name"),
                "rocketlane_customer_id": customer_id,
                "message": "The Rocketlane customer ID maps to multiple Salesforce accounts; automatic matching was blocked.",
                "candidates": customer_crosswalk_collisions[customer_id],
            })
            continue
        crosswalk_account_id = customer_crosswalk.get(customer_id)
        if crosswalk_account_id:
            project_to_account[project_id] = crosswalk_account_id
            match_evidence[project_id] = {
                "basis": "rocketlane_customer_id_crosswalk",
                "account_id": crosswalk_account_id,
                "project_id": project_id,
                "matched_value": customer_id,
            }
            continue

        customer_name = str(project.get("customer_name") or "")
        governed_account_name = str(project.get("account_name") or "")
        # Rocketlane's "Account Name" custom field carries the exact Salesforce
        # account name, so it matches without an alias. normalize_projects falls
        # back to the customer name when the field is absent, so only treat it as
        # governed evidence when it differs from the customer name.
        candidates: List[Tuple[str, Optional[str]]] = []
        if governed_account_name and normalize_name(governed_account_name) != normalize_name(customer_name):
            candidates.append((governed_account_name, "governed_account_name_field"))
        if customer_name:
            candidates.append((customer_name, None))
        if not candidates:
            candidates.append((str(project.get("name") or ""), "project_name_fallback"))

        matched = False
        collided = False
        match_value = candidates[0][0]
        for value, basis_override in candidates:
            key = normalize_name(value)
            matches = index.get(key, [])
            unique_accounts = {str(item["account"].get("id")): item for item in matches}
            if len(unique_accounts) == 1:
                match = next(iter(unique_accounts.values()))
                account_id = str(match["account"]["id"])
                project_to_account[project_id] = account_id
                match_evidence[project_id] = {
                    "basis": basis_override or str(match["basis"]),
                    "account_id": account_id,
                    "project_id": project_id,
                    "matched_value": value,
                }
                matched = True
                break
            if len(unique_accounts) > 1:
                exceptions.append({
                    "type": "account_collision",
                    "project_id": project_id,
                    "project_name": project.get("name"),
                    "rocketlane_customer": value,
                    "message": "Multiple Salesforce accounts normalize to the same name; add an explicit cross-system ID or governed customer-ID mapping.",
                    "candidates": sorted(str(item["account"].get("name")) for item in unique_accounts.values()),
                })
                collided = True
                break
            match_value = value
        if matched or collided:
            continue
        key = normalize_name(match_value)
        suggestions = sorted(
            (
                (SequenceMatcher(None, key, candidate).ratio(), index[candidate][0]["account"])
                for candidate in candidate_keys
            ),
            key=lambda item: (-item[0], str(item[1].get("name", ""))),
        )
        best = suggestions[0] if suggestions else (0.0, {})
        exceptions.append({
            "type": "unmatched_project",
            "project_id": project_id,
            "project_name": project.get("name"),
            "rocketlane_customer": match_value,
            "message": "No Salesforce Account ID, in-scope Opportunity ID, governed customer-ID crosswalk, governed account-name field, exact normalized name, or configured alias matched.",
            "suggested_account": best[1].get("name") if best[0] >= 0.55 else None,
            "suggestion_score": round(best[0], 3),
        })
    return project_to_account, exceptions, match_evidence


def match_projects(
    accounts: Iterable[Mapping[str, Any]],
    projects: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, Any],
    opportunities: Iterable[Mapping[str, Any]] = (),
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    mapping, exceptions, _ = match_projects_with_evidence(accounts, projects, aliases, opportunities=opportunities)
    return mapping, exceptions
