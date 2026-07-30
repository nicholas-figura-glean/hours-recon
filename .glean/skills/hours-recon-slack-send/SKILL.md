---
name: hours-recon-slack-send
description: Send reviewed messages from the local Hours Recon Slack MCP outbox through the user's authenticated Slack identity. Use when the user says “send pending Hours Recon messages,” “send the queued Hours Recon follow-ups,” or asks to process the Hours Recon Slack outbox.
---

# Hours Recon Slack MCP Send

Use the connected Slack MCP tools to deliver messages that the user already reviewed and explicitly queued in the local Hours Recon dashboard. The local app never stores Slack credentials and never sends the messages itself.

## Preconditions

- Work in the Hours Recon repository root.
- The local dashboard must be running, normally at `http://127.0.0.1:8765`.
- Treat the user's request to **send pending/queued Hours Recon messages** as the final send instruction only for items currently returned by the pending outbox.
- Never send an item with status `sending`, `needs_review`, `sent`, or `cancelled`.
- Never modify the queued `message_text`; it is the exact body reviewed in the dashboard and already contains the required Glean Pi attribution.

## Workflow

1. List pending items with:

   ```bash
   python3 scripts/hours_recon_slack_outbox.py list --status pending
   ```

2. If there are no pending items, tell the user and stop.
3. Process items **sequentially**, never in parallel.
4. Resolve `recipient_query` exactly:
   - A Slack user ID can be used directly.
   - For a name, email, or `@handle`, call `glean_Slack_MCP_slack_search_users`. Require one unambiguous exact user and use its `user_id` as `channel_id`.
   - For `#channel`, call `glean_Slack_MCP_slack_search_channels`. Require one exact channel and use its channel ID.
   - If resolution is ambiguous or absent, leave the item pending, report the problem, and continue to the next item. Never guess.
5. Immediately before the external send, atomically claim the item using the `id` and `version` from the list response:

   ```bash
   python3 scripts/hours_recon_slack_outbox.py claim <outbox_id> --version <version>
   ```

   Re-read `message_text`, `recipient_query`, and the incremented `version` from the claim response. If claiming fails, do not send.
6. Call `glean_Slack_MCP_slack_send_message` exactly once with:
   - `channel_id`: the resolved user or channel ID.
   - `message`: the claimed `message_text`, unchanged.
   - `_user_goal`: the user's exact request from this turn.
7. Only after Slack MCP returns a real message permalink, record delivery using the claimed item's incremented version:

   ```bash
   python3 scripts/hours_recon_slack_outbox.py sent <outbox_id> --version <claimed_version> --recipient-id <resolved_id> --permalink <slack_permalink>
   ```

8. If the Slack send call errors or its delivery result is uncertain, do **not** retry. Mark the item for review so a later run cannot duplicate it:

   ```bash
   python3 scripts/hours_recon_slack_outbox.py uncertain <outbox_id> --version <claimed_version> --error "<short error>"
   ```

9. If Slack confirms delivery but recording the permalink fails, report the Slack permalink and the audit failure. Do not resend.
10. To reconcile a `needs_review` item in a later turn:
    - If the user finds the delivered message, use its real permalink with the `sent` command; completion accepts `needs_review` items.
    - Retry only after the user explicitly confirms they checked Slack and no copy was delivered:

      ```bash
      python3 scripts/hours_recon_slack_outbox.py retry <outbox_id> --version <version> --confirmed-not-delivered
      ```

11. Return a concise result with each recipient and Slack message link.

## Safety rules

- The dashboard queue action is not delivery; never claim otherwise.
- A claimed item is excluded from future pending runs, preventing duplicate sends after a crash or uncertain response.
- Never reset `needs_review` to pending unless the user explicitly confirms Slack was checked and the message was not delivered.
- Do not read private Slack messages to resolve a recipient. Use directory/channel search only.
- Do not send to an ambiguous user, an unresolved channel, or an externally shared channel rejected by Slack MCP.
- Do not use raw Slack passwords, cookies, bot tokens, or user OAuth tokens.
- The `sent` API accepts only HTTPS `*.slack.com` permalinks and records the message digest rather than altering the reviewed body.
