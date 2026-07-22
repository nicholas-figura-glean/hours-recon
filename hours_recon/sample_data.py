"""Fictional demo data for local UI development and first-run validation."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Tuple


def build_demo_sources(as_of: date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    def ago(days: int) -> str:
        return (as_of - timedelta(days=days)).isoformat()

    salesforce = {
        "requester": {"id": "005DEMO", "name": "Demo AIOM", "email": "demo.aiom@example.com"},
        "accounts": [
            {"id": "A1", "name": "Acme Systems, Inc."},
            {"id": "A2", "name": "Northstar Labs"},
            {"id": "A3", "name": "Orthogonal Networks (DBA Jellyfish)"},
            {"id": "A4", "name": "Cedar Analytics"},
        ],
        "opportunities": [
            {"id": "O1", "account_id": "A1", "account_name": "Acme Systems, Inc.", "name": "Acme - Outcomes Package", "close_date": ago(340), "line_items": [{"id": "L1", "name": "AI Outcomes - Standard", "quantity": 1, "unit_price": 23000, "list_price": 25000}]},
            {"id": "O2", "account_id": "A1", "account_name": "Acme Systems, Inc.", "name": "Acme - Growth Package 20 hours", "close_date": ago(40), "line_items": []},
            {"id": "O3", "account_id": "A2", "account_name": "Northstar Labs", "name": "Northstar - Strategic Outcomes", "close_date": ago(180), "line_items": [{"id": "L3", "name": "AI Outcomes - Strategic", "quantity": 1, "unit_price": 150000, "list_price": 150000}]},
            {"id": "O4", "account_id": "A3", "account_name": "Orthogonal Networks (DBA Jellyfish)", "name": "Jellyfish - Growth Package 100 hours", "close_date": ago(320), "line_items": []},
            {"id": "O5", "account_id": "A4", "account_name": "Cedar Analytics", "name": "Cedar - Custom Outcomes Package", "close_date": ago(70), "line_items": [{"id": "L5", "name": "AI Outcomes - Custom", "quantity": 1, "unit_price": 42000, "list_price": 42000}]},
        ],
    }
    projects = [
        {"id": "P1", "name": "Acme AI Outcomes", "customer_name": "Acme Systems", "archived": False},
        {"id": "P2", "name": "Northstar Enablement", "customer_name": "Northstar Labs", "archived": False},
        {"id": "P3", "name": "Jellyfish Outcomes", "customer_name": "Jellyfish", "archived": False},
        {"id": "P4", "name": "Unmapped Client", "customer_name": "Unknown Holdings", "archived": False},
    ]
    entries = []
    counter = 1

    def add(project: str, days_ago: int, hours: float, user: str, email: str) -> None:
        nonlocal counter
        entries.append({"id": f"T{counter}", "project_id": project, "date": ago(days_ago), "minutes": int(hours * 60), "billable": True, "user_name": user, "user_email": email, "approval_status": "APPROVED"})
        counter += 1

    add("P1", 300, 34, "Demo AIOM", "demo.aiom@example.com")
    add("P1", 12, 18, "Solutions Consultant", "sc@example.com")
    add("P1", 2, 3, "Demo AIOM", "demo.aiom@example.com")
    add("P2", 30, 72, "Demo AIOM", "demo.aiom@example.com")
    add("P2", 8, 44, "Forward Deployed Engineer", "fde@example.com")
    add("P3", 250, 81, "Demo AIOM", "demo.aiom@example.com")
    add("P3", 1, 14, "AI Engineer", "aie@example.com")
    rocketlane = {"projects": projects, "entries": entries}
    return salesforce, rocketlane
