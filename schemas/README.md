# AgentRelay Protocol Schemas

These JSON Schemas are the public contract for AgentRelay Protocol v0.3. They
are intentionally small: the relay defines collaboration state, delivery,
ownership, audit, and safe envelopes, while each local agent remains responsible
for reasoning and private human interaction.

Protocol-specific filenames are additive. The unversioned schemas remain the
v0.3 contract; `*v04*` preserves the completed v0.4 baseline; `*v05*` defines
the v0.5 maintenance-window target without changing the default protocol.

## Protocol v0.5 Schemas

- `protocol-v05-common.schema.json`: authoritative Task, Message, outbox, parts,
  and mutation-context definitions.
- `task-create-v05.schema.json`: native v0.5 Task and first Message creation.
- `task-message-v05.schema.json`: strict alternating next-Message mutation.
- `message-ack-v05.schema.json`: versioned durable Listener ACK.
- `message-delivery-fail-v05.schema.json`: guarded non-retryable persistence NACK.
- `task-terminal-v05.schema.json`: requester completion or authorized failure.
- `task-detail-v05.schema.json`: full Task plus ordered immutable Messages.
- `task-visibility-v05.schema.json`: Server-computed diagnosis projection.
- `task-visibility-batch-v05.schema.json`: ordered, unique batch lookup request.

The v0.5 bundle is accepted but non-default and reports `write_mode=closed`
until the maintenance-window switch.

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

Production clients still send `agent-collab-v0.3` unless they explicitly use an
accepted versioned surface. v0.5 remains non-writable while implementation and
cross-component rehearsal are incomplete.

## Related Public Resources

- Protocol guide: `/agentrelay/docs/protocol-v03.md`
- Conformance runner guide: `/agentrelay/docs/protocol-v03-conformance.md`
- Validated examples: `/agentrelay/examples/protocol-v03/`
- Example task create: `/agentrelay/examples/protocol-v03/meeting-task-create.json`
- v0.5 lifecycle: `/agentrelay/docs/task-lifecycle-v05.md`
- v0.5 conformance status: `/agentrelay/docs/protocol-v05-conformance.md`
- v0.5 examples: `/agentrelay/examples/protocol-v05/`
