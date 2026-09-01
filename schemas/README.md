# AgentRelay Protocol Schemas

These JSON Schemas are the public contract for AgentRelay Protocol v0.3. They
are intentionally small: the relay defines collaboration state, delivery,
ownership, audit, and safe envelopes, while each local agent remains responsible
for reasoning and private human interaction.

Protocol-specific filenames are additive. The unversioned schemas remain the
v0.3 contract; `*v04*` preserves the completed v0.4 baseline; `*v05*` is the
active production lifecycle; `*v06*` stages offline delivery without changing
the compatibility default.

## Protocol v0.5 Schemas

- `protocol-v05-common.schema.json`: authoritative Task, Message, outbox, parts,
  and mutation-context definitions.
- `task-create-v05.schema.json`: native v0.5 Task and first Message creation.
- `task-message-v05.schema.json`: strict alternating next-Message mutation.
- `message-ack-v05.schema.json`: versioned durable Listener ACK.
- `message-delivery-fail-v05.schema.json`: guarded non-retryable persistence NACK.
- `event-ack-v05.schema.json`: non-recursive informational outbox ACK.
- `task-terminal-v05.schema.json`: requester completion or authorized failure.
- `task-detail-v05.schema.json`: full Task plus ordered immutable Messages.
- `task-visibility-v05.schema.json`: Server-computed diagnosis projection.
- `task-visibility-batch-v05.schema.json`: ordered, unique batch lookup request.

## Protocol v0.6 Schemas

- `protocol-v06-common.schema.json`: v0.6 Task, Message, and recoverable parked outbox objects.
- `task-create-v06.schema.json`: v0.6 Task creation without transient readiness admission.
- `task-detail-v06.schema.json`: full v0.6 Task plus ordered Messages.
- `task-message-v06.schema.json`: next alternating v0.6 Message.
- `task-followup-v06.schema.json`: v0.6 follow-up Task creation.
- `file-part-v06.schema.json`: Message part referencing a task-scoped uploaded
  file blob (`file_id`, `name`, `mime_type`, `size_bytes`, `sha256`); bytes
  never ride the JSON Message.
- `message-ack-v06.schema.json`: epoch-bound durable v0.6 Listener ACK.
- `message-delivery-fail-v06.schema.json`: recoverable persistence NACK that parks delivery.
- `event-ack-v06.schema.json`: v0.6 informational Event ACK.
- `task-terminal-v06.schema.json`: v0.6 requester completion or authorized business failure.
- `task-visibility-v06.schema.json`: v0.6 visibility including `waiting_listener`.
- `task-visibility-batch-v06.schema.json`: ordered, unique v0.6 batch lookup request.

The v0.6 bundle is accepted but non-default. It reports `write_mode=v06` only
when the Server is explicitly started in v0.6 mutation mode.

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

Production v0.5 clients continue to use their versioned surface. v0.6 remains a
staged, additive contract until Server, Client, and Hermes rollout gates pass.

## Related Public Resources

- Protocol guide: `/agentrelay/docs/protocol-v03.md`
- Conformance runner guide: `/agentrelay/docs/protocol-v03-conformance.md`
- Validated examples: `/agentrelay/examples/protocol-v03/`
- Example task create: `/agentrelay/examples/protocol-v03/meeting-task-create.json`
- v0.5 lifecycle: `/agentrelay/docs/task-lifecycle-v05.md`
- v0.5 conformance status: `/agentrelay/docs/protocol-v05-conformance.md`
- v0.5 examples: `/agentrelay/examples/protocol-v05/`
- v0.6 lifecycle: `/agentrelay/docs/task-lifecycle-v06.md`
- v0.6 examples: `/agentrelay/examples/protocol-v06/`
