---
name: hours-recon-refresh
description: Refreshes the local Hours Recon dashboard from authenticated Salesforce and Rocketlane MCP tools. Use when the user asks to refresh hours, update the reconciliation, reload sold versus billed hours, or run an MCP refresh in this repository.
---

# Hours Recon MCP Refresh

Use MCP tools from the active Glean Pi session. Do not require Salesforce or Rocketlane API keys.

## Cost model — read this first

A refresh is slow for exactly two reasons: the number of sequential agent turns,
and the number of tokens you write out. Optimize both.

- **Batch.** One SOQL query with a parent-child subquery replaces a per-account loop.
- **Parallelize.** Issue independent tool calls in a single block, never one per turn.
- **Delegate.** Field mapping, audits, coverage, and validation are Python. You
  copy payloads; you do not transform, sum, or re-derive them.

Target: **8-10 turns**. If you find yourself on turn 20, you have reverted to a
per-account loop — stop and batch.

## Workflow

### 0. Fix the report date, then load pinned bindings and identity — 1 turn

Derive one `report_date` from the system date in the dashboard timezone
(`HOURS_RECON_TIMEZONE`, default `America/Denver`). Use that exact value as the
bound in every query and as `meta.report_date` in the raw pull. Never reuse a
date from an earlier refresh, fixture, or chat message. `normalize_raw_pull`
rejects a pull whose declared date is not today, so a stale date fails loudly
rather than publishing a mixed-date snapshot. If the date rolls over mid-pull,
restart with the new date.

In one parallel block:

- Read `config/mcp_bindings.json`, `config/packages.json`, and `config/account_aliases.json`.
- Call Salesforce `getUserInfo` and Rocketlane `get_my_profile`.

`config/mcp_bindings.json` pins the MCP server IDs, tool names, the Account AIOM
field (`AIOM__c`), and the quote field API names. Reuse them. Check freshness
with `hours_recon.config.binding_freshness`.

**Only rediscover when** `binding_freshness` reports not fresh, the file is
missing, a pinned field is rejected by the source, or the user asks for a forced
rediscovery. Rediscovery means `glean_find_skills` for the Salesforce and
Rocketlane skills, reading their schemas, and `getObjectSchema` on Account to
confirm the AIOM field by API name, label, and whether it references `User`.
Never guess a custom field. After a successful rediscovery, update
`config/mcp_bindings.json` and set `verified_on` to today.

Skipping the Account describe is the point: it is several hundred fields, and
pulling it into context slows every later turn in the run.

**Scope verification is never skipped.** Corroborate `scope_id` against the live
connector identity returned by `getUserInfo` (org ID) and `get_my_profile`
(workspace). Pinned bindings remove discovery round trips, not verification. Set
`meta.scope_verified: true` only after that corroboration; string presence in the
bindings file is not verification.

### 1. Salesforce, batched — 2 turns

Do **not** loop over accounts. Three queries cover the whole portfolio.

1. **Accounts** assigned through the AIOM field:

   ```sql
   SELECT Id, Name, Owner.Name, Owner.Email
   FROM Account WHERE AIOM__c = '<requester user id>' ORDER BY Name
   ```

2. **Opportunities with their line items in one query.** `OpportunityLineItems`
   is a child relationship, so a subquery returns both without a second pass:

   ```sql
   SELECT Id, AccountId, Account.Name, Name, CloseDate, Owner.Name, Owner.Email,
          Approved_Quote__c, Ruby__PrimaryQuote__c,
          (SELECT Id, Quantity, UnitPrice, Product2Id, Product2.Name,
                  Product2.ProductCode, PricebookEntryId, PricebookEntry.UnitPrice,
                  ServiceDate, EndDate
           FROM OpportunityLineItems)
   FROM Opportunity
   WHERE StageName = 'Closed Won' AND CloseDate <= <report_date>
     AND AccountId IN (<all account ids>)
   ORDER BY CloseDate ASC NULLS LAST
   ```

   `StageName` belongs in the `WHERE` clause only. Do not select it, or any
   other field the normalizer does not read; see the field list below.

3. **Quote lines and quote windows for the fallback set only.** Collect
   `Approved_Quote__c` and `Ruby__PrimaryQuote__c` from opportunities that
   returned no `OpportunityLineItems`, then issue both queries in one block:

   ```sql
   SELECT Id, QuoteId, Quantity, UnitPrice, ListPrice, Product2Id, Product2.Name,
          Product2.ProductCode, PricebookEntryId, ServiceDate, EndDate
   FROM QuoteLineItem WHERE QuoteId IN (<those quote ids>)

   SELECT Id, Ruby__StartDate__c, Ruby__EndDate__c
   FROM Quote WHERE Id IN (<those quote ids>)
   ```

   The quote window is only used so a quote line with no explicit dates can
   inherit its subscription period. Quote records are never emitted.

Queries 1 and 2 are dependent; 2 and 3 are dependent.

