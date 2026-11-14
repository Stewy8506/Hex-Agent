"""Tests for Closed-Loop Autonomous Learning Loop & Deterministic Skill Hooks.

Covers:
- Hardware gating (auto_reflect = "cloud_only" | "idle" | "off", HEX_AUTO_REFLECT)
- ReActLoop multi-step friction sensor & event emission
- Reflection candidate logging to pending_reflections.json when suppressed
- Lesson synthesis and LESSONS.md formatting
- Negative guardrail auto-population into GUARDRAILS.md
- MemoryLifecycleManager hit-counter tracking and promotion
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_core.config_loader import (
    get_auto_reflect_policy,
    should_execute_background_reflection,
)
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.react_loop import (
    ReActConfig,
    ReActLoop,
    ReActSessionResult,
    ToolCallRequest,
    ToolCallResult,
)


# =============================================================================
# 1. Hardware Gating Tests
# =============================================================================


def test_hardware_gating_policy_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies parsing of auto_reflect from config.yaml and HEX_AUTO_REFLECT env var."""
    # 1. Default when unconfigured
    monkeypatch.delenv("HEX_AUTO_REFLECT", raising=False)
    policy = get_auto_reflect_policy(workspace_root=tmp_path)
    assert policy == "cloud_only"

    # 2. Config file in workspace
    cfg_dir = tmp_path / ".hex"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.yaml"
    cfg_file.write_text("learning:\n  auto_reflect: idle\n", encoding="utf-8")

    policy = get_auto_reflect_policy(workspace_root=tmp_path)
    assert policy == "idle"

    # 3. Config file with boolean/off
    cfg_file.write_text("learning:\n  auto_reflect: off\n", encoding="utf-8")
    policy = get_auto_reflect_policy(workspace_root=tmp_path)
    assert policy == "off"

    # 4. Env var takes priority over config file
    monkeypatch.setenv("HEX_AUTO_REFLECT", "cloud_only")
    policy = get_auto_reflect_policy(workspace_root=tmp_path)
    assert policy == "cloud_only"

    monkeypatch.setenv("HEX_AUTO_REFLECT", "off")
    policy = get_auto_reflect_policy(workspace_root=tmp_path)
    assert policy == "off"


def test_hardware_gating_decision_off() -> None:
    """Policy 'off' must suppress reflection unconditionally."""
    allowed, reason = should_execute_background_reflection(policy="off")
    assert not allowed
    assert "Autonomous reflection disabled (policy=off)" in reason


def test_hardware_gating_decision_idle() -> None:
    """Policy 'idle' must execute only when is_idle is True."""
    allowed, reason = should_execute_background_reflection(policy="idle", is_idle=False)
    assert not allowed
    assert "Deferred until idle" in reason

    allowed, reason = should_execute_background_reflection(policy="idle", is_idle=True)
    assert allowed
    assert "Executing during idle" in reason


def test_hardware_gating_decision_cloud_only_local() -> None:
    """Policy 'cloud_only' with local model providers must be suppressed to protect memory."""
    # Router with MLX
    router_mlx = MagicMock(active_provider="mlx", cloud_provider=None)
    allowed, reason = should_execute_background_reflection(policy="cloud_only", router=router_mlx)
    assert not allowed
    assert "Suppressed background reflection on local hardware to prevent memory starvation" in reason

    # Router with Ollama
    router_ollama = MagicMock(active_provider="ollama", cloud_provider=None)
    allowed, reason = should_execute_background_reflection(policy="cloud_only", router=router_ollama)
    assert not allowed
    assert "Suppressed background reflection on local hardware to prevent memory starvation" in reason

    # Router with None cloud provider
    router_no_cloud = MagicMock(cloud_provider=None, active_provider=None, cloud_provider_type=None)
    allowed, reason = should_execute_background_reflection(policy="cloud_only", router=router_no_cloud)
    assert not allowed
    assert "Suppressed background reflection on local hardware to prevent memory starvation" in reason


def test_hardware_gating_decision_cloud_only_cloud() -> None:
    """Policy 'cloud_only' with active cloud providers must proceed."""
    # Router with active Anthropic cloud provider
    cloud_mock = MagicMock(provider_name="anthropic", is_available=MagicMock(return_value=True))
    router_cloud = MagicMock(cloud_provider=cloud_mock, active_provider="anthropic")
    allowed, reason = should_execute_background_reflection(policy="cloud_only", router=router_cloud)
    assert allowed
    assert "Cloud-routed reasoner active" in reason

    # Router with Antigravity / Gemini
    router_gemini = MagicMock(active_provider="gemini", cloud_provider=MagicMock(is_available=lambda: True))
    allowed, reason = should_execute_background_reflection(policy="cloud_only", router=router_gemini)
    assert allowed
    assert "Cloud-routed reasoner active" in reason


