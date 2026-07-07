#!/usr/bin/env node

import { readFileSync, readdirSync } from "node:fs";
import Ajv2020 from "ajv/dist/2020.js";

const schemaDir = new URL("../schemas/", import.meta.url);
const exampleDir = new URL("../examples/protocol-v03/", import.meta.url);
const ajv = new Ajv2020({ allErrors: true, strict: false });
const schemas = new Map();

for (const fileName of readdirSync(schemaDir)) {
  if (!fileName.endsWith(".schema.json")) continue;
  const schema = JSON.parse(readFileSync(new URL(fileName, schemaDir), "utf8"));
  schemas.set(fileName, schema);
  ajv.addSchema(schema);
}

validate("task-create.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-create-1",
  task_type: "meeting.schedule",
  subject: "Find a meeting time",
  requester_agent_id: "agent-a",
  target_agent_id: "agent-b",
  done_criteria: {
    type: "meeting_time_agreed",
    required_outputs: ["start_time", "end_time", "timezone"]
  },
  completion_owner_agent_id: "agent-a",
  pending_on_agent_id: "agent-b",
  next_action: "Agent B should return availability.",
  max_turns: 6,
  message: {
    actor_agent_id: "agent-a",
    intent: "request_availability",
    parts: [{ kind: "text", text: "Can you meet next Monday?" }]
  },
  thread_binding: {
    agent_id: "agent-a",
    thread_role: "requester_origin",
    thread_id: "thread-a-1"
  }
});

rejects("task-create.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-create-missing-next-action",
  task_type: "meeting.schedule",
  subject: "Find a meeting time",
  requester_agent_id: "agent-a",
  target_agent_id: "agent-b",
  done_criteria: "Both owners agree on one time.",
  completion_owner_agent_id: "agent-a",
  pending_on_agent_id: "agent-b",
  message: {
    actor_agent_id: "agent-a",
    intent: "request_availability",
    parts: [{ kind: "text", text: "Can you meet next Monday?" }]
  }
});

validate("artifact-submit.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-artifact-1",
  actor_agent_id: "agent-b",
  intent: "provide_availability",
  artifact: {
    kind: "availability_response",
    summary: "Agent B owner can meet Monday 10:30-11:00 Asia/Shanghai.",
    parts: [
      {
        kind: "structured_availability",
        slots: [
          {
            start_time: "2026-07-06T10:30:00+08:00",
            end_time: "2026-07-06T11:00:00+08:00",
            timezone: "Asia/Shanghai"
          }
        ]
      }
    ],
    source_refs: [
      {
        type: "owner_confirmation",
        label: "Owner confirmation",
        summary: "Owner approved sharing this slot.",
        visibility: "redacted"
      }
    ]
  },
  next_status: "delivery_pending",
  pending_on_agent_id: "agent-a",
  next_action: "Agent A should evaluate the artifact against done_criteria."
});

validate("task-amend.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-amend-1",
  actor_agent_id: "agent-a",
  expected_goal_version: 1,
  new_done_criteria: {
    type: "clarified_review_goal",
    description: "Agent B must return the content to review, not only a file path."
  },
  new_max_turns: 4,
  previous_goal_disposition: "clarified",
  human_authority: {
    owner_id: "owner-a",
    via_agent_id: "agent-a",
    approval_ref: "local-feedback-1",
    summary: "Owner A clarified that the requested review content must be included.",
    visibility: "redacted",
    source_refs: [
      {
        type: "owner_confirmation",
        label: "Owner A local clarification",
        visibility: "redacted"
      }
    ]
  },
  reason: "Human clarified the acceptance criteria after reviewing the first artifact.",
  next_action: "Agent B should respond to the amended goal."
});

rejects("task-amend.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-amend-missing-human-authority",
  actor_agent_id: "agent-a",
  expected_goal_version: 1,
  new_done_criteria: "Clarified goal",
  previous_goal_disposition: "clarified",
  reason: "Missing human authority should fail."
});

validate("task-close.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-close-1",
  closed_by_agent_id: "agent-a",
  completion_authority: {
    type: "human",
    owner_id: "owner-a",
    via_agent_id: "agent-a",
    approval_ref: "local-confirmation-1",
    summary: "Owner A accepted the proposed meeting slot.",
    visibility: "redacted",
    source_refs: [
      {
        type: "owner_confirmation",
        label: "Owner A local confirmation",
        visibility: "redacted"
      }
    ]
  },
  terminal_reason: "Both owners accepted the same meeting time.",
  final_artifact: {
    kind: "meeting_confirmation",
    parts: [{ kind: "meeting_time", start_time: "2026-07-06T10:30:00+08:00" }]
  }
});

rejects("task-close.schema.json", {
  protocol_version: "agent-collab-v0.3",
  idempotency_key: "schema-close-missing-human-authority",
  closed_by_agent_id: "agent-a",
  completion_authority: {
    type: "human"
  },
  terminal_reason: "Incomplete human authority should fail."
});

