"""Comprehensive test suite for Asynchronous Background Session Title Synthesis.

Verifies:
1. Instant derived title extraction (markdown stripping, punctuation trimming, <= 48 chars).
2. Anti-chatter & anti-hallucination validation guards (word counts, CJK limits, answer-shaped rejection, echo guards).
3. Title provenance hierarchy in LedgerStore (USER > LLM > DERIVED).
4. Asynchronous non-blocking background synthesis with mock model caller.
5. ReActLoop integration with instant title seeding and turn 1 background upgrade.
6. Start screen resume list visibility and query retrieval.
"""

from __future__ import annotations

import time
from pathlib import Path
import pytest

from harness_core.storage.db import LedgerStore
from harness_core.cognition.title_synthesizer import (
    TitleProvenance,
    derive_instant_title,
    validate_synthesized_title,
    SessionTitleSynthesizer,
)
from harness_core.core.react_loop import ReActLoop, ReActConfig


@pytest.fixture
def temp_ledger(tmp_path: Path) -> LedgerStore:
    db_file = tmp_path / "test_titles.db"
    return LedgerStore(f"sqlite:///{db_file}")


def test_derive_instant_title_clean_cases():
    # 1. Clean single line prompt
    assert derive_instant_title("Implement user authentication with JWT") == "Implement user authentication with JWT"

    # 2. Markdown headers and whitespace
    assert derive_instant_title("### Database Schema Migration\nRun alembic upgrade head\nAdd tables") == "Database Schema Migration"

    # 3. Bullets and numbers
    assert derive_instant_title("- Fix memory leak in buffer pool") == "Fix memory leak in buffer pool"
    assert derive_instant_title("1. Create new REST endpoint for metrics") == "Create new REST endpoint for metrics"

    # 4. Surrounding quotes and trailing punctuation
    assert derive_instant_title('"Optimize SQL query performance!"') == "Optimize SQL query performance"

    # 5. Empty or whitespace
    assert derive_instant_title("") == "New Session"
    assert derive_instant_title("   \n\n  \t  ") == "New Session"


def test_derive_instant_title_word_boundary_truncation():
    # A very long opening sentence that exceeds 48 chars
    long_prompt = "Refactor the distributed consensus protocol to handle network partitions gracefully across regions"
    derived = derive_instant_title(long_prompt, max_chars=48)
    assert len(derived) <= 48
    assert derived.endswith("...")
    # Ensure word boundary preservation (should not cut a word like 'partit...')
    assert not derived.endswith("parti...")


def test_validate_synthesized_title_guards():
    # 1. Valid JSON format
    assert validate_synthesized_title('{"title": "Refactor Memory Pool"}') == "Refactor Memory Pool"
    assert validate_synthesized_title('```json\n{"title": "Deploy API Gateway"}\n```') == "Deploy API Gateway"

    # 2. Plain valid candidate
    assert validate_synthesized_title("Title: Async Worker Dispatch") == "Async Worker Dispatch"
    assert validate_synthesized_title('"Fix Concurrent Lease Race"') == "Fix Concurrent Lease Race"

    # 3. Verbatim prompt instruction echoes rejected
    assert validate_synthesized_title('{"title": "Generate a 3 to 5 word title"}') is None
    assert validate_synthesized_title("3 to 5 word title") is None
    assert validate_synthesized_title("Output JSON: {\"title\": \"...\"}") is None
    assert validate_synthesized_title("Untitled") is None

    # 4. Answer-shaped chat responses rejected
    assert validate_synthesized_title("Yes, I can help you implement that feature.") is None
    assert validate_synthesized_title("Certainly! Here is the revised codebase.") is None
    assert validate_synthesized_title("Here is the plan for building the feature.") is None
    assert validate_synthesized_title("Sure, let's get started on the migration.") is None
    assert validate_synthesized_title("I will now run the test suite for you.") is None

    # 5. Excessive word count rejected (> 12 words)
    long_title = "This is an excessively wordy title candidate that contains far more than twelve words in total length"
    assert validate_synthesized_title(long_title) is None

    # 6. CJK character limit (> 20 chars rejected, <= 20 accepted)
    cjk_valid = "用户认证和权限管理模块实现"  # 13 chars
    assert validate_synthesized_title(cjk_valid) == cjk_valid
    cjk_excessive = "用户认证和权限管理模块实现以及分布式缓存集群的高可用容灾方案设计"  # 32 chars
    assert validate_synthesized_title(cjk_excessive) is None


def test_provenance_hierarchy_in_ledger(temp_ledger: LedgerStore):
    task_id = "tsk_prov_hierarchy"
    temp_ledger.ensure_task_exists(task_id, goal="Initial Goal")

    # 1. Update with DERIVED provenance succeeds
    ok1 = temp_ledger.update_task_goal(task_id, "Instant Derived Title", provenance="derived")
    assert ok1 is True
    row1 = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row1["goal"] == "Instant Derived Title"
    assert row1["title_provenance"] == "derived"

    # 2. Upgrade from DERIVED to LLM succeeds
    ok2 = temp_ledger.update_task_goal(task_id, "Synthesized LLM Title", provenance="llm")
    assert ok2 is True
    row2 = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row2["goal"] == "Synthesized LLM Title"
    assert row2["title_provenance"] == "llm"

    # 3. Overwrite from LLM to USER succeeds
    ok3 = temp_ledger.update_task_goal(task_id, "User Explicit Title", provenance="user")
    assert ok3 is True
    row3 = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row3["goal"] == "User Explicit Title"
    assert row3["title_provenance"] == "user"

    # 4. Attempted downgrade from USER to LLM is BLOCKED
    ok4 = temp_ledger.update_task_goal(task_id, "Sneaky LLM Title", provenance="llm")
    assert ok4 is False
    row4 = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row4["goal"] == "User Explicit Title"  # Preserved!

    # 5. Attempted downgrade from USER to DERIVED is BLOCKED
    ok5 = temp_ledger.update_task_goal(task_id, "Sneaky Derived Title", provenance="derived")
    assert ok5 is False
    row5 = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row5["goal"] == "User Explicit Title"  # Preserved!

    # 6. Verify TASK_TITLE_UPDATED events recorded and hash chain is intact
    chain_rep = temp_ledger.verify_chain_integrity(task_id)
    assert chain_rep.valid is True