# =============================================================================
# 2. Friction Sensor Tests in ReActLoop
# =============================================================================


def test_friction_sensor_retry_count() -> None:
    """ReActLoop detects friction when retry_count reaches >= 3."""
    events: list[tuple[str, dict]] = []

    def on_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = ReActConfig(on_event=on_event)
    loop = ReActLoop(config=cfg)

    # Simulate retries reaching 3
    loop.retry_count = 3
    loop._check_and_trigger_friction("test_retry_threshold")

    assert loop.friction_detected
    friction_events = [e for e in events if e[0] == "friction_detected"]
    assert len(friction_events) == 1
    assert friction_events[0][1]["retry_count"] == 3


def test_friction_sensor_repeat_tool_failure() -> None:
    """ReActLoop detects friction when same tool fails >= 2 times with identical error."""
    events: list[tuple[str, dict]] = []

    def on_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = ReActConfig(on_event=on_event)
    loop = ReActLoop(config=cfg)

    call = ToolCallRequest(call_id="c1", name="run_command", arguments={"command": "npm test"})
    err_res = ToolCallResult(
        call_id="c1",
        name="run_command",
        output="Error: Command failed with exit code 1: Cannot find module 'react'",
        is_error=True,
    )

    # First failure
    loop._handle_tool_failure_guardrail(call, err_res)
    assert not loop.friction_detected

    # Second failure with same signature
    loop._handle_tool_failure_guardrail(call, err_res)
    assert loop.friction_detected

    friction_events = [e for e in events if e[0] == "friction_detected"]
    assert len(friction_events) == 1
    assert "repeat_tool_failure" in friction_events[0][1]["reason"]


def test_friction_sensor_resolved_tool_failure() -> None:
    """ReActLoop detects friction when a tool that previously failed >= 2 times succeeds."""
    events: list[tuple[str, dict]] = []

    def on_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = ReActConfig(on_event=on_event)
    loop = ReActLoop(config=cfg)

    call = ToolCallRequest(call_id="c1", name="read_file", arguments={"path": "missing.py"})
    err_res = ToolCallResult(
        call_id="c1",
        name="read_file",
        output="Error: FileNotFoundError: missing.py does not exist",
        is_error=True,
    )

    # Fail twice
    loop._handle_tool_failure_guardrail(call, err_res)
    loop._handle_tool_failure_guardrail(call, err_res)
    assert loop.friction_detected

    # Tool now succeeds
    loop._reset_tool_failures_on_success("read_file")
    assert any("tool_repeat_failure_resolved" in r for r in loop._friction_reasons)


def test_friction_sensor_compiler_errors() -> None:
    """ReActLoop detects friction when compiler/syntax errors occur repeatedly."""
    events: list[tuple[str, dict]] = []

    def on_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = ReActConfig(on_event=on_event)
    loop = ReActLoop(config=cfg)

    call1 = ToolCallRequest(call_id="c1", name="code_kernel", arguments={"code": "def foo("})
    err1 = ToolCallResult(
        call_id="c1",
        name="code_kernel",
        output="SyntaxError: unexpected EOF while parsing",
        is_error=True,
    )
    call2 = ToolCallRequest(call_id="c2", name="code_kernel", arguments={"code": "if True"})
    err2 = ToolCallResult(
        call_id="c2",
        name="code_kernel",
        output="SyntaxError: expected ':'",
        is_error=True,
    )

    loop._handle_tool_failure_guardrail(call1, err1)
    assert loop.compiler_errors_count == 1
    assert not loop.friction_detected

    loop._handle_tool_failure_guardrail(call2, err2)
    assert loop.compiler_errors_count == 2
    assert loop.friction_detected
    assert any("repeated_compiler_errors" in r for r in loop._friction_reasons)


def test_react_session_result_exposes_friction_info() -> None:
    """ReActLoop exposes comprehensive friction_info dictionary on session result."""
    # Mock caller that calls a tool which fails twice, then finishes
    turn_num = 0

    def mock_model(messages):
        nonlocal turn_num
        turn_num += 1
        if turn_num == 1:
            return '<tool_call>{"name": "fail_tool", "arguments": {}}</tool_call>'
        elif turn_num == 2:
            return '<tool_call>{"name": "fail_tool", "arguments": {}}</tool_call>'
        return "<finish>All resolved successfully</finish>"

    mock_gateway = MagicMock(spec=["execute"])

    def exec_tool(name, args):
        if name == "fail_tool":
            return {"output": "Error: Deterministic failure in tool", "is_error": True, "ok": False}
        return {"output": "ok", "is_error": False, "ok": True}

    mock_gateway.execute = MagicMock(side_effect=exec_tool)

    cfg = ReActConfig(max_turns=5)
    loop = ReActLoop(model_caller=mock_model, gateway=mock_gateway, config=cfg)
    result = loop.run("Fix the issue")

    assert result.success
    assert result.friction_info["friction_detected"] is True
    assert result.friction_info["turn_repair_count"] >= 2
    assert len(result.friction_info["reasons"]) > 0


