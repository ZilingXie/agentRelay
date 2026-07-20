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
templates, functions, loops, commands, file access, or arbitrary URLs. Adapter
contract v2 may publish `agent_tools` definitions that update the title,
description, and input JSON Schema of a fixed local tool allowlist. It cannot
add a new tool, handler, operation, route, identity source, approval source, or
protected protocol field. For create and follow-up, the runtime pre-registers
one optional `/message/metadata` slot. A bundle may declare bounded public
fields inside that non-authoritative container, but cannot choose another
destination or expose metadata on ordinary replies. MCP compiles the verified public Schema locally and
emits `notifications/tools/list_changed` only after the complete bundle passes
validation. MCP rejects unknown or duplicate slots and targets, unsafe JSON
Pointers, prototype-property names, protected-slot rebinding, untrusted Agent
input fields, and unknown adapter fields.

MCP validates the authority id and configured Relay path, schema digest, bundle
digest, immutable revision, adapter contract, bundle size, and publication and
expiration window before activation and again before use. A bundle that
publishes dynamic Agent tools must also carry an Ed25519 signature over its
protocol identity, revision, schema and bundle digests, adapter contract,
authority, validity window, and required capabilities. MCP verifies that
signature before compiling any Agent-facing Schema.

The signing public key and `key_id` are published in the Relay manifest. The
configured Relay origin and TLS therefore remain the trust source for the first
key observation; the signature detects substituted cached or persisted bundle
content and provides an explicit rotation identity, but is not an external PKI
root. It does not protect against simultaneous compromise of the Relay host and
its signing private key. Keep the private key outside Git, readable only by the
Relay process, and rotate `key_id` whenever the key changes.

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
Relay additionally keeps `AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED=0` during the
compatibility deployment. After capable MCP runtimes are installed, enabling
it publishes adapter contract v2, bundle revision 4, and the
`dynamic_agent_tool_schema_v1` capability requirement. Relay refuses to publish
that mode unless `AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE` and
`AGENTRELAY_PROTOCOL_SIGNING_KEY_ID` are configured and signing succeeds.

Protocol documents and examples are cached for inspection only. They are never
automatically inserted into Local Agent context.

## Guardrail relationship

Automatic upgrade is a Guardrail subsystem, not a replacement for authorization.
The non-hot-updatable MCP Core still requires a trusted Local Inbox approval or
a narrowly scoped service-policy grant, resyncs current Task context, preserves
the idempotency key, and validates the local transition. Relay then independently
enforces authenticated identity, schema, permission, idempotency, and state-machine
rules before persistence.
