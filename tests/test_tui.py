"""Unit and Integration Tests for Interactive Terminal UI (TUI) & Slash Commands (PRD Section 8, Pillar 2).

Validates:
1. Diff visualizers (unified and side-by-side) with ANSI color highlighting and border formatting.
2. Active Graphify AST blast-radius tree renderer with hierarchical branches and risk badges.
3. Streaming token renderer with progressive chunks and code block syntax detection.
4. Session banner generation with model profile and backend indicators.
5. Hermes-grade 24-bit TrueColor Retro-Block Banner, Two-Column Capability Splash Card,
   Data-Driven Skin Engine (`SKIN_REGISTRY`), and Persistent Status Bar (`render_status_bar`).
6. HexConsole REPL prompt loop, history, and all 20+ slash command handlers.
7. Natural language prompt submission, `┊` activity feed, and Proof-Carrying Artifact display.
8. CLI `hex chat` / `hex tui` subcommand invocation and argument parsing (`--skin`, `-Q`).
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
    SKIN_REGISTRY,
    HexConsole,
    StreamingTokenRenderer,
    render_blast_radius_tree,
    render_hermes_splash,
    render_session_banner,
    render_side_by_side_diff,
    render_status_bar,
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


class TestSessionBannerAndHermesSplash(unittest.TestCase):
    """Verifies session header banner, Hermes two-column splash card, skins, and status bar."""

    def test_session_banner_contents_and_borders(self):
        with patch.dict(os.environ, {"HEX_MODEL_SOURCE": ""}):
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

    def test_hermes_splash_two_column_box_and_skins(self):
        for skin_name in SKIN_REGISTRY:
            splash = render_hermes_splash(
                skin_name=skin_name,
                model_id="gemini-3.5-pro",
                compressor_mode="local",
                width=110,
            )
            plain = strip_ansi(splash)
            self.assertIn("HEX Autonomous AI Workbench v", plain)
            self.assertIn("Available Tools", plain)
            self.assertIn("Available Skills", plain)
            self.assertIn("query_graphify", plain)
            self.assertIn("nvidia-skillspector-static", plain)
            self.assertIn("Welcome to HEX Autonomous AI Workbench!", plain)

    def test_persistent_status_bar_rendering(self):
        bar = render_status_bar(
            model_id="claude-3-5-sonnet",
            compressor_mode="local",
            cow_backend="apfs_clone",
            tokens_in=6554,
            max_context_tokens=32768,
            compression_ratio=0.76,
            cost_usd=0.04,
            max_cost_usd=2.00,
            elapsed_seconds=18,
            skin_name="amber",
        )
        plain = strip_ansi(bar)
        self.assertIn("claude-3-5-sonnet", plain)
        self.assertIn("comp:local", plain)
        self.assertIn("cow:apfs_clone", plain)
        self.assertIn("ctx 6.6k/32k (76% saved)", plain)
        self.assertIn("[██░░░░░░░░] 20%", plain)
        self.assertIn("18s", plain)


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
        for cmd in (
            "/help", "/status", "/diff", "/sbs", "/blast", "/proof",
            "/rollback", "/fork", "/skills", "/scan", "/model",
            "/compressor", "/budget", "/memory", "/remember",
            "/workers", "/harness2", "/skin", "/banner", "/clear", "/quit",
        ):
            self.assertIn(cmd, text)

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

    def test_handle_slash_command_skin_and_compressor(self):
        out = StringIO()
        with patch("sys.stdout", out):
            self.console.handle_slash_command("/skin cyberpunk")
            self.console.handle_slash_command("/compressor cloud")
        self.assertEqual(self.console.skin_name, "cyberpunk")
        self.assertEqual(self.orch.router.compressor_mode, "cloud")
        self.assertIn("Skin updated to: cyberpunk", out.getvalue())
        self.assertIn("Compressor mode updated to: cloud", out.getvalue())

    def test_handle_slash_command_memory_and_workers(self):
        out = StringIO()
        with patch("sys.stdout", out):
            self.console.handle_slash_command("/remember architecture: CoW uses APFS on macOS")
            self.console.handle_slash_command("/memory APFS")
            self.console.handle_slash_command("/workers")
        text = out.getvalue()
        self.assertIn("Stored governed project memory", text)
        self.assertIn("Governed Memories (1)", text)
        self.assertIn("Registered Distributed Workers", text)

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

    def test_modes_switching_and_prompt_injection(self):
        """Verify 3 modes (plan, agent, code), switching commands, and prompt injection."""
        self.assertEqual(self.console.mode, "agent")

        mock_task = {"task_id": "dummy_task_mode", "status": "COMPLETED"}

        # Switch to plan
        self.console.handle_slash_command("/plan")
        self.assertEqual(self.console.mode, "plan")

        # Verify plan prompt injection and fast_path
        with patch.object(self.orch, "submit_and_run", return_value=mock_task) as mock_submit:
            self.console.handle_user_prompt("Architecture breakdown")
            req = mock_submit.call_args[0][0]
            self.assertIn("[MODE: PLAN", req.goal)
            self.assertEqual(req.execution_mode, "fast_path")

        # Switch to code
        self.console.handle_slash_command("/code")
        self.assertEqual(self.console.mode, "code")

        # Verify code prompt injection and auto execution mode
        with patch.object(self.orch, "submit_and_run", return_value=mock_task) as mock_submit:
            self.console.handle_user_prompt("Synthesize helper function")
            req = mock_submit.call_args[0][0]
            self.assertIn("[MODE: CODE", req.goal)
            self.assertEqual(req.execution_mode, "auto")

        # Switch to agent
        self.console.handle_slash_command("/agent")
        self.assertEqual(self.console.mode, "agent")

        # Verify agent prompt has no prefix and auto execution mode
        with patch.object(self.orch, "submit_and_run", return_value=mock_task) as mock_submit:
            self.console.handle_user_prompt("Fix tests and repair")
            req = mock_submit.call_args[0][0]
            self.assertEqual(req.goal, "Fix tests and repair")
            self.assertEqual(req.execution_mode, "auto")

        # Switch via /mode command
        self.console.handle_slash_command("/mode plan")
        self.assertEqual(self.console.mode, "plan")
        self.console.handle_slash_command("/mode")  # cycle
        self.assertEqual(self.console.mode, "agent")
        self.console.handle_slash_command("/mode")  # cycle
        self.assertEqual(self.console.mode, "code")
        self.console.handle_slash_command("/mode")  # cycle
        self.assertEqual(self.console.mode, "plan")

        # Backward compatibility alias
        self.console.handle_slash_command("/ask")
        self.assertEqual(self.console.mode, "plan")

    def test_handle_user_prompt_submission(self):
        out = StringIO()
        with patch("sys.stdout", out):
            self.console.handle_user_prompt("Write a test function")
        text = out.getvalue()
        self.assertIn("Submitting task to HEX orchestrator", text)
        self.assertIsNotNone(self.console.active_task_id)


class TestDockedInputFrameAndGhostSuggestions(unittest.TestCase):
    """Verifies Hermes-grade docked input frame and dynamic ghost suggestions."""

    def test_default_prompt_suggestions_pool(self):
        from harness_core.tui import DEFAULT_PROMPT_SUGGESTIONS
        self.assertGreaterEqual(len(DEFAULT_PROMPT_SUGGESTIONS), 10)
        self.assertIn("Ask me anything...", DEFAULT_PROMPT_SUGGESTIONS)
        for s in DEFAULT_PROMPT_SUGGESTIONS:
            self.assertTrue(len(s) > 5)

    def test_render_docked_input_frame_empty_with_ghost_text(self):
        from harness_core.tui import render_docked_input_frame
        top, mid, bot = render_docked_input_frame(
            width=100,
            text="",
            suggestion="Ask me anything...",
            mode="ask",
            task_num=3,
            project_name="Harness",
            git_branch="main",
            model_id="gemini-3.8-flash-tiered",
            enable_color=False,
        )
        self.assertTrue(top.startswith("╭"))
        self.assertTrue(top.endswith("╮"))
        self.assertIn("Ask me anything...", mid)
        self.assertIn("│ ❯ ", mid)
        self.assertTrue(mid.endswith("│"))
        self.assertTrue(bot.startswith("╰─"))
        self.assertTrue(bot.endswith("─╯"))
        self.assertIn("Harness", bot)
        self.assertIn("main", bot)
        self.assertIn("enter send", bot)

    def test_render_docked_input_frame_typed_text_replaces_ghost(self):
        from harness_core.tui import render_docked_input_frame
        top, mid, bot = render_docked_input_frame(
            width=100,
            text="Explain AST graphify",
            suggestion="Ask me anything...",
            enable_color=False,
        )
        self.assertIn("Explain AST graphify", mid)
        self.assertNotIn("Ask me anything...", mid)

    def test_render_docked_input_frame_across_widths(self):
        from harness_core.tui import render_docked_input_frame, strip_ansi
        for w in [70, 80, 95, 110]:
            top, mid, bot = render_docked_input_frame(width=w, text="", enable_color=True)
            self.assertEqual(len(strip_ansi(top)), w)
            self.assertEqual(len(strip_ansi(bot)), w)

    def test_console_suggestion_rotation(self):
        from harness_core.tui import HexConsole, DEFAULT_PROMPT_SUGGESTIONS
        mock_orch = MagicMock()
        console = HexConsole(mock_orch, quiet=True)
        s1 = console._get_next_suggestion()
        s2 = console._get_next_suggestion()
        self.assertEqual(s1, DEFAULT_PROMPT_SUGGESTIONS[0])
        self.assertEqual(s2, DEFAULT_PROMPT_SUGGESTIONS[1])
        self.assertNotEqual(s1, s2)


class TestCLIChatSubcommand(unittest.TestCase):
    """Verifies CLI argument parsing and `hex chat` / `hex tui` execution."""

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
                    "--skin", "cyberpunk",
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
