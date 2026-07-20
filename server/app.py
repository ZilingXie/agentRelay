from __future__ import annotations

import json
import hmac
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from server.store import ConflictError, Store, read_alias
from server.store_v05 import V05Store
from server.transitions import TERMINAL_STATES
from server.protocol_v03 import (
    ENVELOPE_V03,
    ProtocolValidationError,
    error_envelope,
    is_protocol_v03,
    next_action_for_payload,
    success_envelope,
    validate_artifact_submit,
    validate_task_amend,
    validate_task_close,
    validate_task_create,
)
from server.protocol_v04 import (
    is_protocol_v04,
    validate_ack as validate_v04_ack,
    validate_complete as validate_v04_complete,
    validate_fail as validate_v04_fail,
    validate_message_submit as validate_v04_message_submit,
    validate_task_create as validate_v04_task_create,
)
from server.protocol_v05 import (
    is_protocol_v05,
    reject_unknown as reject_unknown_v05,
    require_string as require_string_v05,
    validate_ack as validate_v05_ack,
    validate_complete as validate_v05_complete,
    validate_delivery_fail as validate_v05_delivery_fail,
    validate_event_ack as validate_v05_event_ack,
    validate_fail as validate_v05_fail,
    validate_message_submit as validate_v05_message_submit,
    validate_readiness_publish as validate_v05_readiness_publish,
    validate_readiness_register as validate_v05_readiness_register,
    validate_task_create as validate_v05_task_create,
    validate_visibility_batch as validate_v05_visibility_batch,
)
from server.protocol_registry import (
    CURRENT_PROTOCOL_SHORT,
    PROTOCOL_NAME,
    ProtocolNegotiationRequired,
    ensure_protocol_compatible,
    negotiate_protocol,
    negotiation_error_detail,
    protocol_bundle,
    protocol_bundle_v04,
    protocol_bundle_v05,
    protocol_manifest,
    protocol_manifest_v04,
    protocol_manifest_v05,
    protocol_summary,
)


DEFAULT_DB_PATH = "./data/agentrelay.sqlite3"
DEFAULT_V05_DB_PATH = "./data/agentrelay-v05.sqlite3"
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


