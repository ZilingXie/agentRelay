from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_registry import negotiate_protocol, protocol_bundle_v05, protocol_manifest_v05


BASE_URL = "http://127.0.0.1:8802/agentrelay/api"
HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
    "X-AgentRelay-Envelope": "v0.3",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        signing_key_path = Path(tmpdir) / "protocol-signing-key.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(signing_key_path)],
            capture_output=True,
            check=True,
        )
        wrong_signing_key_path = Path(tmpdir) / "wrong-protocol-signing-key.pem"
        subprocess.run(
            [
                "openssl", "genpkey", "-algorithm", "RSA",
                "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(wrong_signing_key_path),
            ],
            capture_output=True,
            check=True,
        )
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8802",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-protocol-negotiation.sqlite3",
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
                "AGENTRELAY_PUBLIC_BASE_URL": "https://example.test/agentrelay",
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()

            health = get_json(f"{BASE_URL}/health")
            protocol = health["protocol"]
            if protocol["version"] != "agent-collab-v0.3":
                raise AssertionError(f"health did not publish current protocol: {protocol}")
            if not protocol["schema_digest"].startswith("sha256:"):
                raise AssertionError("health protocol digest missing sha256 prefix")

            manifest = get_json(f"{BASE_URL}/protocols/current")
            if manifest["urls"]["bundle"] != "https://example.test/agentrelay/api/protocols/agent-collab/v0.3/bundle":
                raise AssertionError("manifest bundle URL should use public base URL")
            if "agent-collab-v0.2" not in manifest["accepted_versions"]:
                raise AssertionError("manifest should publish the compatibility window")
            if "dynamic_protocol_bundle_v0.1" not in manifest["required_client_capabilities"]:
                raise AssertionError("manifest should declare the dynamic protocol bundle capability")

            bundle = get_json(f"{BASE_URL}/protocols/agent-collab/v0.3/bundle")
            if "task-create.schema.json" not in bundle["schemas"]:
                raise AssertionError("protocol bundle missing task-create schema")
            if "meeting-task-create.json" not in bundle["examples"]:
                raise AssertionError("protocol bundle missing meeting task create example")
            if "AgentRelay Protocol v0.3" not in bundle["docs"]["protocol-v03.md"]:
                raise AssertionError("protocol bundle missing protocol doc")
            if manifest["bundle_digest"] != canonical_digest({key: value for key, value in bundle.items() if key != "manifest"}):
                raise AssertionError("manifest bundle digest does not match the served v0.3 bundle")

            negotiated_current = post_json(
                f"{BASE_URL}/protocols/negotiate",
                {
                    "runtime_version": "0.2.0",
                    "runtime_capabilities": ["dynamic_protocol_bundle_v0.1"],
                    "supported_protocol_versions": ["agent-collab-v0.3"],
                    "active": {
                        "version": manifest["version"],
                        "semver": manifest["semver"],
                        "bundle_revision": manifest["bundle_revision"],
                        "bundle_digest": manifest["bundle_digest"],
                    },
                },
                HEADERS,
            )
            if negotiated_current["action"] != "up_to_date":
                raise AssertionError(f"matching bundle should be current: {negotiated_current}")

            negotiated_patch = post_json(
                f"{BASE_URL}/protocols/negotiate",
                {
                    "runtime_version": "0.2.0",
                    "runtime_capabilities": ["dynamic_protocol_bundle_v0.1"],
                    "supported_protocol_versions": ["agent-collab-v0.3"],
                },
                HEADERS,
            )
            if negotiated_patch["action"] != "hot_patch":
                raise AssertionError(f"capable runtime should receive hot patch: {negotiated_patch}")

            negotiated_upgrade = post_json(
                f"{BASE_URL}/protocols/negotiate",
                {
                    "runtime_version": "0.1.0",
                    "runtime_capabilities": [],
                    "supported_protocol_versions": ["agent-collab-v0.2"],
                },
                HEADERS,
            )
            if negotiated_upgrade["action"] != "client_release_required":
                raise AssertionError(f"incapable runtime should require release: {negotiated_upgrade}")

            v05_manifest = get_json(f"{BASE_URL}/protocols/agent-collab/v0.5/manifest")
            if v05_manifest["compatibility"]["current"] != "agent-collab-v0.5":
                raise AssertionError("v0.5 compatibility.current must match its manifest version")
            v05_bundle = get_json(f"{BASE_URL}/protocols/agent-collab/v0.5/bundle")
            if v05_manifest["bundle_digest"] != canonical_digest({key: value for key, value in v05_bundle.items() if key != "manifest"}):
                raise AssertionError("manifest bundle digest does not match the served v0.5 bundle")
            adapters = v05_bundle.get("adapters", {}).get("operations", {})
            if v05_bundle.get("adapters", {}).get("engine") != "semantic_protocol_adapter_v2":
                raise AssertionError("v0.5 bundle must use the hardened adapter v2 engine")
            if v05_manifest.get("adapter_contract_version") != 1:
                raise AssertionError("v0.5 manifest must publish adapter contract version 1")
            if not v05_manifest.get("published_at") or not v05_manifest.get("expires_at"):
                raise AssertionError("v0.5 manifest must publish a bounded validity window")
            expected_operations = {"create_task", "reply", "complete_task", "fail_task", "create_followup"}
            if set(adapters) != expected_operations:
                raise AssertionError(f"v0.5 bundle semantic operations are incomplete: {sorted(adapters)}")
            for operation, adapter in adapters.items():
                if adapter.get("request_schema") not in v05_bundle["schemas"]:
                    raise AssertionError(f"{operation} references an unpublished request schema")
                slots = [binding.get("slot") for binding in adapter.get("bindings", [])]
                if not all(slots) or len(slots) != len(set(slots)):
                    raise AssertionError(f"{operation} must publish unique semantic slots")

            v05_negotiated = negotiate_protocol(
                {
                    "runtime_version": "0.3.0",
                    "runtime_capabilities": [
                        "dynamic_protocol_bundle_v0.1",
                        "semantic_protocol_adapter_v2",
                        "local_authorization_v1",
                    ],
                    "supported_protocol_versions": ["agent-collab-v0.5"],
                },
                "https://example.test/agentrelay",
                write_mode="v05",
            )
            if v05_negotiated["action"] != "hot_patch":
                raise AssertionError(f"hardened runtime should receive v0.5 hot patch: {v05_negotiated}")
            if v05_negotiated["target"].get("adapter_contract_version") != 1:
                raise AssertionError("negotiation target must include adapter contract version")
            if v05_negotiated["target"].get("published_at") != v05_manifest["published_at"]:
                raise AssertionError("negotiation target must bind the manifest publication time")
            if v05_negotiated["target"].get("expires_at") != v05_manifest["expires_at"]:
                raise AssertionError("negotiation target must bind the manifest expiration time")

            previous_dynamic_tools = os.environ.get("AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED")
            previous_signing_key = os.environ.get("AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE")
            previous_signing_key_id = os.environ.get("AGENTRELAY_PROTOCOL_SIGNING_KEY_ID")
            os.environ["AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED"] = "1"
            os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE"] = str(signing_key_path)
            os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_ID"] = "negotiation-smoke-key"
            try:
                dynamic_manifest = protocol_manifest_v05(
                    "https://example.test/agentrelay", write_mode="v05"
                )
                dynamic_bundle = protocol_bundle_v05(
                    "https://example.test/agentrelay", write_mode="v05"
                )
                if dynamic_manifest.get("adapter_contract_version") != 2:
                    raise AssertionError("dynamic Agent tools must publish adapter contract version 2")
                if "dynamic_agent_tool_schema_v1" not in dynamic_manifest["required_client_capabilities"]:
                    raise AssertionError("dynamic Agent tool capability is not required")
                signature = dynamic_manifest.get("signature", {})
                if signature.get("algorithm") != "Ed25519" or signature.get("key_id") != "negotiation-smoke-key":
                    raise AssertionError("dynamic Agent tools must publish an Ed25519 manifest signature")
                tools = dynamic_bundle.get("agent_tools", {}).get("tools", {})
                if set(tools) != {"agentrelay_create_task", "agentrelay_reply", "agentrelay_create_followup"}:
                    raise AssertionError(f"dynamic Agent tools are incomplete: {sorted(tools)}")
                create_schema = tools["agentrelay_create_task"]["input_schema"]
                if create_schema["properties"]["message"]["required"] != ["subject", "parts"]:
                    raise AssertionError("create tool must require structured Message subject and parts")
                reply_schema = tools["agentrelay_reply"]["input_schema"]
                if "subject" in reply_schema["properties"]:
                    raise AssertionError("reply tool must not expose subject")
                os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE"] = str(wrong_signing_key_path)
                try:
                    protocol_manifest_v05("https://example.test/agentrelay", write_mode="v05")
                except ValueError as exc:
                    if "Ed25519 private key" not in str(exc):
                        raise
                else:
                    raise AssertionError("dynamic Agent tools must reject a non-Ed25519 signing key")
                finally:
                    os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE"] = str(signing_key_path)
            finally:
                if previous_dynamic_tools is None:
                    os.environ.pop("AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED", None)
                else:
                    os.environ["AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED"] = previous_dynamic_tools
                if previous_signing_key is None:
                    os.environ.pop("AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE", None)
                else:
                    os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_FILE"] = previous_signing_key
                if previous_signing_key_id is None:
                    os.environ.pop("AGENTRELAY_PROTOCOL_SIGNING_KEY_ID", None)
                else:
                    os.environ["AGENTRELAY_PROTOCOL_SIGNING_KEY_ID"] = previous_signing_key_id

            previous_hot_update = os.environ.get("AGENTRELAY_HOT_UPDATE_ENABLED")
            os.environ["AGENTRELAY_HOT_UPDATE_ENABLED"] = "0"
            try:
                disabled = negotiate_protocol(
                    {
                        "runtime_version": "0.3.0",
                        "runtime_capabilities": ["dynamic_protocol_bundle_v0.1", "semantic_protocol_adapter_v2"],
                        "supported_protocol_versions": ["agent-collab-v0.5"],
                    },
                    "https://example.test/agentrelay",
                    write_mode="v05",
                )
                if disabled["action"] != "client_release_required":
                    raise AssertionError(f"disabled hot update must not activate a bundle: {disabled}")
            finally:
                if previous_hot_update is None:
                    os.environ.pop("AGENTRELAY_HOT_UPDATE_ENABLED", None)
                else:
                    os.environ["AGENTRELAY_HOT_UPDATE_ENABLED"] = previous_hot_update

            valid = post_json(
                f"{BASE_URL}/protocols/validate",
                {"operation": "task_create", "payload": valid_task_payload()},
                HEADERS,
            )
            if not valid["data"]["valid"]:
                raise AssertionError("protocol validate should accept current v0.3 payload")

            rejected = post_json(
                f"{BASE_URL}/protocols/validate",
                {"operation": "task_create", "payload": {**valid_task_payload(), "protocol_version": "agent-collab-v0.1"}},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(rejected, "protocol_patch_required")

            create_rejected = post_json(
                f"{BASE_URL}/tasks",
                {**valid_task_payload(), "protocol_version": "agent-collab-v0.1"},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(create_rejected, "protocol_patch_required")

            future_rejected = post_json(
                f"{BASE_URL}/tasks",
                {**valid_task_payload(), "protocol_version": "agent-collab-v9.0"},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(future_rejected, "client_upgrade_required")

            print(json.dumps({"ok": True, "protocol": protocol["version"], "digest": protocol["schema_digest"]}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def valid_task_payload() -> dict:
    return {
        "protocol_version": "agent-collab-v0.3",
        "idempotency_key": "protocol-negotiation-valid-create",
        "task_type": "agent.task",
        "subject": "Protocol negotiation smoke task",
        "requester_agent_id": "zac-agent",
        "target_agent_id": "frank-agent",
        "requesterThreadId": "protocol-negotiation-thread",
        "done_criteria": "Frank agent replies with ACK.",
        "completion_owner_agent_id": "zac-agent",
        "pending_on_agent_id": "frank-agent",
        "next_action": "Frank agent should reply with ACK.",
        "message": {
            "actor_agent_id": "zac-agent",
            "intent": "connectivity_check",
            "parts": [{"kind": "text", "text": "Please ACK this protocol check."}],
        },
    }


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def assert_protocol_negotiation_error(payload: dict, code: str) -> None:
    if payload.get("ok") is not False:
        raise AssertionError(f"expected error envelope: {payload}")
    error = payload["error"]
    if error["code"] != code:
        raise AssertionError(f"expected {code}, got {error}")
    detail = error.get("detail") or {}
    if detail.get("server_protocol", {}).get("version") != "agent-collab-v0.3":
        raise AssertionError(f"error should include server protocol: {error}")
    if not detail.get("upgrade", {}).get("bundle_url"):
        raise AssertionError(f"error should include bundle URL: {error}")


def wait_for_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            payload = get_json(f"{BASE_URL}/health")
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict[str, str], expected_status: int = 200) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8")
    if status != expected_status:
        raise AssertionError(f"expected HTTP {expected_status}, got {status}: {body}")
    return json.loads(body)


if __name__ == "__main__":
    main()