# =============================================================================
# 3. Orchestrator Hardware-Gated Reflection Suppression
# =============================================================================


def test_orchestrator_friction_handling_suppressed(tmp_path: Path) -> None:
    """When reflection is suppressed by hardware gating, candidate is logged to pending_reflections.json."""
    ws = tmp_path / "project"
    ws.mkdir()

    # Configure policy="cloud_only"
    cfg_dir = ws / ".hex"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("learning:\n  auto_reflect: cloud_only\n", encoding="utf-8")

    # Local MLX router (suppressed)
    mock_router = MagicMock(active_provider="mlx", cloud_provider=None)

    orch = HarnessOrchestrator(
        data_dir=str(ws / ".hex" / "data"),
        default_workspace=str(ws),
    )
    orch.router = mock_router

    res = orch._handle_post_task_reflection(
        task_id="task_friction_001",
        goal="Resolve multi-attempt compilation failure",
        workspace_root=ws,
        repair_attempts=3,
        friction_info={"friction_detected": True, "reasons": ["repair_attempts >= 3"]},
        error_traces=["SyntaxError: invalid syntax in router.py line 42"],
        modified_files=["router.py"],
    )

    assert res["status"] == "suppressed"
    assert "pending_file" in res

    pending_file = ws / ".hex" / "reflections" / "pending_reflections.json"
    assert pending_file.is_file()

    entries = json.loads(pending_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["task_id"] == "task_friction_001"
    assert entries[0]["repair_attempts"] == 3
    assert entries[0]["status"] == "pending"
    assert "Suppressed background reflection on local hardware" in entries[0]["suppression_reason"]


# =============================================================================
# 4. ReflectionSynthesizer & MemoryLifecycleManager Tests (Task 2)
# =============================================================================


def test_lesson_synthesis_llm(tmp_path: Path) -> None:
    """ReflectionSynthesizer extracts structured lesson from LLM response when router available."""
    from harness_core.reflection import ReflectionSynthesizer

    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "symptom": "Pytest failed with 2 assertion errors in test_auth.py",
        "root_cause": "Token expiration header was missing from auth payload",
        "verified_solution": "Injected exp timestamp in JWT payload generation",
        "avoid_invariant": "- [AVOID] Do not omit exp claim from JWT tokens",
    })

    mock_router = MagicMock()
    mock_router.execute_routed_request.return_value = ("ALLOW", mock_resp)

    synth = ReflectionSynthesizer(router=mock_router, workspace_root=tmp_path)
    lesson = synth.synthesize_lesson(
        task_id="task_jwt_auth",
        goal="Fix token validation errors",
        error_traces=["AssertionError: 'exp' missing from token payload"],
        modified_files=["auth/jwt.py"],
    )

    assert lesson.task_id == "task_jwt_auth"
    assert "Pytest failed with 2 assertion errors" in lesson.symptom
    assert "Token expiration header was missing" in lesson.root_cause
    assert "Injected exp timestamp" in lesson.verified_solution
    assert "- [AVOID] Do not omit exp claim" in lesson.avoid_invariant
    assert lesson.source_nodes == ["auth/jwt.py"]


def test_lesson_synthesis_deterministic(tmp_path: Path) -> None:
    """ReflectionSynthesizer falls back to clean deterministic extraction when router is None."""
    from harness_core.reflection import ReflectionSynthesizer

    synth = ReflectionSynthesizer(router=None, workspace_root=tmp_path)
    lesson = synth.synthesize_lesson(
        task_id="task_syntax_repair",
        goal="Fix syntax errors in parser",
        error_traces=["Error: SyntaxError: unexpected EOF while parsing parser.py line 89"],
        final_patch="+    return ast_tree\n",
        modified_files=["parser.py"],
    )

    assert lesson.task_id == "task_syntax_repair"
    assert "SyntaxError: unexpected EOF while parsing parser.py line 89" in lesson.symptom
    assert "parser.py" in lesson.root_cause
    assert "parser.py" in lesson.verified_solution
    assert lesson.avoid_invariant.startswith("- [AVOID]")
    assert "parser.py" in lesson.source_nodes


