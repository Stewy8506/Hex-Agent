"""Filesystem mixin contracts at the gateway's dispatch and policy boundaries."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core import gateway as gateway_module
from harness_core.core.models import TaskSubmitRequest
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.storage.db import LedgerStore, sha256_hex
from harness_core.tools.filesystem import FileToolsMixin


class TestFilesystemBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.ledger = LedgerStore(":memory:")
        self.addCleanup(self.ledger.close)
        self.gateway = ToolGateway(self.ledger)

    def test_registered_handlers_are_bound_to_the_shared_mixin(self):
        for tool_id in ("read_file", "list_files", "search_files", "write_file", "patch"):
            with self.subTest(tool_id=tool_id):
                handler = self.gateway.tools[tool_id].handler
                self.assertIs(handler.__self__, self.gateway)
                self.assertIs(handler.__func__, getattr(FileToolsMixin, f"_tool_{tool_id}"))
        self.assertIs(ToolGateway._apply_unified_diff, FileToolsMixin._apply_unified_diff)
        self.assertIsInstance(FileToolsMixin.__dict__["_apply_unified_diff"], staticmethod)

    def test_read_resolves_graphify_and_memory_patches_at_call_time(self):
        (self.workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
        memories = [{"invariant": "keep value positive"}]
        with patch.object(gateway_module, "ActiveGraphifyAdapter") as adapter_type, \
             patch.object(gateway_module, "query_jit_memories_for_file", return_value=memories) as query, \
             patch.object(gateway_module, "format_jit_memories_block", return_value="JIT invariant") as format_block:
            result = self.gateway.tools["read_file"].handler(self.workspace, {"path": "sample.py"})
            adapter_type.assert_called_once_with(self.workspace)
            adapter_type.return_value.build_or_refresh.assert_called_once_with(self.workspace)
            query.assert_called_once_with(self.workspace, "sample.py", adapter_type.return_value)
            format_block.assert_called_once_with(memories, "sample.py")
        self.assertEqual(result["content"], "value = 1\n\n\nJIT invariant")
        self.assertEqual(result["size"], len(result["content"]))
        self.assertEqual(result["ast_memories"], memories)

    def test_mutations_resolve_gateway_hash_patch_and_keep_blast_hook(self):
        interceptor = MagicMock()
        interceptor.inspect_file_modification.return_value.to_dict.return_value = {"risk_class": "R2"}
        self.gateway.blast_interceptor = interceptor
        with patch.object(gateway_module, "sha256_hex", return_value="patched-digest") as digest:
            written = self.gateway.tools["write_file"].handler(
                self.workspace, {"path": "src/sample.py", "content": "value = 1\n"}
            )
            patched = self.gateway.tools["patch"].handler(
                self.workspace, {"path": "src/sample.py", "find": "value = 1", "replace": "value = 2"}
            )
            self.assertEqual(digest.call_args_list[0].args, ("value = 1\n",))
            self.assertEqual(digest.call_args_list[1].args, ("value = 2\n",))
            self.assertEqual(digest.call_count, 2)
        self.assertEqual(written["sha256"], "patched-digest")
        self.assertEqual(patched["sha256"], "patched-digest")
        self.assertEqual(patched["blast_radius"], {"risk_class": "R2"})
        interceptor.inspect_file_modification.assert_called_once_with(
            self.workspace, "src/sample.py", before_sources={"src/sample.py": "value = 1\n"}
        )
        self.assertEqual((self.workspace / "src/sample.py").read_text(encoding="utf-8"), "value = 2\n")

    def test_confined_handlers_reject_traversal_and_symlink_escape(self):
        outside = self.root / "outside.txt"
        outside.write_text("untouched\n", encoding="utf-8")
        (self.workspace / "escape.txt").symlink_to(outside)
        for tool_id in ("read_file", "write_file", "patch", "search_files"):
            for rel_path in ("../outside.txt", "escape.txt"):
                with self.subTest(tool_id=tool_id, path=rel_path), self.assertRaises(PolicyBlockedError):
                    self.gateway.tools[tool_id].handler(self.workspace, {
                        "path": rel_path, "content": "changed", "find": "untouched",
                        "replace": "changed", "query": "untouched",
                    })
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched\n")

    def test_file_write_journal_replays_without_repeating_side_effect(self):
        task = self.ledger.submit_task(TaskSubmitRequest(goal="Write once", execution_mode="standard"))
        call = dict(
            task_id=task["task_id"], attempt_id="attempt-1", step_id="write-step",
            sequence_in_step=0, tool_id="write_file",
            args={"path": "artifact.txt", "content": "original"}, workspace_dir=self.workspace,
        )
        first = self.gateway.invoke(**call)
        self.assertFalse(first["_replayed"])
        self.assertEqual(first["sha256"], sha256_hex("original"))
        target = self.workspace / "artifact.txt"
        target.write_text("changed after first call", encoding="utf-8")
        replay = self.gateway.invoke(**{**call, "attempt_id": "attempt-2"})
        self.assertTrue(replay["_replayed"])
        self.assertEqual(replay["_replayed_from_call_id"], first["_call_id"])
        self.assertEqual(target.read_text(encoding="utf-8"), "changed after first call")

    def test_fast_path_cannot_bypass_policy_with_inherited_write_handler(self):
        task = self.ledger.submit_task(TaskSubmitRequest(goal="Read only", execution_mode="fast_path"))
        with self.assertRaises(PolicyBlockedError):
            self.gateway.invoke(
                task_id=task["task_id"], attempt_id="attempt-1", step_id="write-step",
                sequence_in_step=0, tool_id="write_file",
                args={"path": "forbidden.txt", "content": "must not be written"},
                workspace_dir=self.workspace,
            )
        self.assertFalse((self.workspace / "forbidden.txt").exists())

    def test_gateway_and_mixin_import_in_either_order_without_a_cycle(self):
        orders = (
            ("harness_core.tools.filesystem", "harness_core.gateway"),
            ("harness_core.gateway", "harness_core.tools.filesystem"),
        )
        for first, second in orders:
            with self.subTest(first=first):
                result = subprocess.run(
                    [sys.executable, "-c", (
                        f"import {first}; import {second}; "
                        "from harness_core.gateway import ToolGateway; "
                        "from harness_core.tools.filesystem import FileToolsMixin; "
                        "assert issubclass(ToolGateway, FileToolsMixin)"
                    )],
                    cwd=self.root, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