Chunk any `IN` list to about 50 IDs per query and issue the chunks in parallel.

Select only the fields listed above. A wider projection costs input tokens on
the way in and output tokens on the way out, and the normalizer discards
anything it does not read.

**Do not merge line-item sources yourself.** `normalize_opportunities` enforces
that OpportunityLineItems win, that the approved quote beats the primary quote,
and that the two are never combined. Pass both sets and let it decide.

**Do not write a per-account audit yourself.** `build_account_retrieval_audit`
groups the batched result by `AccountId` and records every in-scope account,
including accounts with zero Closed Won opportunities. That preserves the
account-isolation guarantee — one account's records can never mask another's
gaps — deterministically and without N round trips. It raises if any opportunity
references an account outside the requested scope.

Record what the wire reported for each query in `salesforce.pagination` as
`{"label": "accounts"|"opportunities"|"quote_lines", "done": <bool>,
"total_size": <n>, "returned": <n>}`. Follow `nextRecordsUrl` until `done` is
true; if you follow a page, set `"followed_next_page": true`. Coverage is derived
from these envelopes, so copy them honestly.

### 2. Rocketlane, in parallel — 2-3 turns

Rocketlane has no join language, so these stay separate calls — but they are
independent, so batch them into single blocks.

1. **One block:** search projects for every account name and every configured
   alias, archived included. All searches go in one parallel block.
2. **One block:** retrieve every candidate project by ID with all fields.
3. **One block:** retrieve billable time entries through `report_date` for every
   matched project, all contributors. Follow every page token.

Preserve `externalReferenceId`, customer IDs, project owner name and email,
lifecycle dates, status, archived state, approval status, activity, category,
and contributor identity. Request only the custom fields that are read:
**Account Name**, **OppID**, and **Salesforce Account ID**; skip a
fetch-all-fields flag, which returns a large blob that nothing consumes.
`normalize_projects` promotes an Account-shaped `externalReferenceId` or governed
custom field into `salesforce_account_id`. Project-name inference remains a Tier 4
fallback and must not be silently accepted. `normalize_time_entries` deduplicates
by time-entry ID, so overlapping pages are safe to include.

Record `rocketlane.project_search_audit` as `{"query", "count", "has_more",
"total_record_count", "followed_next_page"}` and `rocketlane.time_pagination_audit`
as `{"project_id", "count", "has_more", "followed_next_page"}`. Coverage requires
one audit row per retrieved project and a search row per account name and alias.

### 3. Write the raw pull and normalize — 1 turn

Write the payloads to `var/raw_pull.json` **verbatim**, then normalize. Do not
hand-write `var/mcp_snapshot.json`; that used to cost about 15,000 output tokens
per refresh and made every mapping rule depend on model attention.

Use the **columnar form** for the two large blocks. It carries each key once in
the header instead of once per row, which is roughly half the bytes for time
entries and line items. Column names may use dotted paths:

```json
{
  "time_entry_records": {
    "columns": ["timeEntryId", "project.projectId", "project.projectName", "date", "minutes",
                "billable", "approvalStatus", "activityName", "category.categoryName",
                "user.userId", "user.name", "user.emailId"],
    "rows": [[5001, 964197, "Cprime Onboarding", "2026-07-28", 120, true, "APPROVED", "Workshop", "Consulting", 752101, "Nick Figura", "a@glean.com"]]
  }
}
```

Line items may travel as a flat `salesforce.line_item_records` block keyed by
`OpportunityId` instead of a nested envelope repeated inside each parent. Plain
lists of objects and SOQL `{"records": [...]}` envelopes are also accepted;
all shapes produce an identical snapshot.

Raw pull skeleton:

```json
{
  "meta": {"report_date", "retrieval_id", "scope", "scope_id", "scope_verified",
           "salesforce_org_id", "salesforce_mcp_server", "rocketlane_mcp_server",
           "bindings_source": "pinned"|"rediscovered", "identity_evidence": {}},
  "salesforce": {"requester", "aiom_field", "account_records", "opportunity_records",
                 "line_item_records", "quote_line_records", "quote_records", "pagination"},
  "rocketlane": {"requester", "project_records", "time_entry_records",
                 "project_search_audit", "time_pagination_audit"}
}
```

Then:

```bash
HOURS_RECON_MODE=mcp python3 -c '
import json
from hours_recon.config import settings, load_json_optional, ROOT
from hours_recon.mcp_normalize import normalize_raw_pull
s = settings()
raw = json.load(open("var/raw_pull.json"))
snapshot = normalize_raw_pull(
    raw,
    account_aliases=s["account_aliases"],
    timezone_name=s["timezone"],
    bindings=s["mcp_bindings"],
)
json.dump(snapshot, open("var/candidate_snapshot.json", "w"))
print(json.dumps(snapshot["meta"]["coverage"], indent=1))
print(json.dumps(snapshot["meta"]["source_counts"], indent=1))
'
```

