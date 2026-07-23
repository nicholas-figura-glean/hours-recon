# Hours Recon

A local, read-only AIOM dashboard that reconciles hours sold in Salesforce with billable time recorded in Rocketlane.

## What it does

- Resolves the requesting AIOM and finds Salesforce accounts assigned through the Account AIOM field.
- Includes only `Closed Won` opportunities.
- Infers sold hours from Opportunity Products, approved/primary Quote Lines when Opportunity Products are absent, and legacy opportunity names.
- Supports multiple packages per account and multiplies package hours by line-item quantity.
- Allocates Rocketlane hours FIFO against the earliest-expiring active package, then applies pre-entitlement activity to the earliest later package that has closed by the report date.
- Expires each package one year after its Salesforce close date; the expiration date is inclusive.
- Separates usable remaining hours, 90-day at-risk hours, expired-unused hours, and true overage beyond eligible sold capacity.
- Flags weekly account inactivity and missing requesting-AIOM time separately.
- Includes archived Rocketlane projects so historical billable entries are not omitted.
- Excludes future-dated entries and not-yet-active entitlement from the as-of balance.
- Preserves unmatched accounts, unknown packages, and excess negative corrections instead of silently dropping them.
- Scores entitlement source, hours mapping, service period, project linkage, and time quality independently from Tier 1 through Tier 4.
- Keeps current reported totals unchanged in observe-only mode while separating governed and provisional exposure.
- Opens one private, deduplicated remediation case per account whenever any evidence dimension is Tier 3 or Tier 4.

## Requirements

- Python 3.9+
- Glean Pi with authenticated Salesforce and Rocketlane MCP integrations for the recommended MCP workflow

There are no third-party Python or JavaScript dependencies. Direct Salesforce/Rocketlane API credentials are optional and only needed for the legacy `live` connector mode.

## Run the demo

