"""Unit and Integration Tests for Polyglot Graph Unification and Blast-Radius Tracking.

Verifies:
1. ActiveGraphifyAdapter._is_test_file polyglot extensions across Python, TypeScript/JavaScript,
   Go, Rust, Java, C/C++, while correctly rejecting production source files.
2. ActiveGraphifyAdapter.compute_blast_radius upstream & multi-language dependency traversal:
   verifying 1-hop and 2-hop dependents and impacted test pinning across polyglot repositories.
3. ToolGateway with ActiveBlastRadiusInterceptor intercepting polyglot code writes (Go, TS, Rust),
   calculating ast_blast_radius, and journaling impacted test checks in the task ledger.
4. ActiveBlastRadiusInterceptor helper contract for direct usage and non-code file exclusion.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_core.db import LedgerStore
from harness_core.gateway import ActiveBlastRadiusInterceptor, ToolGateway
from harness_core.graphify import (
    ActiveGraphifyAdapter,
    BlastRadiusReport,
    SUPPORTED_CODE_EXTENSIONS,
)


class TestGraphifyPolyglotBlastRadius(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.db_path = self.ws / "test_ledger.db"
        self.ledger = LedgerStore(f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def test_is_test_file_polyglot_extensions(self) -> None:
        """Validates _is_test_file across Python, TS/JS, Go, Rust, Java, C++, rejecting production files."""
        adapter = ActiveGraphifyAdapter(self.ws)

        # 1. Python test conventions
        self.assertTrue(adapter._is_test_file("test_api.py"))
        self.assertTrue(adapter._is_test_file("api_test.py"))
        self.assertTrue(adapter._is_test_file("tests/test_calc.py"))
        self.assertTrue(adapter._is_test_file("sub/tests/utils.py"))

        # 2. TypeScript / JavaScript test conventions
        self.assertTrue(adapter._is_test_file("auth.test.ts"))
        self.assertTrue(adapter._is_test_file("auth.test.tsx"))
        self.assertTrue(adapter._is_test_file("auth.spec.ts"))
        self.assertTrue(adapter._is_test_file("auth.spec.tsx"))
        self.assertTrue(adapter._is_test_file("auth.test.js"))
        self.assertTrue(adapter._is_test_file("auth.spec.js"))
        self.assertTrue(adapter._is_test_file("__tests__/modal.tsx"))

        # 3. Go test conventions
        self.assertTrue(adapter._is_test_file("router_test.go"))
        self.assertTrue(adapter._is_test_file("pkg/server/handler_test.go"))

        # 4. Rust test conventions
        self.assertTrue(adapter._is_test_file("test_parser.rs"))
        self.assertTrue(adapter._is_test_file("parser_test.rs"))
        self.assertTrue(adapter._is_test_file("tests/parser.rs"))
        self.assertTrue(adapter._is_test_file("tests/common/mod.rs"))

        # 5. Java test conventions
        self.assertTrue(adapter._is_test_file("OrderServiceTest.java"))
        self.assertTrue(adapter._is_test_file("OrderServiceTests.java"))
        self.assertTrue(adapter._is_test_file("OrderServiceTestCase.java"))
        self.assertTrue(adapter._is_test_file("TestOrderService.java"))
        self.assertTrue(adapter._is_test_file("src/test/java/OrderServiceTest.java"))

        # 6. C/C++ test conventions
        self.assertTrue(adapter._is_test_file("test_crypto.cpp"))
        self.assertTrue(adapter._is_test_file("test_crypto.cc"))
        self.assertTrue(adapter._is_test_file("crypto_test.cpp"))
        self.assertTrue(adapter._is_test_file("crypto_unittest.cc"))
        self.assertTrue(adapter._is_test_file("tests/crypto.cpp"))

        # 7. Common test directories
        self.assertTrue(adapter._is_test_file("tests/helper.py"))
        self.assertTrue(adapter._is_test_file("__tests__/button.tsx"))
        self.assertTrue(adapter._is_test_file("spec/models/user_spec.rb"))

        # 8. Rejection of production source files
        self.assertFalse(adapter._is_test_file("auth.ts"))
        self.assertFalse(adapter._is_test_file("router.go"))
        self.assertFalse(adapter._is_test_file("OrderService.java"))
        self.assertFalse(adapter._is_test_file("parser.rs"))
        self.assertFalse(adapter._is_test_file("crypto.cpp"))
        self.assertFalse(adapter._is_test_file("api.py"))
        self.assertFalse(adapter._is_test_file("src/service.go"))
        self.assertFalse(adapter._is_test_file("src/components/Modal.tsx"))
        self.assertFalse(adapter._is_test_file("src/main.rs"))
        self.assertFalse(adapter._is_test_file("src/crypto.h"))

    def test_compute_blast_radius_upstream_polyglot(self) -> None:
        """Builds a multi-language graph and verifies 1-hop/2-hop dependents and impacted tests."""
        adapter = ActiveGraphifyAdapter(self.ws)

        # Ingest mock upstream graph containing Go backend and TypeScript frontend
        out_dir = self.ws / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        graph_file = out_dir / "graph.json"

        mock_graph = {
            "version": 1,
            "directed": True,
            "nodes": [
                # Go backend
                {
                    "id": "db.go:Query",
                    "label": "Query",
                    "kind": "function",
                    "source_file": "db.go",
                    "file_type": "code",
                },
                {
                    "id": "service.go:HandleUser",
                    "label": "HandleUser",
                    "kind": "function",
                    "source_file": "service.go",
                    "file_type": "code",
                },
                {
                    "id": "handler.go:ServeHTTP",
                    "label": "ServeHTTP",
                    "kind": "function",
                    "source_file": "handler.go",
                    "file_type": "code",
                },
                {
                    "id": "db_test.go:TestQuery",
                    "label": "TestQuery",
                    "kind": "function",
                    "source_file": "db_test.go",
                    "file_type": "code",
                },
                # TypeScript frontend
                {
                    "id": "src/api.ts:fetchUser",
                    "label": "fetchUser",
                    "kind": "function",
                    "source_file": "src/api.ts",
                    "file_type": "code",
                },
                {
                    "id": "src/Profile.tsx:Profile",
                    "label": "Profile",
                    "kind": "component",
                    "source_file": "src/Profile.tsx",
                    "file_type": "code",
                },
                {
                    "id": "src/Profile.test.tsx:TestProfile",
                    "label": "TestProfile",
                    "kind": "function",
                    "source_file": "src/Profile.test.tsx",
                    "file_type": "code",
                },
            ],
            "links": [
                # Go calls
                {
                    "source": "service.go:HandleUser",
                    "target": "db.go:Query",
                    "relation": "calls",
                    "source_file": "service.go",
                },
                {
                    "source": "handler.go:ServeHTTP",
                    "target": "service.go:HandleUser",
                    "relation": "calls",
                    "source_file": "handler.go",
                },
                {
                    "source": "db_test.go:TestQuery",
                    "target": "db.go:Query",
                    "relation": "calls",
                    "source_file": "db_test.go",
                },
                # TS calls
                {
                    "source": "src/Profile.tsx:Profile",
                    "target": "src/api.ts:fetchUser",
                    "relation": "calls",
                    "source_file": "src/Profile.tsx",
                },
                {
                    "source": "src/Profile.test.tsx:TestProfile",
                    "target": "src/Profile.tsx:Profile",
                    "relation": "calls",
                    "source_file": "src/Profile.test.tsx",
                },
            ],
        }
        graph_file.write_text(json.dumps(mock_graph, indent=2), encoding="utf-8")

        summary = adapter.ingest_upstream_graph(graph_file)
        self.assertTrue(summary["ingested"])
        self.assertEqual(summary["node_count"], 7)

        # 1. Test blast radius on Go backend modification
        br_go = adapter.compute_blast_radius(self.ws, ["db.go"])
        self.assertIn("db.go", br_go.modified_files)
        self.assertIn("Query", br_go.modified_symbols)

        # 1-hop dependent must include service.go:HandleUser
        hop1_ids = {d["qualified_name"] for d in br_go.hop1_dependents}
        self.assertIn("service.go:HandleUser", hop1_ids)

        # 2-hop dependent must include handler.go:ServeHTTP
        hop2_ids = {d["qualified_name"] for d in br_go.hop2_dependents}
        self.assertIn("handler.go:ServeHTTP", hop2_ids)

        # Impacted tests must pin db_test.go
        self.assertIn("db_test.go", br_go.impacted_tests)
        self.assertIn("impacts", br_go.warning_message)
        self.assertIn("db_test.go", br_go.warning_message)

        # 2. Test blast radius on TypeScript frontend modification
        br_ts = adapter.compute_blast_radius(self.ws, ["src/api.ts"])
        self.assertIn("src/api.ts", br_ts.modified_files)
        self.assertIn("fetchUser", br_ts.modified_symbols)

        hop1_ts = {d["qualified_name"] for d in br_ts.hop1_dependents}
        self.assertIn("src/Profile.tsx:Profile", hop1_ts)

        self.assertIn("src/Profile.test.tsx", br_ts.impacted_tests)

    def test_compute_blast_radius_native_java_and_cpp(self) -> None:
        """Validates native polyglot AST indexing and blast-radius tracking for Java and C++ files."""
        # Setup Java source and test files
        (self.ws / "OrderService.java").write_text(
            "package com.hex.app;\n"
            "public class OrderService {\n"
            "    public void placeOrder(String item) {\n"
            "        processPayment(item);\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.ws / "OrderServiceTest.java").write_text(
            "package com.hex.app;\n"
            "import com.hex.app.OrderService;\n"
            "public class OrderServiceTest {\n"
            "    public void testPlaceOrder() {\n"
            "        OrderService service = new OrderService();\n"
            "        service.placeOrder(\"laptop\");\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )

        # Setup C++ source and test files
        (self.ws / "crypto.cpp").write_text(
            '#include "crypto.h"\n'
            "int hash_token(const char* data) {\n"
            "    return 42;\n"
            "}\n",
            encoding="utf-8",
        )
        (self.ws / "test_crypto.cpp").write_text(
            '#include "crypto.h"\n'
            "void test_hash() {\n"
            '    int h = hash_token("secret");\n'
            "}\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.ws)
        stats = adapter.build_or_refresh(self.ws, force=True)
        self.assertGreaterEqual(stats["indexed_symbols"], 2)

        # Blast radius on Java file modification
        br_java = adapter.compute_blast_radius(self.ws, ["OrderService.java"])
        self.assertIn("OrderService.java", br_java.modified_files)
        self.assertIn("OrderServiceTest.java", br_java.impacted_tests)

        # Blast radius on C++ file modification
        br_cpp = adapter.compute_blast_radius(self.ws, ["crypto.cpp"])
        self.assertIn("crypto.cpp", br_cpp.modified_files)
        self.assertIn("test_crypto.cpp", br_cpp.impacted_tests)

    def test_gateway_interceptor_polyglot_write_file(self) -> None:
        """Verifies ToolGateway with polyglot file (Go/TS/Rust) triggers ast_blast_radius and updates ledger."""
        # 1. Setup workspace with Go service and test
        src_dir = self.ws / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "service.go").write_text(
            "package main\n"
            "func ComputeResult() int {\n"
            "    return 100\n"
            "}\n",
            encoding="utf-8",
        )
        (src_dir / "service_test.go").write_text(
            "package main\n"
            'import "testing"\n'
            "func TestComputeResult(t *testing.T) {\n"
            "    val := ComputeResult()\n"
            "}\n",
            encoding="utf-8",
        )

        # 2. Setup Ledger and active task / plan
        task = self.ledger.submit_task(
            {"goal": "Implement Go service with unit test validation", "status": "QUEUED"}
        )
        task_id = task["task_id"]

        attempt = self.ledger.acquire_lease_and_attempt(task_id, worker_id="worker_01")
        attempt_id = attempt["attempt_id"]
        fencing_token = attempt.get("fencing_token", 1)

        self.ledger.save_plan(
            task_id=task_id,
            dag_nodes=[{"id": "step_1", "action": "Update service implementation"}],
            blast_radius_checks=[],
        )
        self.ledger.transition_task(task_id, "RUNNING")

        # 3. Instantiate ActiveGraphifyAdapter and ToolGateway
        adapter = ActiveGraphifyAdapter(self.ws)
        gateway = ToolGateway(ledger=self.ledger, graphify=adapter)

        # 4. Invoke write_file on Go file
        new_go_content = (
            "package main\n"
            "func ComputeResult() int {\n"
            "    return 200\n"
            "}\n"
        )
        result = gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id,
            step_id="step_1",
            sequence_in_step=1,
            tool_id="write_file",
            args={"path": "src/service.go", "content": new_go_content},
            workspace_dir=self.ws,
            fencing_token=fencing_token,
        )

        self.assertTrue(result["ok"])
        self.assertIn("ast_blast_radius", result)
        ast_br = result["ast_blast_radius"]
        self.assertEqual(ast_br["modified_files"], ["src/service.go"])
        self.assertIn("src/service_test.go", ast_br["impacted_tests"])

        # 5. Verify plan in Ledger received blast_radius_checks update
        latest_plan = self.ledger.get_latest_plan(task_id)
        self.assertIsNotNone(latest_plan)
        checks = latest_plan.get("blast_radius_checks") or []
        self.assertIn("src/service_test.go", checks)

        # 6. Test with TypeScript
        (src_dir / "auth.ts").write_text("export function login() { return true; }\n", encoding="utf-8")
        (src_dir / "auth.test.ts").write_text(
            "import { login } from './auth';\nexport function testLogin() { login(); }\n",
            encoding="utf-8",
        )
        res_ts = gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id,
            step_id="step_1",
            sequence_in_step=2,
            tool_id="write_file",
            args={"path": "src/auth.ts", "content": "export function login() { return false; }\n"},
            workspace_dir=self.ws,
            fencing_token=fencing_token,
        )
        self.assertTrue(res_ts["ok"])
        self.assertIn("ast_blast_radius", res_ts)
        self.assertIn("src/auth.test.ts", res_ts["ast_blast_radius"]["impacted_tests"])
        plan_ts = self.ledger.get_latest_plan(task_id)
        self.assertIn("src/auth.test.ts", plan_ts.get("blast_radius_checks") or [])

        # 7. Test with Rust
        (src_dir / "parser.rs").write_text("pub fn parse() -> bool { true }\n", encoding="utf-8")
        (src_dir / "test_parser.rs").write_text(
            "use super::parser::parse;\npub fn run_test() { parse(); }\n",
            encoding="utf-8",
        )
        res_rs = gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id,
            step_id="step_1",
            sequence_in_step=3,
            tool_id="write_file",
            args={"path": "src/parser.rs", "content": "pub fn parse() -> bool { false }\n"},
            workspace_dir=self.ws,
            fencing_token=fencing_token,
        )
        self.assertTrue(res_rs["ok"])
        self.assertIn("ast_blast_radius", res_rs)
        self.assertIn("src/test_parser.rs", res_rs["ast_blast_radius"]["impacted_tests"])
        plan_rs = self.ledger.get_latest_plan(task_id)
        self.assertIn("src/test_parser.rs", plan_rs.get("blast_radius_checks") or [])

    def test_active_blast_radius_interceptor_contract(self) -> None:
        """Validates ActiveBlastRadiusInterceptor helper contract and non-code file exclusion."""
        adapter = ActiveGraphifyAdapter(self.ws)
        interceptor = ActiveBlastRadiusInterceptor(adapter)

        # Create code file and non-code file
        (self.ws / "index.ts").write_text("export function main() {}", encoding="utf-8")
        (self.ws / "README.md").write_text("# Documentation", encoding="utf-8")

        # Inspecting supported code extension returns BlastRadiusReport
        code_report = interceptor.inspect_file_modification(self.ws, "index.ts")
        self.assertIsNotNone(code_report)
        self.assertIsInstance(code_report, BlastRadiusReport)
        self.assertEqual(code_report.modified_files, ["index.ts"])

        # Inspecting non-code extension returns None
        non_code_report = interceptor.inspect_file_modification(self.ws, "README.md")
        self.assertIsNone(non_code_report)

        # When graphify is None, returns None
        empty_interceptor = ActiveBlastRadiusInterceptor(None)
        self.assertIsNone(empty_interceptor.inspect_file_modification(self.ws, "index.ts"))
        self.assertIsNone(empty_interceptor.compute_blast_radius(self.ws, ["index.ts"]))


if __name__ == "__main__":
    unittest.main()
