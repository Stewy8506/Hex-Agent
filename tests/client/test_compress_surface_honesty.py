"""B65 regression: /compress and /compressor surfaces must describe what they
actually operate on.

Before B65 the console ``/compress`` claimed "✓ Context Compacted Successfully"
while compacting only the local transcript buffer that is never fed to the
agent loop (loops build their context per run via ``run_agent_loop`` and
compact it automatically), and ``/compressor`` (TUI + ACP + command catalog)
claimed to govern the "context compressor" although ``router.compressor_mode``
only controls verbose log compression (``Router.compress_verbose_log``).
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness_core.core.orchestrator import HarnessOrchestrator


def _read_output(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class TestConsoleCompressHonesty(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(":memory:", default_workspace=Path(self.tmp_dir.name))
        from hex_cli.tui import HexConsole

        self.console = HexConsole(
            self.orch,
            project_id="test_project",
            workspace_root=Path(self.tmp_dir.name),
            stream=True,
            model_profile="hybrid",
        )

    def tearDown(self) -> None:
        self.orch.shutdown()
        self.tmp_dir.cleanup()

    def _seed_transcript(self) -> None:
        filler = "Context fill line with some moderate length content. " * 12
        self.console.messages = []
        for i in range(8):
            self.console.messages.append({"role": "user", "content": f"Question {i}: {filler}"})
            self.console.messages.append({"role": "assistant", "content": f"Answer {i}: {filler}"})

    def test_compress_reports_transcript_not_live_context(self) -> None:
        self._seed_transcript()
        out = _read_output(self.console._cmd_compress, "")

        # Honest label: the buffer is a transcript, not the model context.
        self.assertIn("Session Transcript Compacted", out)
        self.assertNotIn("Context Compacted Successfully", out)
        # The live-context disclaimer is present.
        self.assertIn("live model", out)
        self.assertIn("transcript buffer only", out)
        # Mechanism preserved: the buffer really was compacted.
        self.assertGreater(self.console.session_compression_ratio, 0.0)
        self.assertIsNotNone(self.console.last_compaction_result)

    def test_compress_already_optimal_message_is_transcript_scoped(self) -> None:
        self.console.messages = [{"role": "user", "content": "tiny"}]
        out = _read_output(self.console._cmd_compress, "")
        self.assertIn("Transcript already compact", out)
        self.assertNotIn("Context already optimal", out)

    def test_compressor_output_labels_log_compression(self) -> None:
        out = _read_output(self.console._cmd_compressor, "")
        self.assertIn("Current log compressor mode", out)
        self.assertNotIn("Current compressor mode:", out)
        self.assertIn("not the live context window", out)

        out_set = _read_output(self.console._cmd_compressor, "cloud")
        self.assertIn("Log compressor mode updated to: cloud", out_set)
        self.assertIn("not the live context window", out_set)


class TestACPCompressorHonesty(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(":memory:", default_workspace=Path(self.tmp_dir.name))
        from hex_cli.acp import ACPServer

        self.server = ACPServer(orchestrator=self.orch, workspace_root=Path(self.tmp_dir.name))

    def tearDown(self) -> None:
        self.orch.shutdown()
        self.tmp_dir.cleanup()

    def test_compressor_response_labels_log_compression(self) -> None:
        sess = None  # _cmd_compressor does not touch the session
        resp = self.server._cmd_compressor(sess, "")
        self.assertIn("Current log compressor mode", resp)
        self.assertNotIn("Current compressor mode:", resp)
        self.assertIn("not the live context window", resp)

        resp_set = self.server._cmd_compressor(sess, "local")
        self.assertEqual(self.orch.router.compressor_mode, "local")
        self.assertIn("Log compressor mode set to: local", resp_set)
        self.assertIn("not the live context window", resp_set)


class TestCommandCatalogHonesty(unittest.TestCase):
    def _entries(self) -> dict[str, dict]:
        catalog_path = Path(__file__).resolve().parents[2] / "hex_cli" / "command_catalog.json"
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("commands", data)
        return {e["name"]: e for e in entries}

    def test_catalog_describes_compress_and_compressor_honestly(self) -> None:
        entries = self._entries()
        compressor_desc = entries["/compressor"]["description"]
        self.assertIn("log compressor", compressor_desc)
        self.assertNotIn("context compressor mode", compressor_desc)

        compress_desc = entries["/compress"]["description"]
        self.assertIn("transcript buffer", compress_desc)
        self.assertNotIn("active dialogue history", compress_desc)


if __name__ == "__main__":
    unittest.main()
