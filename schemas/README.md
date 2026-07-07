# AgentRelay Protocol v0.3 Schemas

These JSON Schemas are the public contract for AgentRelay Protocol v0.3. They
are intentionally small: the relay defines collaboration state, delivery,
ownership, audit, and safe envelopes, while each local agent remains responsible
for reasoning and private human interaction.

## Core Request Schemas

- `task-create.schema.json`: requester agent creates a two-agent task with
  `done_criteria`, `completion_owner_agent_id`, `pending_on_agent_id`, and
  `next_action`.
- `artifact-submit.schema.json`: acting agent submits an action result and hands
  responsibility to the next pending agent. Artifacts do not complete tasks.
- `task-amend.schema.json`: requester-side agent records human-authorized goal
  changes, increments `goal_version`, and starts a new agent-agent exchange.
- `task-close.schema.json`: completion owner closes the task and records whether
  the final authority was an agent or a human through that agent.

## Audit And Delivery Schemas

- `task-event.schema.json`: append-only audit event for task lifecycle,
  ownership, artifact, delivery, thread binding, and completion history.
- `agent-event.schema.json`: durable notification event for local listeners.
  Push payloads stay secret-safe; listeners fetch full task content over HTTP.
- `task-timeline.schema.json`: derived dashboard-ready activity log built from
  task events.

## Reusable Schemas

- `part.schema.json`: typed content block.
- `source-ref.schema.json`: public/redacted/private evidence reference.
- `response-envelope.schema.json`: agent-first success/error API response shape.
- `agent-card.schema.json`: A2A-shaped discovery card with AgentRelay metadata.

## Compatibility

New clients should send `agent-collab-v0.3`. The server still accepts legacy and
v0.2 compatibility payloads while the ecosystem migrates.

## Related Public Resources

- Protocol guide: `/agentrelay/docs/protocol-v03.md`
- Conformance runner guide: `/agentrelay/docs/protocol-v03-conformance.md`
- Validated examples: `/agentrelay/examples/protocol-v03/`
- Example task create: `/agentrelay/examples/protocol-v03/meeting-task-create.json`
