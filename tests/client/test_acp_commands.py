"""F01 regression: ACP slash commands must perform the operation they report.

Every success path is checked against observable backend state; every missing
capability must surface as an explicit error, never a fabricated success.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_core.client.acp import ACPServer, ACPSession


class FakeMemory:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def write_memory(self, *, scope, subject, content, project_id=None, workspace_dir=None, **kw):
        assert scope == "project" and project_id, "governed project memory requires scope + project"
        mem = {"memory_id": f"mem_{len(self.entries) + 1:04d}", "subject": subject, "content": content}
        self.entries.append(mem)
        return mem

    def retrieve_memories(self, *, project_id, query="", workspace_dir=None, **kw):
        q = query.lower()
        return [m for m in self.entries if not q or q in m["subject"].lower() or q in m["content"].lower()]


class FakeLedger:
    def __init__(self) -> None:
        self.searched: list[str] = []

    def list_skills(self):
        return [{"name": "demo-skill", "gate_decision": "ALLOW"}]

    def search_tasks(self, query, project_id=None, limit=20):
        self.searched.append(query)
        if query == "needle":
            return [{"task_id": "tsk_1", "status": "SUCCEEDED", "score": 1.5, "snippet": "found needle"}]
        return []

    def get_proof_bundle(self, task_id):
        if task_id == "tsk_proven":
            return {
                "proof_id": "prf_1",
                "task_id": task_id,
                "workspace_tree_sha": "sha_ws",
                "git_diff_sha": "sha_diff",
                "ledger_signature": "sig_1",
            }
        return None

    def verify_chain_integrity(self, task_id=None):
        return SimpleNamespace(valid=True, errors=[], verified_events=3, tasks_checked=1)

    def list_tasks(self, limit=20):
        return []

    def get_task(self, task_id):
        return {"task_id": task_id, "status": "SUCCEEDED"}


class FakeGraphify:
    def __init__(self) -> None:
        self.symbols_by_file = {"sample.py": []}
        self.refreshed: list[Path] = []
        self.reflected: list[Path] = []

    def build_or_refresh(self, ws):
        self.refreshed.append(Path(ws))

    def compute_blast_radius(self, ws, files):
        return SimpleNamespace(to_dict=lambda: {
            "modified_files": list(files),
            "direct_callers": ["caller_a"],
            "transitive_callers": [],
        })

    def query_symbol(self, name):
        return {"definitions": [], "hop1_callers": [], "hop2_callers": []}

    def reflect_work_memory(self, ws):
        self.reflected.append(Path(ws))
        return {"lessons_path": str(Path(ws) / "LESSONS.md"), "lessons_markdown": "- learned something"}


class FakeScanner:
    def __init__(self) -> None:
        self.scanned: list[Path] = []

    def scan_and_register_skill(self, path):
        self.scanned.append(Path(path))
        return {"name": "demo-skill", "skill_id": "skl_1", "scan": {"gate_decision": "ALLOW", "risk_score": 12}}


class FakeOrchestrator:
    def __init__(self) -> None:
        self.memory = FakeMemory()
        self.ledger = FakeLedger()
        self.graphify = FakeGraphify()
        self.scanner = FakeScanner()
        self.router = SimpleNamespace(
            compressor_mode="deterministic",
            cloud_model_id="model-x",
            budget_tracker=SimpleNamespace(total_usd_spent=0.25),
        )
        self.subagent_manager = SimpleNamespace(list_active_subagents=lambda: [])
        self.react_enabled = True
        self.cancelled: list[tuple[str, str]] = []
        self.forked: list[str] = []
        self.model_switches: list[str] = []

    def cancel_task(self, task_id, actor="owner"):
        self.cancelled.append((task_id, actor))
        return {"task_id": task_id, "status": "CANCELED"}

    def fork_speculative_branches(self, task_id, branch_strategies):
        self.forked.append(task_id)
        return {"winner_branch_id": "br_a"}

    def set_active_model(self, model_id, durable=True):
        self.model_switches.append(model_id)
        return ("fake-provider", model_id, None)

    def submit_task(self, req):
        return {"task_id": "tsk_prompt_1"}

    def execute_task(self, task_id, stream_callback=None, tool_callback=None):
        return {"status": "SUCCEEDED", "payload": {"result": "done"}}


@pytest.fixture(autouse=True)
def hermetic_host_probes(monkeypatch, tmp_path):
    """Keep doctor and Antigravity credential probes off the real host."""
    from harness_core.client import doctor as doctor_mod
    from harness_core.providers import antigravity_provider as ag_mod

    def fake_diagnostics(self, fix=False, live=False):
        return doctor_mod.DoctorReport(checks=[
            doctor_mod.DoctorCheckResult(name="probe", category="python", status="ok", message="fine"),
        ])

    monkeypatch.setattr(doctor_mod.HexDoctor, "run_diagnostics", fake_diagnostics)
    monkeypatch.setattr(
        ag_mod.AntigravityOAuthManager, "resolve_credential", lambda self, auto_refresh=False: None
    )


@pytest.fixture
def server(tmp_path):
    orch = FakeOrchestrator()
    srv = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    srv.sessions["s1"] = ACPSession(session_id="s1", workspace_root=tmp_path)
    return srv


def run_cmd(server, command, args=""):
    resp = server.handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session/command",
        "params": {"sessionId": "s1", "command": command, "args": args},
    })
    assert "result" in resp, resp
    return resp["result"]


def test_remember_then_memory_roundtrip(server):
    res = run_cmd(server, "/remember", "release process: ship on Fridays")
    assert res["isError"] is False
    assert len(server.orchestrator.memory.entries) == 1
    stored = server.orchestrator.memory.entries[0]
    assert stored["subject"] == "release process"
    assert stored["memory_id"] in res["output"]

    query = run_cmd(server, "/memory", "release")
    assert query["isError"] is False
    assert "release process" in query["output"]
    assert stored["memory_id"] in query["output"]


def test_remember_requires_subject_and_content(server):
    for bad in ("", "no colon here", ": content only", "subject:"):
        res = run_cmd(server, "/remember", bad)
        assert res["isError"] is True
    assert server.orchestrator.memory.entries == []


def test_scan_nonexistent_directory_errors_without_scanning(server):
    res = run_cmd(server, "/scan", "not-a-real-directory")
    assert res["isError"] is True
    assert server.orchestrator.scanner.scanned == []


def test_scan_requires_argument(server):
    res = run_cmd(server, "/scan", "")
    assert res["isError"] is True
    assert server.orchestrator.scanner.scanned == []


def test_scan_registers_real_bundle(server, tmp_path):
    bundle = tmp_path / "my-skill"
    bundle.mkdir()
    res = run_cmd(server, "/scan", "my-skill")
    assert res["isError"] is False
    assert server.orchestrator.scanner.scanned == [bundle.resolve()]
    assert "ALLOW" in res["output"]
    assert "skl_1" in res["output"]


def test_rollback_without_task_errors(server):
    res = run_cmd(server, "/rollback")
    assert res["isError"] is True
    assert server.orchestrator.cancelled == []


def test_rollback_cancels_last_prompt_task(server):
    prompt = server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
        "params": {"sessionId": "s1", "prompt": "do something"},
    })
    tid = prompt["result"]["taskId"]
    res = run_cmd(server, "/rollback")
    assert res["isError"] is False
    assert server.orchestrator.cancelled == [(tid, "acp")]
    assert tid in res["output"]
    assert server.sessions["s1"].last_task_id is None


def test_fork_requires_task_then_calls_orchestrator(server):
    assert run_cmd(server, "/fork")["isError"] is True
    assert server.orchestrator.forked == []
    res = run_cmd(server, "/fork", "tsk_42")
    assert res["isError"] is False
    assert server.orchestrator.forked == ["tsk_42"]
    assert "br_a" in res["output"]


def test_search_returns_real_ledger_results(server):
    hit = run_cmd(server, "/search", "needle")
    assert hit["isError"] is False
    assert "tsk_1" in hit["output"]
    miss = run_cmd(server, "/search", "nothing")
    assert miss["isError"] is False
    assert "No tasks matched" in miss["output"]
    assert run_cmd(server, "/search", "")["isError"] is True
    assert server.orchestrator.ledger.searched == ["needle", "nothing"]


def test_reflect_uses_graphify(server, tmp_path):
    res = run_cmd(server, "/reflect")
    assert res["isError"] is False
    assert server.orchestrator.graphify.reflected == [tmp_path]
    assert "LESSONS.md" in res["output"]
    assert "learned something" in res["output"]


def test_budget_reports_tracker_and_errors_without_one(server):
    res = run_cmd(server, "/budget")
    assert res["isError"] is False
    assert "0.2500" in res["output"]
    server.orchestrator.router.budget_tracker = None
    assert run_cmd(server, "/budget")["isError"] is True


def test_compressor_validates_and_sets_mode(server):
    assert run_cmd(server, "/compressor", "bogus")["isError"] is True
    assert server.orchestrator.router.compressor_mode == "deterministic"
    res = run_cmd(server, "/compressor", "local")
    assert res["isError"] is False
    assert server.orchestrator.router.compressor_mode == "local"
    run_cmd(server, "/compressor", "disabled")
    assert server.orchestrator.router.compressor_mode == "off"


def test_model_switch_reaches_orchestrator(server):
    res = run_cmd(server, "/model", "model-y")
    assert res["isError"] is False
    assert server.orchestrator.model_switches == ["model-y"]
    assert server.sessions["s1"].model == "model-y"


def test_proof_reports_real_bundle_and_chain(server):
    res = run_cmd(server, "/proof", "tsk_proven")
    assert res["isError"] is False
    assert "prf_1" in res["output"]
    assert "VALID" in res["output"]
    missing = run_cmd(server, "/proof", "tsk_unknown")
    assert missing["isError"] is False
    assert "No Proof-Carrying Artifact bundle" in missing["output"]


def test_blast_uses_graphify(server, tmp_path):
    res = run_cmd(server, "/blast", "sample.py")
    assert res["isError"] is False
    assert server.orchestrator.graphify.refreshed == [tmp_path]
    assert "caller_a" in res["output"]


def test_recipe_empty_workspace_is_truthful(server):
    res = run_cmd(server, "/recipe")
    assert res["isError"] is False
    assert "No build/test recipe detected" in res["output"]


def test_lsp_status_lists_registry_without_claiming_active_servers(server):
    res = run_cmd(server, "/lsp")
    assert res["isError"] is False
    assert "installed" in res["output"] or "not found" in res["output"]
    assert "Active" not in res["output"]


def test_quota_unauthenticated_is_explicit(server):
    res = run_cmd(server, "/quota")
    assert res["isError"] is False
    assert "unauthenticated" in res["output"]


def test_doctor_formats_real_report(server):
    res = run_cmd(server, "/doctor")
    assert res["isError"] is False
    assert "probe" in res["output"]


def test_status_reports_last_task(server):
    before = run_cmd(server, "/status")
    assert "Last task: none" in before["output"]
    server.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
        "params": {"sessionId": "s1", "prompt": "hi"},
    })
    after = run_cmd(server, "/status")
    assert "tsk_prompt_1" in after["output"]
    assert "SUCCEEDED" in after["output"]


def test_diff_and_sbs_use_real_git(server, tmp_path):
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    f = tmp_path / "file.txt"
    f.write_text("one\n")
    subprocess.run(["git", *env_args, "add", "file.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", *env_args, "commit", "-qm", "init"], cwd=tmp_path, check=True)
    f.write_text("two\n")
    diff = run_cmd(server, "/diff")
    assert diff["isError"] is False
    assert "file.txt" in diff["output"]
    sbs = run_cmd(server, "/sbs", "file.txt")
    assert sbs["isError"] is False
    assert "-one" in sbs["output"] and "+two" in sbs["output"]
    assert run_cmd(server, "/sbs", "missing.txt")["isError"] is True


def test_unknown_command_errors(server):
    res = run_cmd(server, "/definitely-not-real")
    assert res["isError"] is True


BARE_ORCHESTRATOR_COMMANDS = [
    ("/remember", "a: b"),
    ("/memory", "q"),
    ("/scan", "somedir"),
    ("/rollback", ""),
    ("/fork", ""),
    ("/search", "q"),
    ("/reflect", ""),
    ("/budget", ""),
    ("/subagents", ""),
    ("/blast", "x.py"),
    ("/proof", ""),
    ("/skills", ""),
    ("/compressor", "local"),
    ("/model", "foo"),
    ("/react", "on"),
    ("/lsp", "diag"),
]

SUCCESS_MARKERS = (
    "recorded", "scanned", "registered", "rolled back", "pristine",
    "synthesized", "forked", "switched to", "0 errors", "Verified",
)


@pytest.mark.parametrize("command,args", BARE_ORCHESTRATOR_COMMANDS)
def test_bare_orchestrator_never_fabricates_success(tmp_path, command, args):
    srv = ACPServer(orchestrator=object(), workspace_root=tmp_path)
    srv.sessions["s1"] = ACPSession(session_id="s1", workspace_root=tmp_path)
    res = run_cmd(srv, command, args)
    assert res["isError"] is True, f"{command} reported success without a backend: {res['output']}"
    for marker in SUCCESS_MARKERS:
        assert marker.lower() not in res["output"].lower(), (
            f"{command} fabricated '{marker}' without performing the operation"
        )
