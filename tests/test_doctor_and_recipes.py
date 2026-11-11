"""Comprehensive Unit & Integration Test Suite for HexDoctor, Recipes, and Graphify Persistent Cache.

Covers:
1. HexDoctor individual checks (Python, hardware, CoW, sandbox, credentials, git, storage, directories).
2. HexDoctor self-healing (--fix mode): auto-creates directories, generates default config.
3. HexDoctor live ping connectivity (--live mode).
4. Multi-framework recipe auto-detection (Python, Node.js, Rust, Go, Makefile, Docker Compose).
5. ToolGateway integration with auto-detected recipe test runner.
6. Graphify persistent on-disk cache (.hex/graph.json) with sub-30ms warm startup.
7. Graphify multi-language symbol extraction (Python, TS/JS/React, Rust, Go).
8. CLI command execution (hex doctor and hex recipe).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main as cli_main
from harness_core.doctor import DoctorCheckResult, DoctorReport, HexDoctor
from harness_core.gateway import ToolGateway
from harness_core.graphify import ActiveGraphifyAdapter, SymbolDef
from harness_core.recipes import Recipe, detect_recipe


class TestHexDoctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_doc_test_")
        self.ws = Path(self.tmp.name)
        self.data_dir = self.ws / ".hex_data"
        self.doctor = HexDoctor(workspace_root=self.ws, data_dir=self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_doctor_individual_checks(self) -> None:
        """Validate each individual check method returns valid DoctorCheckResult instances."""
        # 1. Python check
        py_results = self.doctor.check_python()
        self.assertTrue(len(py_results) >= 2)
        self.assertEqual(py_results[0].category, "python")
        self.assertIn("Python Version", [c.name for c in py_results])

        # 2. Hardware acceleration check
        hw_results = self.doctor.check_hardware_acceleration()
        self.assertTrue(len(hw_results) >= 1)
        self.assertEqual(hw_results[0].category, "hardware")

        # 3. CoW check
        cow_results = self.doctor.check_cow_providers()
        self.assertTrue(len(cow_results) >= 1)
        self.assertEqual(cow_results[0].category, "cow")

        # 4. Sandbox check
        sb_results = self.doctor.check_sandboxes()
        self.assertTrue(len(sb_results) >= 1)
        self.assertEqual(sb_results[0].category, "sandbox")

        # 5. Credentials check
        cred_results = self.doctor.check_credentials()
        self.assertTrue(len(cred_results) >= 1)
        self.assertEqual(cred_results[0].category, "credentials")

        # 6. Git check
        git_results = self.doctor.check_git()
        self.assertTrue(len(git_results) >= 1)
        self.assertEqual(git_results[0].category, "git")

        # 7. Storage check
        stor_results = self.doctor.check_storage()
        self.assertTrue(len(stor_results) >= 1)
        self.assertEqual(stor_results[0].category, "storage")

        # 8. Directories and config check
        dir_results = self.doctor.check_directories_and_config()
        self.assertTrue(len(dir_results) >= 1)
        self.assertEqual(dir_results[0].category, "directories")

        # 9. Audio hardware & speech engines check
        audio_results = self.doctor.check_audio_devices()
        self.assertEqual(len(audio_results), 3)
        self.assertTrue(all(c.category == "audio" for c in audio_results))
        self.assertTrue(all(c.status in ("ok", "warning") for c in audio_results))

    def test_doctor_fix_mode(self) -> None:
        """Verify --fix auto-remediates missing directories and generates default configuration."""
        hex_dir = self.ws / ".hex"
        skills_custom = self.ws / "skills" / "custom"
        quarantine_dir = hex_dir / "quarantine"
        cfg_file = hex_dir / "config.yaml"

        self.assertFalse(hex_dir.exists())
        self.assertFalse(cfg_file.exists())

        # Initial check without fix: warnings for missing directories and config
        report1 = self.doctor.run_diagnostics(fix=False)
        missing_names = {c.name for c in report1.checks if c.status == "warning" and c.fixable}
        self.assertIn("Workspace .hex Directory", missing_names)
        self.assertIn("Workspace Configuration (.hex/config.yaml)", missing_names)

        # Apply fix
        report2 = self.doctor.run_diagnostics(fix=True)
        self.assertTrue(hex_dir.is_dir())
        self.assertTrue(skills_custom.is_dir())
        self.assertTrue(quarantine_dir.is_dir())
        self.assertTrue(cfg_file.is_file())

        cfg_content = cfg_file.read_text(encoding="utf-8")
        self.assertIn("version: 1", cfg_content)
        self.assertIn("router:", cfg_content)

        # Fixed items should be recorded
        self.assertGreater(report2.fixed, 0)
        fixed_names = {c.name for c in report2.checks if c.status == "fixed"}
        self.assertIn("Generate Default Config", fixed_names)

    def test_doctor_live_mode(self) -> None:
        """Verify --live flag runs bounded connectivity pings."""
        report = self.doctor.run_diagnostics(live=True)
        live_checks = [c for c in report.checks if c.category == "live"]
        self.assertTrue(len(live_checks) >= 1)

    def test_doctor_report_formatting(self) -> None:
        """Verify format_summary produces readable ANSI and non-ANSI outputs."""
        report = self.doctor.run_diagnostics()
        summary_color = report.format_summary(color=True)
        summary_plain = report.format_summary(color=False)

        self.assertIn("HEX SYSTEM DIAGNOSTIC & HEALTH REPORT", summary_plain)
        self.assertIn("Overall Status:", summary_plain)
        self.assertIn("\033[", summary_color)
        self.assertNotIn("\033[", summary_plain)

    def test_doctor_audio_checks_fallback(self) -> None:
        """Verify audio checks return warnings and remain healthy in headless/no-hardware environments."""
        with patch(
            "harness_core.voice.probe_audio_hardware",
            return_value={
                "platform": "Linux",
                "tts_available": False,
                "tts_engine": None,
                "audio_player": None,
                "mic_available": False,
                "mic_tool": None,
                "devices": [],
            },
        ):
            audio_results = self.doctor.check_audio_devices()
            self.assertEqual(len(audio_results), 3)
            # All 3 should be warnings, 0 errors
            self.assertTrue(all(c.status == "warning" for c in audio_results))
            self.assertFalse(any(c.status == "error" for c in audio_results))

            # Report overall health should remain True despite warnings
            report = DoctorReport(checks=audio_results)
            self.assertTrue(report.is_healthy)
            self.assertEqual(report.warnings, 3)
            self.assertEqual(report.errors, 0)


class TestRecipeDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_recipe_test_")
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recipes_python_detection(self) -> None:
        """Test detection across Python toolchains: pytest, uv, poetry, django."""
        # 1. pytest with pytest.ini
        (self.ws / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "python")
        self.assertEqual(r.name, "Python (pytest)")
        self.assertEqual(r.test, "pytest")
        self.assertEqual(r.test_cmd_args, ["pytest"])
        (self.ws / "pytest.ini").unlink()

        # 2. uv with uv.lock
        (self.ws / "uv.lock").write_text("# uv lockfile\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "python")
        self.assertEqual(r.name, "Python (uv pytest)")
        self.assertEqual(r.test, "uv run pytest")
        self.assertEqual(r.test_cmd_args, ["uv", "run", "pytest"])
        (self.ws / "uv.lock").unlink()

        # 3. poetry with poetry.lock
        (self.ws / "poetry.lock").write_text("# poetry lockfile\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "python")
        self.assertEqual(r.name, "Python (poetry pytest)")
        self.assertEqual(r.test, "poetry run pytest")
        self.assertEqual(r.test_cmd_args, ["poetry", "run", "pytest"])
        (self.ws / "poetry.lock").unlink()

        # 4. Django with manage.py
        (self.ws / "manage.py").write_text("# django manage.py\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "python")
        self.assertEqual(r.name, "Python (Django)")
        self.assertEqual(r.test, "python manage.py test")
        self.assertEqual(r.port, 8000)

    def test_recipes_nodejs_detection(self) -> None:
        """Test detection across Node.js package managers and frameworks."""
        pkg = {
            "name": "my-app",
            "scripts": {
                "build": "next build",
                "test": "jest",
                "start": "next start",
            },
            "dependencies": {
                "next": "^14.0.0",
                "react": "^18.0.0",
            },
        }
        (self.ws / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

        # 1. pnpm lockfile
        (self.ws / "pnpm-lock.yaml").write_text("lockfileVersion: 5.4\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "nodejs")
        self.assertEqual(r.name, "Node.js (pnpm)")
        self.assertEqual(r.test_cmd_args, ["pnpm", "test"])
        self.assertEqual(r.port, 3000)
        (self.ws / "pnpm-lock.yaml").unlink()

        # 2. yarn lockfile
        (self.ws / "yarn.lock").write_text("# yarn lock\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "Node.js (yarn)")
        self.assertEqual(r.test_cmd_args, ["yarn", "test"])
        (self.ws / "yarn.lock").unlink()

        # 3. npm fallback
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "Node.js (npm)")
        self.assertEqual(r.test_cmd_args, ["npm", "test"])

    def test_recipes_rust_go_makefile_docker(self) -> None:
        """Test Rust, Go, Makefile, and Docker Compose recipe discovery."""
        # 1. Rust Cargo.toml
        (self.ws / "Cargo.toml").write_text('[package]\nname = "rust-app"\n', encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "rust")
        self.assertEqual(r.test_cmd_args, ["cargo", "test"])
        (self.ws / "Cargo.toml").unlink()

        # 2. Go go.mod
        (self.ws / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "go")
        self.assertEqual(r.test_cmd_args, ["go", "test", "./..."])
        (self.ws / "go.mod").unlink()

        # 3. Makefile
        (self.ws / "Makefile").write_text("build:\n\t@echo build\n\ntest:\n\t@echo test\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "makefile")
        self.assertEqual(r.test_cmd_args, ["make", "test"])
        (self.ws / "Makefile").unlink()

        # 4. Docker Compose
        (self.ws / "docker-compose.yml").write_text("version: '3.8'\nservices:\n  app:\n    image: test\n", encoding="utf-8")
        r = detect_recipe(self.ws)
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "docker")
        self.assertEqual(r.test_cmd_args, ["docker", "compose", "run", "--rm", "test"])


class TestGatewayRecipeIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_gw_recipe_")
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_gateway_uses_detected_recipe(self) -> None:
        """Verify ToolGateway._tool_run_tests utilizes the auto-detected recipe command."""
        # Set up a Cargo.toml in the workspace
        (self.ws / "Cargo.toml").write_text('[package]\nname = "my_rust_pkg"\n', encoding="utf-8")

        mock_ledger = MagicMock()
        mock_sandbox = MagicMock()
        mock_sandbox.execute.return_value = MagicMock(
            exit_code=0,
            stdout="cargo test success: 5 passed",
            stderr="",
            timed_out=False,
            violation_detected=False,
            sandbox_backend="restricted_subprocess",
        )

        gw = ToolGateway(ledger=mock_ledger, sandbox=mock_sandbox)
        res = gw._tool_run_tests(self.ws, {})

        self.assertTrue(res["ok"])
        self.assertEqual(res["recipe"], "Rust (cargo)")

        # Verify sandbox was invoked with cargo test
        mock_sandbox.execute.assert_called_once()
        invoked_cmd = mock_sandbox.execute.call_args[1]["cmd"]
        self.assertEqual(invoked_cmd, ["cargo", "test"])


class TestGraphifyPersistentCacheAndPolyglot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_graphify_cache_")
        self.ws = Path(self.tmp.name)
        self.cache_file = self.ws / ".hex" / "graph.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_graphify_persistent_cache(self) -> None:
        """Verify persistent cache creation, warm cache reload without re-parsing, and <30ms execution."""
        # Create 10 python files
        for i in range(10):
            (self.ws / f"module_{i}.py").write_text(
                f"def func_{i}(x: int) -> int:\n    return x + {i}\n",
                encoding="utf-8",
            )

        # Cold start
        adapter1 = ActiveGraphifyAdapter(self.ws, cache_file=self.cache_file)
        res1 = adapter1.build_or_refresh()
        self.assertEqual(res1["indexed_files"], 10)
        self.assertEqual(res1["cache_hits"], 0)
        self.assertEqual(res1["cache_misses"], 10)

        # Verify cache file was created on disk
        self.assertTrue(self.cache_file.is_file())
        cache_data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(cache_data["version"], 1)
        self.assertEqual(len(cache_data["files"]), 10)

        # Warm start using a completely fresh adapter instance (simulating new process startup)
        adapter2 = ActiveGraphifyAdapter(self.ws, cache_file=self.cache_file)
        t0 = time.perf_counter()
        res2 = adapter2.build_or_refresh()
        duration_ms = (time.perf_counter() - t0) * 1000.0

        # Must hit cache 100% and execute warm start in < 30ms
        self.assertEqual(res2["cache_hits"], 10)
        self.assertEqual(res2["cache_misses"], 0)
        self.assertTrue(res2["disk_cache_loaded"])
        self.assertLess(duration_ms, 30.0, f"Warm reload took {duration_ms:.2f}ms (must be < 30ms)")

        # Invalidate 1 file by updating content
        time.sleep(0.02)
        (self.ws / "module_3.py").write_text(
            "def func_3(x: int) -> int:\n    return x + 999\n",
            encoding="utf-8",
        )

        res3 = adapter2.build_or_refresh()
        self.assertEqual(res3["cache_hits"], 9)
        self.assertEqual(res3["cache_misses"], 1)

    def test_graphify_multilanguage_symbols(self) -> None:
        """Verify symbol extraction across TypeScript/JavaScript, Rust, Go, and Python files."""
        # 1. TypeScript / React file
        (self.ws / "UserWidget.tsx").write_text(
            "import { useState } from 'react';\n"
            "export interface UserProps { id: string; }\n"
            "export function UserWidget(props: UserProps) {\n"
            "  const [name, setName] = useState('Alice');\n"
            "  return <div>{name}</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        # 2. Rust file
        (self.ws / "engine.rs").write_text(
            "pub struct EngineConfig {\n    pub workers: usize,\n}\n\n"
            "pub enum EngineState {\n    Running,\n    Stopped,\n}\n\n"
            "pub fn start_engine(cfg: EngineConfig) -> bool {\n"
            "    true\n"
            "}\n",
            encoding="utf-8",
        )

        # 3. Go file
        (self.ws / "server.go").write_text(
            "package main\n\n"
            "import (\n    \"fmt\"\n)\n\n"
            "type ServerConfig struct {\n    Port int\n}\n\n"
            "func StartServer(cfg ServerConfig) error {\n"
            "    fmt.Println(cfg.Port)\n"
            "    return nil\n"
            "}\n",
            encoding="utf-8",
        )

        # 4. Python file
        (self.ws / "service.py").write_text(
            "class AnalyticsService:\n"
            "    def compute_metrics(self) -> dict:\n"
            "        return {}\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.ws, cache_file=self.cache_file)
        stats = adapter.build_or_refresh()
        self.assertEqual(stats["indexed_files"], 4)

        # Validate TypeScript component
        self.assertIn("UserWidget", adapter.symbols_by_name)
        ts_sym = adapter.symbols_by_name["UserWidget"][0]
        self.assertEqual(ts_sym.kind, "component")

        # Validate Rust struct and function
        self.assertIn("EngineConfig", adapter.symbols_by_name)
        rs_struct = adapter.symbols_by_name["EngineConfig"][0]
        self.assertEqual(rs_struct.kind, "struct")

        self.assertIn("start_engine", adapter.symbols_by_name)
        rs_fn = adapter.symbols_by_name["start_engine"][0]
        self.assertEqual(rs_fn.kind, "function")

        # Validate Go struct and function
        self.assertIn("ServerConfig", adapter.symbols_by_name)
        go_struct = adapter.symbols_by_name["ServerConfig"][0]
        self.assertEqual(go_struct.kind, "struct")

        self.assertIn("StartServer", adapter.symbols_by_name)
        go_fn = adapter.symbols_by_name["StartServer"][0]
        self.assertEqual(go_fn.kind, "function")

        # Validate Python class
        self.assertIn("AnalyticsService", adapter.symbols_by_name)
        py_cls = adapter.symbols_by_name["AnalyticsService"][0]
        self.assertEqual(py_cls.kind, "class")


class TestCLIDoctorAndRecipeSubcommands(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_cli_doc_rcp_")
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_doctor_and_recipe_subcommands(self) -> None:
        """Verify hex doctor and hex recipe CLI subcommands execute and produce structured outputs."""
        # 1. hex doctor --json
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            code = cli_main(["--data-dir", str(self.ws / ".hex_data"), "doctor", "--workspace", str(self.ws), "--json"])

        output_str = stdout_capture.getvalue()
        self.assertEqual(code, 0)
        report_data = json.loads(output_str)
        self.assertTrue(report_data["is_healthy"])
        self.assertIn("checks", report_data)

        # 2. hex recipe --json in an empty directory
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            code = cli_main(["recipe", str(self.ws), "--json"])
        self.assertEqual(code, 1)

        # 3. hex recipe in directory with package.json
        (self.ws / "package.json").write_text('{"name": "test-pkg", "scripts": {"test": "mocha"}}\n', encoding="utf-8")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            code = cli_main(["recipe", str(self.ws), "--json"])
        self.assertEqual(code, 0)
        recipe_data = json.loads(stdout_capture.getvalue())
        self.assertTrue(recipe_data["detected"])
        self.assertEqual(recipe_data["recipe"]["kind"], "nodejs")


if __name__ == "__main__":
    unittest.main()

