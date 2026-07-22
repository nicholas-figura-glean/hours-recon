"""Generate a fully reconciled demo report."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Mapping

from .reconcile import reconcile
from .sample_data import build_demo_sources


def demo_report(package_config: Mapping[str, Any], aliases: Mapping[str, Any], as_of: date = None) -> Dict[str, Any]:
    report_date = as_of or date.today()
    salesforce, rocketlane = build_demo_sources(report_date)
    return reconcile(
        salesforce,
        rocketlane,
        package_config=package_config,
        account_aliases=aliases,
        as_of=report_date,
        mode="demo",
    )
