#!/usr/bin/env node

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..");
const relayPort = 8788;
const relayBaseUrl = `http://127.0.0.1:${relayPort}/agentrelay`;
const dbPath = "/tmp/agentrelay-mcp-smoke.sqlite3";

let relayProcess;
let client;
let transport;

try {
  relayProcess = await startRelay();
  await waitForRelay();
  ({ client, transport } = await startMcpClient());

  const tools = await client.listTools();
  assert(
    tools.tools.some((tool) => tool.name === "agentrelay_create_task"),
    "agentrelay_create_task tool not found"
  );

  await callJson("agentrelay_health", {});

  const created = await callJson("agentrelay_create_task", {
    from: "zac-agent",
    to: "frank-agent",
    requesterThreadId: "zac-thread-mcp-smoke",
    subject: "MCP smoke meeting availability",
    requestText: "Zac wants a 30-minute online meeting with Frank. Please ask Frank when he is available.",
    doneCriteria: "Both Zac and Frank accept the same online meeting time.",
    completionOwnerAgentId: "zac-agent",
    humanBoundaryReason: "Frank must approve sharing availability."
  });
  const taskId = created.task.task_id;
  assert(created.task.completion_owner_agent_id === "zac-agent", "completion owner missing");

  const frankClaim = await callJson("agentrelay_claim_task", { agentId: "frank-agent" });
  assert(frankClaim.task?.task_id === taskId, "frank-agent did not claim task");

  await callJson("agentrelay_set_target_thread", {
    agentId: "frank-agent",
    taskId,
    threadId: "frank-thread-mcp-smoke"
  });

  const afterArtifact = await callJson("agentrelay_submit_artifact", {
    taskId,
    from: "frank-agent",
    to: "zac-agent",
    kind: "meeting_availability",
    text: "Frank is available Tuesday 10:00-11:00 China time."
  });
  assert(afterArtifact.task.status === "delivery_pending", "artifact should produce delivery_pending");
  assert(afterArtifact.task.pending_on_agent_id === "zac-agent", "artifact should return ownership to zac-agent");

  const zacClaim = await callJson("agentrelay_claim_task", { agentId: "zac-agent" });
  assert(zacClaim.task?.task_id === taskId, "zac-agent did not claim returned task");

  const delivered = await callJson("agentrelay_mark_delivery", {
    taskId,
    deliveredByAgentId: "zac-agent",
    threadId: "zac-thread-mcp-smoke",
    deliveryStatus: "delivered",
    pendingOnHumanId: "zac",
    nextAction: "Ask Zac whether Tuesday 10:00 works."
  });
  assert(delivered.task.status === "waiting_human", "delivery should set waiting_human");

  const closed = await callJson("agentrelay_close_task", {
    taskId,
    closedByAgentId: "zac-agent",
    terminalReason: "Requester confirmed the proposed meeting time."
  });
  assert(closed.task.status === "completed", "task did not close");

  const returned = await createClaimableReturnTask();
  console.error(`[mcp] artifact_submitted returned task claimed by zac-agent: ${returned.task_id}`);

  const events = await callJson("agentrelay_get_events", { taskId });
  const eventTypes = events.events.map((event) => event.event_type);
  for (const expected of ["task.created", "artifact.submitted", "reply.delivered", "task.completed"]) {
    assert(eventTypes.includes(expected), `missing event ${expected}`);
  }

  console.log(JSON.stringify({ ok: true, taskId, status: closed.task.status }, null, 2));
} finally {
  await transport?.close().catch(() => {});
  await client?.close().catch(() => {});
  if (relayProcess) {
    relayProcess.kill("SIGTERM");
  }
}

async function startRelay() {
  await import("node:fs/promises").then((fs) => fs.rm(dbPath, { force: true }));
  const child = spawn("python3", ["-m", "server.app"], {
    cwd: repoRoot,
    env: {
      ...process.env,
      AGENTRELAY_DB_PATH: dbPath,
      AGENTRELAY_HOST: "127.0.0.1",
      AGENTRELAY_PORT: String(relayPort)
    },
    stdio: ["ignore", "pipe", "pipe"]
  });
  child.stdout.on("data", (chunk) => process.stderr.write(`[relay] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[relay:err] ${chunk}`));
  return child;
}

async function waitForRelay() {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${relayBaseUrl}/health`);
      if (response.ok) return;
    } catch {
      // Retry until the server starts.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error("AgentRelay HTTP server did not start in time");
}

async function startMcpClient() {
  const mcpClient = new Client({
    name: "agentrelay-mcp-smoke",
    version: "0.1.0"
  });
  const mcpTransport = new StdioClientTransport({
    command: "node",
    args: ["mcp/server.mjs"],
    cwd: repoRoot,
    env: {
      ...process.env,
      AGENTRELAY_BASE_URL: relayBaseUrl
    },
    stderr: "pipe"
  });
  mcpTransport.stderr?.on("data", (chunk) => process.stderr.write(`[mcp:err] ${chunk}`));
  await mcpClient.connect(mcpTransport);
  return { client: mcpClient, transport: mcpTransport };
}

async function callJson(name, args) {
  const result = await client.callTool({ name, arguments: args });
  const first = result.content?.[0];
  if (!first || first.type !== "text") {
    throw new Error(`Tool ${name} did not return text content`);
  }
  return JSON.parse(first.text);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function createClaimableReturnTask() {
  const created = await callJson("agentrelay_create_task", {
    from: "zac-agent",
    to: "frank-agent",
    requesterThreadId: "zac-thread-artifact-submitted-mcp",
    subject: "MCP artifact_submitted claim regression",
    requestText: "Return a candidate time.",
    doneCriteria: "Zac evaluates Frank's returned artifact."
  });
  const taskId = created.task.task_id;
  const frankClaim = await callJson("agentrelay_claim_task", { agentId: "frank-agent" });
  assert(frankClaim.task?.task_id === taskId, "frank-agent did not claim artifact_submitted regression task");
  const afterArtifact = await callJson("agentrelay_submit_artifact", {
    taskId,
    from: "frank-agent",
    to: "zac-agent",
    kind: "meeting_availability",
    text: "Frank is available at 15:00.",
    nextStatus: "artifact_submitted",
    pendingOnAgentId: "zac-agent",
    nextAction: "Zac should evaluate Frank's artifact."
  });
  assert(afterArtifact.task.status === "artifact_submitted", "regression task should use artifact_submitted");
  assert(afterArtifact.task.pending_on_agent_id === "zac-agent", "regression task should be pending on zac-agent");
  const zacClaim = await callJson("agentrelay_claim_task", { agentId: "zac-agent" });
  assert(zacClaim.task?.task_id === taskId, "zac-agent did not claim artifact_submitted returned task");
  return zacClaim.task;
}
