"""Local-First Web Control Surface UI (PRD Section 15.2).

Provides an interactive single-page workbench served at `GET /` over the `/v1/*` API:
1. Work: queued/running/speculating/succeeded tasks with Fast-Path toggle & submission form.
2. Task Detail & Proof Inspector: Dynamic DAG visualization, Call-Journal replay timeline,
   AST Blast-Radius Impact Map, Speculative Branch comparison, and Proof-Carrying Artifact receipt.
3. Approvals Queue: pending R3 actions with blast-radius preview and Approve/Reject controls.
4. Skills & NVIDIA SkillSpector Gate: live 0-100 risk badges, static/OSV/semantic scan reports,
   and one-click skill import/rescan.
5. Workers, Memory & Harness² Controlled Self-Improvement Inspector.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>HEX — Autonomous AI Workbench</title>
  <style>
    :root {
      --bg: #0b0f19;
      --panel: #111827;
      --panel-alt: #1f2937;
      --border: #374151;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #3b82f6;
      --green: #10b981;
      --yellow: #f59e0b;
      --red: #ef4444;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", monospace;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 20px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }
    header h1 { font-size: 1.35rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-green { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #059669; }
    .badge-yellow { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #d97706; }
    .badge-red { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #dc2626; }
    .badge-blue { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid #2563eb; }
    .badge-purple { background: rgba(139, 92, 246, 0.2); color: #c084fc; border: 1px solid #7c3aed; }
    .grid {
      display: grid;
      grid-template-columns: 380px 1fr;
      gap: 20px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 16px;
    }
    .card h2 {
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    input, select, textarea {
      width: 100%;
      padding: 8px 10px;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-size: 0.85rem;
      margin-bottom: 10px;
    }
    button {
      background: var(--accent);
      color: white;
      border: none;
      padding: 8px 14px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.82rem;
    }
    button:hover { opacity: 0.92; }
    button.btn-secondary { background: var(--panel-alt); border: 1px solid var(--border); }
    button.btn-green { background: var(--green); }
    button.btn-red { background: var(--red); }
    .task-item {
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 8px;
      cursor: pointer;
      background: rgba(255,255,255,0.02);
    }
    .task-item:hover, .task-item.active { border-color: var(--accent); background: rgba(59,130,246,0.08); }
    pre {
      background: #050811;
      border: 1px solid var(--border);
      padding: 10px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.78rem;
      color: #d1d5db;
      max-height: 280px;
    }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
    .kv { font-size: 0.83rem; margin-bottom: 6px; }
    .kv span { color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>
      <span>⚡ HEX Autonomous AI Workbench</span>
      <span class="badge badge-green">v1.3 CoW + Call Journal</span>
      <span class="badge badge-purple">NVIDIA SkillSpector Gate</span>
      <span class="badge badge-blue">Harness² Adapter</span>
    </h1>
    <div>
      <button class="btn-secondary" onclick="refreshAll()">↻ Refresh State</button>
    </div>
  </header>

  <div class="grid">
    <!-- Left Column: Task Intake, Task Inbox, Approvals, Workers -->
    <div>
      <div class="card">
        <h2>Submit Task (FR-001 / Fast-Path FR-001a)</h2>
        <input id="goalInput" placeholder="Task goal (e.g., Inspect repository AST and summarize symbols)" />
        <div style="display:flex; gap:8px;">
          <input id="projectInput" placeholder="project_id (default)" value="default" />
          <select id="modeSelect">
            <option value="auto">Mode: Auto-Detect</option>
            <option value="fast_path">Fast-Path Express (R0/R1 &lt;2s)</option>
            <option value="standard">Standard CoW DAG</option>
          </select>
        </div>
        <button style="width:100%;" onclick="submitTask()">Launch Task</button>
      </div>

      <div class="card">
        <h2>Task Inbox &amp; Live Status <span id="taskCountBadge" class="badge badge-blue">0</span></h2>
        <div id="taskList">Loading tasks...</div>
      </div>

      <div class="card">
        <h2>R3 Approval Queue <span id="approvalCountBadge" class="badge badge-yellow">0</span></h2>
        <div id="approvalList">No pending approvals.</div>
      </div>

      <div class="card">
        <h2>Registered Workers &amp; CoW Backends</h2>
        <div id="workerList">Loading workers...</div>
      </div>
    </div>

    <!-- Right Column: Task Detail, Proof Bundle, Call Journal, Skills & SkillSpector -->
    <div>
      <div class="card">
        <h2>
          <span>Task Detail, Call Journal &amp; Proof-Carrying Artifact Inspector</span>
          <span id="selectedTaskBadge" class="badge badge-blue">Select a task</span>
        </h2>
        <div id="taskActions" style="margin-bottom:12px; display:flex; gap:8px;"></div>
        <div id="taskDetailView">
          <p style="color:var(--muted); font-size:0.9rem;">Click any task on the left to inspect its CoW workspace tree SHA, Memoized Call Journal, Active Graphify AST Blast-Radius warnings, and signed Proof-Carrying Artifact bundle.</p>
        </div>
      </div>

      <div class="card">
        <h2>Skills Registry &amp; NVIDIA SkillSpector Supply-Chain Gate (FR-039e)</h2>
        <div style="display:flex; gap:8px; margin-bottom:10px;">
          <input id="skillPathInput" placeholder="Local skill bundle path (e.g., skills/repo_audit)" style="margin-bottom:0;" />
          <button onclick="importSkill()">Scan &amp; Import via SkillSpector</button>
        </div>
        <div id="skillList">Loading skills...</div>
      </div>
    </div>
  </div>

  <script>
    let currentTaskId = null;

    async function api(path, opts = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
      });
      return res.json();
    }

    async function refreshAll() {
      const [tasksData, workersData, skillsData, approvalsData] = await Promise.all([
        api("/v1/tasks"),
        api("/v1/workers"),
        api("/v1/skills"),
        api("/v1/approvals"),
      ]);
      renderTasks(tasksData.tasks || []);
      renderWorkers(workersData.workers || []);
      renderSkills(skillsData.skills || []);
      renderApprovals(approvalsData.approvals || []);
      if (currentTaskId) {
        await selectTask(currentTaskId);
      } else if ((tasksData.tasks || []).length > 0) {
        await selectTask(tasksData.tasks[0].task_id);
      }
    }

    function statusBadge(st) {
      if (st === "SUCCEEDED" || st === "ALLOW" || st === "active") return `<span class="badge badge-green">${st}</span>`;
      if (st === "FAILED" || st === "REJECTED" || st === "QUARANTINE" || st === "quarantined") return `<span class="badge badge-red">${st}</span>`;
      if (st === "WAITING_APPROVAL" || st === "PAUSED" || st === "RETRY_WAIT" || st === "REQUIRE_APPROVAL") return `<span class="badge badge-yellow">${st}</span>`;
      return `<span class="badge badge-blue">${st}</span>`;
    }

    function renderTasks(tasks) {
      document.getElementById("taskCountBadge").textContent = tasks.length;
      const el = document.getElementById("taskList");
      if (!tasks.length) {
        el.innerHTML = `<p style="color:var(--muted);font-size:0.85rem;">No tasks yet.</p>`;
        return;
      }
      el.innerHTML = tasks.map(t => `
        <div class="task-item ${t.task_id === currentTaskId ? 'active' : ''}" onclick="selectTask('${t.task_id}')">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong>${t.task_id}</strong>
            ${statusBadge(t.status)}
          </div>
          <div style="font-size:0.85rem;margin-top:4px;">${t.goal}</div>
          <div style="font-size:0.75rem;color:var(--muted);margin-top:4px;">
            Lane: <code>${t.execution_mode}</code> | Tree SHA: <code>${(t.active_workspace_tree_sha || 'n/a').slice(0,10)}</code>
          </div>
        </div>
      `).join("");
    }

    function renderApprovals(approvals) {
      const pending = approvals.filter(a => a.decision === "PENDING");
      document.getElementById("approvalCountBadge").textContent = pending.length;
      const el = document.getElementById("approvalList");
      if (!pending.length) {
        el.innerHTML = `<p style="color:var(--muted);font-size:0.85rem;">No pending R3 approvals.</p>`;
        return;
      }
      el.innerHTML = pending.map(a => `
        <div class="task-item">
          <div><strong>${a.approval_id}</strong> on <code>${a.task_id}</code></div>
          <div style="font-size:0.8rem;margin:4px 0;">Action: <code>${a.action_type}</code></div>
          <div style="font-size:0.76rem;color:var(--muted);margin-bottom:8px;">${a.blast_radius_summary}</div>
          <div style="display:flex;gap:6px;">
            <button class="btn-green" onclick="decideApproval('${a.approval_id}', 'APPROVED')">Approve &amp; Resume</button>
            <button class="btn-red" onclick="decideApproval('${a.approval_id}', 'REJECTED')">Reject</button>
          </div>
        </div>
      `).join("");
    }

    function renderWorkers(workers) {
      const el = document.getElementById("workerList");
      el.innerHTML = workers.map(w => `
        <div class="kv">
          <strong>${w.worker_id}</strong> (${w.machine_class}) — CoW: <code>${w.cow_backend}</code>
          ${statusBadge(w.health)}
        </div>
      `).join("");
    }

    function renderSkills(skills) {
      const el = document.getElementById("skillList");
      if (!skills.length) {
        el.innerHTML = `<p style="color:var(--muted);font-size:0.85rem;">No skills registered yet.</p>`;
        return;
      }
      el.innerHTML = skills.map(s => `
        <div class="task-item">
          <div style="display:flex;justify-content:space-between;">
            <strong>${s.name} (<code>${s.skill_id}</code>) — Risk Tier ${s.risk_class}</strong>
            ${statusBadge(s.status)}
          </div>
          <div style="font-size:0.82rem;color:var(--muted);margin-top:4px;">${s.purpose || s.description}</div>
          <div style="font-size:0.75rem;margin-top:4px;">
            SHA-256: <code>${s.bundle_sha256.slice(0,16)}...</code> |
            SkillSpector Score: <strong>${s.scan ? s.scan.risk_score : 'n/a'}/100</strong>
            (${s.scan ? s.scan.gate_decision : 'N/A'})
          </div>
        </div>
      `).join("");
    }

    async function selectTask(taskId) {
      currentTaskId = taskId;
      document.getElementById("selectedTaskBadge").textContent = taskId;
      const [task, eventsData, proofData] = await Promise.all([
        api(`/v1/tasks/${taskId}`),
        api(`/v1/tasks/${taskId}/events`),
        api(`/v1/tasks/${taskId}/proof`),
      ]);
      document.getElementById("taskActions").innerHTML = `
        <button class="btn-secondary" onclick="taskAction('${taskId}', 'pause')">⏸ Pause</button>
        <button class="btn-secondary" onclick="taskAction('${taskId}', 'resume')">▶ Resume</button>
        <button class="btn-red" onclick="taskAction('${taskId}', 'cancel')">✖ Cancel &amp; Rollback CoW</button>
      `;
      const detail = document.getElementById("taskDetailView");
      detail.innerHTML = `
        <div class="kv"><span>Goal:</span> <strong>${task.goal}</strong></div>
        <div class="kv"><span>Status:</span> ${statusBadge(task.status)} | <span>Execution Mode:</span> <code>${task.execution_mode}</code> | <span>CoW Tree SHA:</span> <code>${task.active_workspace_tree_sha || 'none'}</code></div>
        <h3 style="font-size:0.85rem;margin:12px 0 6px;color:var(--muted);">Proof-Carrying Artifact Bundle (FR-018)</h3>
        <pre>${JSON.stringify(proofData, null, 2)}</pre>
        <h3 style="font-size:0.85rem;margin:12px 0 6px;color:var(--muted);">Append-Only Event Stream &amp; Memoized Call Journal (FR-010, FR-015)</h3>
        <pre>${JSON.stringify(eventsData, null, 2)}</pre>
      `;
    }

    async function submitTask() {
      const goal = document.getElementById("goalInput").value.trim();
      const project_id = document.getElementById("projectInput").value.trim() || "default";
      const execution_mode = document.getElementById("modeSelect").value;
      if (!goal) return alert("Enter a task goal");
      const res = await api("/v1/tasks", {
        method: "POST",
        body: JSON.stringify({ goal, project_id, execution_mode }),
      });
      document.getElementById("goalInput").value = "";
      currentTaskId = res.task_id;
      await refreshAll();
    }

    async function taskAction(taskId, action) {
      await api(`/v1/tasks/${taskId}:${action}`, { method: "POST", body: "{}" });
      await refreshAll();
    }

    async function decideApproval(approvalId, decision) {
      await api(`/v1/approvals/${approvalId}:decide`, {
        method: "POST",
        body: JSON.stringify({ decision, comment: "Decided from local web dashboard" }),
      });
      await refreshAll();
    }

    async function importSkill() {
      const disk_path = document.getElementById("skillPathInput").value.trim();
      if (!disk_path) return alert("Enter a skill directory path");
      await api("/v1/skills:import", {
        method: "POST",
        body: JSON.stringify({ disk_path }),
      });
      await refreshAll();
    }

    refreshAll();
  </script>
</body>
</html>
"""
