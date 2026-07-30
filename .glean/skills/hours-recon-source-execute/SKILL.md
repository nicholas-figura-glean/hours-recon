---
name: hours-recon-source-execute
description: Execute reviewed Salesforce or Rocketlane writes from the local Hours Recon source-action outbox. Use when the user asks to execute, push, or run pending or queued Hours Recon source actions.
---

# Hours Recon Source Action Execution

Execute only actions that the user reviewed and queued in the local dashboard. The dashboard stores no SaaS credentials and never performs writes itself; use the authenticated Salesforce and Rocketlane tools in the active Glean Pi session.

## Workflow

1. From the repository root, list pending actions:

   ```bash
   python3 scripts/hours_recon_source_outbox.py list --status pending
   ```

2. If none are pending, report that and stop. Process actions sequentially.
3. For each item, use `glean_find_skills` to discover the current connector skill and read its `SKILL.md` plus exact schemas for the queued read/schema/write tool. Never guess a server ID, tool name, custom-field ID, or argument.
4. Before claiming or writing:
   - Re-read every `record_id` using the authenticated connector.
   - Read the current object/custom-field schema.
   - Resolve descriptive field labels in `proposed_fields` to real writable field IDs. For Rocketlane `Link to Salesforce Opportunity`, require the exact custom field whose observed label matches; never create or substitute a field.
   - Verify the record still belongs to the queued account/workstream and the queued current path is still applicable.
   - Compare the current values with the reviewed `proposed_fields`.
5. Show the user one compact confirmation containing the outbox ID, connector/tool, record IDs and links, exact current values, exact proposed values, and post-write validation read. Use `ask_clarifying_question` with options such as `Execute this write` and `Cancel`; the earlier dashboard review is not final write confirmation.
6. If the user cancels, leave the item pending. If confirmed, immediately claim it:

   ```bash
   python3 scripts/hours_recon_source_outbox.py claim <outbox_id> --version <version>
   ```

   Re-read the claimed payload and use its incremented version. If claiming fails, do not write.
7. Call the resolved write tool exactly once. Do not add fields or records that are absent from the claimed action.
8. Immediately re-read every changed record. Only after the desired values are observed, record completion with one or more real HTTPS source links:

   ```bash
   python3 scripts/hours_recon_source_outbox.py completed <outbox_id> --version <claimed_version> \
     --source-link <record_url> --result "Observed <field>=<value> after write"
   ```

9. If the write call fails or its result is uncertain, do not retry. Mark it for review:

   ```bash
   python3 scripts/hours_recon_source_outbox.py uncertain <outbox_id> --version <claimed_version> --error "<short error>"
   ```

10. A `needs_review` item may be marked completed only after a fresh read proves the reviewed values were applied. Retry only after a fresh read proves they were not applied and the user gives a new explicit confirmation:

    ```bash
    python3 scripts/hours_recon_source_outbox.py retry <outbox_id> --version <version> --confirmed-not-applied
    ```

11. After completed writes, tell the user to run a complete Hours Recon refresh. A write response or post-write record read does not validate the reconciliation outcome.

## Safety boundaries

- Never execute browser-supplied tool names, target IDs, or fields outside the claimed server-validated action.
- Never execute values containing unresolved placeholders.
- Never treat queueing, claiming, or a connector success response as reconciliation validation.
- Never parallelize writes. Retry an uncertain write only after a fresh read proves it was not applied and the user confirms again.
- Salesforce and Rocketlane writes require fresh schema/field validation and final confirmation immediately before each write.
