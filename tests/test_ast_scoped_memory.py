"""Tests for AST-Scoped Curated Memory, JIT Observation Injection & KV-Cache Immutability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_core.graphify import ActiveGraphifyAdapter
from harness_core.memory import (
    ASTNodeMemoryStore,
    GuardrailsManager,
    compute_code_fingerprint,
    format_jit_memories_block,
    is_fingerprint_stale,
    query_jit_memories_for_file,
)


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
