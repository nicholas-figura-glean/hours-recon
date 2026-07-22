"""Conservative Salesforce-to-Rocketlane account matching."""

from __future__ import annotations

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


def build_account_index(accounts: Iterable[Mapping[str, Any]], aliases: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    configured = aliases.get("aliases", aliases)
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


def match_projects(
    accounts: Iterable[Mapping[str, Any]],
    projects: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, Any],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    account_list = [dict(account) for account in accounts]
    index = build_account_index(account_list, aliases)
    project_to_account: Dict[str, str] = {}
    exceptions: List[Dict[str, Any]] = []
    candidate_keys = list(index)

    for raw_project in projects:
        project = dict(raw_project)
        project_id = str(project.get("id", ""))
        customer_name = str(project.get("customer_name") or project.get("name") or "")
        key = normalize_name(customer_name)
        matches = index.get(key, [])
        if len(matches) == 1:
            project_to_account[project_id] = str(matches[0]["id"])
            continue
        if len(matches) > 1:
            exceptions.append({
                "type": "account_collision",
                "project_id": project_id,
                "project_name": project.get("name"),
                "rocketlane_customer": customer_name,
                "message": "Multiple Salesforce accounts normalize to the same name; add an explicit alias.",
                "candidates": [item["name"] for item in matches],
            })
            continue
        suggestions = sorted(
            (
                (SequenceMatcher(None, key, candidate).ratio(), index[candidate][0])
                for candidate in candidate_keys
            ),
            key=lambda item: (-item[0], str(item[1].get("name", ""))),
        )
        best = suggestions[0] if suggestions else (0.0, {})
        exceptions.append({
            "type": "unmatched_project",
            "project_id": project_id,
            "project_name": project.get("name"),
            "rocketlane_customer": customer_name,
            "message": "No exact normalized name or configured alias matched.",
            "suggested_account": best[1].get("name") if best[0] >= 0.55 else None,
            "suggestion_score": round(best[0], 3),
        })
    return project_to_account, exceptions