def test_append_to_lessons_md(tmp_path: Path) -> None:
    """append_to_lessons_md preserves standard markdown structure in LESSONS.md."""
    from harness_core.reflection import FrictionLesson, ReflectionSynthesizer

    synth = ReflectionSynthesizer(workspace_root=tmp_path)
    lesson1 = FrictionLesson(
        lesson_id="lesson_001",
        task_id="task_100",
        symptom="Connection pool exhaustion under load",
        root_cause="Unclosed database transactions in error path",
        verified_solution="Wrapped connection in async context manager",
        avoid_invariant="- [AVOID] Do not acquire connection without context manager",
        source_nodes=["src/db.py"],
    )
    lesson2 = FrictionLesson(
        lesson_id="lesson_002",
        task_id="task_200",
        symptom="Type error in router dispatch",
        root_cause="Optional value passed to strict signature",
        verified_solution="Added None check guard before call",
        avoid_invariant="- [AVOID] Do not pass unchecked Optional[str] to router",
        source_nodes=["src/router.py"],
    )

    p1 = synth.append_to_lessons_md(lesson1, workspace_root=tmp_path)
    p2 = synth.append_to_lessons_md(lesson2, workspace_root=tmp_path)
    assert p1 == p2
    assert p1.is_file()

    content = p1.read_text(encoding="utf-8")
    assert "# Autonomous Friction Lessons & Post-Mortems" in content
    assert "### Lesson: lesson_001 (Task: task_100)" in content
    assert "### Lesson: lesson_002 (Task: task_200)" in content
    assert "Connection pool exhaustion under load" in content
    assert "- [AVOID] Do not acquire connection without context manager" in content


def test_lifecycle_integration_auto_registration(tmp_path: Path) -> None:
    """Appending a lesson automatically records reflection and populates GUARDRAILS.md."""
    from harness_core.reflection import FrictionLesson, ReflectionSynthesizer
    from harness_core.memory import GuardrailsManager, MemoryLifecycleManager

    synth = ReflectionSynthesizer(workspace_root=tmp_path)
    lesson = FrictionLesson(
        lesson_id="lesson_guardrail_test",
        task_id="task_guard_01",
        symptom="Compilation failure in native ffi bindings",
        root_cause="Missing C-header include in build script",
        verified_solution="Added #include <unistd.h> to bridge header",
        avoid_invariant="- [AVOID] Do not omit bridge header include in FFI",
        source_nodes=["native/bridge.h"],
    )

    synth.append_to_lessons_md(lesson, workspace_root=tmp_path)

    # 1. Verify GUARDRAILS.md was populated
    guard_mgr = GuardrailsManager(workspace_root=tmp_path)
    guardrails = guard_mgr.list_guardrails()
    assert any("Do not omit bridge header include in FFI" in g for g in guardrails)

    # 2. Verify MemoryLifecycleManager tracked reflection
    lifecycle_mgr = MemoryLifecycleManager(workspace_root=tmp_path)
    reflections = list(lifecycle_mgr._reflections.values())
    assert any("Added #include <unistd.h>" in r["content"] for r in reflections)


def test_lifecycle_integration_hit_counter_and_promotion(tmp_path: Path) -> None:
    """Reflections with hit_counter >= 3 promote to MEMORY.md."""
    from harness_core.memory import MemoryLifecycleManager

    lifecycle_mgr = MemoryLifecycleManager(workspace_root=tmp_path)
    ref = lifecycle_mgr.record_reflection("Always run cargo check before committing")

    # Hit 1 (upon creation)
    assert ref["hit_counter"] == 1

    # Hit 2
    r2 = lifecycle_mgr.record_reference(ref["id"])
    assert r2["hit_counter"] == 2

    # Still < 3, no promotions yet
    assert len(lifecycle_mgr.check_promotions()) == 0

    # Hit 3
    r3 = lifecycle_mgr.record_reference(ref["id"])
    assert r3["hit_counter"] == 3

    # Now eligible for promotion
    promos = lifecycle_mgr.check_promotions()
    assert len(promos) >= 1
    assert any(p["id"] == ref["id"] for p in promos)

    # Execute promotion into MEMORY.md
    ok = lifecycle_mgr.promote_entry(ref["id"], target="memory")
    assert ok

    mem_file = tmp_path / ".hex" / "memories" / "MEMORY.md"
    assert mem_file.is_file()
    assert "Always run cargo check before committing" in mem_file.read_text(encoding="utf-8")


def test_lifecycle_integration_promote_to_skill(tmp_path: Path) -> None:
    """Reflections with hit_counter >= 5 promote to a SKILL.md bundle via SkillCompiler."""
    from harness_core.memory import MemoryLifecycleManager

    lifecycle_mgr = MemoryLifecycleManager(workspace_root=tmp_path)
    ref = lifecycle_mgr.record_reflection("Safely scaffold and register a new RPC schema endpoint")

    for _ in range(4):
        lifecycle_mgr.record_reference(ref["id"])

    assert lifecycle_mgr._reflections[ref["id"]]["hit_counter"] == 5

    promos = lifecycle_mgr.check_promotions()
    candidate = next(p for p in promos if p["id"] == ref["id"])
    assert candidate["action"] == "flag_for_skill"

    # Promote to skill
    res = lifecycle_mgr.promote_entry(ref["id"], target="skill")
    assert res is True

