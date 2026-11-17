"""Unit and integration tests for CuratedMemoryStore, MemorySnapshot, and StreamingContextScrubber."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_core.cognition.memory import (
    ENTRY_DELIMITER,
    GUARDRAIL_CHAR_LIMIT,
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    CuratedMemoryStore,
    GuardrailsManager,
    MemorySnapshot,
    StreamingContextScrubber,
    _drop_repeated_recall_lines,
    _find_unique_match,
    _normalize_for_match,
    sanitize_context,
)


class TestCuratedMemoryStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.reset_cache()

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_curated_store_add_and_list(self):
        res = self.store.add("Prefers pytest over unittest", target="user")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "add")

        res_mem = self.store.add("Architecture uses dual SQLite/Postgres engine", target="memory")
        self.assertTrue(res_mem["ok"])

        # Check list
        listing = self.store.list_entries(target="user")
        self.assertTrue(listing["ok"])
        entries = listing["targets"]["user"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0], "Prefers pytest over unittest")

    def test_curated_store_char_limits(self):
        # Exceed USER_CHAR_LIMIT (1375)
        big_text = "A" * (USER_CHAR_LIMIT + 10)
        res = self.store.add(big_text, target="user")
        self.assertFalse(res["ok"])
        self.assertIn("exceed", res["error"])

        # Below limit works
        valid_text = "B" * 500
        res2 = self.store.add(valid_text, target="user")
        self.assertTrue(res2["ok"])

        # Adding another 1000 should exceed 1375
        another_text = "C" * 1000
        res3 = self.store.add(another_text, target="user")
        self.assertFalse(res3["ok"])
        self.assertIn("exceed", res3["error"])

    def test_curated_store_replace_and_tolerant_matching(self):
        # Add entry with smart quotes and em-dash
        self.store.add("Always use “fast-mode” — avoids timeout", target="memory")

        # Replace using regular straight quotes and hyphen
        res = self.store.replace(
            old_text='Always use "fast-mode" - avoids timeout',
            new_text="Always use turbo-mode for fast builds",
            target="memory",
        )
        self.assertTrue(res["ok"], f"Failed with: {res.get('error')}")

        entries = self.store.read_entries("memory")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0], "Always use turbo-mode for fast builds")

    def test_curated_store_patch(self):
        self.store.add("Database port is 5432 and timeout is 30s", target="memory")

        # Patch just the port
        res = self.store.patch("5432", "5433", target="memory")
        self.assertTrue(res["ok"])

        entries = self.store.read_entries("memory")
        self.assertEqual(entries[0], "Database port is 5433 and timeout is 30s")

        # Repeated span patch ambiguity
        self.store.add("foo bar foo baz", target="memory")
        res_ambig = self.store.patch("foo", "qux", target="memory")
        self.assertFalse(res_ambig["ok"])
        self.assertIn("appears 2 times", res_ambig["error"])

    def test_curated_store_remove(self):
        self.store.add("Note 1", target="memory")
        self.store.add("Note 2", target="memory")
        self.assertEqual(len(self.store.read_entries("memory")), 2)

        res = self.store.remove("Note 1", target="memory")
        self.assertTrue(res["ok"])
        remaining = self.store.read_entries("memory")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0], "Note 2")

    def test_curated_store_drift_detection(self):
        # Manually write an un-delimited oversized entry directly to disk
        path = self.store.get_path_for_target("memory")
        path.parent.mkdir(parents=True, exist_ok=True)
        oversized = "X" * (MEMORY_CHAR_LIMIT + 500)
        path.write_text(oversized, encoding="utf-8")

        # Mutation should detect drift, write .bak snapshot, and refuse write
        res = self.store.add("New note", target="memory")
        self.assertFalse(res["ok"])
        self.assertIn("drift", res["error"].lower())
        self.assertTrue(Path(res["drift_backup"]).is_file())


class TestStreamingContextScrubber(unittest.TestCase):
    def test_scrubber_passthrough(self):
        scrubber = StreamingContextScrubber()
        out = scrubber.feed("Hello world! How are you?") + scrubber.flush()
        self.assertEqual(out, "Hello world! How are you?")

    def test_scrubber_strips_full_tag(self):
        scrubber = StreamingContextScrubber()
        raw = "Hello\n<memory-context>\nSystem notes here\n</memory-context>\nWorld!"
        out = scrubber.feed(raw) + scrubber.flush()
        self.assertEqual(out.strip(), "Hello\n\nWorld!")

    def test_scrubber_chunked_straddling(self):
        scrubber = StreamingContextScrubber()
        chunks = [
            "Before memory block.\n",
            "<memo",
            "ry-context>\n",
            "Sensitive internal notes line 1\n",
            "Sensitive line 2\n",
            "</memory-",
            "context>\n",
            "After memory block.",
        ]
        emitted = []
        for chunk in chunks:
            res = scrubber.feed(chunk)
            if res:
                emitted.append(res)
        res_flush = scrubber.flush()
        if res_flush:
            emitted.append(res_flush)

        combined = "".join(emitted)
        self.assertNotIn("Sensitive", combined)
        self.assertNotIn("memory-context", combined)
        self.assertIn("Before memory block.", combined)
        self.assertIn("After memory block.", combined)


class TestFrozenSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.reset_cache()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_frozen_snapshot_assembly_and_deduplication(self):
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)
        store.add("- prefers pnpm", target="user")
        store.add("- prefers pnpm", target="user")  # duplicate bullet
        store.add("Use PostgreSQL for ledgers", target="memory")

        snap = MemorySnapshot.build_frozen_snapshot(self.workspace, self.global_home)
        self.assertIn("<memory-context>", snap)
        self.assertIn("</memory-context>", snap)
        self.assertIn("prefers pnpm", snap)
        self.assertIn("Use PostgreSQL for ledgers", snap)

        # Confirm bullet deduplicated
        self.assertEqual(snap.count("- prefers pnpm"), 1)

    def test_frozen_snapshot_immutability_mid_session(self):
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)
        store.add("Initial note", target="memory")

        # Session start Turn 0: build frozen snapshot
        snap1 = store.build_frozen_snapshot()
        self.assertIn("Initial note", snap1)

        # Mid-session tool write updates disk
        store.add("Mid-session added fact", target="memory")
        disk_entries = store.read_entries("memory")
        self.assertEqual(len(disk_entries), 2)
        self.assertIn("Mid-session added fact", disk_entries)

        # Frozen snapshot must remain 100% byte-identical for KV-cache preservation!
        snap2 = store.build_frozen_snapshot()
        self.assertEqual(snap1, snap2)
        self.assertNotIn("Mid-session added fact", snap2)


class TestGuardrailsMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.mgr = GuardrailsManager(workspace_root=self.workspace)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_guardrails_record_and_list(self):
        self.mgr.record_avoid_invariant("Do not use synchronous exec in async loop")
        rules = self.mgr.list_guardrails()
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0].startswith("- [AVOID] "))
        self.assertIn("synchronous exec", rules[0])

        formatted = self.mgr.format_guardrails()
        self.assertIn("## Negative Constraints", formatted)
        self.assertIn("- [AVOID]", formatted)

    def test_guardrails_deduplication(self):
        self.mgr.record_avoid_invariant("- [AVOID] Do not delete database tables directly")
        self.mgr.record_avoid_invariant("Do not delete database tables directly")
        rules = self.mgr.list_guardrails()
        self.assertEqual(len(rules), 1)

    def test_guardrails_fifo_eviction(self):
        small_mgr = GuardrailsManager(workspace_root=self.workspace, char_limit=180)
        for i in range(10):
            small_mgr.record_avoid_invariant(f"Rule number {i}: never do action {i}")

        rules = small_mgr.list_guardrails()
        # Ensure FIFO dropped older rules
        self.assertNotIn("Rule number 0", " ".join(rules))
        self.assertIn("Rule number 9", " ".join(rules))
        total_chars = len(ENTRY_DELIMITER.join(rules))
        self.assertLessEqual(total_chars, 180)


class TestReActLoopGuardrailsIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_guardrails_auto_capture_on_repeat_failure(self):
        from harness_core.core.react_loop import ReActConfig, ReActLoop, ToolCallRequest

        guardrails_mgr = GuardrailsManager(workspace_root=self.workspace)

        class MockGateway:
            def invoke(self, name: str, args: dict, context: Any = None):
                return {
                    "ok": False,
                    "error": "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
                }

        turn_count = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal turn_count
            turn_count += 1
            if turn_count <= 2:
                return (
                    '<thought>Calling test tool</thought>\n'
                    '<tool_call>{"name": "broken_tool", "arguments": {"command": "calc"}}</tool_call>'
                )
            return "<finish>Done after failure</finish>"

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=MockGateway(),
            workspace_root=self.workspace,
            guardrails_manager=guardrails_mgr,
            config=ReActConfig(max_turns=5),
        )

        res = loop.run("Test repeated failure")
        self.assertTrue(res.success)

        # Check that GUARDRAILS.md contains the synthesized invariant
        guardrails = guardrails_mgr.list_guardrails()
        self.assertGreaterEqual(len(guardrails), 1)
        self.assertTrue(any("broken_tool" in r and "TypeError" in r for r in guardrails))

    def test_guardrails_ignore_transient_errors(self):
        from harness_core.core.react_loop import ReActConfig, ReActLoop

        guardrails_mgr = GuardrailsManager(workspace_root=self.workspace)

        class TransientGateway:
            def invoke(self, name: str, args: dict, context: Any = None):
                return {
                    "ok": False,
                    "error": "HTTP 429: Rate limit exceeded, retry in 30 seconds",
                }

        turn_count = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal turn_count
            turn_count += 1
            if turn_count <= 2:
                return '<tool_call>{"name": "cloud_api", "arguments": {}}</tool_call>'
            return "<finish>Done</finish>"

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=TransientGateway(),
            workspace_root=self.workspace,
            guardrails_manager=guardrails_mgr,
            config=ReActConfig(max_turns=5),
        )

        loop.run("Test transient errors")
        guardrails = guardrails_mgr.list_guardrails()
        self.assertEqual(len(guardrails), 0, "Transient errors must not be saved into guardrails")

    def test_guardrails_rejected_diff_and_rollback(self):
        from harness_core.core.react_loop import ReActLoop

        guardrails_mgr = GuardrailsManager(workspace_root=self.workspace)
        loop = ReActLoop(workspace_root=self.workspace, guardrails_manager=guardrails_mgr)

        loop.record_rejected_diff("src/auth.py", "breaks existing JWT signature format")
        loop.record_rollback("snap_forensic_01", "corrupted migration file")

        guardrails = guardrails_mgr.list_guardrails()
        self.assertEqual(len(guardrails), 2)
        self.assertTrue(any("Rejected change on src/auth.py" in r for r in guardrails))
        self.assertTrue(any("Rolled back workspace from snap_forensic_01" in r for r in guardrails))


class TestGatewayMemoryIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        self._old_hex_home = os.environ.get("HEX_HOME")
        os.environ["HEX_HOME"] = str(self.global_home)
        MemorySnapshot.reset_cache()

    def tearDown(self):
        if self._old_hex_home is not None:
            os.environ["HEX_HOME"] = self._old_hex_home
        else:
            os.environ.pop("HEX_HOME", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_gateway_tool_memory_crud_and_batch(self):
        from unittest.mock import MagicMock
        from harness_core.gateway import ToolGateway

        mock_ledger = MagicMock()
        gw = ToolGateway(ledger=mock_ledger)

        # 1. Add to user store
        res_add_user = gw._tool_memory(
            self.workspace,
            {"action": "add", "target": "user", "content": "Prefers TypeScript for frontends"},
        )
        self.assertTrue(res_add_user["ok"])

        # 2. Add to project memory
        res_add_mem = gw._tool_memory(
            self.workspace,
            {"action": "add", "target": "memory", "content": "Port is 8080 and host is localhost"},
        )
        self.assertTrue(res_add_mem["ok"])

        # 3. Patch entry in project memory
        res_patch = gw._tool_memory(
            self.workspace,
            {"action": "patch", "target": "memory", "old_text": "8080", "new_text": "9090"},
        )
        self.assertTrue(res_patch["ok"])

        # 4. Replace entry in user store
        res_replace = gw._tool_memory(
            self.workspace,
            {
                "action": "replace",
                "target": "user",
                "old_text": "Prefers TypeScript",
                "new_text": "Prefers TypeScript and Vue 3",
            },
        )
        self.assertTrue(res_replace["ok"])

        # 5. List entries
        res_list = gw._tool_memory(self.workspace, {"action": "list", "target": "all"})
        self.assertTrue(res_list["ok"])
        self.assertEqual(len(res_list["targets"]["user"]["entries"]), 1)
        self.assertIn("Vue 3", res_list["targets"]["user"]["entries"][0])
        self.assertIn("9090", res_list["targets"]["memory"]["entries"][0])

        # 6. Batch operations
        batch_ops = [
            {"action": "add", "target": "guardrails", "content": "- [AVOID] Do not commit credentials"},
            {"action": "remove", "target": "memory", "old_text": "Port is 9090"},
        ]
        res_batch = gw._tool_memory(self.workspace, {"operations": batch_ops})
        self.assertTrue(res_batch["ok"])

        res_check = gw._tool_memory(self.workspace, {"action": "list", "target": "all"})
        self.assertEqual(len(res_check["targets"]["memory"]["entries"]), 0)
        self.assertEqual(len(res_check["targets"]["guardrails"]["entries"]), 1)


class TestPromptBuilderKVStability(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.reset_cache()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_prompt_builder_slot4_memory_and_prefix_hash_stability(self):
        import hashlib
        from harness_core.cognition.prompt_builder import PromptBuilder
        from harness_core.cognition.soul import SoulManager

        soul_mgr = SoulManager(global_home=self.global_home)
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)

        # Seed initial memories
        store.add("- prefers clean code without unnecessary dependencies", target="user")
        store.add("Project uses SQLite WAL mode for concurrency", target="memory")
        store.add("- [AVOID] Do not run destructive drop database commands", target="guardrails")

        builder = PromptBuilder(soul_manager=soul_mgr)

        # Turn 0 prompt assembly
        prompt_t0 = builder.build_system_prompt(workspace_root=self.workspace, mode="agent")
        self.assertIn("<memory-context>", prompt_t0)
        self.assertIn("</memory-context>", prompt_t0)
        self.assertIn("prefers clean code", prompt_t0)
        self.assertIn("SQLite WAL mode", prompt_t0)
        self.assertIn("[AVOID] Do not run destructive", prompt_t0)

        hash_t0 = hashlib.sha256(prompt_t0.encode("utf-8")).hexdigest()

        # Mid-session mutation directly to disk
        store.add("Mid-session dynamically learned trivia", target="memory")
        self.assertEqual(len(store.read_entries("memory")), 2)

        # Turn 1 prompt assembly for the same session
        prompt_t1 = builder.build_system_prompt(workspace_root=self.workspace, mode="agent")
        hash_t1 = hashlib.sha256(prompt_t1.encode("utf-8")).hexdigest()

        # KV-cache prefix guarantee: Prompt hash must be 100% byte-identical!
        self.assertEqual(hash_t0, hash_t1)
        self.assertEqual(prompt_t0, prompt_t1)
        self.assertNotIn("Mid-session dynamically learned trivia", prompt_t1)


if __name__ == "__main__":
    unittest.main()
