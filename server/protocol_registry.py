from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from server.protocol_v03 import PROTOCOL_V03
from server.protocol_v04 import PROTOCOL_V04
from server.protocol_v05 import PROTOCOL_V05


PROTOCOL_NAME = "agent-collab"
CURRENT_PROTOCOL_VERSION = PROTOCOL_V03
CURRENT_PROTOCOL_SEMVER = "0.3.0"
CURRENT_PROTOCOL_SHORT = "v0.3"
ACCEPTED_PROTOCOL_VERSIONS = [PROTOCOL_V05, PROTOCOL_V04, PROTOCOL_V03, "agent-collab-v0.2"]
DEPRECATED_PROTOCOL_VERSIONS = ["agent-collab-v0.2"]
PATCHABLE_PROTOCOL_VERSIONS = ["agent-collab-v0.1"]
REJECTED_PROTOCOL_VERSIONS = ["agent-collab-v0.1"]
PATCH_CAPABILITY = "dynamic_protocol_bundle_v0.1"
ADAPTER_CAPABILITY = "semantic_protocol_adapter_v2"
AUTHORIZATION_CAPABILITY = "local_authorization_v1"
ADAPTER_CONTRACT_VERSION = 1
PROTOCOL_AUTHORITY_ID = "server.stellarix.space/agentrelay"
BUNDLE_REVISION_V03 = 1
BUNDLE_REVISION_V04 = 1
BUNDLE_REVISION_V05 = 2
BUNDLE_PUBLISHED_AT_V05 = "2026-07-19T00:00:00Z"
BUNDLE_EXPIRES_AT_V05 = "2027-07-19T00:00:00Z"

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
DOCS_DIR = ROOT / "docs"
EXAMPLES_DIR = ROOT / "examples" / "protocol-v03"
PROTECTED_BINDING_SLOTS = [
    "actor_agent_id",
    "requester_agent_id",
    "target_agent_id",
    "idempotency_key",
    "message_id",
    "turn_sequence",
    "expected_task_version",
    "completed_against_message_id",
    "failure_reason",
]

