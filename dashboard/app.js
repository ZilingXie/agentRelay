const state = {
  token: sessionStorage.getItem("agentrelay_admin_token") || "",
  agents: [],
  tasks: [],
  summary: null,
  selectedTaskId: null,
  refreshTimer: null,
};

const $ = (id) => document.getElementById(id);

const els = {
  authForm: $("authForm"),
  adminToken: $("adminToken"),
  refreshButton: $("refreshButton"),
  metricAgents: $("metricAgents"),
  metricActiveTasks: $("metricActiveTasks"),
  metricTotalTasks: $("metricTotalTasks"),
  metricUnackedEvents: $("metricUnackedEvents"),
  agentFilter: $("agentFilter"),
  statusFilter: $("statusFilter"),
  activeFilter: $("activeFilter"),
  limitFilter: $("limitFilter"),
  tasksBody: $("tasksBody"),
  taskCount: $("taskCount"),
  agentsBody: $("agentsBody"),
  agentCount: $("agentCount"),
  activityList: $("activityList"),
  lastUpdated: $("lastUpdated"),
  selectedTaskId: $("selectedTaskId"),
  taskDetail: $("taskDetail"),
  toast: $("toast"),
};

els.adminToken.value = state.token;

els.authForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = els.adminToken.value.trim();
  sessionStorage.setItem("agentrelay_admin_token", state.token);
  loadAll();
});

els.refreshButton.addEventListener("click", () => loadAll());
els.agentFilter.addEventListener("change", () => loadTasks());
els.statusFilter.addEventListener("change", () => loadTasks());
els.activeFilter.addEventListener("change", () => loadTasks());
els.limitFilter.addEventListener("change", () => loadTasks());