class AgentRelayHandler(BaseHTTPRequestHandler):
    store: Store
    v05_store: V05Store | None = None
    mutation_mode: str = "legacy"
    auth_identities: dict[str, dict[str, str]] = {}
    auth_required: bool = False
    admin_token: str = ""

    def do_GET(self) -> None:
        self.protocol_v03_response = False
        try:
            self.route_get()
        except ConflictError as exc:
            self.respond_error(
                409, str(exc), error_type="conflict", code=exc.code,
                detail={"current_task": exc.current_task} if exc.current_task else None,
            )
        except ProtocolNegotiationRequired as exc:
            self.respond_protocol_negotiation_required(exc)
        except ProtocolValidationError as exc:
            self.respond_protocol_error(exc)
        except ValueError as exc:
            self.respond_error(400, str(exc), error_type="validation", code="VALIDATION_ERROR")
        except Exception as exc:
            self.respond_error(500, f"internal error: {exc}", error_type="internal", code="INTERNAL_ERROR")

    def do_POST(self) -> None:
        self.protocol_v03_response = False
        try:
            self.route_post()
        except ConflictError as exc:
            self.respond_error(
                409, str(exc), error_type="conflict", code=exc.code,
                detail={"current_task": exc.current_task} if exc.current_task else None,
            )
        except ProtocolNegotiationRequired as exc:
            self.respond_protocol_negotiation_required(exc)
        except ProtocolValidationError as exc:
            self.respond_protocol_error(exc)
        except ValueError as exc:
            self.respond_error(400, str(exc), error_type="validation", code="VALIDATION_ERROR")
        except Exception as exc:
            self.respond_error(500, f"internal error: {exc}", error_type="internal", code="INTERNAL_ERROR")

    def route_get(self) -> None:
        path = clean_path(self.path)
        query = query_params(self.path)
        if path == "/health":
            self.respond_json({"ok": True, "service": "agentrelay", "protocol": self.current_protocol_summary()})
            return
        if path == "/agentrelay/health":
            self.respond_json({"ok": True, "service": "agentrelay", "protocol": self.current_protocol_summary()})
            return
        if path == "/agentrelay/protocols":
            self.respond_json({"protocols": [protocol_summary(public_base_url())]})
            return
        if path == "/agentrelay/protocols/current":
            self.respond_json(self.current_protocol_manifest())
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/{CURRENT_PROTOCOL_SHORT}/manifest":
            self.respond_json(protocol_manifest(public_base_url()))
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/{CURRENT_PROTOCOL_SHORT}/bundle":
            self.respond_json(protocol_bundle(public_base_url()))
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/v0.4/manifest":
            self.respond_json(protocol_manifest_v04(public_base_url()))
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/v0.4/bundle":
            self.respond_json(protocol_bundle_v04(public_base_url()))
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/v0.5/manifest":
            self.respond_json(
                protocol_manifest_v05(public_base_url(), write_mode=self.mutation_mode)
            )
            return
        if path == f"/agentrelay/protocols/{PROTOCOL_NAME}/v0.5/bundle":
            self.respond_json(
                protocol_bundle_v05(public_base_url(), write_mode=self.mutation_mode)
            )
            return
        if path in {"/agentrelay/dashboard", "/agentrelay/dashboard/"}:
            self.serve_dashboard_asset("index.html")
            return
        if path.startswith("/agentrelay/dashboard/"):
            self.serve_dashboard_asset(path.removeprefix("/agentrelay/dashboard/"))
            return
        if path in {"/agentrelay/schemas", "/agentrelay/schemas/"}:
            self.serve_schema_asset("README.md")
            return
        if path.startswith("/agentrelay/schemas/"):
            self.serve_schema_asset(path.removeprefix("/agentrelay/schemas/"))
            return
        if path.startswith("/agentrelay/docs/"):
            self.serve_public_asset(DOCS_DIR, path.removeprefix("/agentrelay/docs/"), {".md"})
            return
        if path.startswith("/agentrelay/examples/"):
            self.serve_public_asset(EXAMPLES_DIR, path.removeprefix("/agentrelay/examples/"), {".json"})
            return
        if path.startswith("/agentrelay/admin/api/"):
            if not self.require_admin_auth():
                return
            self.route_admin_get(path, query)
            return
        auth = self.require_auth()
        if auth is None:
            return
        if match := re.fullmatch(r"/agentrelay/legacy/tasks/([^/]+)", path):
            task = self.store.get_task(match.group(1))
            if not task:
                self.respond_error(404, "legacy task not found")
                return
            if not self.require_task_participant(auth, task):
                return
            self.respond_json({"task": task, "archive": {"read_only": True}})
            return
        if match := re.fullmatch(r"/agentrelay/legacy/tasks/([^/]+)/timeline", path):
            task = self.store.get_task(match.group(1))
            if not task:
                self.respond_error(404, "legacy task not found")
                return
            if not self.require_task_participant(auth, task):
                return
            self.respond_json(
                {"timeline": self.store.get_timeline(match.group(1)), "archive": {"read_only": True}}
            )
            return
        if match := re.fullmatch(r"/agentrelay/legacy/tasks/([^/]+)/lineage", path):
            task = self.store.get_task(match.group(1))
            if not task:
                self.respond_error(404, "legacy task not found")
                return
            if not self.require_task_participant(auth, task):
                return
            self.respond_json(
                {"tasks": self.store.get_task_lineage(match.group(1)), "archive": {"read_only": True}}
            )
            return
        if path == "/agentrelay/agents":
            if self.mutation_mode in {"closed", "v05"}:
                store = self.require_v05_store()
                if store is None:
                    return
                self.respond_json({"agents": store.admin_agents()})
                return
            self.respond_json({"agents": self.store.list_agents()})
            return
        if path == "/agentrelay/agents/cards":
            cards = [agent_card(agent) for agent in self.store.list_agents()]
            self.respond_json({"agentCards": cards, "agent_cards": cards})
            return
        if match := re.fullmatch(r"/agentrelay/agents/([^/]+)/card", path):
            agent_id = match.group(1)
            agent = self.store.get_agent(agent_id)
            if not agent:
                self.respond_error(404, "agent not found")
                return
            self.respond_json(agent_card(agent))
            return
        if match := re.fullmatch(r"/agentrelay/agents/([^/]+)/a2a-map", path):
            agent_id = match.group(1)
            agent = self.store.get_agent(agent_id)
            if not agent:
                self.respond_error(404, "agent not found")
                return
            self.respond_json(a2a_mapping(agent))
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/visibility", path):
            store = self.require_v05_store()
            if store is None:
                return
            item = store.visibility(match.group(1))
            if not item:
                self.respond_error(404, "task not found", code="task_not_found")
                return
            if not self.require_task_participant(auth, item["task"]):
                return
            self.respond_json(item)
            return
        if (
            (match := re.fullmatch(r"/agentrelay/tasks/([^/]+)", path))
            and self.mutation_mode in {"closed", "v05"}
        ):
            store = self.require_v05_store()
            if store is None:
                return
            detail = store.get_task_detail(match.group(1))
            if not detail:
                self.respond_error(404, "task not found", code="task_not_found")
                return
            if not self.require_task_participant(auth, detail["task"]):
                return
            self.respond_json(detail)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)", path):
            task = self.store.get_task(match.group(1))
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if (
            (match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/lineage", path))
            and self.mutation_mode in {"closed", "v05"}
        ):
            store = self.require_v05_store()
            if store is None:
                return
            detail = store.get_task_detail(match.group(1))
            if not detail:
                self.respond_error(404, "task not found", code="task_not_found")
                return
            if not self.require_task_participant(auth, detail["task"]):
                return
            self.respond_json({"tasks": store.get_lineage(match.group(1))})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/lineage", path):
            tasks = self.store.get_task_lineage(match.group(1))
            if tasks is None:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"tasks": tasks})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/events", path):
            events = self.store.get_events(match.group(1))
            if events is None:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"events": events})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/timeline", path):
            timeline = self.store.get_timeline(match.group(1))
            if timeline is None:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"timeline": timeline})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/pending", path):
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            self.respond_protocol({"tasks": self.store.list_pending_tasks(agent_id)})
            return
        if (
            (match := re.fullmatch(r"/agentrelay/workers/([^/]+)/events", path))
            and self.mutation_mode in {"closed", "v05"}
        ):
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            store = self.require_v05_store()
            if store is None:
                return
            instance_id = first_query_value(query, "listener_instance_id")
            epoch = parse_required_positive_int_query(query, "readiness_epoch")
            if not instance_id:
                raise ValueError("missing required query field: listener_instance_id")
            store.assert_listener_epoch(agent_id, instance_id, epoch)
            event = store.recover_event(agent_id)
            self.respond_json({"events": [event] if event else []})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/events", path):
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            limit = parse_int_query(query, "limit", 100, min_value=1, max_value=500)
            cursor = first_query_value(query, "cursor") or first_query_value(query, "after")
            delivery_state = first_query_value(query, "delivery_state") or first_query_value(query, "state")
            should_claim = parse_bool_query(query, "claim", False)
            if should_claim:
                lease_seconds = parse_int_query(query, "lease_seconds", 60, min_value=1, max_value=3600)
                events = self.store.claim_agent_events(agent_id, limit, cursor, lease_seconds)
            else:
                include_acked = parse_bool_query(query, "include_acked", False)
                events = self.store.list_agent_events(
                    agent_id,
                    include_acked=include_acked,
                    limit=limit,
                    after_cursor=cursor,
                    delivery_state=delivery_state,
                )
            self.respond_protocol(agent_events_response(events))
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/claim", path):
            if not self.require_legacy_writes():
                return
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            task = self.store.claim_task(agent_id)
            if not task:
                self.respond_protocol({"task": None})
                return
            self.respond_protocol({"task": task})
            return
        self.respond_error(404, "not found")

    def route_admin_get(self, path: str, query: dict[str, list[str]]) -> None:
        if self.mutation_mode in {"closed", "v05"}:
            store = self.require_v05_store()
            if store is None:
                return
            if path == "/agentrelay/admin/api/summary":
                self.respond_json(store.admin_summary())
                return
            if path == "/agentrelay/admin/api/agents":
                self.respond_json({"agents": store.admin_agents()})
                return
            if path == "/agentrelay/admin/api/tasks":
                self.respond_json(
                    {
                        "tasks": store.admin_tasks(
                            agent_id=first_query_value(query, "agent_id") or first_query_value(query, "agent"),
                            status=first_query_value(query, "status"),
                            active=parse_optional_bool_query(query, "active"),
                            limit=parse_int_query(query, "limit", 100, min_value=1, max_value=500),
                        )
                    }
                )
                return
            if match := re.fullmatch(r"/agentrelay/admin/api/tasks/([^/]+)", path):
                detail = store.admin_task_detail(match.group(1))
                if not detail:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                self.respond_json(detail)
                return
            if path == "/agentrelay/admin/api/events":
                self.respond_json(
                    {
                        "events": store.admin_outbox_events(
                            agent_id=first_query_value(query, "agent_id") or first_query_value(query, "agent"),
                            outbox_status=first_query_value(query, "outbox_status") or first_query_value(query, "state"),
                            task_id=first_query_value(query, "task_id"),
                            limit=parse_int_query(query, "limit", 100, min_value=1, max_value=500),
                        )
                    }
                )
                return
            self.respond_error(404, "admin endpoint not found")
            return
        if path == "/agentrelay/admin/api/summary":
            self.respond_json(admin_summary(self.store))
            return
        if path == "/agentrelay/admin/api/agents":
            self.respond_json({"agents": admin_list_agents(self.store)})
            return
        if path == "/agentrelay/admin/api/tasks":
            self.respond_json(
                {
                    "tasks": admin_list_tasks(
                        self.store,
                        agent_id=first_query_value(query, "agent_id") or first_query_value(query, "agent"),
                        status=first_query_value(query, "status"),
                        active=parse_optional_bool_query(query, "active"),
                        limit=parse_int_query(query, "limit", 100, min_value=1, max_value=500),
                    )
                }
            )
            return
        if match := re.fullmatch(r"/agentrelay/admin/api/tasks/([^/]+)", path):
            task_id = match.group(1)
            task = self.store.get_task(task_id)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json(
                {
                    "task": task,
                    "timeline": self.store.get_timeline(task_id),
                    "events": self.store.get_events(task_id) or [],
                    "agent_events": admin_list_agent_events(self.store, task_id=task_id, limit=500),
                }
            )
            return
        if path == "/agentrelay/admin/api/events":
            self.respond_json(
                {
                    "events": admin_list_agent_events(
                        self.store,
                        agent_id=first_query_value(query, "agent_id") or first_query_value(query, "agent"),
                        delivery_state=first_query_value(query, "delivery_state") or first_query_value(query, "state"),
                        include_acked=parse_bool_query(query, "include_acked", False),
                        limit=parse_int_query(query, "limit", 100, min_value=1, max_value=500),
                    )
                }
            )
            return
        self.respond_error(404, "admin endpoint not found")

    def route_post(self) -> None:
        path = clean_path(self.path)
        auth = self.require_auth()
        if auth is None:
            return
        payload = self.read_json()
        self.protocol_v03_response = is_protocol_v03(payload)
        if path == "/agentrelay/protocols/negotiate":
            self.respond_json(
                negotiate_protocol(
                    payload,
                    public_base_url(),
                    write_mode=self.mutation_mode,
                )
            )
            return
        if path == "/agentrelay/protocols/validate":
            protocol_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            ensure_protocol_compatible(protocol_payload)
            operation = payload.get("operation")
            if is_protocol_v03(protocol_payload):
                validate_protocol_operation(operation, protocol_payload)
            self.respond_protocol(
                {
                    "valid": True,
                    "operation": operation,
                    "protocol": protocol_summary(public_base_url()),
                }
            )
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/readiness/register", path):
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            validate_v05_readiness_register(payload)
            store = self.require_v05_store()
            if store is None:
                return
            readiness = store.register_listener(
                agent_id,
                listener_instance_id=payload["listener_instance_id"],
                client_version=payload["client_version"],
                workspace_version=payload["workspace_version"],
                transport=payload["transport"],
            )
            self.respond_json({"readiness": readiness}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/readiness", path):
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            validate_v05_readiness_publish(payload)
            store = self.require_v05_store()
            if store is None:
                return
            readiness = store.publish_readiness(
                agent_id,
                listener_instance_id=payload["listener_instance_id"],
                readiness_epoch=payload["readiness_epoch"],
                ready=payload["ready"],
            )
            self.respond_json({"readiness": readiness})
            return
        if path == "/agentrelay/task-visibility/batch":
            task_ids = validate_v05_visibility_batch(payload)
            store = self.require_v05_store()
            if store is None:
                return
            result = store.visibility_batch(task_ids)
            if self.auth_required:
                permitted = []
                errors = list(result["errors"])
                for item in result["items"]:
                    task = item["task"]
                    if auth["agent_id"] in {
                        task["requester_agent_id"], task["target_agent_id"]
                    }:
                        permitted.append(item)
                    else:
                        errors.append(
                            {"task_id": task["task_id"], "code": "task_participant_required"}
                        )
                result = {"items": permitted, "errors": errors}
            self.respond_json(result)
            return
        if path == "/agentrelay/healthchecks/install":
            requester_agent_id = auth.get("agent_id")
            if not requester_agent_id:
                self.respond_error(
                    401,
                    "install health check requires an authenticated agent token",
                    error_type="auth_error",
                    code="AUTHENTICATED_AGENT_REQUIRED",
                )
                return
            requested_agent_id = read_alias(payload, "requester_agent_id", "requesterAgentId", payload.get("from"))
            if requested_agent_id and not self.require_agent(auth, requested_agent_id):
                return
            if self.mutation_mode == "v05":
                reject_unknown_v05(
                    payload,
                    {"idempotency_key", "requester_agent_id", "requesterAgentId", "from"},
                )
                key = require_string_v05(payload, "idempotency_key")
                store = self.require_v05_writes()
                if store is None:
                    return
                self.respond_json(
                    store.create_install_healthcheck(
                        requester_agent_id,
                        idempotency_key=key,
                    ),
                    status=201,
                )
                return
            if not self.require_legacy_writes():
                return
            result = self.store.create_install_healthcheck(
                requester_agent_id,
                requester_owner=auth.get("username") or requester_agent_id,
                requester_thread_id=payload.get("requesterThreadId") or payload.get("requester_thread_id"),
                idempotency_key=payload.get("idempotency_key"),
            )
            self.respond_protocol(result, status=201)
            return
        if path == "/agentrelay/tasks":
            ensure_protocol_compatible(payload)
            if is_protocol_v05(payload):
                validate_v05_task_create(payload)
                requester_agent_id = payload.get("requester_agent_id")
                if not self.require_agent(auth, requester_agent_id):
                    return
                store = self.require_v05_writes()
                if store is None:
                    return
                self.respond_json(store.create_task(payload), status=201)
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            if self.mutation_mode == "v05":
                self.reject_retired_protocol_mutation()
                return
            if is_protocol_v03(payload):
                validate_task_create(payload)
            elif is_protocol_v04(payload):
                validate_v04_task_create(payload)
            requester_agent_id = read_alias(payload, "requester_agent_id", "requesterAgentId", payload.get("from"))
            if not self.require_agent(auth, requester_agent_id):
                return
            task = self.store.create_task(payload)
            self.respond_protocol({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/messages", path):
            if self.mutation_mode == "v05":
                if "expected_status_version" in payload:
                    self.reject_retired_protocol_mutation()
                    return
                validate_v05_message_submit(payload)
                actor_agent_id = payload.get("actor_agent_id")
                if not self.require_agent(auth, actor_agent_id):
                    return
                store = self.require_v05_writes()
                if store is None:
                    return
                detail = store.submit_message(match.group(1), payload)
                if not detail:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                self.respond_json(detail, status=201)
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            validate_v04_message_submit(payload)
            actor_agent_id = payload.get("actor_agent_id")
            if not self.require_agent(auth, actor_agent_id):
                return
            task = self.store.submit_v04_message(match.group(1), actor_agent_id, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/messages/([^/]+)/ack", path):
            agent_id, message_id = match.groups()
            if self.mutation_mode == "v05":
                if "expected_status_version" in payload:
                    self.reject_retired_protocol_mutation()
                    return
                if payload.get("message_id") != message_id:
                    raise ValueError("message_id must match the URL path")
                validate_v05_ack(payload)
                if not self.require_agent(auth, agent_id):
                    return
                store = self.require_v05_writes()
                if store is None:
                    return
                detail = store.ack_message(agent_id, payload)
                if not detail:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                self.respond_json(detail)
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            validate_v04_ack(payload)
            if not self.require_agent(auth, agent_id):
                return
            task = self.store.ack_v04_message(agent_id, message_id, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/messages/([^/]+)/delivery-fail", path):
            agent_id, message_id = match.groups()
            if self.mutation_mode != "v05":
                self.require_v05_writes()
                return
            if payload.get("message_id") != message_id:
                raise ValueError("message_id must match the URL path")
            validate_v05_delivery_fail(payload)
            if not self.require_agent(auth, agent_id):
                return
            store = self.require_v05_writes()
            if store is None:
                return
            detail = store.fail_delivery(agent_id, payload)
            if not detail:
                self.respond_error(404, "task not found", code="task_not_found")
                return
            self.respond_json(detail)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/complete", path):
            if self.mutation_mode == "v05":
                if "expected_status_version" in payload:
                    self.reject_retired_protocol_mutation()
                    return
                validate_v05_complete(payload)
                actor_agent_id = payload.get("actor_agent_id")
                if not self.require_agent(auth, actor_agent_id):
                    return
                store = self.require_v05_writes()
                if store is None:
                    return
                detail = store.complete_task(match.group(1), payload)
                if not detail:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                self.respond_json(detail)
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            validate_v04_complete(payload)
            actor_agent_id = payload.get("actor_agent_id")
            if not self.require_agent(auth, actor_agent_id):
                return
            task = self.store.complete_v04_task(match.group(1), actor_agent_id, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/fail", path):
            if self.mutation_mode == "v05":
                if "expected_status_version" in payload:
                    self.reject_retired_protocol_mutation()
                    return
                validate_v05_fail(payload)
                actor_agent_id = payload.get("actor_agent_id")
                if not self.require_agent(auth, actor_agent_id):
                    return
                store = self.require_v05_writes()
                if store is None:
                    return
                detail = store.fail_task(match.group(1), payload)
                if not detail:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                self.respond_json(detail)
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            validate_v04_fail(payload)
            actor_agent_id = payload.get("actor_agent_id")
            if not self.require_agent(auth, actor_agent_id):
                return
            task = self.store.fail_v04_task(match.group(1), actor_agent_id, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/followups", path):
            if self.mutation_mode == "v05":
                store = self.require_v05_writes()
                if store is None:
                    return
                source = store.get_task_detail(match.group(1))
                if not source:
                    self.respond_error(404, "task not found", code="task_not_found")
                    return
                source_task = source["task"]
                payload = {
                    **payload,
                    "protocol_version": "agent-collab-v0.5",
                    "requester_agent_id": source_task["requester_agent_id"],
                    "target_agent_id": source_task["target_agent_id"],
                }
                validate_v05_task_create(payload)
                if not self.require_agent(auth, source_task["requester_agent_id"]):
                    return
                self.respond_json(
                    store.create_task(payload, source_task_id=source_task["task_id"]),
                    status=201,
                )
                return
            if self.mutation_mode == "closed":
                self.require_v05_writes()
                return
            source = self.store.get_task(match.group(1))
            if not source:
                self.respond_error(404, "task not found")
                return
            payload = {
                **payload,
                "protocol_version": "agent-collab-v0.4",
                "requester_agent_id": source["requester_agent_id"],
                "target_agent_id": source["target_agent_id"],
            }
            validate_v04_task_create(payload)
            if not self.require_agent(auth, source["requester_agent_id"]):
                return
            task = self.store.create_task_v04(payload, source_task_id=source["task_id"])
            self.respond_protocol({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/status", path):
            if not self.require_legacy_writes():
                return
            status = payload.get("status")
            if not status:
                raise ValueError("missing required field: status")
            task = self.store.update_status(match.group(1), status, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/artifacts", path):
            if not self.require_legacy_writes():
                return
            ensure_protocol_compatible(payload)
            if is_protocol_v03(payload):
                validate_artifact_submit(payload)
            artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
            actor_agent_id = (
                read_alias(payload, "actor_agent_id", "actorAgentId")
                or read_alias(artifact, "actor_agent_id", "actorAgentId")
                or payload.get("from")
            )
            if not self.require_agent(auth, actor_agent_id):
                return
            task = self.store.submit_artifact(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/amend", path):
            if not self.require_legacy_writes():
                return
            ensure_protocol_compatible(payload)
            if is_protocol_v03(payload):
                validate_task_amend(payload)
            actor_agent_id = read_alias(payload, "actor_agent_id", "actorAgentId")
            if not self.require_agent(auth, actor_agent_id):
                return
            task = self.store.amend_task(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/close", path):
            if not self.require_legacy_writes():
                return
            ensure_protocol_compatible(payload)
            if is_protocol_v03(payload):
                validate_task_close(payload)
                payload = normalize_close_payload(payload)
            closed_by_agent_id = read_alias(payload, "closed_by_agent_id", "closedByAgentId")
            if not self.require_agent(auth, closed_by_agent_id):
                return
            task = self.store.close_task(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/deliveries", path):
            if not self.require_legacy_writes():
                return
            if not self.require_agent(auth, payload.get("deliveredByAgentId")):
                return
            task = self.store.mark_delivery(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/tasks/([^/]+)/claim", path):
            if not self.require_legacy_writes():
                return
            agent_id, task_id = match.groups()
            if not self.require_agent(auth, agent_id):
                return
            task = self.store.claim_task_by_id(agent_id, task_id)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/events/([^/]+)/ack", path):
            agent_id, event_id = match.groups()
            if self.mutation_mode in {"closed", "v05"}:
                validate_v05_event_ack(payload)
                if not self.require_agent(auth, agent_id):
                    return
                store = self.require_v05_store()
                if store is None:
                    return
                event = store.ack_informational_event(agent_id, event_id, payload)
                if not event:
                    self.respond_error(404, "event not found", code="event_not_found")
                    return
                self.respond_json({"event": event})
                return
            if not self.require_agent(auth, agent_id):
                return
            task_id = payload.get("taskId")
            delivery_state = payload.get("deliveryState") or payload.get("delivery_state") or payload.get("status") or "done"
            if delivery_state == "acked":
                delivery_state = "done"
            if delivery_state not in {"done", "failed"}:
                delivery_state = "done"
            event = self.store.ack_agent_event(
                agent_id,
                event_id,
                task_id,
                delivery_state=delivery_state,
                error=payload.get("error"),
            )
            if not event:
                self.respond_error(404, "event not found")
                return
            binding = None
            thread_id = payload.get("threadId")
            if thread_id:
                binding = self.store.upsert_thread_binding(
                    event["task_id"],
                    agent_id,
                    thread_id,
                    payload.get("threadRole") or "agent_inbox",
                    payload.get("projectPath"),
                )
            self.respond_protocol({"event": event, "threadBinding": binding})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/tasks/([^/]+)/thread", path):
            if not self.require_legacy_writes():
                return
            agent_id, task_id = match.groups()
            if not self.require_agent(auth, agent_id):
                return
            thread_id = payload.get("threadId")
            if not thread_id:
                raise ValueError("missing required field: threadId")
            task = self.store.set_thread(agent_id, task_id, thread_id)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_protocol({"task": task})
            return
        self.respond_error(404, "not found")

    def require_admin_auth(self) -> bool:
        if not self.admin_token:
            self.respond_error(
                503,
                "admin API is disabled",
                error_type="admin_auth",
                code="ADMIN_TOKEN_NOT_CONFIGURED",
                hint="Set AGENTRELAY_ADMIN_TOKEN on the relay server to enable the read-only dashboard API.",
            )
            return False
        authorization = self.headers.get("Authorization", "")
        token = ""
        if authorization.startswith("Bearer "):
            token = authorization[len("Bearer "):]
        token = token or self.headers.get("X-AgentRelay-Admin-Token", "")
        if not token:
            self.respond_error(
                401,
                "missing admin token",
                error_type="admin_auth",
                code="MISSING_ADMIN_TOKEN",
            )
            return False
        if not hmac.compare_digest(token, self.admin_token):
            self.respond_error(
                403,
                "invalid admin token",
                error_type="admin_auth",
                code="INVALID_ADMIN_TOKEN",
            )
            return False
        return True

    def require_auth(self) -> dict[str, str] | None:
        if not self.auth_required:
            return {"username": "", "agent_id": ""}
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            self.respond_error(401, "missing bearer token", error_type="auth_error", code="MISSING_BEARER_TOKEN")
            return None
        token = authorization[len(prefix):]
        identity = self.auth_identities.get(token)
        if not identity:
            # Avoid leaking whether a username or agent id exists.
            self.respond_error(401, "invalid bearer token", error_type="auth_error", code="INVALID_BEARER_TOKEN")
            return None
        username = self.headers.get("X-AgentRelay-Username", "")
        agent_id = self.headers.get("X-AgentRelay-Agent-Id", "")
        if username and not hmac.compare_digest(username, identity["username"]):
            self.respond_error(403, "username does not match token", error_type="permission", code="USERNAME_TOKEN_MISMATCH")
            return None
        if agent_id and not hmac.compare_digest(agent_id, identity["agent_id"]):
            self.respond_error(403, "agent id does not match token", error_type="permission", code="AGENT_TOKEN_MISMATCH")
            return None
        return identity

    def require_agent(self, auth: dict[str, str], requested_agent_id: Any) -> bool:
        if not self.auth_required:
            return True
        if not isinstance(requested_agent_id, str) or not requested_agent_id:
            self.respond_error(
                400,
                "missing agent id for authenticated action",
                error_type="validation",
                code="MISSING_AGENT_ID",
            )
            return False
        if not hmac.compare_digest(auth["agent_id"], requested_agent_id):
            self.respond_error(
                403,
                "token cannot act as requested agent",
                error_type="permission",
                code="TOKEN_AGENT_MISMATCH",
                hint="Use the token that belongs to the actor/requester agent in this request.",
            )
            return False
        return True

    def require_task_participant(
        self, auth: dict[str, str], task: dict[str, Any]
    ) -> bool:
        if not self.auth_required:
            return True
        if auth["agent_id"] not in {
            task.get("requester_agent_id"),
            task.get("target_agent_id"),
        }:
            self.respond_error(
                403,
                "token is not a Task participant",
                error_type="permission",
                code="TASK_PARTICIPANT_REQUIRED",
            )
            return False
        return True

    def require_v05_store(self) -> V05Store | None:
        if self.v05_store is None:
            self.respond_error(
                503,
                "Protocol v0.5 storage is not configured",
                error_type="service_unavailable",
                code="V05_STORAGE_UNAVAILABLE",
            )
            return None
        return self.v05_store

    def current_protocol_manifest(self) -> dict[str, Any]:
        if self.mutation_mode in {"closed", "v05"}:
            return protocol_manifest_v05(public_base_url(), write_mode=self.mutation_mode)
        return protocol_manifest(public_base_url())

    def current_protocol_summary(self) -> dict[str, Any]:
        if self.mutation_mode in {"closed", "v05"}:
            return protocol_manifest_v05(public_base_url(), write_mode=self.mutation_mode)
        return protocol_summary(public_base_url())

    def require_v05_writes(self) -> V05Store | None:
        store = self.require_v05_store()
        if store is None:
            return None
        if self.mutation_mode != "v05":
            self.respond_error(
                503,
                "collaboration mutations are closed during Protocol v0.5 rollout",
                error_type="service_unavailable",
                code="mutations_closed",
            )
            return None
        return store

    def require_legacy_writes(self) -> bool:
        if self.mutation_mode == "v05":
            self.reject_retired_protocol_mutation()
            return False
        if self.mutation_mode == "closed":
            self.require_v05_writes()
            return False
        return True

    def reject_retired_protocol_mutation(self) -> None:
        self.respond_error(
            410,
            "v0.3/v0.4 collaboration mutations are retired",
            error_type="protocol_retired",
            code="protocol_retired",
        )

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def respond_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_static(self, raw: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def serve_dashboard_asset(self, relative_path: str) -> None:
        asset = "index.html" if not relative_path else relative_path
        if asset.endswith("/"):
            asset += "index.html"
        if asset not in {"index.html", "app.js", "styles.css"}:
            self.respond_error(404, "dashboard asset not found")
            return
        file_path = DASHBOARD_DIR / asset
        if not file_path.exists():
            self.respond_error(404, "dashboard asset not found")
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        self.respond_static(file_path.read_bytes(), content_type)

    def serve_schema_asset(self, relative_path: str) -> None:
        asset = "README.md" if not relative_path else relative_path
        if "/" in asset or "\\" in asset or asset.startswith("."):
            self.respond_error(404, "schema asset not found")
            return
        if not (asset.endswith(".schema.json") or asset == "README.md"):
            self.respond_error(404, "schema asset not found")
            return
        file_path = SCHEMA_DIR / asset
        if not file_path.exists():
            self.respond_error(404, "schema asset not found")
            return
        content_type = {
            ".json": "application/schema+json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        self.respond_static(file_path.read_bytes(), content_type)

    def serve_public_asset(self, root: Path, relative_path: str, allowed_suffixes: set[str]) -> None:
        if not relative_path or relative_path.endswith("/"):
            self.respond_error(404, "asset not found")
            return
        file_path = (root / relative_path).resolve()
        try:
            file_path.relative_to(root.resolve())
        except ValueError:
            self.respond_error(404, "asset not found")
            return
        if file_path.suffix not in allowed_suffixes or not file_path.exists():
            self.respond_error(404, "asset not found")
            return
        content_type = {
            ".json": "application/json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        self.respond_static(file_path.read_bytes(), content_type)

    def respond_protocol(self, payload: dict[str, Any], status: int = 200) -> None:
        if self.wants_envelope():
            self.respond_json(
                success_envelope(
                    payload,
                    next_action=next_action_for_payload(payload),
                    meta={"envelope": ENVELOPE_V03},
                ),
                status=status,
            )
            return
        self.respond_json(payload, status=status)

    def respond_error(
        self,
        status: int,
        message: str,
        *,
        error_type: str = "api_error",
        code: str = "ERROR",
        hint: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self.wants_envelope():
            self.respond_json(
                error_envelope(
                    message,
                    error_type=error_type,
                    code=code,
                    hint=hint,
                    detail=detail,
                ),
                status=status,
            )
            return
        body: dict[str, Any] = {"error": message, "code": code}
        if detail is not None:
            body["detail"] = detail
        self.respond_json(body, status=status)

    def respond_protocol_error(self, exc: ProtocolValidationError) -> None:
        self.respond_error(
            400,
            str(exc),
            error_type="validation",
            code=exc.code,
            hint=exc.hint,
            detail={"field": exc.field} if exc.field else None,
        )

    def respond_protocol_negotiation_required(self, exc: ProtocolNegotiationRequired) -> None:
        self.protocol_v03_response = True
        self.respond_error(
            exc.status,
            str(exc),
            error_type="protocol_negotiation",
            code=exc.code,
            hint=exc.hint,
            detail=negotiation_error_detail(
                exc,
                public_base_url(),
                manifest=self.current_protocol_manifest(),
            ),
        )

    def wants_envelope(self) -> bool:
        return (
            self.headers.get("X-AgentRelay-Envelope", "") == ENVELOPE_V03
            or bool(getattr(self, "protocol_v03_response", False))
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def clean_path(path: str) -> str:
    parsed = urlparse(path)
    clean = parsed.path.rstrip("/") or "/"
    if clean == "/agentrelay/api":
        return "/agentrelay"
    if clean.startswith("/agentrelay/api/"):
        return "/agentrelay/" + clean[len("/agentrelay/api/"):]
    return clean


def query_params(path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(path).query, keep_blank_values=False)


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def parse_bool_query(query: dict[str, list[str]], key: str, default: bool) -> bool:
    value = first_query_value(query, key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_optional_bool_query(query: dict[str, list[str]], key: str) -> bool | None:
    value = first_query_value(query, key)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def parse_int_query(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    value = first_query_value(query, key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < min_value or parsed > max_value:
        raise ValueError(f"{key} must be between {min_value} and {max_value}")
    return parsed


def parse_required_positive_int_query(query: dict[str, list[str]], key: str) -> int:
    value = first_query_value(query, key)
    if value is None:
        raise ValueError(f"missing required query field: {key}")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def public_base_url() -> str:
    return os.environ.get("AGENTRELAY_PUBLIC_BASE_URL", "https://server.stellarix.space/agentrelay")


def validate_protocol_operation(operation: Any, payload: dict[str, Any]) -> None:
    if operation in {"task_create", "create_task", "POST /tasks", None, ""}:
        validate_task_create(payload)
        return
    if operation in {"artifact_submit", "submit_artifact", "POST /tasks/{task_id}/artifacts"}:
        validate_artifact_submit(payload)
        return
    if operation in {"task_amend", "amend_task", "POST /tasks/{task_id}/amend"}:
        validate_task_amend(payload)
        return
    if operation in {"task_close", "close_task", "POST /tasks/{task_id}/close"}:
        validate_task_close(payload)
        return
    raise ValueError("unknown protocol validation operation")


def agent_events_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    next_cursor = events[-1]["cursor"] if events else None
    return {
        "events": events,
        "next_cursor": next_cursor,
        "nextCursor": next_cursor,
    }


def agent_card(agent: dict[str, Any]) -> dict[str, Any]:
    agent_id = agent["agent_id"]
    relay_base_url = public_base_url()
    api_base_url = f"{relay_base_url.rstrip('/')}/api"
    a2a_url = f"{api_base_url}/a2a/{agent_id}"
    agentrelay_url = f"{api_base_url}/agents/{agent_id}/card"
    role_profile = agent_role_profile(agent)
    skills = default_agent_skills(agent)
    return {
        "protocolVersion": "agentrelay-agent-card-v0.3",
        "a2aProtocolVersion": "0.3",
        "name": agent["name"],
        "description": agent["description"],
        "url": a2a_url,
        "supportedInterfaces": [
            {
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "0.3",
                "url": a2a_url,
                "tenant": agent_id,
            },
            {
                "protocolBinding": "AGENTRELAY_HTTP",
                "protocolVersion": "agent-collab-v0.3",
                "url": api_base_url,
                "tenant": agent_id,
            },
        ],
        "provider": {
            "organization": agent["owner"],
            "url": relay_base_url.rstrip("/"),
        },
        "version": "agentrelay-agent-card-v0.3",
        "documentationUrl": f"{relay_base_url.rstrip('/')}/plan.html",
        "capabilities": {
            "streaming": False,
            "pushNotifications": True,
            "stateTransitionHistory": True,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": "https://server.stellarix.space/agentrelay/protocol/agent-collab-v0.3",
                    "description": "AgentRelay two-agent collaboration semantics over HTTP relay events.",
                    "required": False,
                    "params": {
                        "completion_owner": "requester_agent",
                        "artifact_auto_completes_task": False,
                        "event_delivery": "cursor+lease+ack",
                        "agent_role": role_profile["agent_role"],
                        "execution_mode": role_profile["execution_mode"],
                    },
                }
            ],
        },
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "AgentRelay bearer token bound to X-AgentRelay-Agent-Id and X-AgentRelay-Username.",
            }
        },
        "securityRequirements": [{"bearerAuth": []}],
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
        "agentRelay": {
            "agent_id": agent_id,
            "owner": agent["owner"],
            "agent_role": role_profile["agent_role"],
            "execution_mode": role_profile["execution_mode"],
            "role_description": role_profile["role_description"],
            "protocol_capabilities": role_profile["capabilities"],
            "policy": role_profile["policy"],
            "accepted_task_types": accepted_task_types(skills),
            "scopes": default_agent_scopes(agent_id),
            "human_approval_policy": default_human_approval_policy(agent),
            "endpoints": {
                "card": agentrelay_url,
                "a2a_map": f"{api_base_url}/agents/{agent_id}/a2a-map",
                "events": f"{api_base_url}/workers/{agent_id}/events",
                "websocket": f"{relay_base_url.rstrip('/')}/workers/{agent_id}/events/ws",
            },
        },
    }


def agent_role_profile(agent: dict[str, Any]) -> dict[str, Any]:
    agent_role = str(agent.get("agent_role") or "personal_agent")
    execution_mode = str(agent.get("execution_mode") or ("autonomous" if agent_role == "service_agent" else "notify_only"))
    capabilities = parse_json_list(agent.get("capabilities_json")) or default_protocol_capabilities(agent_role)
    policy = parse_json_object(agent.get("policy_json")) or default_role_policy(agent_role)
    role_description = (
        "Autonomous or semi-autonomous service agent. It can claim assigned work and submit artifacts, but it must not change requester-owned goals."
        if agent_role == "service_agent"
        else "Human-owned personal agent. It is notifier-first by default and may amend or close requester-owned work only with human authority."
    )
    return {
        "agent_role": agent_role,
        "execution_mode": execution_mode,
        "role_description": role_description,
        "capabilities": capabilities,
        "policy": policy,
    }


def default_protocol_capabilities(agent_role: str) -> list[str]:
    if agent_role == "service_agent":
        return ["task_claim", "task_execute", "artifact_submit", "clarification_request"]
    return ["task_create", "task_review", "task_close", "task_amend_with_human_authority"]


def default_role_policy(agent_role: str) -> dict[str, Any]:
    if agent_role == "service_agent":
        return {
            "autonomous_execution_allowed": True,
            "can_amend_goal": False,
            "can_close_owned_task": False,
            "high_impact_requires_approval": True,
            "secret_safe_push_only": True,
        }
    return {
        "autonomous_execution_allowed": False,
        "can_amend_goal": True,
        "can_close_owned_task": True,
        "requires_human_authority_for_amend": True,
        "requires_human_authority_for_close": True,
        "high_impact_requires_approval": True,
        "secret_safe_push_only": True,
    }


def parse_json_list(raw: Any) -> list[str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if str(item).strip()]


def parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def a2a_mapping(agent: dict[str, Any]) -> dict[str, Any]:
    agent_id = agent["agent_id"]
    role_profile = agent_role_profile(agent)
    return {
        "agent_id": agent_id,
        "agent_role": role_profile["agent_role"],
        "execution_mode": role_profile["execution_mode"],
        "a2a_protocol_version": "0.3",
        "agent_card_url": f"/agentrelay/api/agents/{agent_id}/card",
        "preferred_interface": {
            "protocolBinding": "AGENTRELAY_HTTP",
            "protocolVersion": "agent-collab-v0.3",
            "tenant": agent_id,
        },
        "object_map": {
            "AgentCard": "AgentRelay agent card with agentRelay extension metadata",
            "Message": "Task create message.parts or artifact.parts",
            "Task": "AgentRelay task",
            "Artifact": "AgentRelay artifact; never completes task automatically",
            "TaskStatus": "AgentRelay task.status plus pending_on_agent_id and next_action",
            "PushNotification": "AgentRelay agent_events via cursor reads or WebSocket push",
        },
        "operation_map": {
            "message/send": {
                "agentrelay": "POST /agentrelay/api/tasks",
                "notes": "Requester agent creates a task for target_agent_id; tenant maps to target agent.",
            },
            "tasks/get": {
                "agentrelay": "GET /agentrelay/api/tasks/{task_id}",
            },
            "tasks/cancel": {
                "agentrelay": "POST /agentrelay/api/tasks/{task_id}/status",
                "notes": "Use terminal status with terminalReason; destructive cancellation policy is still protocol-local.",
            },
            "tasks/pushNotificationConfig/*": {
                "agentrelay": "GET /agentrelay/api/workers/{agent_id}/events or /events/ws",
                "notes": "AgentRelay uses durable event delivery instead of A2A webhook registration in this phase.",
            },
            "agent/getAuthenticatedExtendedCard": {
                "agentrelay": "GET /agentrelay/api/agents/{agent_id}/card",
                "notes": "Extended cards are not separated yet; capabilities.extendedAgentCard is false.",
            },
        },
        "compatibility": {
            "full_a2a_runtime": False,
            "json_rpc_endpoint": False,
            "http_json_mapping": True,
            "agent_card_discovery": True,
        },
    }


def default_agent_skills(agent: dict[str, Any]) -> list[dict[str, Any]]:
    if agent["agent_id"] == "project-hermes":
        return [
            {
                "id": "collab-workspace-governance",
                "name": "Collab workspace governance",
                "description": "Receive Project Hermes relay tasks, record governed workspace notes, and maintain collab_workspace project state within L0/L1/L2 policy.",
                "tags": ["project-hermes", "collab_workspace", "workspace", "governance"],
                "examples": [
                    "Record an AgentRelay request as a governed Project Hermes workspace note.",
                    "Summarize a workspace state update with source references.",
                ],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
                "agentRelay": {
                    "accepted_task_types": ["workspace.note", "project.state_update", "artifact.review"],
                    "intents": ["workspace_change_request", "project_state_update", "review_result_ingestion"],
                    "l3_actions": "forbidden",
                },
            },
            {
                "id": "bounded-dashboard-edit",
                "name": "Bounded dashboard edit",
                "description": "Execute narrow approved edits to the Project Hermes dashboard, including known title font-size adjustments in dashboard.html.",
                "tags": ["dashboard", "html", "font-size", "bounded-edit"],
                "examples": [
                    "Increase the dashboard title text font size by 2px and report the file location and verification result.",
                ],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
                "agentRelay": {
                    "accepted_task_types": ["workspace.dashboard_title_font_size"],
                    "intents": ["dashboard_title_font_size_updated"],
                    "scope": "/home/ubuntu/collab_workspace/dashboard.html",
                },
            },
            {
                "id": "human-agent-dispatch",
                "name": "Human agent dispatch",
                "description": "Dispatch Project Hermes tasks to zac-agent or vivi-agent when the target human agent can be inferred.",
                "tags": ["dispatch", "zac-agent", "vivi-agent", "task-routing"],
                "examples": [
                    "Dispatch a review task to vivi-agent and return the child task id.",
                ],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
                "agentRelay": {
                    "accepted_task_types": ["project.dispatch"],
                    "intents": ["dispatch_to_human_agent"],
                    "targets": ["zac-agent", "vivi-agent"],
                },
            },
        ]

    return [
        {
            "id": "meeting-coordination",
            "name": "Meeting coordination",
            "description": "Coordinate with the represented owner to return approved meeting availability or confirmations.",
            "tags": ["meeting", "calendar", "availability", "coordination"],
            "examples": [
                f"Ask {agent['owner']} whether Tuesday 10:00 works for a 30-minute online meeting.",
                "Return candidate availability with redacted source references.",
            ],
            "inputModes": ["application/json", "text/plain"],
            "outputModes": ["application/json", "text/plain"],
            "agentRelay": {
                "accepted_task_types": ["meeting.schedule", "meeting.availability"],
                "intents": ["request_availability", "provide_availability", "meeting_confirmation"],
                "requires_owner_approval_for": ["sharing_availability", "committing_to_meeting_time"],
            },
        },
        {
            "id": "agentrelay-artifact-review",
            "name": "Artifact review and completion handoff",
            "description": "Return work artifacts with source references and transfer task ownership without closing requester-owned tasks.",
            "tags": ["artifact", "handoff", "source_refs", "approval"],
            "examples": [
                "Submit an availability_response artifact with source_refs.",
                "Transfer pending ownership back to the requester agent for final completion review.",
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
            "agentRelay": {
                "accepted_task_types": ["artifact.review", "approval.summary"],
                "intents": ["work_result", "approval_summary"],
                "artifact_auto_completes_task": False,
            },
        },
    ]


def accepted_task_types(skills: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for skill in skills:
        task_types = skill.get("agentRelay", {}).get("accepted_task_types", [])
        for task_type in task_types:
            if task_type not in result:
                result.append(task_type)
    return result


def default_agent_scopes(agent_id: str) -> list[str]:
    return [
        f"agent:{agent_id}:tasks:create",
        f"agent:{agent_id}:tasks:claim",
        f"agent:{agent_id}:artifacts:submit",
        f"agent:{agent_id}:events:read",
        f"agent:{agent_id}:events:ack",
    ]


def default_human_approval_policy(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": agent["owner"],
        "human_is_represented_as": "agent_owner",
        "private_owner_agent_conversation": "not_relayed_by_default",
        "approval_recording": "redacted_summary_or_source_ref",
        "requires_approval_for": [
            "sharing_private_availability",
            "making_external_commitments",
            "disclosing_private_source_details",
            "closing_when_human_judgment_is_required",
        ],
    }


def normalize_close_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "closedByAgentId" not in normalized and "closed_by_agent_id" in normalized:
        normalized["closedByAgentId"] = normalized["closed_by_agent_id"]
    if "terminalReason" not in normalized and "terminal_reason" in normalized:
        normalized["terminalReason"] = normalized["terminal_reason"]
    return normalized


def admin_summary(store: Store) -> dict[str, Any]:
    with store.connect() as conn:
        return {
            "agents": scalar(conn, "SELECT COUNT(*) FROM agents"),
            "tasks": {
                "total": scalar(conn, "SELECT COUNT(*) FROM tasks"),
                "active": scalar(
                    conn,
                    f"SELECT COUNT(*) FROM tasks WHERE status NOT IN ({placeholders(TERMINAL_STATES)})",
                    sorted(TERMINAL_STATES),
                ),
                "terminal": scalar(
                    conn,
                    f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders(TERMINAL_STATES)})",
                    sorted(TERMINAL_STATES),
                ),
                "by_status": grouped_counts(conn, "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status"),
                "pending_by_agent": grouped_counts(
                    conn,
                    """
                    SELECT COALESCE(pending_on_agent_id, 'none'), COUNT(*)
                    FROM tasks
                    GROUP BY COALESCE(pending_on_agent_id, 'none')
                    ORDER BY 1
                    """,
                ),
            },
            "agent_events": {
                "total": scalar(conn, "SELECT COUNT(*) FROM agent_events"),
                "unacked": scalar(conn, "SELECT COUNT(*) FROM agent_events WHERE acked_at IS NULL"),
                "by_delivery_state": grouped_counts(
                    conn,
                    """
                    SELECT delivery_state, COUNT(*)
                    FROM agent_events
                    GROUP BY delivery_state
                    ORDER BY delivery_state
                    """,
                ),
            },
            "recent_task_events": admin_recent_task_events(conn, 20),
        }


def admin_list_agents(store: Store) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.*,
                   COALESCE(pending.pending_count, 0) AS pending_task_count,
                   COALESCE(active.active_count, 0) AS active_task_count,
                   COALESCE(events.unacked_count, 0) AS unacked_event_count
            FROM agents a
            LEFT JOIN (
                SELECT pending_on_agent_id AS agent_id, COUNT(*) AS pending_count
                FROM tasks
                WHERE pending_on_agent_id IS NOT NULL
                GROUP BY pending_on_agent_id
            ) pending ON pending.agent_id = a.agent_id
            LEFT JOIN (
                SELECT agent_id, COUNT(*) AS active_count
                FROM (
                    SELECT requester_agent_id AS agent_id FROM tasks WHERE status NOT IN ({terminal})
                    UNION ALL
                    SELECT target_agent_id AS agent_id FROM tasks WHERE status NOT IN ({terminal})
                    UNION ALL
                    SELECT completion_owner_agent_id AS agent_id FROM tasks WHERE status NOT IN ({terminal})
                )
                GROUP BY agent_id
            ) active ON active.agent_id = a.agent_id
            LEFT JOIN (
                SELECT agent_id, COUNT(*) AS unacked_count
                FROM agent_events
                WHERE acked_at IS NULL
                GROUP BY agent_id
            ) events ON events.agent_id = a.agent_id
            ORDER BY a.agent_id
            """.format(terminal=placeholders(TERMINAL_STATES)),
            sorted(TERMINAL_STATES) * 3,
        ).fetchall()
        return [dict(row) for row in rows]


def admin_list_tasks(
    store: Store,
    agent_id: str | None = None,
    status: str | None = None,
    active: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if agent_id:
        where.append(
            """
            (
              requester_agent_id = ?
              OR target_agent_id = ?
              OR completion_owner_agent_id = ?
              OR pending_on_agent_id = ?
              OR claimed_by = ?
            )
            """
        )
        params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])
    if status:
        where.append("status = ?")
        params.append(status)
    if active is True:
        where.append(f"status NOT IN ({placeholders(TERMINAL_STATES)})")
        params.extend(sorted(TERMINAL_STATES))
    elif active is False:
        where.append(f"status IN ({placeholders(TERMINAL_STATES)})")
        params.extend(sorted(TERMINAL_STATES))
    sql = """
        SELECT task_id, context_id, subject, status, requester_agent_id, target_agent_id,
               completion_owner_agent_id, pending_on_agent_id, claimed_by, next_action,
               turn_count, max_turns, delivery_status, created_at, updated_at
        FROM tasks
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, created_at DESC, task_id LIMIT ?"
    params.append(limit)
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def admin_list_agent_events(
    store: Store,
    agent_id: str | None = None,
    delivery_state: str | None = None,
    include_acked: bool = False,
    task_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if agent_id:
        where.append("ae.agent_id = ?")
        params.append(agent_id)
    if delivery_state:
        where.append("ae.delivery_state = ?")
        params.append(delivery_state)
    if task_id:
        where.append("ae.task_id = ?")
        params.append(task_id)
    if not include_acked:
        where.append("ae.acked_at IS NULL")
    sql = """
        SELECT ae.event_id, ae.agent_id, ae.event_type, ae.task_id, ae.delivery_state,
               ae.delivery_attempts, ae.inflight_until, ae.done_at, ae.failed_at,
               ae.last_error, ae.acked_at, ae.created_at, t.subject, t.status AS task_status
        FROM agent_events ae
        LEFT JOIN tasks t ON t.task_id = ae.task_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ae.created_at DESC, ae.event_id DESC LIMIT ?"
    params.append(limit)
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def admin_recent_task_events(conn: Any, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT te.event_id, te.task_id, te.event_type, te.created_at,
               t.subject, t.status, t.pending_on_agent_id
        FROM task_events te
        LEFT JOIN tasks t ON t.task_id = te.task_id
        ORDER BY te.created_at DESC, te.event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def scalar(conn: Any, sql: str, params: list[Any] | None = None) -> int:
    return int(conn.execute(sql, params or []).fetchone()[0])


def grouped_counts(conn: Any, sql: str) -> dict[str, int]:
    return {str(key): int(count) for key, count in conn.execute(sql).fetchall()}


def placeholders(values: set[str]) -> str:
    return ", ".join("?" for _ in values)


def create_server() -> ThreadingHTTPServer:
    host = os.environ.get("AGENTRELAY_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTRELAY_PORT", "8787"))
    db_path = os.environ.get("AGENTRELAY_DB_PATH", DEFAULT_DB_PATH)
    store = Store(db_path)
    mutation_mode = os.environ.get("AGENTRELAY_MUTATION_MODE", "legacy").strip().lower()
    if mutation_mode not in {"legacy", "closed", "v05"}:
        raise ValueError("AGENTRELAY_MUTATION_MODE must be legacy, closed, or v05")
    v05_db_path = os.environ.get("AGENTRELAY_V05_DB_PATH", "").strip()
    if mutation_mode in {"closed", "v05"} and not v05_db_path:
        v05_db_path = DEFAULT_V05_DB_PATH
    v05_store = V05Store(v05_db_path) if v05_db_path else None
    AgentRelayHandler.store = store
    AgentRelayHandler.v05_store = v05_store
    AgentRelayHandler.mutation_mode = mutation_mode
    auth_required, identities = load_auth_identities()
    AgentRelayHandler.auth_required = auth_required
    AgentRelayHandler.auth_identities = identities
    AgentRelayHandler.admin_token = os.environ.get("AGENTRELAY_ADMIN_TOKEN", "").strip()
    return ThreadingHTTPServer((host, port), AgentRelayHandler)


def load_auth_identities() -> tuple[bool, dict[str, dict[str, str]]]:
    auth_file = os.environ.get("AGENTRELAY_AUTH_FILE", "")
    if auth_file:
        with open(auth_file, "r", encoding="utf-8") as handle:
            raw_identities = json.load(handle)
        if not isinstance(raw_identities, list):
            raise ValueError("AGENTRELAY_AUTH_FILE must contain a JSON array")
        return True, parse_auth_identities(raw_identities)

    raw_tokens = os.environ.get("AGENTRELAY_TOKENS", "")
    if not raw_tokens:
        return False, {}
    identities = []
    for entry in raw_tokens.split(","):
        if not entry.strip():
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise ValueError("AGENTRELAY_TOKENS entries must be username:agent_id:token")
        username, agent_id, token = parts
        identities.append({"username": username, "agent_id": agent_id, "token": token})
    return True, parse_auth_identities(identities)


def parse_auth_identities(raw_identities: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    for identity in raw_identities:
        username = str(identity.get("username", "")).strip()
        agent_id = str(identity.get("agent_id", "")).strip()
        token = str(identity.get("token", "")).strip()
        if not username or not agent_id or not token:
            raise ValueError("each auth identity needs username, agent_id, and token")
        identities[token] = {"username": username, "agent_id": agent_id}
    return identities


def main() -> None:
    server = create_server()
    host, port = server.server_address
    print(f"AgentRelay listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