```bash
python3 app.py --demo
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

Demo mode uses fictional data and is always labeled clearly in the dashboard.

## Refresh through MCP (recommended)

1. Authenticate the Salesforce and Rocketlane integrations in Glean Pi.
2. In this repository, ask Pi: **“Run Hours Recon MCP refresh.”** The project skill at `.glean/skills/hours-recon-refresh/SKILL.md` retrieves the source records, writes `var/mcp_snapshot.json`, and imports the report.
3. Configure and run the local server:

   ```dotenv
   HOURS_RECON_MODE=mcp
   HOURS_RECON_MCP_SNAPSHOT_PATH=var/mcp_snapshot.json
   ```

   ```bash
   python3 app.py
   ```

4. Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The dashboard labels the source as **MCP data**.

Glean Pi owns the authenticated MCP session; the standalone Python server cannot inherit it. Therefore, a new external fetch is initiated from Pi. **Reload MCP snapshot** in the dashboard reprocesses the latest snapshot but does not call MCP itself.

The snapshot and report cache under `var/` are ignored by Git and written with owner-only permissions. The report cache expires after 30 days by default. The remediation database is retained separately at `var/remediation.sqlite3` so workflow history survives report-cache expiry. A failed import leaves the last successful dataset visible and reports a redacted error reference.

## Optional direct API mode

Set `HOURS_RECON_MODE=live`, `HOURS_RECON_REQUESTER_EMAIL`, Salesforce OAuth values, and `ROCKETLANE_API_KEY` as documented in `.env.example`. In this mode, **Refresh data** calls the direct connectors from the local server.

The server intentionally binds only to `127.0.0.1` or `localhost`. It rejects non-loopback Host headers and cross-origin refresh requests because this local application does not provide remote-user authentication.

## Salesforce field discovery

On refresh, the connector reads the complete Account schema and discovers the AIOM field by API name, label, and whether it references `User`. If discovery is ambiguous, set the exact API field name:

```dotenv
SF_AIOM_FIELD=Your_AIOM_Field__c
```

Reference fields match the resolved Salesforce User ID. String and multipicklist fields default to the requester's Salesforce name; override the stored value if needed:

```dotenv
SF_AIOM_MATCH_VALUE=Exact stored Salesforce value
```

## Package inference

Package mappings are in [`config/packages.json`](./config/packages.json). Opportunity-level overrides intentionally short-circuit the entire Opportunity. Otherwise, exact `product_codes` mappings take precedence over line-item overrides and text/price inference and are Tier 1 hours evidence; fallback paths remain available but receive lower governance confidence.

Supported Outcome tiers:

- Starter: 20 hours
- Standard: 50 hours
- Select: 100 hours
- Advanced: 200 hours
- Strategic: 300 hours

Supported Growth tiers are 20, 50, 100, and 300 hours. Explicit wording such as `Growth Package (10 hours)` or `Custom 300 PS hours` is honored even when it is outside the standard tier list.

For MCP refreshes, Opportunity Products are the primary product source. If an Opportunity has none, its approved Quote Lines (or primary Quote Lines when no approved Quote is set) are normalized as line items; the two sources are never combined for one Opportunity.

Inference order:

1. Explicit opportunity or line-item override
2. Explicit hours in the product name
3. Outcome tier name
4. Growth numeric tier
5. Outcome list price fallback
6. Opportunity-name fallback when no product line resolves

Custom packages with no explicit hours remain unresolved until configured. They are never guessed.

### Custom hours overrides

Edit the `overrides` object in `config/packages.json`:

```json
{
  "overrides": {
    "opportunities": {
      "006XXXXXXXXXXXX": 125
    },
    "line_items": {
      "00kXXXXXXXXXXXX": 75
    },
    "product_names": {
      "Exact Salesforce Product Name": 40
    }
  }
}
```

Opportunity and line-item IDs are the safest override keys.

## Salesforce-to-Rocketlane aliases

Exact normalized names match automatically. Normalization handles case, punctuation, parenthetical text, and common legal suffixes. Fuzzy matches are shown as suggestions only.

Add complex relationships to [`config/account_aliases.json`](./config/account_aliases.json):

```json
{
  "aliases": {
    "Orthogonal Networks (DBA Jellyfish)": ["Jellyfish"]
  }
}
```

The key is the exact Salesforce Account name. Values are accepted Rocketlane customer names.

For a governed Tier 2 cross-system mapping, configure stable Rocketlane customer IDs instead of names:

```json
{
  "rocketlane_customer_ids": {
    "001XXXXXXXXXXXX": ["123456"]
  }
}
```

A Rocketlane project carrying the exact Salesforce Account ID is Tier 1. A configured customer-ID crosswalk is Tier 2. Normalized names and aliases remain Tier 3 and launch remediation in observe-only mode.

## Governance and remediation workflow

The default integration mode is observe-only:

```dotenv
HOURS_RECON_GOVERNANCE_MODE=observe_only
HOURS_RECON_REMEDIATION_MODE=observe_only
HOURS_RECON_REMEDIATION_DB_PATH=var/remediation.sqlite3
```

Five evidence dimensions are scored independently:

1. Salesforce entitlement source
2. Sold-hours mapping
3. Entitlement service period
4. Salesforce-to-Rocketlane project linkage
5. Rocketlane project and time-entry quality

The weakest dimension controls the account tier. Tier 1 and Tier 2 are governed; Tier 3 and Tier 4 are provisional. Observe-only mode does not replace the existing portfolio totals. It adds governed/provisional shadow metrics and opens a local remediation workflow for every Tier 3/4 dimension.

Each Salesforce Account has one deterministic case containing dimension-level gaps. Repeated imports of the same source retrieval update nothing and do not create duplicates. A new retrieval can resolve or reopen a gap only when all coverage flags are literal `true`, `through_date` equals the report date, `scope_id` is accompanied by literal `scope_verified: true` after connector-identity validation, and that value exactly matches the configured `HOURS_RECON_REMEDIATION_SCOPE_ID`. Without both sides of that verification, the queue may observe gaps but cannot resolve, reopen, or fail validation. A resolved gap that later returns is reopened with regression history. Marking a gap ready for validation never promotes it by itself.

The local workflow supports `open`, `acknowledged`, `in_progress`, `pending_validation`, `snoozed`, `waived`, and `resolved` gap states. Waivers require a reason and expiration date and remain provisional.

Read-only and local workflow endpoints:

- `GET /api/data`
- `GET /api/status`
- `GET /api/remediation/cases`
- `GET /api/remediation/cases/{case_id}`
- `POST /api/remediation/gaps/{gap_id}/actions`
- `POST /api/refresh`

The remediation database and its parent directory use owner-only permissions. Dashboard mutations require an ephemeral per-process action token and responses deny framing to reduce local CSRF/clickjacking risk. The application remains a loopback-only, single-user tool rather than a multi-user authorization system. No remediation action writes to Salesforce or Rocketlane.

## Calculation rules

For each billable time entry, packages are eligible when:

```text
close_date <= entry_date <= close_date + 1 year
```

Eligible capacity is consumed by earliest expiration, then close date, then stable package ID. Packages active on the time-entry date are used first. Historical entries that predate an entitlement consume the earliest later package that has closed by the report date and are surfaced as `pre_entitlement_activity` timing warnings. Packages still in the future remain unavailable. Time beyond all eligible sold capacity, or after expiration without a later entitlement, is overage. Negative Rocketlane corrections reduce overage first and then reverse the latest consumed package capacity; corrections larger than prior usage are surfaced for review. The configured `HOURS_RECON_TIMEZONE` controls the report date and Monday-based weekly boundaries.

Risk bands for unused hours:

- Expired: expiration is before the report date
- Critical: 0–30 days
- High: 31–60 days
- Medium: 61–90 days
- Healthy: more than 90 days

## Tests

```bash
python3 -m unittest discover -v
```

The suite covers package inference, canonical ProductCode mappings, explicit service periods, evidence-tier weakest-link behavior, governed/provisional conservation, aliases and match provenance, remediation fingerprints and lifecycle, queue idempotency, regression reopening, private persistence, collision safety, fuzzy suggestions, FIFO allocation, pre-entitlement timing, future-entitlement isolation, inclusive expiration, overage, billable filtering, weekly activity, risk boundaries, deterministic ordering, JSON output, and total rollups.

## Repository layout

```text
app.py                       Local HTTP server
hours_recon/salesforce.py    Salesforce REST connector
hours_recon/rocketlane.py    Rocketlane REST connector
hours_recon/inference.py     Package inference and exact SKU mappings
hours_recon/evidence.py      Tier scoring and governed/provisional partitions
hours_recon/matching.py      Conservative matching with provenance
hours_recon/reconcile.py     FIFO, risk, weekly checks, totals
hours_recon/remediation.py   Case/gap policy and deterministic identities
hours_recon/remediation_store.py  Private SQLite workflow persistence
hours_recon/service.py       Refresh, cache, governance, and queue orchestration
static/index.html            Interactive dashboard
config/                      Package and account mappings
tests/                       Unit and integration-shape tests
```
