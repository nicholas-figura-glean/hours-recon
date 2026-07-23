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
5. Query each assigned Account independently by Salesforce Account ID. For each account, query every Closed Won Opportunity through today and its OpportunityLineItems before moving to the next account. Include stable Account, Opportunity, Product2, PricebookEntry, and line IDs; account names; close dates; product names/codes; quantities; and prices. This account-isolated evidence bundle prevents one account's records or aliases from masking another account's gaps. Respect MCP record limits and paginate when offered.
   - Inspect schema for explicit entitlement/service start and end fields and a governed no-entitlement/service disposition. Normalize them as `service_start_date`, `service_end_date`, and `entitlement_disposition` only when schema-validated and populated. Never invent field API names or infer these values from an Opportunity name.
   - Also retrieve the schema-validated approved and primary Quote references (currently `Approved_Quote__c` and `Ruby__PrimaryQuote__c`) for every Opportunity.
   - When an Opportunity has no OpportunityLineItems, use its approved Quote, falling back to its primary Quote, and retrieve every QuoteLineItem with Product2/PricebookEntry name, product code, quantity, sales price, and list price.
   - Normalize those QuoteLineItems into `opportunities[].line_items` with `source: "approved_quote"` or `source: "primary_quote"` and `quote_id`. Never combine OpportunityLineItems and QuoteLineItems for the same Opportunity; OpportunityLineItems take precedence to prevent double counting.
   - Audit every no-OpportunityLineItem record, including Quotes with no lines. Do not treat an empty OpportunityLineItem query as evidence that zero hours were sold.
6. Search Rocketlane projects independently for each assigned Account name and every configured alias, with archived projects included. Retrieve each candidate by ID with all fields. Prefer an explicit Salesforce Account ID on the Rocketlane customer/project, then a governed Rocketlane customer-ID crosswalk, then the Rocketlane `Account Name` custom field or customer company. Preserve `salesforce_account_id`, `customer_id`, and the observed match basis when available; project-name inference is a Tier 4 fallback and must not be silently accepted.
7. Retrieve all billable time entries through today for every matched project, from all contributors. Follow every page token and deduplicate by time-entry ID.
8. Normalize the source records into `var/mcp_snapshot.json` using schema version 1:
   - `salesforce.requester`, `accounts`, `opportunities[].line_items`
   - `rocketlane.requester`, `projects`, `entries`
   - Preserve product IDs/codes, PricebookEntry IDs, line source/Quote IDs, explicit service dates, Rocketlane customer IDs, explicit Salesforce IDs, project lifecycle fields, approval status, activity, category, and contributor identity when available.
   - `meta.created_at`, scope, MCP server identifiers, and source counts
   - Generate a unique `meta.retrieval_id` for every new external fetch, a stable tenant/workspace `meta.scope_id`, and `meta.through_date`. Set `meta.scope_verified: true` only after the scope ID is corroborated against the authenticated connector tenant/workspace identity; string presence alone is not verification. Remediation validation transitions additionally require `HOURS_RECON_REMEDIATION_SCOPE_ID` to exactly match this verified source value.
   - Add `meta.coverage` with explicit booleans for `accounts`, `opportunities`, `projects`, `time_entries`, and `pagination_complete`, plus `complete`. Set a value true only after the account-isolated retrieval and every pagination terminal page are verified. `meta.through_date` must equal the report date, and `meta.scope_id` must be a verified stable tenant/workspace identifier before a retrieval can resolve or reopen remediation. Existing counts alone do not prove completeness.
9. Write the snapshot with directory mode `0700` and file mode `0600`. Never commit `var/`.
10. Run the importer through `HOURS_RECON_MODE=mcp python3 -c` using `ReconciliationService(settings()).refresh()`.
11. Validate:
    - every assigned Account has its own evidence bundle and explicit account-level retrieval audit
    - source and report counts agree
    - sold hours equal inferred package totals, including normalized approved/primary QuoteLineItem fallbacks
    - each Opportunity uses exactly one line-item source and no line is duplicated
    - billed hours equal the sum of loaded billable minutes / 60
    - no pagination page was skipped
    - unmatched projects and pre-entitlement overage are surfaced, not silently discarded
    - exact ProductCode mappings, source tiers, service-period sources, project match bases, and time-quality reasons are retained in the report
    - governed plus provisional metrics equal the unchanged reported metrics
    - every Tier 3/4 dimension creates or updates exactly one account remediation gap; reloading the same `retrieval_id` creates no duplicates
    - only a new retrieval with `meta.coverage.complete=true` can resolve a missing gap or reopen a regression
12. Restart the local server in MCP mode if needed and smoke-test `/api/status`, `/api/data`, and the dashboard.

## Important architecture boundary

Glean Pi owns the authenticated MCP session. The local Python server cannot invoke Pi's connected tools directly. In MCP mode, its button reloads the latest private snapshot; a new external fetch is initiated by asking Glean Pi to “run Hours Recon MCP refresh.”
