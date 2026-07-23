"""Read-only Rocketlane REST connector."""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .http_client import ApiError, request_json


class RocketlaneClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("ROCKETLANE_BASE_URL", "https://api.rocketlane.com/api/1.0").rstrip("/")
        self.api_key = os.getenv("ROCKETLANE_API_KEY", "")
        if not self.api_key:
            raise ApiError("ROCKETLANE_API_KEY is required in live mode. See .env.example.")

    @property
    def headers(self) -> Dict[str, str]:
        return {"api-key": self.api_key}

    def _paginate(self, path: str, params: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        url = self.base_url + "/" + path.lstrip("/")
        query = {"limit": 100, **dict(params or {})}
        rows: List[Dict[str, Any]] = []
        seen_pages = set()
        pages = 0
        while url:
            page_key = (url, str(query.get("pageToken", "")))
            if page_key in seen_pages or pages >= 1000:
                raise ApiError("Rocketlane pagination repeated or exceeded 1,000 pages.")
            seen_pages.add(page_key)
            response = request_json(
                "GET", url, headers=self.headers, params=query, allowed_origin=self.base_url
            )
            pages += 1
            rows.extend(response.get("data", []))
            if len(rows) > 100000:
                raise ApiError("Rocketlane request exceeded the 100,000-record safety limit.")
            pagination = response.get("pagination", {})
            if not pagination.get("hasMore"):
                break
            next_url = pagination.get("nextPage")
            if next_url:
                url = str(next_url)
                query = {}
            else:
                token = pagination.get("nextPageToken")
                if not token:
                    break
                query["pageToken"] = token
        return rows

    def fetch_projects(self) -> List[Dict[str, Any]]:
        records = self._paginate("projects", {"includeAllFields": "true", "includeArchive.eq": "true"})
        return [self._normalize_project(item) for item in records]

    def fetch_time_entries(self, project_ids: Iterable[str]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        seen = set()
        for project_id in sorted(set(str(value) for value in project_ids if value)):
            records = self._paginate(
                "time-entries/search",
                {"billable.eq": "true", "project.eq": project_id, "includeAllFields": "true", "sortBy": "DATE", "sortOrder": "ASC"},
            )
            for item in records:
                normalized = self._normalize_entry(item, project_id)
                entry_id = normalized["id"]
                if entry_id not in seen:
                    seen.add(entry_id)
                    entries.append(normalized)
        return entries

    @staticmethod
    def _normalize_project(item: Mapping[str, Any]) -> Dict[str, Any]:
        customer = item.get("customer") or {}
        project_id = item.get("projectId")
        return {
            "id": str(project_id).strip() if project_id not in (None, "") else None,
            "name": item.get("projectName"),
            "customer_id": str(customer.get("companyId")) if customer.get("companyId") is not None else None,
            "customer_name": customer.get("companyName"),
            "archived": bool(item.get("archived", False)),
            "status": (item.get("status") or {}).get("label"),
            "start_date": item.get("startDateActual") or item.get("startDate"),
            "due_date": item.get("dueDateActual") or item.get("dueDate"),
        }

    @staticmethod
    def _normalize_entry(item: Mapping[str, Any], fallback_project_id: str) -> Dict[str, Any]:
        user = item.get("user") or item.get("createdBy") or {}
        project = _find_project(item)
        category = item.get("category") or {}
        return {
            "id": str(item.get("timeEntryId") or item.get("id")),
            "project_id": str(project.get("projectId") or fallback_project_id),
            "project_name": project.get("projectName"),
            "date": item.get("date"),
            "minutes": item.get("minutes") or 0,
            "billable": bool(item.get("billable", False)),
            "approval_status": item.get("approvalStatus"),
            "activity_name": item.get("activityName"),
            "category": category.get("categoryName") or category.get("name"),
            "user_id": str(user.get("userId")) if user.get("userId") is not None else None,
            "user_name": " ".join(filter(None, [user.get("firstName"), user.get("lastName")])).strip(),
            "user_email": user.get("emailId") or user.get("email"),
        }


def _find_project(item: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = item.get("project")
    if isinstance(direct, Mapping):
        return direct
    for key in ("task", "projectPhase", "milestone"):
        source = item.get(key)
        if isinstance(source, Mapping) and isinstance(source.get("project"), Mapping):
            return source["project"]
    return {}