V05_OPERATION_ADAPTERS: dict[str, Any] = {
    "engine": ADAPTER_CAPABILITY,
    "contract_version": ADAPTER_CONTRACT_VERSION,
    "allowed_binding_sources": ["input", "identity", "task", "runtime"],
    "protected_slots": PROTECTED_BINDING_SLOTS,
    "operations": {
        "create_task": {
            "method": "POST",
            "path": "/tasks",
            "request_schema": "task-create-v05.schema.json",
            "bindings": [
                {"slot": "protocol_version", "to": "/protocol_version", "value": PROTOCOL_V05},
                {"slot": "idempotency_key", "to": "/idempotency_key", "from": "runtime.idempotency_key"},
                {"slot": "requester_agent_id", "to": "/requester_agent_id", "from": "identity.agent_id"},
                {"slot": "target_agent_id", "to": "/target_agent_id", "from": "input.targetAgentId"},
                {"slot": "done_criteria", "to": "/done_criteria", "from": "input.doneCriteria"},
                {"slot": "max_turns", "to": "/max_turns", "from": "input.maxTurns", "optional": True},
                {"slot": "task_expires_at", "to": "/task_expires_at", "from": "input.taskExpiresAt", "optional": True},
                {"slot": "message_kind", "to": "/message/parts/0/kind", "value": "text"},
                {"slot": "request_text", "to": "/message/parts/0/text", "from": "input.requestText"},
            ],
        },
        "reply": {
            "method": "POST",
            "path": "/tasks/{task_id}/messages",
            "request_schema": "task-message-v05.schema.json",
            "bindings": [
                {"slot": "actor_agent_id", "to": "/actor_agent_id", "from": "identity.agent_id"},
                {"slot": "message_id", "to": "/message_id", "from": "task.current_message_id"},
                {"slot": "turn_sequence", "to": "/turn_sequence", "from": "task.turn_sequence"},
                {"slot": "expected_task_version", "to": "/expected_task_version", "from": "task.task_version"},
                {"slot": "idempotency_key", "to": "/idempotency_key", "from": "runtime.idempotency_key"},
                {"slot": "message_kind", "to": "/parts/0/kind", "value": "text"},
                {"slot": "reply_text", "to": "/parts/0/text", "from": "input.text"},
            ],
        },
        "complete_task": {
            "method": "POST",
            "path": "/tasks/{task_id}/complete",
            "request_schema": "task-terminal-v05.schema.json",
            "bindings": [
                {"slot": "actor_agent_id", "to": "/actor_agent_id", "from": "identity.agent_id"},
                {"slot": "message_id", "to": "/message_id", "from": "task.current_message_id"},
                {"slot": "turn_sequence", "to": "/turn_sequence", "from": "task.turn_sequence"},
                {"slot": "expected_task_version", "to": "/expected_task_version", "from": "task.task_version"},
                {"slot": "idempotency_key", "to": "/idempotency_key", "from": "runtime.idempotency_key"},
                {"slot": "completed_against_message_id", "to": "/completed_against_message_id", "from": "task.current_message_id"},
            ],
        },
        "fail_task": {
            "method": "POST",
            "path": "/tasks/{task_id}/fail",
            "request_schema": "task-terminal-v05.schema.json",
            "bindings": [
                {"slot": "actor_agent_id", "to": "/actor_agent_id", "from": "identity.agent_id"},
                {"slot": "message_id", "to": "/message_id", "from": "task.current_message_id"},
                {"slot": "turn_sequence", "to": "/turn_sequence", "from": "task.turn_sequence"},
                {"slot": "expected_task_version", "to": "/expected_task_version", "from": "task.task_version"},
                {"slot": "idempotency_key", "to": "/idempotency_key", "from": "runtime.idempotency_key"},
                {"slot": "failure_reason", "to": "/reason", "from": "input.reason"},
            ],
        },
        "create_followup": {
            "method": "POST",
            "path": "/tasks/{task_id}/followups",
            "request_schema": "task-followup-v05.schema.json",
            "bindings": [
                {"slot": "idempotency_key", "to": "/idempotency_key", "from": "runtime.idempotency_key"},
                {"slot": "done_criteria", "to": "/done_criteria", "from": "input.doneCriteria"},
                {"slot": "max_turns", "to": "/max_turns", "from": "input.maxTurns", "optional": True},
                {"slot": "task_expires_at", "to": "/task_expires_at", "from": "input.taskExpiresAt", "optional": True},
                {"slot": "message_kind", "to": "/message/parts/0/kind", "value": "text"},
                {"slot": "request_text", "to": "/message/parts/0/text", "from": "input.requestText"},
            ],
        },
    },
}


