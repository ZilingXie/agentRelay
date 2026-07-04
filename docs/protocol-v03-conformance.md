# AgentRelay Protocol v0.3 Conformance Runner

The v0.3 conformance runner proves that an AgentRelay server and two agent
identities can complete the core collaboration loop:

```text
create -> target event -> target claim -> artifact -> requester event ->
requester claim -> close -> task events -> timeline
```

The runner does not test an agent's private reasoning quality. It tests the
relay-facing contract that any agent client must satisfy.

## Local Development

Run against a temporary local relay:

```bash
npm run test:protocol:v03:conformance
```

This starts a local AgentRelay server with disposable seeded identities
`zac-agent` and `frank-agent`, runs the complete flow, closes the task, and
deletes the temporary database when done.

## Real Relay

Use two disposable conformance agents. Do not use a human's normal production
agent identity, because the runner creates, claims, and closes a real task.

```bash
python3 scripts/protocol_v03_conformance_runner.py \
  --base-url https://server.stellarix.space/agentrelay/api \
  --agent-a-id conformance-a-agent \
  --agent-a-username conformance-a \
  --agent-a-token "$AGENT_A_TOKEN" \
  --agent-b-id conformance-b-agent \
  --agent-b-username conformance-b \
  --agent-b-token "$AGENT_B_TOKEN"
```

Environment variable form:

```bash
export AGENTRELAY_CONFORMANCE_BASE_URL=https://server.stellarix.space/agentrelay/api
export AGENTRELAY_CONFORMANCE_AGENT_A_ID=conformance-a-agent
export AGENTRELAY_CONFORMANCE_AGENT_A_USERNAME=conformance-a
export AGENTRELAY_CONFORMANCE_AGENT_A_TOKEN=...
export AGENTRELAY_CONFORMANCE_AGENT_B_ID=conformance-b-agent
export AGENTRELAY_CONFORMANCE_AGENT_B_USERNAME=conformance-b
export AGENTRELAY_CONFORMANCE_AGENT_B_TOKEN=...

python3 scripts/protocol_v03_conformance_runner.py
```

The runner never prints tokens.

## Expected Output

Successful output:

```json
{
  "ok": true,
  "protocol_version": "agent-collab-v0.3",
  "status": "completed",
  "checked": [
    "health",
    "task.create.v0.3",
    "target.agent_event",
    "target.precise_claim",
    "artifact.submit.v0.3",
    "requester.agent_event",
    "requester.precise_claim",
    "task.close.v0.3",
    "task.events",
    "task.timeline"
  ]
}
```

Failure output is JSON on stderr:

```json
{
  "ok": false,
  "error": "task.completed missing agent-collab-v0.3"
}
```

## What It Verifies

- Health endpoint is reachable.
- Requester can create a v0.3 task.
- Target receives a durable `task.pending` event.
- Target can precisely claim the task.
- Target can submit a v0.3 artifact without completing the task.
- Pending ownership moves back to requester.
- Requester receives a durable `task.pending` event.
- Requester can precisely claim the returned task.
- Completion owner can close the task.
- Task events include v0.3 protocol payloads.
- Timeline reconstructs the task history.
- Artifact source references are redacted in audit payloads.
- WebSocket-style event payloads stay secret-safe and point to `payloadRef`.

## What It Does Not Verify

- Local Codex App thread creation.
- User-owned inbox/thread adapters.
- The quality of an agent's private reasoning.
- Calendar, filesystem, Slack, WeChat, or other local integrations.
