"""Tests for AST-Scoped Curated Memory, JIT Observation Injection & KV-Cache Immutability."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness_core.storage.db import LedgerStore
from harness_core.gateway import ToolGateway
from harness_core.graphify import ActiveGraphifyAdapter
from harness_core.cognition.memory import (
    ASTNodeMemoryStore,
    GuardrailsManager,
    compute_code_fingerprint,
    format_jit_memories_block,
    is_fingerprint_stale,
    query_jit_memories_for_file,
)
from harness_core.core.react_loop import ReActConfig, ReActLoop


class TestASTNodeMemoryStore(unittest.TestCase):
    """Test ASTNodeMemoryStore CRUD, fingerprinting, and staleness detection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ast_node_memory_crud(self) -> None:
        store = ASTNodeMemoryStore(workspace_root=self.ws)

        # Create a sample file
        src_file = self.ws / "service.py"
        src_file.write_text("def process_data(payload):\n    return payload.strip()\n")

        # Record a memory on symbol
        mem = store.record_node_memory(
            symbol_name="process_data",
            rel_path="service.py",
            note="Payload must be sanitized before processing.",
            category="gotcha",
        )

        self.assertTrue(mem.id.startswith("ast_mem_"))
        self.assertEqual(mem.symbol_name, "process_data")
        self.assertEqual(mem.rel_path, "service.py")
        self.assertEqual(mem.category, "gotcha")
        self.assertTrue(len(mem.code_fingerprint) == 64)

        # Query by symbol
        by_sym = store.get_node_memories("process_data")
        self.assertEqual(len(by_sym), 1)
        self.assertEqual(by_sym[0].id, mem.id)

        # Query by path
        by_path = store.get_node_memories("service.py")
        self.assertEqual(len(by_path), 1)
        self.assertEqual(by_path[0].id, mem.id)

        # List all
        all_mems = store.list_all_node_memories()
        self.assertEqual(len(all_mems), 1)

        # Persistence test
        fresh_store = ASTNodeMemoryStore(workspace_root=self.ws)
        reloaded = fresh_store.get_node_memories("process_data")
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].note, "Payload must be sanitized before processing.")

        # Delete
        self.assertTrue(store.delete_node_memory(mem.id))
        self.assertEqual(len(store.list_all_node_memories()), 0)

    def test_ast_node_memory_staleness(self) -> None:
        store = ASTNodeMemoryStore(workspace_root=self.ws)

        src_file = self.ws / "worker.py"
        src_file.write_text("def do_work():\n    return 42\n")

        mem = store.record_node_memory(
            symbol_name="do_work",
            rel_path="worker.py",
            note="Return value must be 42.",
            category="invariant",
        )

        # Initially fresh
        self.assertFalse(store.is_memory_stale(mem))
        self.assertFalse(is_fingerprint_stale(self.ws, "worker.py", mem.code_fingerprint))

        # Mutate the file
        src_file.write_text("def do_work():\n    return 999  # mutated\n")

        # Now stale
        self.assertTrue(store.is_memory_stale(mem))
        self.assertTrue(is_fingerprint_stale(self.ws, "worker.py", mem.code_fingerprint))