validate("task-event.schema.json", {
  event_id: "evt_123",
  task_id: "task_123",
  event_type: "task.created",
  created_at: 1783072859,
  cursor: "1783072859:evt_123",
  payload: {
    protocol_version: "agent-collab-v0.3",
    idempotency_key: "schema-create-1",
    actor_agent_id: "agent-a",
    intent: "request_availability",
    requester_agent_id: "agent-a",
    target_agent_id: "agent-b",
    pending_on_agent_id: "agent-b",
    next_action: "Agent B should return availability."
  }
});

validate("agent-event.schema.json", {
  event_id: "aevt_123",
  event_type: "task.pending",
  task_id: "task_123",
  agent_id: "agent-b",
  delivery_state: "pending",
  delivery_attempts: 0,
  payload_ref: {
    method: "GET",
    href: "/agentrelay/tasks/task_123"
  },
  payload: {
    task_id: "task_123",
    reason: "task.created"
  }
});

validate("task-timeline.schema.json", {
  task_id: "task_123",
  entries: [
    {
      timeline_id: "evt_123",
      sequence: 1,
      task_id: "task_123",
      event_type: "task.created",
      category: "lifecycle",
      title: "Task created",
      summary: "agent-a created a request_availability task for agent-b.",
      actor_agent_id: "agent-a",
      intent: "request_availability",
      artifact_id: null,
      status: null,
      pending_on_agent_id: "agent-b",
      source_refs: [],
      completion_authority: null,
      delivery: null,
      created_at: 1783072859,
      payload: {
        protocol_version: "agent-collab-v0.3",
        actor_agent_id: "agent-a",
        intent: "request_availability"
      }
    }
  ],
  summary: {
    total_entries: 1,
    categories: { lifecycle: 1 },
    last_event_type: "task.created",
    last_created_at: 1783072859
  }
});

validate("response-envelope.schema.json", {
  ok: true,
  data: {
    task: {
      task_id: "task_123"
    }
  },
  next_action: {
    type: "claim_task",
    agent_id: "agent-b"
  },
  meta: {
    envelope: "v0.3"
  }
});

validate("response-envelope.schema.json", {
  ok: false,
  error: {
    type: "validation_error",
    code: "VALIDATION_ERROR",
    message: "missing required field: task_type",
    hint: "Check the AgentRelay protocol v0.3 schema for this request.",
    detail: {
      field: "task_type"
    }
  }
});

validate("agent-card.schema.json", {
  protocolVersion: "agentrelay-agent-card-v0.3",
  a2aProtocolVersion: "0.3",
  name: "Agent B",
  description: "Agent B can coordinate meetings.",
  url: "https://server.stellarix.space/agentrelay/api/a2a/agent-b",
  provider: {
    organization: "Agent B Owner"
  },
  capabilities: {
    streaming: false,
    pushNotifications: true,
    stateTransitionHistory: true
  },
  skills: [
    {
      id: "meeting-coordination",
      name: "Meeting coordination",
      description: "Return availability and confirmations."
    }
  ],
  agentRelay: {
    agent_id: "agent-b",
    owner: "owner-b",
    accepted_task_types: ["meeting.schedule"],
    scopes: ["agent:agent-b:tasks:claim"],
    human_approval_policy: {
      private_owner_agent_conversation: "not_relayed_by_default"
    },
    endpoints: {
      claim: "/agentrelay/api/workers/agent-b/claim"
    }
  }
});

const exampleSchemaMap = {
  "meeting-task-create.json": "task-create.schema.json",
  "meeting-artifact-submit.json": "artifact-submit.schema.json",
  "meeting-task-amend.json": "task-amend.schema.json",
  "meeting-task-close.json": "task-close.schema.json",
  "dashboard-task-create.json": "task-create.schema.json",
  "dashboard-artifact-submit.json": "artifact-submit.schema.json",
  "unavailable-artifact-submit.json": "artifact-submit.schema.json"
};

for (const [exampleFileName, schemaFileName] of Object.entries(exampleSchemaMap)) {
  const example = JSON.parse(readFileSync(new URL(exampleFileName, exampleDir), "utf8"));
  validate(schemaFileName, example);
}

console.log(
  JSON.stringify(
    {
      ok: true,
      schemas: [...schemas.keys()].sort(),
      examples: Object.keys(exampleSchemaMap).sort()
    },
    null,
    2
  )
);

function validate(schemaFileName, payload) {
  const schema = schemas.get(schemaFileName);
  if (!schema) {
    throw new Error(`missing schema fixture: ${schemaFileName}`);
  }
  const validator = ajv.getSchema(schema.$id);
  if (!validator) {
    throw new Error(`schema not registered: ${schema.$id}`);
  }
  if (!validator(payload)) {
    throw new Error(`${schemaFileName} rejected valid payload:\n${formatErrors(validator.errors)}`);
  }
}

function rejects(schemaFileName, payload) {
  const schema = schemas.get(schemaFileName);
  if (!schema) {
    throw new Error(`missing schema fixture: ${schemaFileName}`);
  }
  const validator = ajv.getSchema(schema.$id);
  if (!validator) {
    throw new Error(`schema not registered: ${schema.$id}`);
  }
  if (validator(payload)) {
    throw new Error(`${schemaFileName} accepted invalid payload`);
  }
}

function formatErrors(errors) {
  return (errors || [])
    .map((error) => `${error.instancePath || "/"} ${error.message}`)
    .join("\n");
}
