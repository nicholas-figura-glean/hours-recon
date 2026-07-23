"""Read-only Salesforce REST connector with dynamic AIOM field discovery."""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import quote

from .http_client import ApiError, request_json


def _soql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _chunks(values: Sequence[str], size: int = 75) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class SalesforceClient:
    def __init__(self) -> None:
        self.api_version = os.getenv("SF_API_VERSION", "62.0")
        self.instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
        self.access_token = os.getenv("SF_ACCESS_TOKEN", "")
        if not self.access_token:
            self._refresh_access_token()
        if not self.instance_url or not self.access_token:
            raise ApiError("Salesforce credentials are incomplete. See .env.example.")

    def _refresh_access_token(self) -> None:
        required = {
            "client_id": os.getenv("SF_CLIENT_ID", ""),
            "client_secret": os.getenv("SF_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("SF_REFRESH_TOKEN", ""),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ApiError("Missing Salesforce authentication values: " + ", ".join(missing))
        login_url = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com").rstrip("/")
        token = request_json(
            "POST",
            login_url + "/services/oauth2/token",
            form={"grant_type": "refresh_token", **required},
            allowed_origin=login_url,
        )
        self.access_token = str(token["access_token"])
        self.instance_url = str(token.get("instance_url") or self.instance_url).rstrip("/")

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _url(self, path: str) -> str:
        return f"{self.instance_url}/services/data/v{self.api_version}/{path.lstrip('/')}"

    def describe(self, object_name: str) -> Dict[str, Any]:
        return request_json(
            "GET",
            self._url(f"sobjects/{object_name}/describe"),
            headers=self.headers,
            allowed_origin=self.instance_url,
        )

    def query_all(self, soql: str) -> List[Dict[str, Any]]:
        response = request_json(
            "GET", self._url("query"), headers=self.headers, params={"q": soql}, allowed_origin=self.instance_url
        )
        records = list(response.get("records", []))
        seen_next_urls = set()
        pages = 1
        while not response.get("done", True):
            next_url = str(response["nextRecordsUrl"])
            if next_url in seen_next_urls or pages >= 1000:
                raise ApiError("Salesforce pagination repeated or exceeded 1,000 pages.")
            seen_next_urls.add(next_url)
            response = request_json(
                "GET", self.instance_url + next_url, headers=self.headers, allowed_origin=self.instance_url
            )
            records.extend(response.get("records", []))
            pages += 1
            if len(records) > 100000:
                raise ApiError("Salesforce query exceeded the 100,000-record safety limit.")
        for record in records:
            record.pop("attributes", None)
        return records

    def discover_aiom_field(self) -> Dict[str, Any]:
        fields = self.describe("Account").get("fields", [])
        explicit = os.getenv("SF_AIOM_FIELD", "").strip()
        if explicit:
            for field in fields:
                if field.get("name") == explicit:
                    return field
            raise ApiError(f"SF_AIOM_FIELD={explicit} was not found on Account.")

        def score(field: Mapping[str, Any]) -> int:
            name = str(field.get("name", "")).lower()
            label = str(field.get("label", "")).lower()
            combined = f"{name} {label}"
            points = 0
            if label.strip() == "aiom" or name.strip("_c") == "aiom":
                points += 100
            if "ai outcomes manager" in combined:
                points += 80
            if "aiom" in combined:
                points += 50
            if "outcomes manager" in combined:
                points += 30
            if "User" in field.get("referenceTo", []):
                points += 10
            return points

        ranked = sorted(((score(field), field) for field in fields), key=lambda item: (-item[0], str(item[1].get("name"))))
        if not ranked or ranked[0][0] == 0:
            raise ApiError("Could not discover the Account AIOM field. Set SF_AIOM_FIELD in .env.")
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            candidates = ", ".join(str(item[1].get("name")) for item in ranked[:5] if item[0] > 0)
            raise ApiError(f"AIOM field discovery is ambiguous ({candidates}). Set SF_AIOM_FIELD in .env.")
        return ranked[0][1]

    def resolve_requester(self, email: str) -> Dict[str, Any]:
        if not email:
            raise ApiError("HOURS_RECON_REQUESTER_EMAIL is required in live mode.")
        escaped = _soql_string(email)
        records = self.query_all(f"SELECT Id, Name, Email, Username FROM User WHERE Email = {escaped} OR Username = {escaped} LIMIT 2")
        if not records:
            raise ApiError(f"No Salesforce User matched {email}.")
        if len(records) > 1:
            exact = [item for item in records if str(item.get("Email", "")).lower() == email.lower()]
            if len(exact) == 1:
                records = exact
            else:
                raise ApiError(f"Multiple Salesforce users matched {email}.")
        user = records[0]
        return {"id": user["Id"], "name": user.get("Name"), "email": user.get("Email") or email}

    def fetch(self, requester_email: str) -> Dict[str, Any]:
        requester = self.resolve_requester(requester_email)
        field = self.discover_aiom_field()
        field_name = str(field["name"])
        match_value = os.getenv("SF_AIOM_MATCH_VALUE", "").strip()
        field_type = str(field.get("type", ""))
        references_user = "User" in field.get("referenceTo", [])
        if references_user:
            predicate = f"{field_name} = {_soql_string(requester['id'])}"
        elif field_type == "multipicklist":
            value = match_value or requester["name"]
            predicate = f"{field_name} INCLUDES ({_soql_string(str(value))})"
        else:
            value = match_value or requester["name"] or requester["email"]
            predicate = f"{field_name} = {_soql_string(str(value))}"

        account_records = self.query_all(f"SELECT Id, Name, {field_name} FROM Account WHERE {predicate} ORDER BY Name")
        accounts = [{"id": item["Id"], "name": item["Name"]} for item in account_records]
        account_ids = [item["id"] for item in accounts]
        opportunities: List[Dict[str, Any]] = []
        for chunk in _chunks(account_ids):
            ids = ",".join(_soql_string(value) for value in chunk)
            query = (
                "SELECT Id, AccountId, Account.Name, Name, StageName, IsWon, CloseDate, HasOpportunityLineItem "
                f"FROM Opportunity WHERE StageName = 'Closed Won' AND CloseDate <= TODAY AND AccountId IN ({ids}) "
                "ORDER BY CloseDate ASC NULLS LAST"
            )
            for item in self.query_all(query):
                opportunities.append({
                    "id": item["Id"],
                    "account_id": item["AccountId"],
                    "account_name": (item.get("Account") or {}).get("Name"),
                    "name": item.get("Name"),
                    "stage": item.get("StageName"),
                    "is_won": item.get("IsWon"),
                    "close_date": item.get("CloseDate"),
                    "has_line_items": item.get("HasOpportunityLineItem"),
                    "line_items": [],
                })
        self._attach_line_items(opportunities)
        return {
            "requester": requester,
            "accounts": accounts,
            "opportunities": opportunities,
            "metadata": {"aiom_field": field_name, "aiom_field_label": field.get("label"), "instance_url": self.instance_url},
        }

    def _attach_line_items(self, opportunities: List[Dict[str, Any]]) -> None:
        lookup = {item["id"]: item for item in opportunities}
        opportunity_ids = [item["id"] for item in opportunities if item.get("has_line_items")]
        for chunk in _chunks(opportunity_ids):
            ids = ",".join(_soql_string(value) for value in chunk)
            rich_query = (
                "SELECT Id, OpportunityId, Name, Quantity, UnitPrice, TotalPrice, Product2Id, PricebookEntryId, "
                "Product2.Name, Product2.ProductCode, PricebookEntry.UnitPrice "
                f"FROM OpportunityLineItem WHERE OpportunityId IN ({ids})"
            )
            try:
                records = self.query_all(rich_query)
            except ApiError:
                records = self.query_all(
                    "SELECT Id, OpportunityId, Name, Quantity, UnitPrice, Product2Id, Product2.Name "
                    f"FROM OpportunityLineItem WHERE OpportunityId IN ({ids})"
                )
            for item in records:
                opportunity = lookup.get(item.get("OpportunityId"))
                if not opportunity:
                    continue
                product = item.get("Product2") or {}
                pricebook = item.get("PricebookEntry") or {}
                opportunity["line_items"].append({
                    "id": item.get("Id"),
                    "source": "opportunity_line_item",
                    "name": product.get("Name") or item.get("Name"),
                    "product_id": item.get("Product2Id"),
                    "product_code": product.get("ProductCode"),
                    "pricebook_entry_id": item.get("PricebookEntryId"),
                    "quantity": item.get("Quantity") or 1,
                    "unit_price": item.get("UnitPrice"),
                    "list_price": pricebook.get("UnitPrice"),
                    "total_price": item.get("TotalPrice"),
                })
