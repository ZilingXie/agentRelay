from __future__ import annotations

from typing import Any


PROTOCOL_V03 = "agent-collab-v0.3"
ENVELOPE_V03 = "v0.3"
SOURCE_REF_TYPES = {
    "owner_confirmation",
    "calendar_lookup",
    "file",
    "message",
    "tool_result",
    "external_url",
    "other",
}
SOURCE_REF_VISIBILITIES = {"public", "redacted", "private"}
PREVIOUS_GOAL_DISPOSITIONS = {
    "accepted_and_extended",
    "clarified",
    "superseded_by_human",
    "rejected_by_human",
    "cancelled_by_human",
}


class ProtocolValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        code: str = "VALIDATION_ERROR",
        hint: str | None = None,
    ):
        super().__init__(message)
        self.field = field
        self.code = code
        self.hint = hint or "Check the AgentRelay protocol v0.3 schema for this request."


def is_protocol_v03(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("protocol_version") == PROTOCOL_V03)


def validate_task_create(payload: dict[str, Any]) -> None:
    require_literal(payload, "protocol_version", PROTOCOL_V03)
    require_str(payload, "idempotency_key")
    require_str(payload, "task_type")
    require_str(payload, "subject")
    requester = require_str(payload, "requester_agent_id")
    require_str(payload, "target_agent_id")
    completion_owner = require_str(payload, "completion_owner_agent_id")
    pending_on = require_str(payload, "pending_on_agent_id")
    require_str(payload, "next_action")
    if completion_owner != requester:
        raise ProtocolValidationError(
            "completion_owner_agent_id must match requester_agent_id for v0.3 two-agent tasks",
            field="completion_owner_agent_id",
            hint="For v0.3 two-agent tasks, let the requester-side agent own semantic completion.",
        )
    if not isinstance(payload.get("done_criteria"), (str, dict)):
        raise ProtocolValidationError(
            "done_criteria must be a string or object",
            field="done_criteria",
        )
    max_turns = payload.get("max_turns", payload.get("maxTurns"))
    if max_turns is not None and not is_positive_int(max_turns):
        raise ProtocolValidationError("max_turns must be a positive integer", field="max_turns")
    message = require_object(payload, "message")
    actor = require_str(message, "actor_agent_id")
    if actor != requester:
        raise ProtocolValidationError(
            "message.actor_agent_id must match requester_agent_id",
            field="message.actor_agent_id",
        )
    require_str(message, "intent")
    require_non_empty_list(message, "parts")
    thread_binding = payload.get("thread_binding")
    if thread_binding is not None:
        if not isinstance(thread_binding, dict):
            raise ProtocolValidationError("thread_binding must be an object", field="thread_binding")
        require_str(thread_binding, "agent_id")
        require_str(thread_binding, "thread_role")
        require_str(thread_binding, "thread_id")
    if pending_on == requester:
        raise ProtocolValidationError(
            "pending_on_agent_id should start on the target agent for a new request",
            field="pending_on_agent_id",
        )


def validate_artifact_submit(payload: dict[str, Any]) -> None:
    require_literal(payload, "protocol_version", PROTOCOL_V03)
    require_str(payload, "idempotency_key")
    require_str(payload, "actor_agent_id")
    require_str(payload, "intent")
    require_str(payload, "next_status")
    require_str(payload, "pending_on_agent_id")
    require_str(payload, "next_action")
    artifact = require_object(payload, "artifact")
    require_str(artifact, "kind")
    require_str(artifact, "summary")
    require_non_empty_list(artifact, "parts")
    normalize_source_refs(artifact.get("source_refs", []), field="artifact.source_refs")


def validate_task_close(payload: dict[str, Any]) -> None:
    require_literal(payload, "protocol_version", PROTOCOL_V03)
    require_str(payload, "idempotency_key")
    require_str(payload, "closed_by_agent_id")
    require_str(payload, "terminal_reason")
    authority = require_object(payload, "completion_authority")
    authority_type = require_str(authority, "type", "completion_authority.type")
    if authority_type not in {"agent", "human"}:
        raise ProtocolValidationError(
            "completion_authority.type must be agent or human",
            field="completion_authority.type",
        )
    if authority_type == "human":
        require_str(authority, "owner_id", "completion_authority.owner_id")
        require_str(authority, "via_agent_id", "completion_authority.via_agent_id")
        require_str(authority, "approval_ref", "completion_authority.approval_ref")
    normalize_completion_authority(authority)
    final_artifact = payload.get("final_artifact")
    if final_artifact is not None:
        if not isinstance(final_artifact, dict):
            raise ProtocolValidationError("final_artifact must be an object", field="final_artifact")
        require_str(final_artifact, "kind", "final_artifact.kind")
        require_non_empty_list(final_artifact, "parts", "final_artifact.parts")
        normalize_source_refs(final_artifact.get("source_refs", []), field="final_artifact.source_refs")


