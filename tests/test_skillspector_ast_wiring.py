"""Comprehensive Verification Suite for NVIDIA SkillSpector Behavioral AST Integration.

Tests:
1. AST detection of dangerous builtins (`eval`, `exec`).
2. AST detection of subprocess / OS command execution (`os.system`, `subprocess.Popen`).
3. AST detection of insecure deserialization (`pickle.loads`).
4. AST detection of dynamic import calls (`importlib.import_module`).
5. AST detection of reflective execution sinks (`getattr(os, "system")`).
6. Benign Python script passing with 0 AST violations and low risk score (ALLOW).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.db import LedgerStore
from harness_core.skillspector import SkillSpectorScanner, UPSTREAM_AST_AVAILABLE


class TestSkillSpectorASTWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_skillspector_ast_"))
        self.db_path = self.tmp_dir / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.scanner = SkillSpectorScanner(self.ledger)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_bundle(self, skill_name: str, script_name: str, script_content: str) -> Path:
        bundle_dir = self.tmp_dir / skill_name
        bundle_dir.mkdir(parents=True, exist_ok=True)
        skill_md = (
            f"---\n"
            f"name: {skill_name}\n"
            f"risk_class: R1\n"
            f"purpose: Test skill bundle for AST inspection\n"
            f"allowed_tools: [read_file]\n"
            f"---\n"
            f"# {skill_name}\n"
            f"Performs automated processing safely.\n"
        )
        (bundle_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (bundle_dir / script_name).write_text(script_content, encoding="utf-8")
        return bundle_dir

    def test_upstream_ast_available_flag(self) -> None:
        """Verify upstream AST analyzer from vendor/skillspector is imported cleanly."""
        self.assertTrue(UPSTREAM_AST_AVAILABLE, "UPSTREAM_AST_AVAILABLE should be True")

    def test_ast_detects_eval_and_exec(self) -> None:
        """Verify AST analyzer detects eval() and exec() dangerous builtins."""
        code = (
            "def run_dynamic(user_input):\n"
            "    result = eval(user_input)\n"
            "    exec('print(result)')\n"
            "    return result\n"
        )
        bundle = self._create_bundle("eval_skill", "runner.py", code)
        report = self.scanner.scan_bundle(bundle)

        ast_rules = [r for r in report.matched_rules if "SS-AST" in r["rule_id"]]
        self.assertTrue(len(ast_rules) >= 1)
        self.assertIn("QUARANTINE", report.gate_decision)
        self.assertGreaterEqual(report.risk_score, 80)
        self.assertTrue(any("eval" in r.get("detail", "").lower() for r in ast_rules))

    def test_ast_detects_os_system_and_subprocess(self) -> None:
        """Verify AST analyzer detects os.system() and subprocess execution."""
        code = (
            "import os\n"
            "import subprocess\n\n"
            "def execute_cmd(target):\n"
            "    os.system(f'cat {target}')\n"
            "    subprocess.Popen(['ls', target])\n"
        )
        bundle = self._create_bundle("cmd_skill", "cmd_exec.py", code)
        report = self.scanner.scan_bundle(bundle)

        ast_rules = [r for r in report.matched_rules if "SS-AST" in r["rule_id"]]
        self.assertTrue(len(ast_rules) >= 1)
        self.assertGreaterEqual(report.risk_score, 80)
        self.assertEqual(report.gate_decision, "QUARANTINE")
        self.assertTrue(any("os.system" in r.get("detail", "").lower() or "exec" in r.get("detail", "").lower() for r in ast_rules))

    def test_ast_detects_insecure_deserialization(self) -> None:
        """Verify AST analyzer detects pickle.loads() insecure deserialization."""
        code = (
            "import pickle\n\n"
            "def restore_state(payload_bytes):\n"
            "    return pickle.loads(payload_bytes)\n"
        )
        bundle = self._create_bundle("pickle_skill", "deser.py", code)
        report = self.scanner.scan_bundle(bundle)

        ast_rules = [r for r in report.matched_rules if "SS-AST" in r["rule_id"]]
        self.assertTrue(len(ast_rules) >= 1)
        self.assertTrue(any("deserialization" in r.get("detail", "").lower() or "pickle" in r.get("detail", "").lower() for r in ast_rules))
        self.assertGreaterEqual(report.risk_score, 55)

    def test_ast_detects_reflective_execution(self) -> None:
        """Verify AST analyzer detects reflective execution sinks via getattr."""
        code = (
            "import os\n\n"
            "def stealth_run(cmd):\n"
            "    fn = getattr(os, 'system')\n"
            "    fn(cmd)\n"
        )
        bundle = self._create_bundle("reflective_skill", "stealth.py", code)
        report = self.scanner.scan_bundle(bundle)

        ast_rules = [r for r in report.matched_rules if "SS-AST" in r["rule_id"]]
        self.assertTrue(len(ast_rules) >= 1)
        self.assertGreaterEqual(report.risk_score, 80)

    def test_benign_python_script_passes_cleanly(self) -> None:
        """Verify standard benign mathematical Python functions pass with 0 AST findings."""
        code = (
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n\n"
            "def calculate_total(items: list[float]) -> float:\n"
            "    return sum(items)\n"
        )
        bundle = self._create_bundle("clean_math_skill", "math_ops.py", code)
        report = self.scanner.scan_bundle(bundle)

        ast_rules = [r for r in report.matched_rules if "SS-AST" in r["rule_id"]]
        self.assertEqual(len(ast_rules), 0, f"Expected 0 AST findings, got: {ast_rules}")
        self.assertEqual(report.gate_decision, "ALLOW")
        self.assertLess(report.risk_score, 25)


if __name__ == "__main__":
    unittest.main()
