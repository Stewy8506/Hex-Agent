"""Regression tests for router compression gating, clean discovery scans, and ACP multi-turn ReAct execution."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.router import RouteDecision, TwoTierModelRouter
from harness_core.client.acp import ACPServer
from harness_core.core.models import TaskSubmitRequest
from harness_core.gateway import ActiveGraphifyAdapter, ToolGateway
from harness_core.providers.base import ModelResponse, ModelUsage
from harness_core.storage.db import LedgerStore


def _make_decision(prompt: str = "") -> RouteDecision:
    tok = max(1, len(prompt) // 4)
    return RouteDecision(
        tier="tier2_cloud_reasoner",
        provider="openrouter",
        model_id="test-model",
        reason="test",
        raw_tokens_estimated=tok,
        compressed_tokens_estimated=tok,
        token_savings_pct=0.0,
        compressed_context=prompt,
    )


class TestRouterCompressionGating(unittest.TestCase):
    """Ensure log compression runs only for failure/log tasks, never general prompts."""

    def test_non_bug_long_prompts_are_not_log_compressed(self) -> None:
        router = TwoTierModelRouter(compressor_mode="deterministic")
        long_prompt = ("Analyze this repository and explain the architecture in detail.\n" * 60).strip()
        self.assertGreater(len(long_prompt), 1200)

        for task_cls in ("synthesis", "agent_loop", "planning", "general"):
            decision = router.route_request(task_class=task_cls, raw_context=long_prompt)
            self.assertEqual(decision.token_savings_pct, 0.0)
            self.assertNotIn("Tier-1 Local Compressed Failure Digest", decision.compressed_context)
            self.assertIn("Analyze this repository", decision.compressed_context)

    def test_bug_fix_and_explicit_compress_logs_still_compress(self) -> None:
        router = TwoTierModelRouter(compressor_mode="deterministic")
        passing = "\n".join(f"tests/test_{i}.py ... PASSED" for i in range(60))
        failing = '\nFile "app/main.py", line 14\nAssertionError: boom\nFAILED app/main.py::test_x\n'
        trace = passing + failing

        bug_decision = router.route_request(task_class="complex_bug_fix", raw_context=trace)
        self.assertGreater(bug_decision.token_savings_pct, 0.0)
        self.assertIn("Tier-1 Local Compressed Failure Digest", bug_decision.compressed_context)

        explicit_decision = router.route_request(
            task_class="synthesis",
            raw_context=trace,
            compress_logs=True,
        )
        self.assertIn("Tier-1 Local Compressed Failure Digest", explicit_decision.compressed_context)


class TestDiscoveryExclusionsAndSummaries(unittest.TestCase):
    """Ensure list_files, search_files, Graphify indexing, and query_graphify skip build/vendor bloat."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hex_discovery_"))
        self.ws = self.tmp / "repo"
        self.ws.mkdir(parents=True)
        (self.ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        (self.ws / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
        (self.ws / "src").mkdir()
        (self.ws / "src" / "main.py").write_text(
            "def entrypoint():\n    return helper()\n\ndef helper():\n    return 1\n",
            encoding="utf-8",
        )
        (self.ws / "App" / "desktop" / "src").mkdir(parents=True)
        (self.ws / "App" / "desktop" / "src" / "lib.rs").write_text("pub fn ui_init() {}\n", encoding="utf-8")
        # Heavy/vendor/build dirs that must be skipped
        for ignored in ("target/debug", "build/out", "dist", "benchmarks/suite", "vendor/pkg", ".pytest_cache"):
            d = self.ws / ignored
            d.mkdir(parents=True)
            (d / "noise.py").write_text("def noise_func():\n    return 0\n", encoding="utf-8")

        self.ledger = LedgerStore(self.tmp / "ledger.db")
        self.gateway = ToolGateway(ledger=self.ledger)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_files_bfs_and_exclusions_and_non_recursive(self) -> None:
        res = self.gateway._tool_list_files(self.ws, {})
        self.assertTrue(res["ok"])
        files = res["files"]
        self.assertIn("pyproject.toml", files)
        self.assertIn("Cargo.toml", files)
        # Breadth-first puts root files ahead of nested src/ and App/... files
        self.assertLess(files.index("pyproject.toml"), files.index("src/main.py"))
        self.assertLess(files.index("src/main.py"), files.index("App/desktop/src/lib.rs"))
        for bad in ("target/", "build/", "dist/", "benchmarks/", "vendor/", ".pytest_cache/"):
            self.assertFalse(any(bad in f for f in files), f"Unexpected ignored path in {files}")
        self.assertEqual(res["top_level_dirs"], ["App", "src"])
        self.assertFalse(res["truncated"])

        shallow = self.gateway._tool_list_files(self.ws, {"path": ".", "recursive": False})
        self.assertEqual(shallow["files"], ["Cargo.toml", "pyproject.toml"])

        grep_res = self.gateway._tool_search_files(self.ws, {"query": "def "})
        matched_paths = [m["path"] for m in grep_res["matches"]]
        self.assertEqual(matched_paths, ["src/main.py", "src/main.py"])

    def test_graphify_ignores_heavy_dirs_cleans_tmp_and_summarizes(self) -> None:
        stale_tmp = self.ws / ".hex" / ".tmp_999_stale.json"
        stale_tmp.parent.mkdir(parents=True, exist_ok=True)
        stale_tmp.write_text("{}", encoding="utf-8")

        summary = self.gateway._tool_query_graphify(self.ws, {})
        self.assertTrue(summary["ok"])
        self.assertFalse(stale_tmp.exists())
        indexed_files = set(self.gateway.graphify.symbols_by_file.keys())
        self.assertEqual(indexed_files, {"src/main.py", "App/desktop/src/lib.rs"})
        self.assertNotIn("noise_func", self.gateway.graphify.symbols_by_name)
        self.assertEqual(summary["indexed_files"], 2)
        self.assertIn(".py", summary["languages"])
        self.assertIn(".rs", summary["languages"])
        self.assertLessEqual(len(summary["symbols"]), 50)
        self.assertIn("total_symbols", summary)


class TestACPMultiTurnReActAndFastPath(unittest.TestCase):
    """End-to-end ACP session/prompt ReAct execution with native tools and clean fast_path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hex_acp_react_"))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir(parents=True)
        (self.ws / "pyproject.toml").write_text('[project]\nname = "sample-app"\n', encoding="utf-8")
        (self.ws / "Cargo.toml").write_text('[package]\nname = "sample-rust"\n', encoding="utf-8")
        self.notifications: list[dict] = []
        self.orch = HarnessOrchestrator(
            data_dir=self.tmp / "data",
            default_workspace=self.ws,
            demo_mode=False,
        )
        self.server = ACPServer(
            orchestrator=self.orch,
            workspace_root=self.ws,
            config_dir=self.tmp / "cfg",
        )
        self._emit_patch = patch.object(self.server, "_emit", side_effect=self.notifications.append)
        self._emit_patch.start()

    def tearDown(self) -> None:
        self._emit_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acp_default_react_runs_native_tool_loop_and_streams(self) -> None:
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": str(self.ws)},
        })
        sid = new_resp["result"]["sessionId"]

        turn_calls: list[dict] = []

        def fake_execute_routed_request(**kwargs):
            decision = _make_decision(str(kwargs.get("prompt") or ""))
            if not kwargs.get("tools"):
                return decision, ModelResponse(
                    text='{"title": "Repo Tech Stack"}',
                    model_id="test-model",
                    provider_id="openrouter",
                    usage=ModelUsage(4, 3, 7, 0.0),
                    finish_reason="stop",
                )
            recorded = dict(kwargs)
            if isinstance(kwargs.get("messages"), list):
                recorded["messages"] = [dict(m) for m in kwargs["messages"]]
            turn_calls.append(recorded)
            step = len(turn_calls)
            if step == 1:
                return decision, ModelResponse(
                    text="",
                    model_id="test-model",
                    provider_id="openrouter",
                    usage=ModelUsage(10, 5, 15, 0.0),
                    finish_reason="tool_calls",
                    tool_calls=[{"id": "tc_1", "name": "list_files", "arguments": {"path": "."}}],
                )
            if step == 2:
                return decision, ModelResponse(
                    text="",
                    model_id="test-model",
                    provider_id="openrouter",
                    usage=ModelUsage(20, 5, 25, 0.0),
                    finish_reason="tool_calls",
                    tool_calls=[{"id": "tc_2", "name": "read_file", "arguments": {"path": "pyproject.toml"}}],
                )
            return decision, ModelResponse(
                text="This repo uses Python (pyproject.toml) and Rust (Cargo.toml).",
                model_id="test-model",
                provider_id="openrouter",
                usage=ModelUsage(30, 12, 42, 0.0),
                finish_reason="stop",
            )

        with patch.object(self.orch.router.cloud_provider, "is_available", return_value=True), \
             patch.object(self.orch.router, "execute_routed_request", side_effect=fake_execute_routed_request):
            resp = self.server.handle_request({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": "Analyze this repo. Tell me its tech stack."},
            })

        self.assertEqual(resp["result"]["status"], "completed")
        self.assertIn("Python", resp["result"]["content"][0]["text"])
        self.assertEqual(len(turn_calls), 3)
        self.assertIsNotNone(turn_calls[0].get("tools"))
        self.assertEqual(turn_calls[0].get("compress_logs"), False)
        # Verify turn 2 carried native assistant tool_calls + tool result message
        second_msgs = turn_calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" and m.get("name") == "list_files" for m in second_msgs))
        self.assertTrue(any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_msgs))
        # Verify turn 3 carried read_file observation
        third_msgs = turn_calls[2]["messages"]
        self.assertTrue(any(m.get("role") == "tool" and "sample-app" in str(m.get("content", "")) for m in third_msgs))

        tool_updates = [
            n["params"]["tool"]
            for n in self.notifications
            if n.get("method") == "session/update" and n["params"].get("updateType") == "tool"
        ]
        tool_pairs = [(t["toolId"], t["eventType"]) for t in tool_updates]
        self.assertEqual(
            tool_pairs,
            [
                ("list_files", "start"),
                ("list_files", "complete"),
                ("read_file", "start"),
                ("read_file", "complete"),
            ],
        )

    def test_acp_conceptual_question_runs_zero_tools(self) -> None:
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": str(self.ws)},
        })
        sid = new_resp["result"]["sessionId"]

        def fake_exec(**kwargs):
            return (
                _make_decision(str(kwargs.get("prompt") or "")),
                ModelResponse(text="KV-cache freezing keeps the system prefix stable.", model_id="m", provider_id="openrouter", usage=ModelUsage(5, 5, 10, 0.0)),
            )

        with patch.object(self.orch.router.cloud_provider, "is_available", return_value=True), \
             patch.object(self.orch.router, "execute_routed_request", side_effect=fake_exec):
            resp = self.server.handle_request({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": "Explain how KV-cache works."},
            })

        self.assertEqual(resp["result"]["status"], "completed")
        tool_updates = [
            n for n in self.notifications
            if n.get("method") == "session/update" and n["params"].get("updateType") == "tool"
        ]
        self.assertEqual(tool_updates, [])

    def test_explicit_fast_path_has_no_auto_scan_or_tools_in_prompt(self) -> None:
        captured_prompts: list[str] = []

        def fake_exec(**kwargs):
            captured_prompts.append(str(kwargs.get("system_prompt") or ""))
            return (
                _make_decision(str(kwargs.get("prompt") or "")),
                ModelResponse(text="Direct reply.", model_id="m", provider_id="openrouter", usage=ModelUsage(5, 5, 10, 0.0)),
            )

        task = self.orch.submit_task(
            TaskSubmitRequest(goal="Analyze this repo", execution_mode="fast_path", workspace_root=str(self.ws))
        )
        with patch.object(self.orch.router.cloud_provider, "is_available", return_value=True), \
             patch.object(self.orch.router, "execute_routed_request", side_effect=fake_exec):
            res = self.orch.execute_task(task["task_id"])

        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(len(captured_prompts), 1)
        self.assertNotIn("query_graphify", captured_prompts[0])
        self.assertNotIn("Available Tools:", captured_prompts[0])


class TestSymlinkConfinementInDiscovery(unittest.TestCase):
    """Ensure list_files and search_files do not follow escaping directory symlinks or crash."""

    def test_escaping_and_cyclic_symlinks_do_not_crash_or_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            outside = root / "outside"
            ws.mkdir()
            outside.mkdir()
            (ws / "inside.py").write_text("SECRET_MARKER = 'ok'\n", encoding="utf-8")
            (outside / "secret.py").write_text("SECRET_MARKER = 'leaked'\n", encoding="utf-8")
            (ws / "escape_dir").symlink_to(outside, target_is_directory=True)
            (ws / "escape_file.py").symlink_to(outside / "secret.py")
            (ws / "broken_link.py").symlink_to(outside / "missing.py")
            (ws / "loop_dir").symlink_to(ws, target_is_directory=True)

            gw = ToolGateway(ledger=LedgerStore(root / "ledger.db"))
            listed = gw._tool_list_files(ws, {})
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["files"], ["inside.py"])
            self.assertEqual(listed["top_level_dirs"], [])

            searched = gw._tool_search_files(ws, {"query": "SECRET_MARKER"})
            self.assertTrue(searched["ok"])
            self.assertEqual([m["path"] for m in searched["matches"]], ["inside.py"])


if __name__ == "__main__":
    unittest.main()