def validate_task_amend(payload: dict[str, Any]) -> None:
    require_literal(payload, "protocol_version", PROTOCOL_V03)
    require_str(payload, "idempotency_key")
    require_str(payload, "actor_agent_id")
    expected_goal_version = payload.get("expected_goal_version")
    if not is_positive_int(expected_goal_version):
        raise ProtocolValidationError(
            "expected_goal_version must be a positive integer",
            field="expected_goal_version",
        )
    if not isinstance(payload.get("new_done_criteria"), (str, dict)):
        raise ProtocolValidationError(
            "new_done_criteria must be a string or object",
            field="new_done_criteria",
        )
    new_max_turns = payload.get("new_max_turns", payload.get("newMaxTurns"))
    if new_max_turns is not None and not is_positive_int(new_max_turns):
        raise ProtocolValidationError("new_max_turns must be a positive integer", field="new_max_turns")
    disposition = payload.get("previous_goal_disposition", "clarified")
    if disposition not in PREVIOUS_GOAL_DISPOSITIONS:
        raise ProtocolValidationError(
            "previous_goal_disposition is not supported",
            field="previous_goal_disposition",
            hint="Use accepted_and_extended, clarified, superseded_by_human, rejected_by_human, or cancelled_by_human.",
        )
    require_str(payload, "reason")
    normalize_human_authority(require_object(payload, "human_authority"))


def normalize_source_refs(source_refs: Any, *, field: str = "source_refs") -> list[dict[str, Any]]:
    if source_refs is None:
        return []
    if not isinstance(source_refs, list):
        raise ProtocolValidationError(f"{field} must be an array", field=field)
    normalized: list[dict[str, Any]] = []
    for index, source_ref in enumerate(source_refs):
        item_field = f"{field}[{index}]"
        if not isinstance(source_ref, dict):
            raise ProtocolValidationError(
                f"{field} entries must be objects",
                field=item_field,
            )
        ref_type = require_str(source_ref, "type", f"{item_field}.type")
        if ref_type not in SOURCE_REF_TYPES:
            raise ProtocolValidationError(
                f"{item_field}.type is not supported",
                field=f"{item_field}.type",
                hint="Use a known source ref type or 'other'.",
            )
        label = require_str(source_ref, "label", f"{item_field}.label")
        visibility = source_ref.get("visibility", "redacted")
        if visibility not in SOURCE_REF_VISIBILITIES:
            raise ProtocolValidationError(
                "source ref visibility must be public, redacted, or private",
                field=f"{item_field}.visibility",
            )
        summary = source_ref.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ProtocolValidationError("source ref summary must be a string", field=f"{item_field}.summary")
        uri = source_ref.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ProtocolValidationError("source ref uri must be a string", field=f"{item_field}.uri")
        metadata = source_ref.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ProtocolValidationError("source ref metadata must be an object", field=f"{item_field}.metadata")
        normalized.append(redact_source_ref(source_ref, ref_type, label, visibility, summary, uri, metadata))
    return normalized


def normalize_human_authority(authority: Any) -> dict[str, Any]:
    if not isinstance(authority, dict):
        raise ProtocolValidationError("human_authority must be an object", field="human_authority")
    normalized: dict[str, Any] = {}
    for key in ("owner_id", "via_agent_id", "approval_ref"):
        value = authority.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProtocolValidationError(
                f"human_authority.{key} must be a non-empty string",
                field=f"human_authority.{key}",
            )
        normalized[key] = value.strip()
    summary = authority.get("summary")
    if summary is None:
        raise ProtocolValidationError(
            "human_authority.summary must be a non-empty string",
            field="human_authority.summary",
        )
    if not isinstance(summary, str) or not summary.strip():
        raise ProtocolValidationError(
            "human_authority.summary must be a non-empty string",
            field="human_authority.summary",
        )
    normalized["summary"] = summary.strip()
    visibility = authority.get("visibility", "redacted")
    if visibility not in SOURCE_REF_VISIBILITIES:
        raise ProtocolValidationError(
            "human_authority.visibility must be public, redacted, or private",
            field="human_authority.visibility",
        )
    normalized["visibility"] = visibility
    if visibility == "private":
        normalized["redacted"] = True
        normalized["summary"] = "Private human task amendment retained by the local agent."
    source_refs = normalize_source_refs(authority.get("source_refs", []), field="human_authority.source_refs")
    if source_refs:
        normalized["source_refs"] = source_refs
    return normalized


