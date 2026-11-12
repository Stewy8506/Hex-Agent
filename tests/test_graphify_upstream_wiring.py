"""Unit and Integration Tests for Graphify Upstream CLI Runner, Ingestion Adapter & CLI Wiring.

Verifies:
1. GraphifyUpstreamRunner discovery, CLI/module execution, timeout protection.
2. In-process fallback execution via graphify.watch._rebuild_code.
3. ActiveGraphifyAdapter.ingest_upstream_graph loading valid JSON graphs into NetworkX DiGraph,
   populating SymbolDef records, callers_of, and file_of_qualified mappings.
4. Clean fallback when graph.json is absent, preserving 0ms native AST indexing.
5. hex graphify CLI commands (status, update, ingest, god-nodes, affected, tree).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.graphify import (
    ActiveGraphifyAdapter,
    GraphifyUpstreamRunner,
    SymbolDef,
    UPSTREAM_GRAPHIFY_AVAILABLE,
)


class TestGraphifyUpstreamWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

        # Create basic workspace with code and test files
        (self.ws / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n"
            "    res = 0\n"
            "    for _ in range(b):\n"
            "        res = add(res, a)\n"
            "    return res\n",
            encoding="utf-8",
        )
        (self.ws / "test_calc.py").write_text(
            "import unittest\n"
            "from calc import add, multiply\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_runner_discovery_and_command(self) -> None:
        runner = GraphifyUpstreamRunner()
        self.assertTrue(runner.is_cli_available(), "Graphify CLI or vendor directory must be discovered")

        # Test command invocation when binary exists
        with patch("shutil.which", return_value="/fake/bin/graphify"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["/fake/bin/graphify", "status"],
                    returncode=0,
                    stdout="Graphify ready",
                    stderr="",
                )
                code, out, err = runner.run_command("status", args=["--json"], cwd=self.ws)
                self.assertEqual(code, 0)
                self.assertEqual(out, "Graphify ready")
                mock_run.assert_called_once()
                call_args = mock_run.call_args[0][0]
                self.assertEqual(call_args[:2], ["/fake/bin/graphify", "status"])
                self.assertIn("--json", call_args)

        # Test command invocation via python -m graphify with vendor dir on PYTHONPATH
        with patch("shutil.which", return_value=None):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["python", "-m", "graphify", "update"],
                    returncode=0,
                    stdout="Rebuilt",
                    stderr="",
                )
                code, out, err = runner.run_command("update", cwd=self.ws)
                self.assertEqual(code, 0)
                self.assertEqual(out, "Rebuilt")
                call_args = mock_run.call_args[0][0]
                self.assertIn("-m", call_args)
                self.assertIn("graphify", call_args)
                env = mock_run.call_args[1]["env"]
                self.assertIn("PYTHONPATH", env)
                self.assertIn(str(runner.vendor_dir), env["PYTHONPATH"])

        # Test timeout handling
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["graphify"], timeout=5.0)):
            code, out, err = runner.run_command("update", timeout=5.0)
            self.assertEqual(code, -1)
            self.assertIn("Timeout expired", err)

    def test_runner_inprocess_fallback(self) -> None:
        runner = GraphifyUpstreamRunner()

        def fake_rebuild(watch_path: Path, **kwargs) -> bool:
            out_dir = watch_path / "graphify-out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "graph.json").write_text(
                json.dumps({"nodes": [{"id": "calc.py", "kind": "file"}], "edges": []}),
                encoding="utf-8",
            )
            return True

        # When CLI fails or binary is absent, falls back to in-process _rebuild_code
        with patch.object(runner, "run_command", return_value=(-1, "", "Command failed")):
            with patch("graphify.watch._rebuild_code", side_effect=fake_rebuild):
                ok, path = runner.run_update(self.ws, force=True, no_cluster=True)
                self.assertTrue(ok)
                self.assertTrue(path.is_file())
                self.assertTrue(path.name == "graph.json")

                ok_ext, path_ext = runner.run_extract(self.ws, force=True)
                self.assertTrue(ok_ext)
                self.assertTrue(path_ext.is_file())

    def test_ingest_upstream_graph_valid_json(self) -> None:
        adapter = ActiveGraphifyAdapter(self.ws)

        # Prepare a rich mock upstream graph.json in graphify-out/
        out_dir = self.ws / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        graph_file = out_dir / "graph.json"

        mock_payload = {
            "version": 1,
            "directed": True,
            "nodes": [
                {
                    "id": "calc.py",
                    "label": "calc.py",
                    "kind": "file",
                    "file_type": "code",
                    "source_file": "calc.py",
                },
                {
                    "id": "calc.py:add",
                    "label": "add",
                    "kind": "function",
                    "file_type": "code",
                    "source_file": "calc.py",
                    "lineno": 1,
                    "end_lineno": 2,
                },
                {
                    "id": "calc.py:multiply",
                    "label": "multiply",
                    "kind": "function",
                    "file_type": "code",
                    "source_file": "calc.py",
                    "lineno": 4,
                    "end_lineno": 8,
                },
                {
                    "id": "test_calc.py:TestCalc",
                    "label": "TestCalc",
                    "kind": "class",
                    "file_type": "code",
                    "source_file": "test_calc.py",
                    "lineno": 4,
                    "end_lineno": 7,
                },
            ],
            "links": [
                {
                    "source": "calc.py",
                    "target": "calc.py:add",
                    "relation": "defines",
                    "confidence": "EXTRACTED",
                    "source_file": "calc.py",
                },
                {
                    "source": "calc.py",
                    "target": "calc.py:multiply",
                    "relation": "defines",
                    "confidence": "EXTRACTED",
                    "source_file": "calc.py",
                },
                {
                    "source": "calc.py:multiply",
                    "target": "calc.py:add",
                    "relation": "calls",
                    "confidence": "EXTRACTED",
                    "source_file": "calc.py",
                },
                {
                    "source": "test_calc.py:TestCalc",
                    "target": "calc.py:add",
                    "relation": "calls",
                    "confidence": "INFERRED",
                    "source_file": "test_calc.py",
                },
            ],
        }
        graph_file.write_text(json.dumps(mock_payload, indent=2), encoding="utf-8")

        res = adapter.ingest_upstream_graph()
        self.assertTrue(res["ingested"])
        self.assertEqual(res["path"], str(graph_file))
        self.assertEqual(res["node_count"], 4)
        self.assertEqual(res["edge_count"], 4)

        # Check NetworkX graph
        self.assertIsNotNone(adapter.graph)
        self.assertIn("calc.py:add", adapter.graph.nodes)
        self.assertIn("calc.py:multiply", adapter.graph.nodes)
        self.assertTrue(adapter.graph.has_edge("calc.py:multiply", "calc.py:add"))

        # Check symbol definitions
        calc_syms = adapter.symbols_by_file.get("calc.py", [])
        sym_names = {s.name for s in calc_syms}
        self.assertIn("add", sym_names)
        self.assertIn("multiply", sym_names)

        add_sym = [s for s in calc_syms if s.name == "add"][0]
        self.assertEqual(add_sym.lineno, 1)
        self.assertEqual(add_sym.end_lineno, 2)
        self.assertEqual(add_sym.kind, "function")

        # Check callers_of reverse mapping
        add_callers = adapter.callers_of.get("calc.py:add", set())
        self.assertIn("calc.py:multiply", add_callers)
        self.assertIn("test_calc.py:TestCalc", add_callers)

        # Check file_of_qualified mapping
        self.assertEqual(adapter.file_of_qualified.get("calc.py:add"), "calc.py")
        self.assertEqual(adapter.file_of_qualified.get("add"), "calc.py")

    def test_ingest_upstream_graph_missing_fallback(self) -> None:
        adapter = ActiveGraphifyAdapter(self.ws)
        # No graphify-out/graph.json or .hex/graph.json present
        res = adapter.ingest_upstream_graph()
        self.assertFalse(res["ingested"])
        self.assertEqual(res["node_count"], 0)
        self.assertEqual(res["edge_count"], 0)
        self.assertIsNone(res["path"])

        # Preserves 0ms native AST indexing
        stats = adapter.build_or_refresh(self.ws)
        self.assertGreater(stats["indexed_files"], 0)
        self.assertIn("calc.py", adapter.symbols_by_file)
        self.assertIn("add", [s.name for s in adapter.symbols_by_file["calc.py"]])

    def test_cli_graphify_commands(self) -> None:
        buf = io.StringIO()

        # 1. hex graphify status
        with redirect_stdout(buf):
            ret = main(["--data-dir", str(self.ws / ".hex_data"), "graphify", "status", "--workspace", str(self.ws)])
        self.assertEqual(ret, 0)
        self.assertIn("HEX Graphify Upstream Status", buf.getvalue())

        # 2. hex graphify status --json
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--data-dir", str(self.ws / ".hex_data"), "graphify", "status", "--workspace", str(self.ws), "--json"])
        self.assertEqual(ret, 0)
        status_data = json.loads(buf.getvalue())
        self.assertTrue(status_data["cli_available"])

        # 3. hex graphify ingest (clean fallback when graph not yet built)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--data-dir", str(self.ws / ".hex_data"), "graphify", "ingest", "--workspace", str(self.ws), "--json"])
        self.assertEqual(ret, 0)
        ing_data = json.loads(buf.getvalue())
        self.assertFalse(ing_data["ingested"])

        # 4. hex graphify god-nodes
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--data-dir", str(self.ws / ".hex_data"), "graphify", "god-nodes", "--workspace", str(self.ws)])
        self.assertEqual(ret, 0)
        self.assertIn("Architectural God Nodes", buf.getvalue())

        # 5. hex graphify affected
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--data-dir", str(self.ws / ".hex_data"), "graphify", "affected", "add", "--workspace", str(self.ws), "--json"])
        self.assertEqual(ret, 0)
        aff_data = json.loads(buf.getvalue())
        self.assertEqual(aff_data["target"], "add")

        # 6. hex graphify tree
        out_tree = self.ws / "tree.html"
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main([
                "--data-dir", str(self.ws / ".hex_data"),
                "graphify", "tree",
                "--workspace", str(self.ws),
                "--output", str(out_tree),
                "--json",
            ])
        self.assertEqual(ret, 0)
        self.assertTrue(out_tree.is_file())


if __name__ == "__main__":
    unittest.main()