async function api(path) {
  if (!state.token) {
    throw new Error("Enter the admin token first.");
  }
  const response = await fetch(path, {
    headers: {
      Authorization: `Bearer ${state.token}`,
      "X-AgentRelay-Admin-Token": state.token,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || payload?.error?.message || `HTTP ${response.status}`);
  }
  return payload;
}

async function loadAll() {
  try {
    const [summary, agents] = await Promise.all([
      api("/agentrelay/admin/api/summary"),
      api("/agentrelay/admin/api/agents"),
    ]);
    state.summary = summary;
    state.agents = agents.agents || [];
    renderSummary();
    renderAgents();
    populateFilters();
    await loadTasks();
    els.lastUpdated.textContent = `Updated ${formatTime(Date.now() / 1000)}`;
    showToast("Dashboard refreshed.");
  } catch (error) {
    showToast(error.message);
  }
}

async function loadTasks() {
  try {
    const params = new URLSearchParams();
    if (els.agentFilter.value) params.set("agent_id", els.agentFilter.value);
    if (els.statusFilter.value) params.set("status", els.statusFilter.value);
    if (els.activeFilter.value) params.set("active", els.activeFilter.value);
    params.set("limit", els.limitFilter.value || "100");
    const payload = await api(`/agentrelay/admin/api/tasks?${params}`);
    state.tasks = payload.tasks || [];
    renderTasks();
    if (state.selectedTaskId && state.tasks.some((task) => task.task_id === state.selectedTaskId)) {
      await loadTaskDetail(state.selectedTaskId);
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function loadTaskDetail(taskId) {
  try {
    state.selectedTaskId = taskId;
    const payload = await api(`/agentrelay/admin/api/tasks/${encodeURIComponent(taskId)}`);
    renderTaskDetail(payload);
    renderTasks();
  } catch (error) {
    showToast(error.message);
  }
}

function renderSummary() {
  const summary = state.summary || {};
  els.metricAgents.textContent = summary.agents ?? "-";
  els.metricActiveTasks.textContent = summary.tasks?.active ?? "-";
  els.metricTotalTasks.textContent = summary.tasks?.total ?? "-";
  els.metricUnackedEvents.textContent = summary.agent_events?.unacked ?? "-";

  els.activityList.innerHTML = "";
  for (const event of summary.recent_task_events || []) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="event-line">
        <span class="event-type">${escapeHtml(event.event_type)}</span>
        <span class="event-meta">${formatTime(event.created_at)}</span>
      </div>
      <div>${escapeHtml(event.subject || event.task_id)}</div>
      <div class="event-meta mono">${escapeHtml(event.task_id)}</div>
    `;
    li.addEventListener("click", () => loadTaskDetail(event.task_id));
    els.activityList.appendChild(li);
  }
}

function renderAgents() {
  els.agentCount.textContent = `${state.agents.length} agents`;
  els.agentsBody.innerHTML = "";
  for (const agent of state.agents) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(agent.agent_id)}</strong><div class="muted">${escapeHtml(agent.name || "")}</div></td>
      <td>${escapeHtml(agent.owner || "")}</td>
      <td>${agent.pending_task_count ?? 0}</td>
      <td>${agent.active_task_count ?? 0}</td>
      <td>${agent.unacked_event_count ?? 0}</td>
    `;
    tr.addEventListener("click", () => {
      els.agentFilter.value = agent.agent_id;
      loadTasks();
    });
    els.agentsBody.appendChild(tr);
  }
}

function renderTasks() {
  els.taskCount.textContent = `${state.tasks.length} shown`;
  els.tasksBody.innerHTML = "";
  for (const task of state.tasks) {
    const tr = document.createElement("tr");
    if (task.task_id === state.selectedTaskId) tr.classList.add("selected");
    tr.innerHTML = `
      <td class="subject">${escapeHtml(task.subject || "(no subject)")}<div class="muted mono">${escapeHtml(task.task_id)}</div></td>
      <td>${badge(task.status)}</td>
      <td class="mono">${escapeHtml(task.requester_agent_id || "")}</td>
      <td class="mono">${escapeHtml(task.target_agent_id || "")}</td>
      <td class="mono">${escapeHtml(task.pending_on_agent_id || "none")}</td>
      <td>${formatTime(task.updated_at)}</td>
    `;
    tr.addEventListener("click", () => loadTaskDetail(task.task_id));
    els.tasksBody.appendChild(tr);
  }
}

function renderTaskDetail(payload) {
  const task = payload.task;
  els.selectedTaskId.textContent = task.task_id;
  const timeline = payload.timeline?.entries || [];
  const agentEvents = payload.agent_events || [];
  els.taskDetail.className = "detail";
  els.taskDetail.innerHTML = `
    <dl class="kv">
      <dt>Subject</dt><dd>${escapeHtml(task.subject || "")}</dd>
      <dt>Status</dt><dd>${badge(task.status)}</dd>
      <dt>Requester</dt><dd class="mono">${escapeHtml(task.requester_agent_id || "")}</dd>
      <dt>Target</dt><dd class="mono">${escapeHtml(task.target_agent_id || "")}</dd>
      <dt>Completion Owner</dt><dd class="mono">${escapeHtml(task.completion_owner_agent_id || "")}</dd>
      <dt>Pending</dt><dd class="mono">${escapeHtml(task.pending_on_agent_id || "none")}</dd>
      <dt>Next Action</dt><dd>${escapeHtml(task.next_action || "none")}</dd>
      <dt>Turns</dt><dd>${task.turn_count ?? 0} / ${task.max_turns ?? "-"}</dd>
    </dl>
    <section>
      <h2>Timeline</h2>
      <ol class="timeline">${timeline.map(renderTimelineItem).join("") || "<li>No timeline entries.</li>"}</ol>
    </section>
    <section>
      <h2>Agent Events</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Agent</th><th>Type</th><th>State</th><th>Acked</th></tr></thead>
          <tbody>${agentEvents.map(renderAgentEventRow).join("") || "<tr><td colspan=\"4\">No agent events.</td></tr>"}</tbody>
        </table>
      </div>
    </section>
    <section>
      <h2>Messages / Artifacts</h2>
      <pre>${escapeHtml(JSON.stringify({ messages: task.messages || [], artifacts: task.artifacts || [] }, null, 2))}</pre>
    </section>
  `;
}

function renderTimelineItem(entry) {
  return `
    <li>
      <div class="event-line">
        <span class="event-type">${escapeHtml(entry.event_type)}</span>
        <span class="event-meta">${formatTime(entry.created_at)}</span>
      </div>
      <div class="event-meta">${escapeHtml(entry.summary || entry.category || "")}</div>
    </li>
  `;
}

function renderAgentEventRow(event) {
  return `
    <tr>
      <td class="mono">${escapeHtml(event.agent_id || "")}</td>
      <td>${escapeHtml(event.event_type || "")}</td>
      <td>${badge(event.delivery_state || "")}</td>
      <td>${event.acked_at ? formatTime(event.acked_at) : "no"}</td>
    </tr>
  `;
}

function populateFilters() {
  const currentAgent = els.agentFilter.value;
  els.agentFilter.innerHTML = `<option value="">All agents</option>${state.agents
    .map((agent) => `<option value="${escapeAttr(agent.agent_id)}">${escapeHtml(agent.agent_id)}</option>`)
    .join("")}`;
  els.agentFilter.value = currentAgent;

  const currentStatus = els.statusFilter.value;
  const statuses = Object.keys(state.summary?.tasks?.by_status || {});
  els.statusFilter.innerHTML = `<option value="">All statuses</option>${statuses
    .map((status) => `<option value="${escapeAttr(status)}">${escapeHtml(status)}</option>`)
    .join("")}`;
  els.statusFilter.value = currentStatus;
}

function badge(value) {
  const text = escapeHtml(value || "none");
  const klass = String(value || "none").replace(/[^a-zA-Z0-9_-]/g, "_");
  return `<span class="badge ${klass}">${text}</span>`;
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(Number(value) * 1000);
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

let toastTimer = null;
function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 2800);
}

state.refreshTimer = setInterval(() => {
  if (state.token) loadAll();
}, 10000);

if (state.token) {
  loadAll();
}