class TestAST1HopResolution(unittest.TestCase):
    """Test 1-hop caller/callee resolution and staleness validation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_1hop_resolution_callers_and_callees(self) -> None:
        # Create calc.py (callee) and app.py (caller)
        calc_file = self.ws / "calc.py"
        calc_file.write_text(
            "def multiply(a, b):\n"
            "    return a * b\n"
        )

        app_file = self.ws / "app.py"
        app_file.write_text(
            "from calc import multiply\n\n"
            "def calculate_total(qty, price):\n"
            "    return multiply(qty, price)\n"
        )

        # Build graphify adapter
        graphify = ActiveGraphifyAdapter(self.ws)
        graphify.build_or_refresh(self.ws)

        # Record memories
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        store.record_node_memory(
            symbol_name="multiply",
            rel_path="calc.py",
            note="May overflow on unbounded integer products.",
            category="gotcha",
        )
        store.record_node_memory(
            symbol_name="calculate_total",
            rel_path="app.py",
            note="Must receive non-negative integer values.",
            category="invariant",
        )

        # Add guardrail
        gm = GuardrailsManager(workspace_root=self.ws)
        gm.record_avoid_invariant("- [AVOID] Do not invoke multiply with negative factors")

        # 1. Query for calc.py
        # Should find:
        # - Direct Gotcha on multiply
        # - 1-Hop Caller Warning (calculate_total -> multiply)
        # - Guardrail mentioning multiply
        calc_mems = query_jit_memories_for_file(self.ws, "calc.py", graphify_adapter=graphify)
        self.assertTrue(any(m["type"] == "gotcha" and "multiply" in m["formatted"] for m in calc_mems))
        self.assertTrue(any(m["type"] == "caller_warning" and "calculate_total" in m["formatted"] for m in calc_mems))
        self.assertTrue(any(m["type"] == "guardrail" and "multiply" in m["formatted"] for m in calc_mems))

        calc_block = format_jit_memories_block(calc_mems, "calc.py")
        self.assertIn("[AST Memory & Invariants for calc.py]", calc_block)
        self.assertIn("- Gotcha (multiply): May overflow on unbounded integer products.", calc_block)
        self.assertIn("- 1-Hop Caller Warning (calculate_total -> multiply):", calc_block)
        self.assertIn("- Guardrail: [AVOID] Do not invoke multiply with negative factors", calc_block)

        # 2. Query for app.py
        # Should find:
        # - Direct Invariant on calculate_total
        # - 1-Hop Callee Warning (calculate_total -> multiply)
        app_mems = query_jit_memories_for_file(self.ws, "app.py", graphify_adapter=graphify)
        self.assertTrue(any(m["type"] == "invariant" and "calculate_total" in m["formatted"] for m in app_mems))
        self.assertTrue(any(m["type"] == "callee_warning" and "multiply" in m["formatted"] for m in app_mems))

        app_block = format_jit_memories_block(app_mems, "app.py")
        self.assertIn("[AST Memory & Invariants for app.py]", app_block)
        self.assertIn("- Invariant (calculate_total): Must receive non-negative integer values.", app_block)
        self.assertIn("- 1-Hop Callee Warning (calculate_total -> multiply):", app_block)

        # 3. Staleness detection: mutate calc.py
        calc_file.write_text(
            "def multiply(a, b):\n"
            "    # mutated body\n"
            "    return int(a) * int(b)\n"
        )

        # With include_stale=False: Gotcha on multiply is omitted
        fresh_mems = query_jit_memories_for_file(self.ws, "calc.py", graphify_adapter=graphify, include_stale=False)
        self.assertFalse(any(m["type"] == "gotcha" and m["symbol"] == "multiply" for m in fresh_mems))

        # With include_stale=True: Gotcha on multiply is included and marked stale
        stale_mems = query_jit_memories_for_file(self.ws, "calc.py", graphify_adapter=graphify, include_stale=True)
        gotcha_m = [m for m in stale_mems if m["type"] == "gotcha" and m["symbol"] == "multiply"][0]
        self.assertTrue(gotcha_m["is_stale"])
        self.assertIn("[STALE]", gotcha_m["formatted"])


class TestJITObservationInjection(unittest.TestCase):
    """Test JIT AST memory injection into read_file tool observations and KV-cache prompt stability."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.db_path = self.ws / "ledger.db"
        self.ledger = LedgerStore(db_path=self.db_path)
        self.graphify = ActiveGraphifyAdapter(self.ws)
        self.gateway = ToolGateway(ledger=self.ledger, graphify=self.graphify)

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def test_read_file_with_attached_ast_memory_injected(self) -> None:
        code_file = self.ws / "auth.py"
        code_file.write_text("def verify_token(token):\n    return token == 'secret'\n")

        self.graphify.build_or_refresh(self.ws)

        store = ASTNodeMemoryStore(workspace_root=self.ws)
        store.record_node_memory(
            symbol_name="verify_token",
            rel_path="auth.py",
            note="Use constant-time comparison to prevent timing attacks.",
            category="gotcha",
        )

        res = self.gateway._tool_read_file(self.ws, {"path": "auth.py"})
        self.assertTrue(res["ok"])
        self.assertIn("ast_memories", res)
        self.assertEqual(len(res["ast_memories"]), 1)

        # Content must contain the original file content AND the injected block
        content = res["content"]
        self.assertIn("def verify_token(token):", content)
        self.assertIn("[AST Memory & Invariants for auth.py]", content)
        self.assertIn("- Gotcha (verify_token): Use constant-time comparison to prevent timing attacks.", content)

    def test_read_file_connected_via_1hop_call_surfaces_caller_callee_invariant(self) -> None:
        # Create core.py (callee) and handler.py (caller)
        core_file = self.ws / "core.py"
        core_file.write_text("def execute_query(sql):\n    return f'Executing {sql}'\n")

        handler_file = self.ws / "handler.py"
        handler_file.write_text(
            "from core import execute_query\n\n"
            "def handle_request(req):\n"
            "    return execute_query(req['query'])\n"
        )

        self.graphify.build_or_refresh(self.ws)

        store = ASTNodeMemoryStore(workspace_root=self.ws)
        # Note on handler (caller)
        store.record_node_memory(
            symbol_name="handle_request",
            rel_path="handler.py",
            note="Request payload must be schema-validated prior to SQL dispatch.",
            category="invariant",
        )
        # Note on execute_query (callee)
        store.record_node_memory(
            symbol_name="execute_query",
            rel_path="core.py",
            note="Direct raw SQL invocation strictly forbidden.",
            category="gotcha",
        )

        # 1. Reading core.py should surface 1-hop caller warning for handle_request -> execute_query
        core_res = self.gateway._tool_read_file(self.ws, {"path": "core.py"})
        self.assertTrue(core_res["ok"])
        self.assertIn("def execute_query(sql):", core_res["content"])
        self.assertIn("- Gotcha (execute_query): Direct raw SQL invocation strictly forbidden.", core_res["content"])
        self.assertIn("- 1-Hop Caller Warning (handle_request -> execute_query):", core_res["content"])

        # 2. Reading handler.py should surface 1-hop callee warning for handle_request -> execute_query
        handler_res = self.gateway._tool_read_file(self.ws, {"path": "handler.py"})
        self.assertTrue(handler_res["ok"])
        self.assertIn("def handle_request(req):", handler_res["content"])
        self.assertIn("- Invariant (handle_request): Request payload must be schema-validated prior to SQL dispatch.", handler_res["content"])
        self.assertIn("- 1-Hop Callee Warning (handle_request -> execute_query):", handler_res["content"])

    def test_read_file_unannotated_returns_unmodified_content(self) -> None:
        plain_file = self.ws / "plain.txt"
        plain_content = "This is a clean unannotated file with zero memories."
        plain_file.write_text(plain_content)

        res = self.gateway._tool_read_file(self.ws, {"path": "plain.txt"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["content"], plain_content)
        self.assertNotIn("ast_memories", res)
        self.assertNotIn("[AST Memory & Invariants", res["content"])

    def test_code_staleness_invalidation(self) -> None:
        worker_file = self.ws / "job.py"
        worker_file.write_text("def run_job():\n    return 'initial'\n")
        self.graphify.build_or_refresh(self.ws)

        store = ASTNodeMemoryStore(workspace_root=self.ws)
        store.record_node_memory(
            symbol_name="run_job",
            rel_path="job.py",
            note="Initial implementation gotcha note.",
            category="gotcha",
        )

        # Fresh read includes gotcha
        res1 = self.gateway._tool_read_file(self.ws, {"path": "job.py"})
        self.assertIn("- Gotcha (run_job): Initial implementation gotcha note.", res1["content"])

        # Edit/mutate source file
        worker_file.write_text("def run_job():\n    return 'completely_rewritten'\n")

        # Second read detects mutated fingerprint and omits stale gotcha
        res2 = self.gateway._tool_read_file(self.ws, {"path": "job.py"})
        self.assertNotIn("- Gotcha (run_job): Initial implementation gotcha note.", res2["content"])
        self.assertNotIn("ast_memories", res2)

    def test_prompt_cache_prefix_stability_across_turns(self) -> None:
        """Verify system prompt string and SHA-256 remain 100% frozen across multiple turns."""
        # Setup files and memory
        target_file = self.ws / "target.py"
        target_file.write_text("def important_function():\n    return 'data'\n")
        self.graphify.build_or_refresh(self.ws)

        store = ASTNodeMemoryStore(workspace_root=self.ws)
        store.record_node_memory(
            symbol_name="important_function",
            rel_path="target.py",
            note="Must never be called in multi-threaded context without GIL.",
            category="gotcha",
        )

        system_instruction = (
            "You are the HEX Autonomous Cognitive Workbench.\n"
            "All responses must adhere to strict invariant validation.\n"
            "Execute tools with deterministic precision."
        )
        initial_hash = hashlib.sha256(system_instruction.encode("utf-8")).hexdigest()

        step = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal step
            # Verify system prompt in messages[0] has not drifted at any turn
            assert messages[0]["role"] == "system"
            assert messages[0]["content"] == system_instruction
            assert hashlib.sha256(messages[0]["content"].encode("utf-8")).hexdigest() == initial_hash

            if step == 0:
                step += 1
                return '<tool_call>{"name": "read_file", "arguments": {"path": "target.py"}}</tool_call>'
            elif step == 1:
                step += 1
                return '<tool_call>{"name": "list_files", "arguments": {}}</tool_call>'
            elif step == 2:
                step += 1
                return '<tool_call>{"name": "read_file", "arguments": {"path": "target.py"}}</tool_call>'
            elif step == 3:
                step += 1
                return '<tool_call>{"name": "write_file", "arguments": {"path": "out.txt", "content": "done"}}</tool_call>'
            elif step == 4:
                step += 1
                return "<finish>Session successfully completed after 5 turns.</finish>"
            return "<finish>Done</finish>"

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(
                max_turns=6,
                system_prompt=system_instruction,
            ),
        )

        # Assert config prompt matches initial hash before turn 1
        self.assertEqual(
            hashlib.sha256(loop.config.system_prompt.encode("utf-8")).hexdigest(),
            initial_hash,
        )

        session = loop.run("Inspect and update repository")
        self.assertTrue(session.success)
        self.assertEqual(session.total_turns, 5)

        # CRUCIAL CHECK: Verify system prompt string and SHA-256 after turn 5 remain 100% byte-identical
        final_hash = hashlib.sha256(loop.config.system_prompt.encode("utf-8")).hexdigest()
        self.assertEqual(initial_hash, final_hash)
        self.assertEqual(loop.config.system_prompt, system_instruction)

        # Verify messages[0] remained 100% frozen
        self.assertEqual(loop.messages[0]["role"], "system")
        self.assertEqual(loop.messages[0]["content"], system_instruction)
        self.assertEqual(hashlib.sha256(loop.messages[0]["content"].encode("utf-8")).hexdigest(), initial_hash)

        # Verify the injected AST memory block appears in tool observations, NOT in the system prompt
        tool_observations = [
            m.get("content", "")
            for m in loop.messages
            if m.get("role") == "tool"
        ]
        self.assertTrue(any("Must never be called in multi-threaded context" in obs for obs in tool_observations))
        self.assertNotIn("Must never be called in multi-threaded context", loop.messages[0]["content"])
        self.assertNotIn("Must never be called in multi-threaded context", loop.config.system_prompt)
