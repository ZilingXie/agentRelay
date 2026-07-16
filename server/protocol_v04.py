from __future__ import annotations

from typing import Any


PROTOCOL_V04 = "agent-collab-v0.4"
ACTIVE_STATES = {"submitted", "delivered"}
TERMINAL_STATES = {"completed", "expired", "failed"}
RESERVED_STATES = {"cancelled", "archived"}
FAILED_REASONS = {
    "delivery_retry_exhausted",
    "listener_persistence_failed",
    "relay_persistence_failed",
    "agent_reported_failure",
    "max_turns_exhausted",
    "internal_consistency_error",
}


def is_protocol_v04(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("protocol_version") == PROTOCOL_V04)


def require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value.strip()


def require_positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def validate_message_parts(parts: Any) -> list[dict[str, Any]]:
    if not isinstance(parts, list) or not parts:
        raise ValueError("message.parts must be a non-empty array")
    if not all(isinstance(part, dict) for part in parts):
        raise ValueError("message.parts entries must be objects")
    return parts


def validate_task_create(payload: dict[str, Any]) -> None:
    if payload.get("protocol_version") != PROTOCOL_V04:
        raise ValueError(f"protocol_version must be {PROTOCOL_V04}")
    requester = require_string(payload, "requester_agent_id")
    target = require_string(payload, "target_agent_id")
    if requester == target:
        raise ValueError("requester_agent_id and target_agent_id must differ")
    require_string(payload, "idempotency_key")
    if not isinstance(payload.get("done_criteria"), (str, dict)) or not payload.get("done_criteria"):
        raise ValueError("done_criteria must be a non-empty string or object")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    validate_message_parts(message.get("parts"))
    if "max_turns" in payload:
        require_positive_int(payload, "max_turns")


def validate_mutation_context(payload: dict[str, Any]) -> None:
    require_string(payload, "current_message_id")
    require_positive_int(payload, "turn_sequence")
    require_positive_int(payload, "expected_status_version")
    require_string(payload, "idempotency_key")


def validate_message_submit(payload: dict[str, Any]) -> None:
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    validate_message_parts(payload.get("parts"))


def validate_ack(payload: dict[str, Any]) -> None:
    validate_mutation_context(payload)
    require_string(payload, "task_id")
    require_string(payload, "message_id")


def validate_complete(payload: dict[str, Any]) -> None:
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    require_string(payload, "completed_against_message_id")


def validate_fail(payload: dict[str, Any]) -> None:
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    reason = require_string(payload, "reason")
    if reason not in FAILED_REASONS:
        raise ValueError(f"unsupported failed reason: {reason}")