`normalize_raw_pull` derives `meta.coverage`, `source_counts`,
`approval_status_counts`, `account_retrieval_audit`, and `created_at`. Coverage
is computed from the pagination envelopes and audits you copied, never from an
assertion that the pull looked complete. If a coverage flag is false, read
`unsearched_account_queries` and `unaudited_project_ids` to see exactly what is
missing, fetch it, and re-normalize.

**The emitted schema is deliberately minimal.** Every field it keeps has a
consumer in the reconciliation engine, the remediation workflow, the validator,
or the dashboard; anything else is dropped so the agent never pays to write it.
`test_snapshot_schema_is_locked_to_consumed_fields` pins the exact field set. To
add a field, add its consumer first, then widen that test.

### 4. Validate — 1 turn

Do not re-derive totals by reading the snapshot. Run the checks:

```bash
HOURS_RECON_MODE=mcp python3 -c '
import json
from hours_recon.config import settings
from hours_recon.mcp_validate import validate_refresh, format_findings
s = settings()
snapshot = json.load(open("var/candidate_snapshot.json"))
print(format_findings(validate_refresh(
    snapshot,
    package_config=s["packages"],
    account_aliases=s["account_aliases"],
    expected_requester_email=s["mcp_requester_email"],
    expected_scope_id=s["remediation_scope_id"],
    timezone_name=s["timezone"],
)))
'
```

This covers schema, requester binding, through-date currency, scope
verification, per-account audit coverage, in-scope opportunities, one line-item
source per opportunity, no duplicated line items or time entries, source counts,
pagination completeness, and coverage flags. Fix every `FAIL` before publishing.

### 5. Publish and import — 1 turn

```bash
HOURS_RECON_MODE=mcp python3 -c '
import json
from hours_recon.config import settings
from hours_recon.mcp_snapshot import publish_mcp_snapshot
from hours_recon.service import ReconciliationService
s = settings()
snapshot = json.load(open("var/candidate_snapshot.json"))
publish_mcp_snapshot(
    s["mcp_snapshot_path"], snapshot,
    expected_requester_email=s["mcp_requester_email"],
    expected_scope_id=s["remediation_scope_id"],
    timezone_name=s["timezone"],
)
report = ReconciliationService(s).refresh()
print(json.dumps(report["metrics"], indent=1))
'
```

Publication is atomic and replaces the active snapshot only when schema,
requester, complete coverage, current through-date, and verified scope all pass,
so a failed refresh leaves the last good file untouched. Parent directories are
created at `0700` and the file at `0600`. Never commit `var/`.

For a test requester, point `HOURS_RECON_MCP_SNAPSHOT_PATH` at a separate ignored
path such as `var/fixtures/jason_mcp_snapshot.json` and redirect
`HOURS_RECON_CACHE_PATH` and `HOURS_RECON_REMEDIATION_DB_PATH` too. Do not
replace the active requester's file.

### 6. Confirm the report — 1 turn

Re-run `validate_refresh` with the report to cross-check the derived output:

```python
report = json.load(open(settings()["cache_path"]))
validate_refresh(snapshot, report, package_config=..., account_aliases=...)
```

The report checks confirm sold hours equal inferred package totals including
quote fallbacks, billed hours equal the source minutes over matched in-window
entries, every project is matched or surfaced as an exception, pre-entitlement
activity is surfaced, and governed plus provisional equals reported for every
governance metric.

Remediation transitions are unchanged: only a new retrieval with
`meta.coverage.complete=true` can govern an instance at T2/T1, fail pending
validation, or reopen a regression, and `HOURS_RECON_REMEDIATION_SCOPE_ID` must
exactly match the verified source scope. Every Tier 3/4 dimension creates or
updates exactly one account/dimension instance; instances sharing a safe
root-cause key may be grouped into one systemic workstream, and reloading the
same `retrieval_id` creates no duplicates. A selected T1 goal stays optional if
refreshed evidence reaches T2.

Restart the local server in MCP mode if needed and smoke-test `/api/status`,
`/api/data`, and the dashboard.

## Important architecture boundary

Glean Pi owns the authenticated Salesforce, Rocketlane, and Slack MCP sessions.
The local Python server cannot invoke Pi's connected tools directly and stores no
SaaS credentials. In MCP mode, its refresh button reloads the latest private
snapshot; a new external fetch is initiated by asking Glean Pi to "run Hours
Recon MCP refresh." The remediation execution workspace prepares selected-path
packets and reviewed owner handoffs. Salesforce and Rocketlane writes still
require a fresh read, schema/field validation, and explicit user confirmation;
only a later complete refresh can validate the outcome. Slack handoffs use the
private local outbox: the dashboard queues but never sends, and the user asks
Glean Pi to "send pending Hours Recon messages" so the `hours-recon-slack-send`
skill can deliver through the user's connected Slack identity and record the
returned permalink.
