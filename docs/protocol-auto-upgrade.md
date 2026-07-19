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
tool registration. MCP validates the bundle digest, authority/origin, operation
allowlist, binding sources, protected targets, and JSON Schema before activation
and again before use.

## Activation And Recovery

Bundles are cached per Relay authority and origin in immutable digest-named
directories. MCP stages and validates a bundle before atomically updating the
active pointer under an inter-process lock. The prior verified pointer becomes
last-known-good. A mutation may retry once with its original idempotency key.
Failure to verify never changes the active pointer.

Protocol documents and examples are cached for inspection only. They are never
automatically inserted into Local Agent context.
