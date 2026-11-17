"""Comprehensive test suite for Conversational Timeline Rewind & Ledger Branching.

Verifies:
1. Relative rewind (/rewind -1, /rewind -2).
2. Absolute turn index rewind (/rewind 2, turn_2).
3. Boundary clamping (rewinding past turn 0 clamps to root, rewinding past max turn preserves all).
4. Branch ledger integrity: soft-deleted turns exist under branch_id with is_active = 0,
   active turns remain with is_active = 1, and cryptographic hash chain verifies 100%.
5. Tool execution reversal: tool calls corresponding to rewound turns are marked REVERTED.
6. ReActLoop integration: rewind_to updates messages, turns, and ledger.
7. HexConsole integration: /rewind slash command truncates messages and renders status.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
import pytest

from harness_core.storage.db import LedgerStore
from harness_core.storage.timeline_rewind import (
    TimelineRewindManager,
    RewindTarget,
    RewindResult,
    group_messages_by_turn,
)
from harness_core.core.react_loop import ReActLoop, ReActConfig, ReActTurn


@pytest.fixture
def temp_ledger(tmp_path: Path) -> LedgerStore:
    db_file = tmp_path / "test_ledger.db"
    return LedgerStore(f"sqlite:///{db_file}")


def test_group_messages_by_turn():
    messages = [
        {"role": "system", "content": "You are HEX."},
        {"role": "user", "content": "Goal 1"},
        {"role": "assistant", "content": "Working on goal 1"},
        {"role": "tool", "content": "Tool observation 1"},
        {"role": "user", "content": "Goal 2"},
        {"role": "assistant", "content": "Working on goal 2"},
        {"role": "user", "content": "Goal 3"},
        {"role": "assistant", "content": "Done with goal 3"},
    ]
    groups = group_messages_by_turn(messages)
    assert len(groups) == 4  # Turn 0 (system), Turn 1, Turn 2, Turn 3
    assert groups[0][0] == 0
    assert len(groups[0][1]) == 1  # system
    assert groups[1][0] == 1
    assert len(groups[1][1]) == 3  # user, assistant, tool
    assert groups[2][0] == 2
    assert len(groups[2][1]) == 2  # user, assistant
    assert groups[3][0] == 3
    assert len(groups[3][1]) == 2  # user, assistant


def test_relative_rewind_single_turn(temp_ledger: LedgerStore):
    task_id = "tsk_rewind_rel1"
    temp_ledger.ensure_task_exists(task_id, goal="Initial task")

    messages = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "Turn 1 prompt"},
        {"role": "assistant", "content": "Turn 1 reply"},
        {"role": "user", "content": "Turn 2 prompt"},
        {"role": "assistant", "content": "Turn 2 reply"},
        {"role": "user", "content": "Turn 3 prompt"},
        {"role": "assistant", "content": "Turn 3 reply"},
    ]

    manager = TimelineRewindManager(ledger=temp_ledger)
    res = manager.rewind(task_id=task_id, target="-1", messages=messages)

    assert res.target_turn == 2
    assert res.turns_rewound == 1
    assert res.branch_id.startswith("br_rewind_")
    # Live messages should only contain turn 0, turn 1, and turn 2
    assert len(res.live_messages) == 5
    assert res.live_messages[-1]["content"] == "Turn 2 reply"
    # Superseded messages should contain turn 3
    assert len(res.superseded_messages) == 2
    assert res.superseded_messages[0]["content"] == "Turn 3 prompt"


def test_relative_rewind_multiple_turns(temp_ledger: LedgerStore):
    task_id = "tsk_rewind_rel2"
    temp_ledger.ensure_task_exists(task_id)

    messages = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Turn 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Turn 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]

    manager = TimelineRewindManager(ledger=temp_ledger)
    res = manager.rewind(task_id=task_id, target="-2", messages=messages)

    assert res.target_turn == 1
    assert res.turns_rewound == 2
    assert len(res.live_messages) == 3
    assert res.live_messages[-1]["content"] == "Reply 1"
    assert len(res.superseded_messages) == 4


def test_absolute_rewind(temp_ledger: LedgerStore):
    task_id = "tsk_rewind_abs"
    temp_ledger.ensure_task_exists(task_id)

    messages = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Turn 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Turn 3"},
        {"role": "assistant", "content": "Reply 3"},
    ]

    manager = TimelineRewindManager(ledger=temp_ledger)
    # Test with string "turn_2"
    res1 = manager.rewind(task_id=task_id, target="turn_2", messages=messages)
    assert res1.target_turn == 2
    assert res1.turns_rewound == 1
    assert len(res1.live_messages) == 5

    # Test with int 1
    res2 = manager.rewind(task_id=task_id, target=1, messages=messages)
    assert res2.target_turn == 1
    assert res2.turns_rewound == 2
    assert len(res2.live_messages) == 3


def test_boundary_clamping(temp_ledger: LedgerStore):
    task_id = "tsk_rewind_bounds"
    temp_ledger.ensure_task_exists(task_id)

    messages = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Turn 2"},
        {"role": "assistant", "content": "Reply 2"},
    ]

    manager = TimelineRewindManager(ledger=temp_ledger)

    # Rewind far past turn 0: target -10 clamps to root (turn 0)
    res_past_root = manager.rewind(task_id=task_id, target="-10", messages=messages)
    assert res_past_root.target_turn == 0
    assert res_past_root.turns_rewound == 2
    assert len(res_past_root.live_messages) == 1
    assert res_past_root.live_messages[0]["role"] == "system"

    # Rewind forward past max turn: target 10 preserves all messages
    res_forward = manager.rewind(task_id=task_id, target=10, messages=messages)
    assert res_forward.target_turn == 2
    assert res_forward.turns_rewound == 0
    assert len(res_forward.live_messages) == len(messages)


def test_branch_ledger_integrity_and_tool_reversal(temp_ledger: LedgerStore):
    task_record = temp_ledger.submit_task({"goal": "Audited task"})
    task_id = task_record["task_id"]

    # 1. Record dialogue turns in ledger
    t1 = temp_ledger.record_dialogue_turn(task_id, 1, "user", "Turn 1 command")
    t1_r = temp_ledger.record_dialogue_turn(task_id, 1, "assistant", "Turn 1 result")
    t2 = temp_ledger.record_dialogue_turn(task_id, 2, "user", "Turn 2 command")
    t2_r = temp_ledger.record_dialogue_turn(task_id, 2, "assistant", "Turn 2 result")
    t3 = temp_ledger.record_dialogue_turn(task_id, 3, "user", "Turn 3 command")
    t3_r = temp_ledger.record_dialogue_turn(task_id, 3, "assistant", "Turn 3 result")

    # 2. Record tool calls for turn 2 and turn 3
    c1 = temp_ledger.begin_tool_call(
        task_id=task_id,
        attempt_id="att_1",
        step_id="turn_2",
        sequence_in_step=0,
        tool_id="read_file",
        risk_class="R1",
        canonical_args_hash="hash1",
        arguments_redacted={"path": "a.txt"},
        policy_decision="ALLOW",
        idempotency_key="idem1",
    )
    temp_ledger.seal_tool_call(c1, {"output": "contents"}, status="SEALED")

    c2 = temp_ledger.begin_tool_call(
        task_id=task_id,
        attempt_id="att_1",
        step_id="turn_3",
        sequence_in_step=0,
        tool_id="write_file",
        risk_class="R2",
        canonical_args_hash="hash2",
        arguments_redacted={"path": "b.txt"},
        policy_decision="ALLOW",
        idempotency_key="idem2",
    )
    temp_ledger.seal_tool_call(c2, {"output": "written"}, status="SEALED")

    # Verify tool calls are SEALED before rewind
    tc_rows = temp_ledger.conn.execute("SELECT call_id, status FROM tool_calls WHERE task_id = ?", (task_id,)).fetchall()
    assert all(r["status"] == "SEALED" for r in tc_rows)

    # 3. Perform rewind to turn 1
    manager = TimelineRewindManager(ledger=temp_ledger)
    res = manager.rewind(
        task_id=task_id,
        target=1,
        messages=[
            {"role": "user", "content": "Turn 1 command", "turn_index": 1},
            {"role": "assistant", "content": "Turn 1 result", "turn_index": 1},
            {"role": "user", "content": "Turn 2 command", "turn_index": 2},
            {"role": "assistant", "content": "Turn 2 result", "turn_index": 2},
            {"role": "user", "content": "Turn 3 command", "turn_index": 3},
            {"role": "assistant", "content": "Turn 3 result", "turn_index": 3},
        ],
    )

    assert res.target_turn == 1
    assert res.turns_rewound == 2
    assert res.reverted_tool_count == 2

    # 4. Verify dialogue turns in ledger: turn 1 is active, turns 2 & 3 are inactive under branch_id
    active_turns = temp_ledger.get_active_dialogue(task_id)
    assert len(active_turns) == 2
    assert all(t["turn_index"] == 1 for t in active_turns)
    assert all(t["is_active"] == 1 for t in active_turns)

    all_turns = temp_ledger.get_all_dialogue_turns(task_id)
    assert len(all_turns) == 6
    inactive_turns = [t for t in all_turns if t["is_active"] == 0]
    assert len(inactive_turns) == 4
    assert all(t["branch_id"] == res.branch_id for t in inactive_turns)

    # 5. Verify tool calls are marked REVERTED
    tc_rows_after = temp_ledger.conn.execute(
        "SELECT call_id, step_id, status FROM tool_calls WHERE task_id = ?", (task_id,)
    ).fetchall()
    for row in tc_rows_after:
        if row["step_id"] in ("turn_2", "turn_3"):
            assert row["status"] == "REVERTED"

    # 6. Verify cryptographic hash chain remains 100% valid
    chain_report = temp_ledger.verify_chain_integrity(task_id)
    assert chain_report.valid is True
    assert chain_report.verified_events >= 2  # TASK creation + TIMELINE_REWOUND


def test_react_loop_rewind_to(temp_ledger: LedgerStore):
    task_id = "tsk_react_rewind"
    temp_ledger.ensure_task_exists(task_id)

    loop = ReActLoop(
        config=ReActConfig(system_prompt="You are HEX"),
        ledger=temp_ledger,
    )
    loop.active_task_id = task_id
    loop.messages = [
        {"role": "system", "content": "You are HEX"},
        {"role": "user", "content": "Run inspection"},
        {"role": "assistant", "content": "Inspecting..."},
        {"role": "user", "content": "Run fix"},
        {"role": "assistant", "content": "Fixing..."},
    ]
    loop.turns = [
        ReActTurn(turn_index=1, thought="Inspecting..."),
        ReActTurn(turn_index=2, thought="Fixing..."),
    ]

    active_msgs = loop.rewind_to("-1")
    assert len(active_msgs) == 3
    assert active_msgs[-1]["content"] == "Inspecting..."
    assert len(loop.turns) == 1
    assert loop.turns[0].turn_index == 1


def test_hex_console_rewind_slash_command(temp_ledger: LedgerStore, tmp_path: Path):
    from harness_core.client.tui import HexConsole
    from harness_core.core.orchestrator import HarnessOrchestrator

    orchestrator = HarnessOrchestrator(data_dir=tmp_path / "orch_data")
    orchestrator.ledger = temp_ledger
    console = HexConsole(orchestrator=orchestrator)
    task_id = "tsk_console_rewind"
    temp_ledger.ensure_task_exists(task_id)
    console.active_task_id = task_id

    console.messages = [
        {"role": "system", "content": "System directives"},
        {"role": "user", "content": "Turn 1 prompt"},
        {"role": "assistant", "content": "Turn 1 response"},
        {"role": "user", "content": "Turn 2 prompt"},
        {"role": "assistant", "content": "Turn 2 response"},
    ]

    should_exit = console.handle_slash_command("/rewind -1")
    assert should_exit is False
    assert len(console.messages) == 3
    assert console.messages[-1]["content"] == "Turn 1 response"

    # Verify command also works with /undo alias
    should_exit_undo = console.handle_slash_command("/undo -1")
    assert should_exit_undo is False
    assert len(console.messages) == 1  # Clamped to root (turn 0 system)
