# In-Flight Sub-Agent Delegation (Tier C)

HEX can spawn focused, budget-bounded child agents on the fly to execute parallel research, specialized coding, or test verification without polluting your primary conversation history.

---

## 1. Subagent Delegation Model

When a complex task is submitted, the primary coordinator agent can invoke child workers:

```text
Primary Coordinator
   ├── 🔍 Researcher (Read-only AST & web investigation)
   ├── 💻 Coder (Surgical edits inside isolated CoW scratchpad)
   └── 🧪 Tester (Test execution & compiler diagnostic validation)
```

### Delegation Principles:
1. **Context Isolation**: Subagents inherit **no conversational history** from the parent, keeping their context windows clean and hyper-focused.
2. **Explicit Handoff**: The parent provides an exact goal, file paths, and verifiable success criteria in the initial prompt.
3. **Structured Reporting**: When the subagent finishes, it sends a structured completion summary back to the parent.

---

## 2. Least-Privilege Role Boundaries

To prevent unauthorized or destructive operations, each subagent is assigned a strict role with gated tool permissions:

| Role | Allowed Tools | Prohibited Operations |
| :--- | :--- | :--- |
| **`researcher`** | `read_file`, `search_files`, `query_graphify`, `web_search` | File editing, shell commands, git operations |
| **`coder`** | `read_file`, `write_file`, `patch`, `query_graphify`, `run_tests` | Git push, remote deployment, shell forks |
| **`tester`** | `run_tests`, `lsp_diagnostics`, `read_file` | File creation, permanent file deletion |
| **`reviewer`** | `diff`, `query_graphify`, `read_file` | Workspace modification |
| **`planner`** | `read_file`, `search_files`, task breakdown tools | Direct execution |

---

## 3. Copy-on-Write Workspace Sandboxing

Each subagent operates inside an independent Copy-on-Write scratchpad:
- Staged at `.hex/subagents/<subagent_id>/`.
- Reflinked from the parent workspace in **<10ms**.
- If a subagent's implementation fails verification, its scratchpad is pruned without leaving dirty or uncommitted files in your project directory.

---

## 4. Recursion Limits & Safety Controls

- **Depth Capping**: Subagent delegation is capped to a maximum depth (default: `max_depth = 2`) to prevent uncontrolled recursive agent spawning.
- **Token Budgets**: Each subagent has an independent token and turn budget. If the budget is exhausted, the subagent halts and reports partial progress to the coordinator.
- **TUI Observability**: Inspect in-flight subagents and their status in real time via the `/subagents` slash command.
