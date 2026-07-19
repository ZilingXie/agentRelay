# AgentRelay Automatic Protocol Upgrade

## Purpose

AgentRelay separates stable local Agent intent from the versioned wire contract.
Local Agents call stable semantic MCP tools such as create, reply, complete,
fail, and follow-up. The MCP runtime reads a verified protocol bundle, derives
identity and current Task context locally, and produces the wire payload that
Relay validates again.

Wire-only changes can ship as immutable protocol bundles. Changes to lifecycle,
authority, transport, persistence, approval, or local side effects require a
new MCP runtime release.

## Negotiation

Authenticated clients call `POST /agentrelay/api/protocols/negotiate` with the
runtime version, compiled capabilities, supported protocol families, and active
bundle pointer. Relay returns one action:

- `up_to_date`: the verified active bundle is current.
- `hot_patch`: fetch, verify, and atomically activate the target bundle.
- `client_release_required`: installed code cannot implement the protocol.
- `hot_rollback`: activate an older bundle revision already authorized by the
  same Relay authority.

Clients never infer compatibility by comparing version strings. The Relay
protocol registry is authoritative.

## Non-Programmable Adapter Boundary

The largest risk is turning Server-provided protocol rules into a remotely
programmable execution environment. The adapter is therefore restricted data
mapping. Identity, approval, authorization guardrails, idempotency, endpoint
allowlists, and local side effects remain in the non-hot-updatable MCP core.

Bundle bindings may read only stable tool input, local identity, normalized Task
context, and MCP runtime idempotency context. They cannot contain scripts,
templates, functions, loops, commands, file access, arbitrary URLs, or dynamic
tool registration. Adapter v2 requires the exact compiled operation and semantic
slot contracts. MCP rejects unknown or duplicate slots and targets, unsafe JSON
Pointers, prototype-property names, protected-slot rebinding, and unknown adapter
fields.

MCP validates the authority id and configured Relay path, schema digest, bundle
digest, immutable revision, adapter contract, bundle size, and publication and
expiration window before activation and again before use. Relay remains the
trusted publisher; these controls do not defend against a fully compromised
Relay host. Independent bundle signing is deferred.

## Activation And Recovery

Bundles are cached per Relay authority and origin in immutable digest-named
directories. MCP stages and validates a bundle before atomically updating the
active pointer under an inter-process lock. The prior verified pointer becomes
last-known-good. A mutation may retry once with its original idempotency key.
Failure to verify never changes the active pointer.

Only an explicit Relay `hot_rollback` action may reduce the active revision. The
client rejects a different digest under the same revision. Operators can set
`AGENTRELAY_DISABLE_HOT_UPDATE=1` on MCP or
`AGENTRELAY_HOT_UPDATE_ENABLED=0` on Relay as independent emergency stops.

Protocol documents and examples are cached for inspection only. They are never
automatically inserted into Local Agent context.

## Guardrail relationship

Automatic upgrade is a Guardrail subsystem, not a replacement for authorization.
The non-hot-updatable MCP Core still requires a trusted Local Inbox approval or
a narrowly scoped service-policy grant, resyncs current Task context, preserves
the idempotency key, and validates the local transition. Relay then independently
enforces authenticated identity, schema, permission, idempotency, and state-machine
rules before persistence.