def test_session_title_synthesizer_async(temp_ledger: LedgerStore):
    task_id = "tsk_async_synth"
    temp_ledger.ensure_task_exists(task_id)

    def mock_model(prompt: str) -> str:
        return '{"title": "Fast Cache Optimizer"}'

    synthesizer = SessionTitleSynthesizer(model_caller=mock_model, ledger=temp_ledger)

    # 1. Instant title
    instant = synthesizer.ensure_instant_title(task_id, "Build a fast in-memory cache with LRU eviction")
    assert instant == "Build a fast in-memory cache with LRU eviction"

    # 2. Async synthesis with callback
    callback_results = []
    def on_ready(tid: str, title: str) -> None:
        callback_results.append((tid, title))

    thread = synthesizer.synthesize_async(
        task_id=task_id,
        prompt="Build a fast in-memory cache with LRU eviction",
        on_title_ready=on_ready,
        wait=True,
    )
    assert not thread.is_alive()

    # 3. Verify ledger task goal was upgraded
    row = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row["goal"] == "Fast Cache Optimizer"
    assert row["title_provenance"] == "llm"

    # 4. Verify callback was executed
    assert len(callback_results) == 1
    assert callback_results[0] == (task_id, "Fast Cache Optimizer")


def test_non_blocking_behavior(temp_ledger: LedgerStore):
    task_id = "tsk_non_blocking"
    temp_ledger.ensure_task_exists(task_id)

    def slow_model(prompt: str) -> str:
        time.sleep(0.3)
        return '{"title": "Slow Synthesized Title"}'

    synthesizer = SessionTitleSynthesizer(model_caller=slow_model, ledger=temp_ledger)

    t0 = time.perf_counter()
    thread = synthesizer.synthesize_async(
        task_id=task_id,
        prompt="A very complex task requiring deep analysis",
        wait=False,
    )
    elapsed = time.perf_counter() - t0

    # Spawning the background worker must not block (sub-50ms)
    assert elapsed < 0.1
    assert thread.is_alive()

    # Wait for completion and verify result landed
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    row = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row["goal"] == "Slow Synthesized Title"
    assert row["title_provenance"] == "llm"


def test_react_loop_title_synthesis_integration(temp_ledger: LedgerStore):
    task = temp_ledger.submit_task({"goal": "Initial Unrefined Goal"})
    task_id = task["task_id"]

    def model_caller(messages: list[dict] | str) -> str:
        if isinstance(messages, str):
            if "Generate a 3 to 5 word title" in messages:
                return '{"title": "Inspect File Tree"}'
            return "I have inspected the file tree and everything is in order."
        # If model is asked to synthesize title
        for m in messages:
            content = str(m.get("content", "")) if isinstance(m, dict) else str(m)
            if "Generate a 3 to 5 word title" in content:
                return '{"title": "Inspect File Tree"}'
        # Normal ReAct response (turn 1 natural finish)
        return "I have inspected the file tree and everything is in order."

    synthesizer = SessionTitleSynthesizer(model_caller=model_caller, ledger=temp_ledger)
    loop = ReActLoop(
        config=ReActConfig(system_prompt="You are HEX"),
        ledger=temp_ledger,
        model_caller=model_caller,
        title_synthesizer=synthesizer,
    )

    prompt = "Inspect the directory structure of the repository"
    res = loop.run(prompt=prompt, task_id=task_id)
    assert res.success is True

    # Wait for the background daemon thread to finish synthesis
    for t in synthesizer._active_threads:
        t.join(timeout=3.0)

    # Verify that tasks.goal in ledger was updated by the background synthesizer
    row = temp_ledger.conn.execute("SELECT goal, title_provenance FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row["goal"] == "Inspect File Tree"
    assert row["title_provenance"] == "llm"


def test_start_screen_resume_query_retrieval(temp_ledger: LedgerStore):
    # Mimics the exact query executed by hex-tui app.rs Start screen resume list:
    # "SELECT task_id, goal, created_at FROM tasks ORDER BY created_at DESC LIMIT 50;"
    task_a = temp_ledger.submit_task({"goal": "Raw goal A"})
    task_b = temp_ledger.submit_task({"goal": "Raw goal B"})

    temp_ledger.update_task_goal(task_a["task_id"], "Synthesized Title A", provenance="llm")
    temp_ledger.update_task_goal(task_b["task_id"], "User Title B", provenance="user")

    rows = temp_ledger.conn.execute(
        "SELECT task_id, goal, created_at FROM tasks ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    goals_by_id = {r["task_id"]: r["goal"] for r in rows}
    assert goals_by_id[task_a["task_id"]] == "Synthesized Title A"
    assert goals_by_id[task_b["task_id"]] == "User Title B"
