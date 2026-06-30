#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";

const DEFAULT_BASE_URL = "http://127.0.0.1:8787/agentrelay";
const baseUrl = normalizeBaseUrl(process.env.AGENTRELAY_BASE_URL || DEFAULT_BASE_URL);

const server = new McpServer({
  name: "agentrelay-mcp",
  version: "0.1.0"
});

registerTools(server);

const transport = new StdioServerTransport();
await server.connect(transport);

function registerTools(mcpServer) {
  mcpServer.registerTool(
    "agentrelay_health",
    {
      title: "AgentRelay health",
      description: "Check whether the AgentRelay HTTP server is reachable.",
      inputSchema: {}
    },
    async () => jsonResult(await relayGet("/health"))
  );

  mcpServer.registerTool(
    "agentrelay_list_agents",
    {
      title: "List AgentRelay agents",
      description: "List known AgentRelay agents.",
      inputSchema: {}
    },
    async () => jsonResult(await relayGet("/agents"))
  );

  mcpServer.registerTool(
    "agentrelay_get_agent_card",
    {
      title: "Get AgentRelay agent card",
      description: "Fetch an A2A-shaped agent card from AgentRelay.",
      inputSchema: {
        agentId: z.string().min(1).describe("Agent id, for example frank-agent")
      }
    },
    async ({ agentId }) => jsonResult(await relayGet(`/agents/${encodeURIComponent(agentId)}/card`))
  );

  mcpServer.registerTool(
    "agentrelay_create_task",
    {
      title: "Create AgentRelay task",
      description: "Create an AgentRelay protocol v0.2 task and record requester-side completion ownership.",
      inputSchema: {
        requester_agent_id: z.string().min(1).optional().describe("Protocol v0.2 requester agent id"),
        target_agent_id: z.string().min(1).optional().describe("Protocol v0.2 target agent id"),
        from: z.string().min(1).optional().describe("Legacy requester agent id alias"),
        to: z.string().min(1).optional().describe("Legacy target agent id alias"),
        requestText: z.string().min(1).describe("Human-readable request to send"),
        requesterThreadId: z.string().min(1).describe("Codex App thread id to deliver replies back to"),
        intent: z.string().optional().describe("Protocol v0.2 message intent, for example request_availability"),
        subject: z.string().optional(),
        contextId: z.string().optional(),
        doneCriteria: z.string().optional(),
        completionOwnerAgentId: z.string().optional(),
        pendingOnAgentId: z.string().optional(),
        humanBoundaryReason: z.string().optional(),
        ttl: z.number().int().positive().optional(),
        maxTurns: z.number().int().positive().optional()
      }
    },
    async (args) => {
      const requesterAgentId = args.requester_agent_id || args.from;
      const targetAgentId = args.target_agent_id || args.to;
      if (!requesterAgentId || !targetAgentId) {
        throw new Error("agentrelay_create_task requires requester_agent_id/target_agent_id or legacy from/to");
      }
      const payload = {
        protocol_version: "agent-collab-v0.2",
        contextId: args.contextId,
        requester_agent_id: requesterAgentId,
        target_agent_id: targetAgentId,
        requesterThreadId: args.requesterThreadId,
        subject: args.subject || "AgentRelay task",
        done_criteria: args.doneCriteria || "",
        completion_owner_agent_id: args.completionOwnerAgentId || requesterAgentId,
        pending_on_agent_id: args.pendingOnAgentId || targetAgentId,
        ttl: args.ttl,
        maxTurns: args.maxTurns,
        message: {
          actor_agent_id: requesterAgentId,
          intent: args.intent || "request",
          parts: [{ kind: "text", text: args.requestText }]
        },
        humanBoundary: args.humanBoundaryReason
          ? { requiresHuman: true, reason: args.humanBoundaryReason }
          : undefined
      };
      return jsonResult(await relayPost("/tasks", compact(payload)));
    }
  );

  mcpServer.registerTool(
    "agentrelay_get_task",
    {
      title: "Get AgentRelay task",
      description: "Fetch a task with messages and artifacts.",
      inputSchema: {
        taskId: z.string().min(1)
      }
    },
    async ({ taskId }) => jsonResult(await relayGet(`/tasks/${encodeURIComponent(taskId)}`))
  );

  mcpServer.registerTool(
    "agentrelay_get_events",
    {
      title: "Get AgentRelay task events",
      description: "Fetch audit events for a task.",
      inputSchema: {
        taskId: z.string().min(1)
      }
    },
    async ({ taskId }) => jsonResult(await relayGet(`/tasks/${encodeURIComponent(taskId)}/events`))
  );

  mcpServer.registerTool(
    "agentrelay_claim_task",
    {
      title: "Claim AgentRelay task",
      description: "Claim the next task pending on the provided agent id.",
      inputSchema: {
        agentId: z.string().min(1)
      }
    },
    async ({ agentId }) => jsonResult(await relayGet(`/workers/${encodeURIComponent(agentId)}/claim`))
  );

  mcpServer.registerTool(
    "agentrelay_set_target_thread",
    {
      title: "Record target thread",
      description: "Record or reuse the target Codex App thread for a claimed task.",
      inputSchema: {
        agentId: z.string().min(1),
        taskId: z.string().min(1),
        threadId: z.string().min(1)
      }
    },
    async ({ agentId, taskId, threadId }) =>
      jsonResult(
        await relayPost(
          `/workers/${encodeURIComponent(agentId)}/tasks/${encodeURIComponent(taskId)}/thread`,
          { threadId }
        )
      )
  );

  mcpServer.registerTool(
    "agentrelay_submit_artifact",
    {
      title: "Submit AgentRelay artifact",
      description: "Submit a protocol v0.2 artifact. By default, this transfers ownership back to the completion owner instead of completing the task.",
      inputSchema: {
        taskId: z.string().min(1),
        actor_agent_id: z.string().min(1).optional().describe("Protocol v0.2 agent that produced the artifact"),
        target_agent_id: z.string().min(1).optional().describe("Optional target/receiving agent id"),
        from: z.string().min(1).optional().describe("Legacy actor agent id alias"),
        to: z.string().min(1).optional().describe("Legacy target agent id alias"),
        intent: z.string().optional().describe("Protocol v0.2 artifact intent, for example availability_response"),
        kind: z.string().optional(),
        text: z.string().min(1),
        pendingOnAgentId: z.string().optional(),
        pendingOnHumanId: z.string().optional(),
        nextStatus: z.string().optional(),
        nextAction: z.string().optional()
      }
    },
    async (args) => {
      const actorAgentId = args.actor_agent_id || args.from;
      if (!actorAgentId) {
        throw new Error("agentrelay_submit_artifact requires actor_agent_id or legacy from");
      }
      const payload = {
        protocol_version: "agent-collab-v0.2",
        actor_agent_id: actorAgentId,
        target_agent_id: args.target_agent_id || args.to,
        pending_on_agent_id: args.pendingOnAgentId,
        pendingOnHumanId: args.pendingOnHumanId,
        nextStatus: args.nextStatus,
        nextAction: args.nextAction,
        artifact: {
          intent: args.intent || "work_result",
          kind: args.kind || "text",
          parts: [{ kind: "text", text: args.text }]
        }
      };
      return jsonResult(await relayPost(`/tasks/${encodeURIComponent(args.taskId)}/artifacts`, compact(payload)));
    }
  );

  mcpServer.registerTool(
    "agentrelay_mark_delivery",
    {
      title: "Mark origin-thread delivery",
      description: "Record successful or failed delivery to the requester thread.",
      inputSchema: {
        taskId: z.string().min(1),
        deliveredByAgentId: z.string().min(1),
        threadId: z.string().min(1),
        deliveryStatus: z.enum(["delivered", "failed"]).default("delivered"),
        pendingOnHumanId: z.string().optional(),
        nextAction: z.string().optional(),
        nextStatus: z.string().optional(),
        error: z.string().optional()
      }
    },
    async (args) =>
      jsonResult(
        await relayPost(
          `/tasks/${encodeURIComponent(args.taskId)}/deliveries`,
          compact({
            deliveredByAgentId: args.deliveredByAgentId,
            threadId: args.threadId,
            deliveryStatus: args.deliveryStatus,
            pendingOnHumanId: args.pendingOnHumanId,
            nextAction: args.nextAction,
            nextStatus: args.nextStatus,
            error: args.error
          })
        )
      )
  );

  mcpServer.registerTool(
    "agentrelay_update_status",
    {
      title: "Update AgentRelay task status",
      description: "Update relay transport status and pending ownership fields.",
      inputSchema: {
        taskId: z.string().min(1),
        status: z.string().min(1),
        pendingOnAgentId: z.string().optional(),
        pendingOnHumanId: z.string().optional(),
        nextAction: z.string().optional(),
        terminalReason: z.string().optional()
      }
    },
    async (args) =>
      jsonResult(
        await relayPost(
          `/tasks/${encodeURIComponent(args.taskId)}/status`,
          compact({
            status: args.status,
            pendingOnAgentId: args.pendingOnAgentId,
            pendingOnHumanId: args.pendingOnHumanId,
            nextAction: args.nextAction,
            terminalReason: args.terminalReason
          })
        )
      )
  );

  mcpServer.registerTool(
    "agentrelay_close_task",
    {
      title: "Close AgentRelay task",
      description: "Close a task. Only completion_owner_agent_id should call this.",
      inputSchema: {
        taskId: z.string().min(1),
        closedByAgentId: z.string().min(1),
        terminalReason: z.string().min(1)
      }
    },
    async ({ taskId, closedByAgentId, terminalReason }) =>
      jsonResult(await relayPost(`/tasks/${encodeURIComponent(taskId)}/close`, { closedByAgentId, terminalReason }))
  );
}

async function relayGet(path) {
  return relayRequest("GET", path);
}

async function relayPost(path, payload) {
  return relayRequest("POST", path, payload);
}

async function relayRequest(method, path, payload) {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload)
  });
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new Error(`AgentRelay returned non-JSON response (${response.status}): ${text}`);
  }
  if (!response.ok) {
    throw new Error(`AgentRelay ${method} ${path} failed (${response.status}): ${JSON.stringify(data)}`);
  }
  return data;
}

function jsonResult(data) {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(data, null, 2)
      }
    ]
  };
}

function normalizeBaseUrl(value) {
  return value.replace(/\/+$/, "");
}

function compact(value) {
  return Object.fromEntries(
    Object.entries(value).filter(([, entry]) => entry !== undefined && entry !== null)
  );
}
