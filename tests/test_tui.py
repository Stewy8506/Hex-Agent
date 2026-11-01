"""Unit and Integration Tests for Interactive Terminal UI (TUI) & Slash Commands (PRD Section 8, Pillar 2).

Validates:
1. Diff visualizers (unified and side-by-side) with ANSI color highlighting and border formatting.
2. Active Graphify AST blast-radius tree renderer with hierarchical branches and risk badges.
3. Streaming token renderer with progressive chunks and code block syntax detection.
4. Session banner generation with model profile and backend indicators.
5. HexConsole REPL prompt loop, history, and all 10 slash command handlers.
6. Natural language prompt submission and Proof-Carrying Artifact display.
7. CLI `hex chat` subcommand invocation and argument parsing.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.cow import compute_workspace_diff
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.tui import (
    ANSI_CYAN,
    ANSI_GREEN,
    ANSI_RED,
    ANSI_YELLOW,
    HexConsole,
    StreamingTokenRenderer,
    render_blast_radius_tree,
    render_session_banner,
    render_side_by_side_diff,
    render_unified_diff,
    strip_ansi,
)


class TestDiffVisualizers(unittest.TestCase):
    """Verifies unified and side-by-side terminal diff rendering."""

    def test_unified_diff_rendering(self):
        diff_text = (
            "--- a/calculator.py\n"
            "+++ b/calculator.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    # fixed calculation\n"
            "+    return a + b\n"
        )
        rendered = render_unified_diff(diff_text, title="Calculator Patch")
        self.assertIn("Calculator Patch", rendered)
        self.assertIn("┌──", rendered)
        self.assertIn("└──", rendered)

        # Check color codes
        self.assertIn(ANSI_YELLOW, rendered)  # File headers
        self.assertIn(ANSI_CYAN, rendered)    # Hunk header @@
        self.assertIn(ANSI_GREEN, rendered)   # Additions +
        self.assertIn(ANSI_RED, rendered)     # Deletions -

    def test_unified_diff_empty_or_whitespace(self):
        empty_res = render_unified_diff("", title="Empty Diff")
        self.assertIn("(no changes)", empty_res)

        ws_res = render_unified_diff("   \n   \n", title="Blank Diff")
        self.assertIn("(no changes)", ws_res)

    def test_side_by_side_diff_rendering(self):
        old_text = "def calc():\n    return 1\n"
        new_text = "def calc():\n    val = 2\n    return val\n"

        rendered = render_side_by_side_diff(old_text, new_text, filename="calc.py", width=80)
        self.assertIn("Old: calc.py", rendered)
        self.assertIn("New: calc.py", rendered)
        self.assertIn("│", rendered)
        self.assertIn("┌─", rendered)
        self.assertIn("└", rendered)
        self.assertIn(ANSI_GREEN, rendered)

    def test_compute_workspace_diff_utility(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dir_a = Path(tmp_dir) / "base"
            dir_b = Path(tmp_dir) / "target"
            dir_a.mkdir()
            dir_b.mkdir()

            (dir_a / "file1.txt").write_text("Hello World\nLine 2\n", encoding="utf-8")
            (dir_b / "file1.txt").write_text("Hello HEX\nLine 2\n", encoding="utf-8")

            (dir_b / "new_file.py").write_text("print('new')\n", encoding="utf-8")

            diff = compute_workspace_diff(dir_a, dir_b)
            self.assertIn("--- a/file1.txt", diff)
            self.assertIn("+++ b/file1.txt", diff)
            self.assertIn("+Hello HEX", diff)
            self.assertIn("-Hello World", diff)
            self.assertIn("+++ b/new_file.py", diff)


class TestGraphifyBlastRadiusTree(unittest.TestCase):
    """Verifies Active Graphify AST blast-radius tree visualization."""

    def test_blast_radius_tree_rendering_with_callers(self):
        data = {
            "modified_files": ["src/auth.py:validate_token", "src/models.py:User"],
            "direct_callers": ["src/api.py:login_endpoint", "src/middleware.py:auth_hook"],
            "transitive_callers": ["src/server.py:init_routes"],
            "pinned_tests": ["tests/test_auth.py", "tests/test_api.py"],
            "risk_class": "R2",
        }
        tree = render_blast_radius_tree(data)
        plain = strip_ansi(tree)
        self.assertIn("Active Graphify AST Impact Matrix", plain)
        self.assertIn("[R2 - Medium Risk]", plain)
        self.assertIn("Modified Files / Symbols (2)", plain)
        self.assertIn("validate_token", plain)
        self.assertIn("1-Hop Direct Callers (2)", plain)
        self.assertIn("login_endpoint", plain)
        self.assertIn("2-Hop Transitive Callers (1)", plain)
        self.assertIn("init_routes", plain)
        self.assertIn("Pinned Verification Tests (2)", plain)
        self.assertIn("test_auth.py", plain)
        self.assertIn("├──", tree)
        self.assertIn("└──", tree)

    def test_blast_radius_tree_empty_sections(self):
        data = {
            "modified_files": ["foo.py"],
            "direct_callers": [],
            "transitive_callers": [],
            "pinned_tests": [],
            "risk_class": "R0",
        }
        tree = render_blast_radius_tree(data)
        self.assertIn("[R0 - No Risk / Read Only]", tree)
        self.assertIn("(none)", tree)

    def test_blast_radius_risk_classes(self):
        for r_code in ("R0", "R1", "R2", "R3", "R4"):
            tree = render_blast_radius_tree({"risk_class": r_code})
            self.assertIn(f"[{r_code}", tree)


class TestStreamingTokenRenderer(unittest.TestCase):
    """Verifies progressive token stream accumulation and syntax highlighting."""

    def test_streaming_token_renderer_accumulation(self):
        stream = StringIO()
        renderer = StreamingTokenRenderer(output_stream=stream, enable_color=True)

        chunks = ["def ", "main():\n", "    ```python\n", "    print('hello')\n", "    ```\n", "    return 0\n"]
        for c in chunks:
            renderer.feed(c)

        accumulated = renderer.flush()
        expected = "".join(chunks)
        self.assertEqual(accumulated, expected)
        self.assertEqual(renderer.get_content(), expected)

        # Output stream should have received rendered tokens
        out = stream.getvalue()
        self.assertIn("main():", out)
        self.assertIn("print('hello')", out)

    def test_streaming_token_renderer_none_stream(self):
        renderer = StreamingTokenRenderer(output_stream=None)
        renderer.feed("token1")
        renderer.feed(" token2")
        self.assertEqual(renderer.flush(), "token1 token2")


class TestSessionBanner(unittest.TestCase):
    """Verifies session header banner formatting and parameters."""

    def test_session_banner_contents_and_borders(self):
        banner = render_session_banner(
            model_id="claude-3-7-sonnet",
            compressor_mode="M4 MLX Local Engine",
            sandbox_backend="bwrap",
            cow_backend="apfs_clone",
        )
        self.assertIn("HEX Autonomous AI Workbench", banner)
        self.assertIn("claude-3-7-sonnet", banner)
        self.assertIn("M4 MLX Local Engine", banner)
        self.assertIn("bwrap", banner)
        self.assertIn("apfs_clone", banner)
        self.assertIn("╔", banner)
        self.assertIn("╝", banner)

        # Check line strip lengths are properly padded
        for line in banner.splitlines():
            stripped = strip_ansi(line)
            self.assertEqual(len(stripped), 78)


class TestHexConsoleSlashCommands(unittest.TestCase):
    """Verifies HexConsole interactive REPL and slash command handling."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(":memory:", default_workspace=Path(self.temp_dir.name))
        self.console = HexConsole(
            self.orch,
            project_id="test_project",
            workspace_root=Path(self.temp_dir.name),
            stream=True,
            model_profile="hybrid",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_handle_slash_command_help(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/help")
        self.assertFalse(should_exit)
        text = out.getvalue()
        self.assertIn("/help", text)
        self.assertIn("/status", text)
        self.assertIn("/diff", text)
        self.assertIn("/rollback", text)
        self.assertIn("/fork", text)
        self.assertIn("/skills", text)
        self.assertIn("/model", text)
        self.assertIn("/budget", text)
        self.assertIn("/clear", text)
        self.assertIn("/quit", text)

    def test_handle_slash_command_status(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/status")
        self.assertFalse(should_exit)
        text = out.getvalue()
        self.assertIn("HEX Console Session Status", text)
        self.assertIn("test_project", text)
        self.assertIn("Active Workers", text)

    def test_handle_slash_command_diff_empty(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/diff")
        self.assertFalse(should_exit)
        self.assertIn("No uncommitted changes detected", out.getvalue())

    def test_handle_slash_command_rollback_none(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/rollback")
        self.assertFalse(should_exit)
        self.assertIn("No active task to roll back", out.getvalue())

    def test_handle_slash_command_fork_none(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/fork")
        self.assertFalse(should_exit)
        self.assertIn("No active task to fork", out.getvalue())

    def test_handle_slash_command_skills(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/skills")
        self.assertFalse(should_exit)
        # Without seeded skills
        self.assertIn("No registered skills found", out.getvalue())

    def test_handle_slash_command_model(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/model")
        self.assertFalse(should_exit)
        self.assertIn("Active Model Routing Configuration", out.getvalue())

        # Switch model profile
        out_switch = StringIO()
        with patch("sys.stdout", out_switch):
            self.console.handle_slash_command("/model local_mlx")
        self.assertEqual(self.console.model_profile, "local_mlx")
        self.assertIn("Model profile updated to: local_mlx", out_switch.getvalue())

    def test_handle_slash_command_budget(self):
        self.console.session_tokens_in = 1200
        self.console.session_tokens_out = 450
        self.console.session_tokens_saved = 3500
        self.console.session_compression_ratio = 0.74
        self.console.session_cost_usd = 0.0042

        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/budget")
        self.assertFalse(should_exit)
        text = out.getvalue()
        self.assertIn("1,200", text)
        self.assertIn("450", text)
        self.assertIn("74.0%", text)
        self.assertIn("$0.0042 USD", text)

    def test_handle_slash_command_clear(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/clear")
        self.assertFalse(should_exit)
        # Should output ANSI clear escape sequence
        self.assertIn("\033[H\033[2J", out.getvalue())

    def test_handle_slash_command_quit_and_exit(self):
        self.assertTrue(self.console.handle_slash_command("/quit"))
        self.assertTrue(self.console.handle_slash_command("/exit"))

    def test_handle_slash_command_unknown(self):
        out = StringIO()
        with patch("sys.stdout", out):
            should_exit = self.console.handle_slash_command("/unknown_command")
        self.assertFalse(should_exit)
        self.assertIn("Unknown command", out.getvalue())

    def test_console_prompt_loop_execution(self):
        inputs = ["/help", "/status", "/model", "/budget", "/quit"]
        call_idx = 0

        def mock_input(_prompt: str) -> str:
            nonlocal call_idx
            if call_idx < len(inputs):
                val = inputs[call_idx]
                call_idx += 1
                return val
            raise EOFError()

        out = StringIO()
        with patch("sys.stdout", out):
            res = self.console.start(input_fn=mock_input)
        self.assertEqual(res, 0)
        self.assertEqual(self.console.history, ["/help", "/status", "/model", "/budget", "/quit"])

    def test_handle_user_prompt_submission(self):
        out = StringIO()
        with patch("sys.stdout", out):
            self.console.handle_user_prompt("Write a test function")
        text = out.getvalue()
        self.assertIn("Submitting task to HEX orchestrator", text)
        self.assertIsNotNone(self.console.active_task_id)


class TestCLIChatSubcommand(unittest.TestCase):
    """Verifies CLI argument parsing and `hex chat` execution."""

    def test_cli_chat_subcommand_arguments(self):
        old_stdin = sys.stdin
        try:
            sys.stdin = StringIO("/help\n/quit\n")
            out = StringIO()
            with patch("sys.stdout", out):
                res = main([
                    "chat",
                    "--data-dir", ":memory:",
                    "--project", "test_p",
                    "--model", "hybrid",
                    "--no-stream",
                ])
            self.assertEqual(res, 0)
            text = out.getvalue()
            self.assertIn("HEX Autonomous AI Workbench", text)
            self.assertIn("Available HEX Slash Commands", text)
        finally:
            sys.stdin = old_stdin


if __name__ == "__main__":
    unittest.main()
