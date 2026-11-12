"""Comprehensive tests for Terminal UI Selector, Config Loader, and Web Search."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.config_loader import (
    has_active_credentials,
    load_hex_environment,
    parse_config_yaml,
    parse_env_file,
)
from harness_core.gateway import RiskClass, ToolGateway
from harness_core.ui_selector import (
    SelectorCancelledError,
    prompt_secret,
    select_multiple,
    select_option,
)
from harness_core.web_search import (
    execute_web_search,
)


class TestUISelector(unittest.TestCase):
    def test_select_option_non_tty(self) -> None:
        in_buf = io.StringIO("\n")
        out_buf = io.StringIO()
        options = ["Option A", "Option B", "Option C"]
        idx = select_option("Test Header", options, default_index=1, stream_in=in_buf, stream_out=out_buf)
        self.assertEqual(idx, 1)
        output = out_buf.getvalue()
        self.assertIn("◈ Test Header", output)
        self.assertIn("[•] 2. Option B", output)

    def test_select_option_empty_error(self) -> None:
        with self.assertRaises(ValueError):
            select_option("Test", [])

    def test_select_multiple_non_tty(self) -> None:
        in_buf = io.StringIO("\n")
        out_buf = io.StringIO()
        options = ["Opt 1", "Opt 2", "Opt 3"]
        chosen = select_multiple(
            "Multi Test",
            options,
            default_selected=[0, 2],
            stream_in=in_buf,
            stream_out=out_buf,
        )
        self.assertEqual(chosen, [0, 2])
        output = out_buf.getvalue()
        self.assertIn("[✓] 1. Opt 1", output)
        self.assertIn("[ ] 2. Opt 2", output)
        self.assertIn("[✓] 3. Opt 3", output)

    def test_select_multiple_empty_error(self) -> None:
        with self.assertRaises(ValueError):
            select_multiple("Empty", [])

    def test_prompt_secret_non_tty(self) -> None:
        in_buf = io.StringIO("super-secret-key-123\n")
        out_buf = io.StringIO()
        with patch("getpass.getpass", side_effect=Exception("No getpass")):
            val = prompt_secret(prompt="Secret: ", stream_in=in_buf, stream_out=out_buf)
        self.assertEqual(val, "super-secret-key-123")

    def test_interactive_select_option_navigation(self) -> None:
        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True
        fake_stdin.fileno.return_value = 0

        fake_stdout = io.StringIO()
        fake_stdout.isatty = lambda: True  # type: ignore

        # Keys: Down arrow (\x1b[B), Down arrow, Enter (\n)
        key_sequence = ["\x1b", "[", "B", "\x1b", "[", "B", "\n"]

        with patch("termios.tcgetattr", return_value=[]), patch("termios.tcsetattr"), patch("tty.setraw"):
            with patch("sys.stdin.read", side_effect=key_sequence):
                idx = select_option(
                    "Pick",
                    ["Choice 0", "Choice 1", "Choice 2"],
                    default_index=0,
                    stream_in=fake_stdin,
                    stream_out=fake_stdout,
                )
        self.assertEqual(idx, 2)

    def test_interactive_select_option_cancelled(self) -> None:
        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True
        fake_stdin.fileno.return_value = 0

        fake_stdout = io.StringIO()
        fake_stdout.isatty = lambda: True  # type: ignore

        with patch("termios.tcgetattr", return_value=[]), patch("termios.tcsetattr"), patch("tty.setraw"):
            with patch("sys.stdin.read", side_effect=["q"]):
                with self.assertRaises(SelectorCancelledError):
                    select_option(
                        "Cancel Me",
                        ["A", "B"],
                        stream_in=fake_stdin,
                        stream_out=fake_stdout,
                    )


class TestConfigLoader(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("TEST_KEY_ALPHA", None)
        os.environ.pop("HEX_CLOUD_PROVIDER", None)
        os.environ.pop("HEX_CLOUD_MODEL_ID", None)

    def test_parse_env_file(self) -> None:
        env_file = self.cfg_dir / "test.env"
        env_file.write_text("FOO=bar\n# comment\nBAZ='qux'\nQUUX=\"value with spaces\"\n")
        parsed = parse_env_file(env_file)
        self.assertEqual(parsed["FOO"], "bar")
        self.assertEqual(parsed["BAZ"], "qux")
        self.assertEqual(parsed["QUUX"], "value with spaces")

    def test_load_hex_environment(self) -> None:
        creds_file = self.cfg_dir / "credentials.env"
        creds_file.write_text("TEST_KEY_ALPHA=secret123\nHEX_CLOUD_PROVIDER=anthropic\n")
        config_file = self.cfg_dir / "config.yaml"
        config_file.write_text("model:\n  cloud_provider: anthropic\n  cloud_model_id: claude-3-5-sonnet\n")

        # Clean existing test env vars
        os.environ.pop("TEST_KEY_ALPHA", None)
        os.environ.pop("HEX_CLOUD_PROVIDER", None)
        os.environ.pop("HEX_CLOUD_MODEL_ID", None)

        loaded = load_hex_environment(config_dir=self.cfg_dir, workspace_root=self.cfg_dir)
        self.assertIn("TEST_KEY_ALPHA", loaded)
        self.assertEqual(os.environ.get("TEST_KEY_ALPHA"), "secret123")
        self.assertEqual(os.environ.get("HEX_CLOUD_PROVIDER"), "anthropic")
        self.assertEqual(os.environ.get("HEX_CLOUD_MODEL_ID"), "claude-3-5-sonnet")

    def test_has_active_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(has_active_credentials(config_dir=self.cfg_dir))

            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
            self.assertTrue(has_active_credentials(config_dir=self.cfg_dir))


class TestWebSearch(unittest.TestCase):
    def test_execute_web_search_empty(self) -> None:
        res = execute_web_search("")
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["results"], [])

    @patch("harness_core.web_search._search_duckduckgo")
    def test_execute_web_search_duckduckgo(self, mock_ddg: MagicMock) -> None:
        mock_ddg.return_value = [
            {"title": "Python Docs", "url": "https://python.org", "snippet": "Official documentation"}
        ]
        res = execute_web_search("python documentation", provider="duckduckgo")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["provider"], "duckduckgo")
        self.assertEqual(res["results"][0]["title"], "Python Docs")

    @patch("harness_core.web_search._search_tavily")
    def test_execute_web_search_tavily(self, mock_tav: MagicMock) -> None:
        mock_tav.return_value = [
            {"title": "FastAPI Guide", "url": "https://fastapi.tiangolo.com", "snippet": "Modern Python framework"}
        ]
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            res = execute_web_search("fastapi guide", provider="auto")
            self.assertEqual(res["provider"], "tavily")
            self.assertEqual(res["count"], 1)

    def test_gateway_web_search_integration(self) -> None:
        mock_ledger = MagicMock()
        gw = ToolGateway(ledger=mock_ledger)
        self.assertIn("web_search", gw.tools)
        spec = gw.tools["web_search"]
        self.assertEqual(spec.risk_class, RiskClass.R1)

        with patch("harness_core.web_search.execute_web_search") as mock_exec:
            mock_exec.return_value = {
                "query": "pytest fixtures",
                "provider": "duckduckgo",
                "count": 1,
                "results": [{"title": "Pytest Fixtures", "url": "https://pytest.org", "snippet": "Fixtures guide"}],
                "error": None,
            }
            with tempfile.TemporaryDirectory() as td:
                res = spec.handler(Path(td), {"query": "pytest fixtures"})
                self.assertEqual(res["count"], 1)
                self.assertEqual(res["results"][0]["title"], "Pytest Fixtures")


if __name__ == "__main__":
    unittest.main()
