"""Conservative Salesforce-to-Rocketlane account matching."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Mapping, Tuple

LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company"}


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\([^)]*\)", " ", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _configured_aliases(aliases: Mapping[str, Any]) -> Mapping[str, Any]:
    return aliases.get("aliases", aliases)


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
) -> Tuple[Dict[str, str], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Match projects and retain the exact basis used for governance scoring."""
    account_list = [dict(account) for account in accounts]
    account_by_id = {str(item.get("id")): item for item in account_list}
    index = _build_provenance_index(account_list, aliases)
    customer_crosswalk, customer_crosswalk_collisions = _customer_id_crosswalk(account_list, aliases)
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
        match_value = customer_name or str(project.get("name") or "")
        key = normalize_name(match_value)
        matches = index.get(key, [])
        unique_accounts = {str(item["account"].get("id")): item for item in matches}
        if len(unique_accounts) == 1:
            match = next(iter(unique_accounts.values()))
            account_id = str(match["account"]["id"])
            basis = str(match["basis"])
            if not customer_name:
                basis = "project_name_fallback"
            project_to_account[project_id] = account_id
            match_evidence[project_id] = {
                "basis": basis,
                "account_id": account_id,
                "project_id": project_id,
                "matched_value": match_value,
            }
            continue
        if len(unique_accounts) > 1:
            exceptions.append({
                "type": "account_collision",
                "project_id": project_id,
                "project_name": project.get("name"),
                "rocketlane_customer": match_value,
                "message": "Multiple Salesforce accounts normalize to the same name; add an explicit cross-system ID or governed customer-ID mapping.",
                "candidates": sorted(str(item["account"].get("name")) for item in unique_accounts.values()),
            })
            continue
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
            "message": "No exact normalized name, configured alias, Salesforce Account ID, or governed customer-ID crosswalk matched.",
            "suggested_account": best[1].get("name") if best[0] >= 0.55 else None,
            "suggestion_score": round(best[0], 3),
        })
    return project_to_account, exceptions, match_evidence


def match_projects(
    accounts: Iterable[Mapping[str, Any]],
    projects: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, Any],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    mapping, exceptions, _ = match_projects_with_evidence(accounts, projects, aliases)
    return mapping, exceptions
