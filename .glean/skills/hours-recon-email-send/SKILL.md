---
name: hours-recon-email-send
description: Send reviewed weekly professional-services hours digests from the local Hours Recon email outbox through the user's authenticated Gmail identity. Use when the user says "send pending Hours Recon digests," "send the queued hours digests," or asks to process the Hours Recon email outbox.
---

# Hours Recon Email Digest Send

Deliver weekly hours-threshold digests that the user already reviewed and queued in the local Hours Recon dashboard. The local app never stores mail credentials and never sends the digests itself.

## Preconditions

- Work in the Hours Recon repository root.
- The local dashboard must be running, normally at `http://127.0.0.1:8765`.
- Treat the user's request to **send pending Hours Recon digests** as the final send instruction only for items currently returned by the pending outbox.
- Never send an item with status `sending`, `needs_review`, `sent`, or `cancelled`.
- Never modify the queued `subject` or `body_text`. That body is the exact text reviewed in the dashboard and already contains the required Glean Pi attribution.
- These digests go to **colleagues**, including Salesforce account owners. There is no undo. Treat every send as externally visible.

## Workflow

1. List pending digests:

   ```bash
   python3 scripts/hours_recon_email_outbox.py list --status pending
   ```

2. If there are no pending items, tell the user and stop.
3. Process items **sequentially**, never in parallel.
4. Verify the sender identity **once per run**, before the first send. Call `GMAIL_GET_PROFILE` and confirm the authenticated address equals the `sender_email` on the queued items. If they differ, stop and report it: a mismatch means the digest would be sent from the wrong mailbox.
5. Re-read the recipients on the item. Every address must be on the configured allowlist; the dashboard enforces this at queue time and again at claim time, so if a claim fails on allowlist grounds, do not work around it.
6. Immediately before the external send, atomically claim the item using the `id` and `version` from the list response:

   ```bash
   python3 scripts/hours_recon_email_outbox.py claim <outbox_id> --version <version>
   ```

   Re-read `recipients`, `subject`, `body_text`, and the incremented `version` from the claim response. If claiming fails, do not send.
7. Send exactly once with the Gmail MCP tool, using the claimed values verbatim:
   - `recipient_email` / `to`: the claimed recipients.
   - `subject`: the claimed subject, unchanged.
   - `body`: the claimed `body_text`, unchanged, as plain text.
8. Only after Gmail returns a real message ID, record delivery using the claimed item's incremented version:

   ```bash
   python3 scripts/hours_recon_email_outbox.py sent <outbox_id> --version <claimed_version> \
     --provider-message-id <gmail_message_id>
   ```

9. If the send call errors or its delivery result is uncertain, do **not** retry. Mark the item for review so a later run cannot duplicate it:

   ```bash
   python3 scripts/hours_recon_email_outbox.py uncertain <outbox_id> --version <claimed_version> --error "<short error>"
   ```

10. If Gmail confirms delivery but recording the message ID fails, report the message ID and the audit failure. Do not resend.
11. To reconcile a `needs_review` item in a later turn:
    - If the user finds the delivered message, record it with the `sent` command; completion accepts `needs_review` items.
    - Retry only after the user explicitly confirms they checked the mailbox and no copy was delivered:

      ```bash
      python3 scripts/hours_recon_email_outbox.py retry <outbox_id> --version <version> --confirmed-not-delivered
      ```

12. Return a concise result listing each recipient, the account names covered, and the Gmail message ID.

## Assembling a digest

Digests are not created by this skill. Detection happens during a refresh; assembly groups the pending crossings into one email per recipient:

```bash
python3 scripts/hours_recon_email_outbox.py pending-crossings   # what would be reported
python3 scripts/hours_recon_email_outbox.py assemble            # create the digests
```

Assembly requires `HOURS_RECON_NOTIFY_MODE=active`. It is idempotent for a given week and recipient, and produces nothing when no crossings are pending. Run it after a refresh, review the queued bodies, then send.

## Safety rules

- The dashboard queue action is not delivery; never claim otherwise.
- A claimed item is excluded from future pending runs, preventing duplicate sends after a crash or uncertain response.
- Never reset `needs_review` to pending unless the user explicitly confirms the mailbox was checked and nothing was delivered.
- Never add, remove, or substitute a recipient. If a recipient looks wrong, stop and report it.
- Never edit the reviewed body to add commentary, soften a figure, or fix a typo. Cancel and re-assemble instead.
- Do not send a digest whose `crossing_count` is zero.
- Before the first send of a run, state plainly to the user how many emails will go out and to whom.