class ProtocolNegotiationRequired(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        action: str,
        client_protocol: str | None,
        status: int = 426,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.action = action
        self.client_protocol = client_protocol
        self.status = status
        self.hint = hint or "Fetch the current protocol bundle and retry only if the local client can satisfy the required capability."


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def protocol_authority_id() -> str:
    return os.environ.get("AGENTRELAY_PROTOCOL_AUTHORITY_ID", PROTOCOL_AUTHORITY_ID).strip()


def v03_content() -> dict[str, Any]:
    return {
        "schemas": read_json_dir(SCHEMA_DIR, "*.schema.json"),
        "examples": read_json_dir(EXAMPLES_DIR, "*.json"),
        "docs": {
            "protocol-v03.md": read_text_if_exists(DOCS_DIR / "protocol-v03.md"),
            "protocol-v03-conformance.md": read_text_if_exists(DOCS_DIR / "protocol-v03-conformance.md"),
        },
    }


def v04_content() -> dict[str, Any]:
    return {
        "schemas": read_json_dir(SCHEMA_DIR, "*v04*.schema.json"),
        "examples": read_json_dir(ROOT / "examples" / "protocol-v04", "*.json"),
        "docs": {"task-lifecycle-v04.md": read_text_if_exists(DOCS_DIR / "task-lifecycle-v04.md")},
    }


def v05_content() -> dict[str, Any]:
    return {
        "schemas": read_json_dir(SCHEMA_DIR, "*v05*.schema.json"),
        "examples": read_json_dir(ROOT / "examples" / "protocol-v05", "*.json"),
        "docs": {
            "task-lifecycle-v05.md": read_text_if_exists(DOCS_DIR / "task-lifecycle-v05.md"),
            "protocol-v05-rollout-plan.md": read_text_if_exists(DOCS_DIR / "protocol-v05-rollout-plan.md"),
            "protocol-v05-conformance.md": read_text_if_exists(DOCS_DIR / "protocol-v05-conformance.md"),
            "protocol-auto-upgrade.md": read_text_if_exists(DOCS_DIR / "protocol-auto-upgrade.md"),
        },
        "adapters": V05_OPERATION_ADAPTERS,
    }


def content_digests(content: dict[str, Any]) -> tuple[str, str]:
    return canonical_digest(content.get("schemas", {})), canonical_digest(content)


def protocol_manifest(public_base_url: str | None = None) -> dict[str, Any]:
    base = (public_base_url or "https://server.stellarix.space/agentrelay").rstrip("/")
    api = f"{base}/api"
    schema_digest, bundle_digest = content_digests(v03_content())
    return {
        "protocol": PROTOCOL_NAME,
        "version": CURRENT_PROTOCOL_VERSION,
        "semver": CURRENT_PROTOCOL_SEMVER,
        "bundle_revision": BUNDLE_REVISION_V03,
        "schema_digest": schema_digest,
        "bundle_digest": bundle_digest,
        "authority": {"id": protocol_authority_id(), "origin": base},
        "accepted_versions": ACCEPTED_PROTOCOL_VERSIONS,
        "deprecated_versions": DEPRECATED_PROTOCOL_VERSIONS,
        "rejected_versions": REJECTED_PROTOCOL_VERSIONS,
        "patchable_versions": PATCHABLE_PROTOCOL_VERSIONS,
        "required_client_capabilities": [PATCH_CAPABILITY],
        "compatibility": {
            "current": CURRENT_PROTOCOL_VERSION,
            "accepted": ACCEPTED_PROTOCOL_VERSIONS,
            "deprecated": DEPRECATED_PROTOCOL_VERSIONS,
            "rejected": REJECTED_PROTOCOL_VERSIONS,
            "policy": "Deprecated versions are accepted during the compatibility window; rejected versions must be redrafted or the client upgraded.",
        },
        "urls": {
            "current": f"{api}/protocols/current",
            "manifest": f"{api}/protocols/{PROTOCOL_NAME}/{CURRENT_PROTOCOL_SHORT}/manifest",
            "bundle": f"{api}/protocols/{PROTOCOL_NAME}/{CURRENT_PROTOCOL_SHORT}/bundle",
            "validate": f"{api}/protocols/validate",
            "schemas": f"{base}/schemas/",
            "docs": f"{base}/docs/protocol-v03.md",
            "examples": f"{base}/examples/protocol-v03/",
        },
        "redraft_policy": {
            "safe_to_auto_redraft": ["task_create", "artifact_submit"],
            "requires_local_agent_review": ["task_amend", "task_close"],
            "idempotency": "Retry redrafted requests with the original idempotency_key when available, or include retry_of when a new key is required.",
        },
    }


def protocol_summary(public_base_url: str | None = None) -> dict[str, Any]:
    manifest = protocol_manifest(public_base_url)
    return {
        "name": manifest["protocol"],
        "version": manifest["version"],
        "semver": manifest["semver"],
        "schema_digest": manifest["schema_digest"],
        "bundle_digest": manifest["bundle_digest"],
        "bundle_revision": manifest["bundle_revision"],
        "accepted_versions": manifest["accepted_versions"],
        "deprecated_versions": manifest["deprecated_versions"],
        "manifest_url": manifest["urls"]["manifest"],
        "bundle_url": manifest["urls"]["bundle"],
    }


def negotiate_protocol(
    payload: dict[str, Any],
    public_base_url: str | None = None,
    *,
    write_mode: str = "legacy",
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("protocol negotiation payload must be an object")
    runtime_version = required_text(payload.get("runtime_version"), "runtime_version")
    capabilities = required_string_list(payload.get("runtime_capabilities"), "runtime_capabilities")
    supported_versions = required_string_list(payload.get("supported_protocol_versions"), "supported_protocol_versions")
    active = payload.get("active")
    if active is not None and not isinstance(active, dict):
        raise ValueError("active must be an object when present")

    target = (
        protocol_manifest_v05(public_base_url, write_mode=write_mode)
        if write_mode in {"closed", "v05"}
        else protocol_manifest(public_base_url)
    )
    active = active or {}
    active_version = str(active.get("version") or "")
    active_digest = str(active.get("bundle_digest") or "")
    active_revision = optional_nonnegative_int(active.get("bundle_revision"), "active.bundle_revision")
    target_revision = int(target["bundle_revision"])
    required_capabilities = list(target["required_client_capabilities"])
    missing_capabilities = sorted(set(required_capabilities) - set(capabilities))
    hot_update_enabled = os.environ.get("AGENTRELAY_HOT_UPDATE_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

    if active_version == target["version"] and active_digest == target["bundle_digest"]:
        action = "up_to_date"
        reason = "The active protocol bundle matches the Relay authority."
    elif not hot_update_enabled:
        action = "client_release_required"
        reason = "Protocol hot update is disabled by the Relay operator."
    elif active_version == target["version"] and active_revision is not None and active_revision > target_revision:
        action = "hot_rollback" if not missing_capabilities else "client_release_required"
        reason = "The Relay authority rolled back to an earlier verified bundle revision."
    elif target["version"] in supported_versions and not missing_capabilities:
        action = "hot_patch"
        reason = "The installed protocol runtime can activate the current declarative bundle."
    else:
        action = "client_release_required"
        reason = "The current protocol requires MCP runtime code or capabilities that are not installed."

    return {
        "action": action,
        "reason": reason,
        "runtime_version": runtime_version,
        "missing_capabilities": missing_capabilities,
        "authority": target["authority"],
        "target": {
            "protocol": target["protocol"],
            "version": target["version"],
            "semver": target["semver"],
            "bundle_revision": target["bundle_revision"],
            "schema_digest": target["schema_digest"],
            "bundle_digest": target["bundle_digest"],
            "bundle_url": target["urls"]["bundle"],
            "adapter_contract_version": target.get("adapter_contract_version"),
            "published_at": target.get("published_at"),
            "expires_at": target.get("expires_at"),
            "required_client_capabilities": required_capabilities,
        },
        "retry_policy": {
            "max_automatic_retries": 1,
            "preserve_idempotency_key": True,
        },
    }


def protocol_bundle(public_base_url: str | None = None) -> dict[str, Any]:
    return {"manifest": protocol_manifest(public_base_url), **v03_content()}


def protocol_manifest_v04(public_base_url: str | None = None) -> dict[str, Any]:
    base = (public_base_url or "https://server.stellarix.space/agentrelay").rstrip("/")
    manifest = protocol_manifest(base)
    schema_digest, bundle_digest = content_digests(v04_content())
    manifest.update(
        {
            "version": PROTOCOL_V04,
            "semver": "0.4.0",
            "bundle_revision": BUNDLE_REVISION_V04,
            "schema_digest": schema_digest,
            "bundle_digest": bundle_digest,
            "status": "accepted_non_default",
        }
    )
    manifest["compatibility"]["current"] = PROTOCOL_V04
    manifest["urls"] = {
        **manifest["urls"],
        "manifest": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.4/manifest",
        "bundle": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.4/bundle",
        "docs": f"{base}/docs/task-lifecycle-v04.md",
        "examples": f"{base}/examples/protocol-v04/",
    }
    return manifest


def protocol_bundle_v04(public_base_url: str | None = None) -> dict[str, Any]:
    return {"manifest": protocol_manifest_v04(public_base_url), **v04_content()}


def protocol_manifest_v05(
    public_base_url: str | None = None,
    *,
    write_mode: str = "closed",
) -> dict[str, Any]:
    base = (public_base_url or "https://server.stellarix.space/agentrelay").rstrip("/")
    manifest = protocol_manifest(base)
    schema_digest, bundle_digest = content_digests(v05_content())
    manifest.update(
        {
            "version": PROTOCOL_V05,
            "semver": "0.5.0",
            "bundle_revision": BUNDLE_REVISION_V05,
            "schema_digest": schema_digest,
            "bundle_digest": bundle_digest,
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "published_at": BUNDLE_PUBLISHED_AT_V05,
            "expires_at": BUNDLE_EXPIRES_AT_V05,
            "status": "accepted_non_default",
            "write_mode": write_mode,
        }
    )
    manifest["compatibility"]["current"] = PROTOCOL_V05
    manifest["required_client_capabilities"] = [PATCH_CAPABILITY, ADAPTER_CAPABILITY, AUTHORIZATION_CAPABILITY]
    manifest["hot_update"] = {
        "engine": ADAPTER_CAPABILITY,
        "contract_version": ADAPTER_CONTRACT_VERSION,
        "enabled": os.environ.get("AGENTRELAY_HOT_UPDATE_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
        "hot_patch_from": [PROTOCOL_V05],
        "protected_slots": PROTECTED_BINDING_SLOTS,
    }
    manifest["urls"] = {
        **manifest["urls"],
        "manifest": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.5/manifest",
        "bundle": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.5/bundle",
        "docs": f"{base}/docs/task-lifecycle-v05.md",
        "examples": f"{base}/examples/protocol-v05/",
    }
    manifest["constants"] = {
        "max_delivery_attempts": 4,
        "retry_backoff_seconds": [60, 300, 600],
        "delivery_ack_lease_seconds": 60,
        "listener_readiness_publish_interval_seconds": 60,
        "listener_readiness_max_age_seconds": 300,
        "max_visibility_batch_size": 100,
    }
    return manifest


def protocol_bundle_v05(
    public_base_url: str | None = None,
    *,
    write_mode: str = "closed",
) -> dict[str, Any]:
    return {
        "manifest": protocol_manifest_v05(public_base_url, write_mode=write_mode),
        **v05_content(),
    }


def ensure_protocol_compatible(payload: dict[str, Any] | None) -> None:
    protocol_version = payload.get("protocol_version") if isinstance(payload, dict) else None
    if not protocol_version:
        return
    if protocol_version in ACCEPTED_PROTOCOL_VERSIONS:
        return
    if protocol_version in PATCHABLE_PROTOCOL_VERSIONS or is_older_agent_collab(protocol_version):
        raise ProtocolNegotiationRequired(
            f"Client protocol {protocol_version} is no longer accepted.",
            code="protocol_patch_required",
            action="fetch_protocol_and_redraft",
            client_protocol=protocol_version,
            hint="Fetch the current protocol bundle, ask the local agent to redraft the payload without changing user intent, then retry with idempotency protection.",
        )
    raise ProtocolNegotiationRequired(
        f"Client protocol {protocol_version} is not supported by this AgentRelay server.",
        code="client_upgrade_required",
        action="upgrade_mcp_client",
        client_protocol=protocol_version,
        hint="Upgrade the AgentRelay MCP client if the requested protocol needs new client code, tools, endpoints, or workflow semantics.",
    )


def negotiation_error_detail(
    exc: ProtocolNegotiationRequired,
    public_base_url: str | None = None,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = manifest or protocol_manifest(public_base_url)
    return {
        "client_protocol": exc.client_protocol,
        "server_protocol": {
            "name": manifest["protocol"],
            "version": manifest["version"],
            "semver": manifest["semver"],
            "schema_digest": manifest["schema_digest"],
            "bundle_digest": manifest["bundle_digest"],
            "bundle_revision": manifest["bundle_revision"],
        },
        "accepted_versions": manifest["accepted_versions"],
        "deprecated_versions": manifest["deprecated_versions"],
        "upgrade": {
            "action": exc.action,
            "manifest_url": manifest["urls"]["manifest"],
            "bundle_url": manifest["urls"]["bundle"],
            "required_client_capabilities": manifest["required_client_capabilities"],
            "authority": manifest["authority"],
        },
        "redraft_policy": manifest["redraft_policy"],
    }


def read_json_dir(root: Path, pattern: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path in sorted(root.glob(pattern)):
        values[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return values


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def required_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must be an array of non-empty strings")
    return list(dict.fromkeys(value))


def optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def is_older_agent_collab(protocol_version: str) -> bool:
    match = re.fullmatch(r"agent-collab-v0\.(\d+)", str(protocol_version))
    return bool(match and int(match.group(1)) < 2)
