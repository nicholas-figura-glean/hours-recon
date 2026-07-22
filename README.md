# Hours Recon

A local, read-only AIOM dashboard that reconciles hours sold in Salesforce with billable time recorded in Rocketlane.

## What it does

- Resolves the requesting AIOM and finds Salesforce accounts assigned through the Account AIOM field.
- Includes only `Closed Won` opportunities.
- Infers sold hours from opportunity products and legacy opportunity names.
- Supports multiple packages per account and multiplies package hours by line-item quantity.
- Allocates Rocketlane hours FIFO against the earliest-expiring active package, then applies pre-entitlement activity to the earliest later package that has closed by the report date.
- Expires each package one year after its Salesforce close date; the expiration date is inclusive.
- Separates usable remaining hours, 90-day at-risk hours, expired-unused hours, and true overage beyond eligible sold capacity.
- Flags weekly account inactivity and missing requesting-AIOM time separately.
- Includes archived Rocketlane projects so historical billable entries are not omitted.
- Excludes future-dated entries and not-yet-active entitlement from the as-of balance.
- Preserves unmatched accounts, unknown packages, and excess negative corrections in a review queue instead of silently dropping them.

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

The snapshot and report cache under `var/` are ignored by Git, written with owner-only permissions, and expire after 30 days by default. A failed import leaves the last successful dataset visible and reports a redacted error reference.

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

Package mappings are in [`config/packages.json`](./config/packages.json).

Supported Outcome tiers:

- Starter: 20 hours
- Standard: 50 hours
- Select: 100 hours
- Advanced: 200 hours
- Strategic: 300 hours

Supported Growth tiers are 20, 50, 100, and 300 hours. Explicit wording such as `Growth Package (10 hours)` or `Custom 300 PS hours` is honored even when it is outside the standard tier list.

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

The suite covers package inference, real Salesforce product naming, custom-package fallbacks, aliases, collision safety, fuzzy suggestions, FIFO allocation, pre-entitlement timing, future-entitlement isolation, inclusive expiration, overage, billable filtering, weekly activity, risk boundaries, deterministic ordering, JSON output, and total rollups.

## Repository layout

```text
app.py                       Local HTTP server
hours_recon/salesforce.py    Salesforce REST connector
hours_recon/rocketlane.py    Rocketlane REST connector
hours_recon/inference.py     Package inference
hours_recon/matching.py      Conservative account matching
hours_recon/reconcile.py     FIFO, risk, weekly checks, totals
hours_recon/service.py       Refresh orchestration and cache
static/index.html            Interactive dashboard
config/                      Package and account mappings
tests/                       Unit and integration-shape tests
```
