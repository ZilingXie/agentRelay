from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from server.protocol_v03 import PROTOCOL_V03
from server.protocol_v04 import PROTOCOL_V04


PROTOCOL_NAME = "agent-collab"
CURRENT_PROTOCOL_VERSION = PROTOCOL_V03
CURRENT_PROTOCOL_SEMVER = "0.3.0"
CURRENT_PROTOCOL_SHORT = "v0.3"
ACCEPTED_PROTOCOL_VERSIONS = [PROTOCOL_V04, PROTOCOL_V03, "agent-collab-v0.2"]
DEPRECATED_PROTOCOL_VERSIONS = ["agent-collab-v0.2"]
PATCHABLE_PROTOCOL_VERSIONS = ["agent-collab-v0.1"]
REJECTED_PROTOCOL_VERSIONS = ["agent-collab-v0.1"]
PATCH_CAPABILITY = "dynamic_protocol_bundle_v0.1"

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
DOCS_DIR = ROOT / "docs"
EXAMPLES_DIR = ROOT / "examples" / "protocol-v03"
PROTOCOL_FILES = [
    *sorted(SCHEMA_DIR.glob("*.schema.json")),
    DOCS_DIR / "protocol-v03.md",
    DOCS_DIR / "protocol-v03-conformance.md",
    DOCS_DIR / "task-lifecycle-v04.md",
    *sorted(EXAMPLES_DIR.glob("*.json")),
]


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


def protocol_digest() -> str:
    hasher = hashlib.sha256()
    for path in PROTOCOL_FILES:
        if not path.exists():
            continue
        hasher.update(str(path.relative_to(ROOT)).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def protocol_manifest(public_base_url: str | None = None) -> dict[str, Any]:
    base = (public_base_url or "https://server.stellarix.space/agentrelay").rstrip("/")
    api = f"{base}/api"
    return {
        "protocol": PROTOCOL_NAME,
        "version": CURRENT_PROTOCOL_VERSION,
        "semver": CURRENT_PROTOCOL_SEMVER,
        "schema_digest": protocol_digest(),
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
        "accepted_versions": manifest["accepted_versions"],
        "deprecated_versions": manifest["deprecated_versions"],
        "manifest_url": manifest["urls"]["manifest"],
        "bundle_url": manifest["urls"]["bundle"],
    }


def protocol_bundle(public_base_url: str | None = None) -> dict[str, Any]:
    return {
        "manifest": protocol_manifest(public_base_url),
        "schemas": read_json_dir(SCHEMA_DIR, "*.schema.json"),
        "examples": read_json_dir(EXAMPLES_DIR, "*.json"),
        "docs": {
            "protocol-v03.md": read_text_if_exists(DOCS_DIR / "protocol-v03.md"),
            "protocol-v03-conformance.md": read_text_if_exists(DOCS_DIR / "protocol-v03-conformance.md"),
        },
    }


def protocol_manifest_v04(public_base_url: str | None = None) -> dict[str, Any]:
    base = (public_base_url or "https://server.stellarix.space/agentrelay").rstrip("/")
    manifest = protocol_manifest(base)
    manifest.update(
        {
            "version": PROTOCOL_V04,
            "semver": "0.4.0",
            "status": "accepted_non_default",
        }
    )
    manifest["urls"] = {
        **manifest["urls"],
        "manifest": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.4/manifest",
        "bundle": f"{base}/api/protocols/{PROTOCOL_NAME}/v0.4/bundle",
        "docs": f"{base}/docs/task-lifecycle-v04.md",
        "examples": f"{base}/examples/protocol-v04/",
    }
    return manifest


def protocol_bundle_v04(public_base_url: str | None = None) -> dict[str, Any]:
    return {
        "manifest": protocol_manifest_v04(public_base_url),
        "schemas": read_json_dir(SCHEMA_DIR, "*v04*.schema.json"),
        "examples": read_json_dir(ROOT / "examples" / "protocol-v04", "*.json"),
        "docs": {"task-lifecycle-v04.md": read_text_if_exists(DOCS_DIR / "task-lifecycle-v04.md")},
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


def negotiation_error_detail(exc: ProtocolNegotiationRequired, public_base_url: str | None = None) -> dict[str, Any]:
    manifest = protocol_manifest(public_base_url)
    return {
        "client_protocol": exc.client_protocol,
        "server_protocol": {
            "name": manifest["protocol"],
            "version": manifest["version"],
            "semver": manifest["semver"],
            "schema_digest": manifest["schema_digest"],
        },
        "accepted_versions": manifest["accepted_versions"],
        "deprecated_versions": manifest["deprecated_versions"],
        "upgrade": {
            "action": exc.action,
            "manifest_url": manifest["urls"]["manifest"],
            "bundle_url": manifest["urls"]["bundle"],
            "required_client_capabilities": manifest["required_client_capabilities"],
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


def is_older_agent_collab(protocol_version: str) -> bool:
    match = re.fullmatch(r"agent-collab-v0\.(\d+)", str(protocol_version))
    return bool(match and int(match.group(1)) < 2)