def redact_source_ref(
    source_ref: dict[str, Any],
    ref_type: str,
    label: str,
    visibility: str,
    summary: str | None,
    uri: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if visibility == "private":
        return {
            "type": ref_type,
            "label": label,
            "visibility": "private",
            "summary": summary or "Private source retained by the local agent.",
            "redacted": True,
        }
    normalized: dict[str, Any] = {
        "type": ref_type,
        "label": label,
        "visibility": visibility,
    }
    if summary:
        normalized["summary"] = summary
    if visibility == "public":
        if uri:
            normalized["uri"] = uri
        if metadata:
            normalized["metadata"] = metadata
    elif visibility == "redacted":
        normalized["redacted"] = True
        if source_ref.get("uri") or source_ref.get("metadata"):
            normalized["redaction_reason"] = "uri_and_metadata_hidden"
    return normalized


def normalize_completion_authority(authority: Any) -> dict[str, Any] | None:
    if authority is None:
        return None
    if not isinstance(authority, dict):
        raise ProtocolValidationError("completion_authority must be an object", field="completion_authority")
    authority_type = require_str(authority, "type", "completion_authority.type")
    if authority_type not in {"agent", "human"}:
        raise ProtocolValidationError(
            "completion_authority.type must be agent or human",
            field="completion_authority.type",
        )
    normalized: dict[str, Any] = {"type": authority_type}
    for key in ("owner_id", "via_agent_id", "approval_ref"):
        value = authority.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ProtocolValidationError(
                    f"completion_authority.{key} must be a non-empty string",
                    field=f"completion_authority.{key}",
                )
            normalized[key] = value.strip()
    summary = authority.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ProtocolValidationError(
                "completion_authority.summary must be a non-empty string",
                field="completion_authority.summary",
            )
        normalized["summary"] = summary.strip()
    visibility = authority.get("visibility", "redacted")
    if visibility not in SOURCE_REF_VISIBILITIES:
        raise ProtocolValidationError(
            "completion_authority.visibility must be public, redacted, or private",
            field="completion_authority.visibility",
        )
    normalized["visibility"] = visibility
    if visibility == "private":
        normalized["redacted"] = True
        normalized["summary"] = summary.strip() if isinstance(summary, str) and summary.strip() else "Private approval retained by the local agent."
    approval_refs = normalize_source_refs(authority.get("source_refs", []), field="completion_authority.source_refs")
    if approval_refs:
        normalized["source_refs"] = approval_refs
    return normalized


def success_envelope(
    data: dict[str, Any],
    *,
    next_action: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "data": data}
    if next_action is not None:
        payload["next_action"] = next_action
    if meta:
        payload["meta"] = meta
    return payload


def error_envelope(
    message: str,
    *,
    error_type: str = "api_error",
    code: str = "ERROR",
    hint: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": error_type,
        "code": code,
        "message": message,
    }
    if hint:
        error["hint"] = hint
    if detail:
        error["detail"] = detail
    return {"ok": False, "error": error}


def next_action_for_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    task = payload.get("task")
    if not isinstance(task, dict):
        return None
    if task.get("status") == "completed":
        return {"type": "none", "reason": "task_completed"}
    pending_agent = task.get("pending_on_agent_id")
    if pending_agent:
        return {
            "type": "wait_for_agent",
            "agent_id": pending_agent,
            "description": task.get("next_action") or "Waiting for the pending agent to act.",
        }
    next_action = task.get("next_action")
    if next_action:
        return {"type": "local_action", "description": next_action}
    return None


def require_literal(payload: dict[str, Any], key: str, expected: str) -> None:
    value = payload.get(key)
    if value != expected:
        raise ProtocolValidationError(
            f"{key} must be {expected}",
            field=key,
            hint=f"Set {key} to {expected} for Protocol v0.3 requests.",
        )


def require_object(payload: dict[str, Any], key: str, field: str | None = None) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ProtocolValidationError(f"{key} must be an object", field=field or key)
    return value


def require_str(payload: dict[str, Any], key: str, field: str | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolValidationError(f"{key} must be a non-empty string", field=field or key)
    return value.strip()


def require_non_empty_list(payload: dict[str, Any], key: str, field: str | None = None) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list) or len(value) == 0:
        raise ProtocolValidationError(f"{key} must be a non-empty array", field=field or key)
    return value


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
