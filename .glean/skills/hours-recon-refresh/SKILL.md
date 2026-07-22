---
name: hours-recon-refresh
description: Refreshes the local Hours Recon dashboard from authenticated Salesforce and Rocketlane MCP tools. Use when the user asks to refresh hours, update the reconciliation, reload sold versus billed hours, or run an MCP refresh in this repository.
---

# Hours Recon MCP Refresh

Use MCP tools from the active Glean Pi session. Do not require Salesforce or Rocketlane API keys.

## Workflow

1. Read `config/packages.json`, `config/account_aliases.json`, `hours_recon/mcp_snapshot.py`, and this skill.
2. Use `glean_find_skills` to discover the current Salesforce and Rocketlane skills. Read each `SKILL.md` and the exact schemas for:
   - Salesforce: `getUserInfo`, `getObjectSchema`, `soqlQuery`
   - Rocketlane: `get_my_profile`, `get_projects`, `get_time_entries`
3. Resolve the authenticated identities with `getUserInfo` and `get_my_profile`. If either connector requests OAuth, show its authorization link and wait for the user to confirm sign-in.
4. Confirm the Account AIOM field through Salesforce schema. Never guess a custom field.
5. Query all assigned Accounts. Then query all Closed Won Opportunities through today and their OpportunityLineItems. Include stable IDs, account names, close dates, product names/codes, quantities, and prices. Respect MCP record limits and paginate when offered.
6. Search Rocketlane projects for each assigned Account name and every configured alias, with archived projects included. Retrieve each candidate by ID with all fields. Prefer the Rocketlane `Account Name` custom field or customer company over project-name inference when available.
7. Retrieve all billable time entries through today for every matched project, from all contributors. Follow every page token and deduplicate by time-entry ID.
8. Normalize the source records into `var/mcp_snapshot.json` using schema version 1:
   - `salesforce.requester`, `accounts`, `opportunities[].line_items`
   - `rocketlane.requester`, `projects`, `entries`
   - `meta.created_at`, scope, MCP server identifiers, and source counts
9. Write the snapshot with directory mode `0700` and file mode `0600`. Never commit `var/`.
10. Run the importer through `HOURS_RECON_MODE=mcp python3 -c` using `ReconciliationService(settings()).refresh()`.
11. Validate:
    - source and report counts agree
    - sold hours equal inferred package totals
    - billed hours equal the sum of loaded billable minutes / 60
    - no pagination page was skipped
    - unmatched projects and pre-entitlement overage are surfaced, not silently discarded
12. Restart the local server in MCP mode if needed and smoke-test `/api/status`, `/api/data`, and the dashboard.

## Important architecture boundary

Glean Pi owns the authenticated MCP session. The local Python server cannot invoke Pi's connected tools directly. In MCP mode, its button reloads the latest private snapshot; a new external fetch is initiated by asking Glean Pi to “run Hours Recon MCP refresh.”
