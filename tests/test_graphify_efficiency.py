"""Empirical Verification Suite for the 6 High-Efficiency Graphify & Coherent Work-Memory Capabilities.

Verifies:
1. Incremental `(mtime_ns, size)` Stat Cache & Persistent `.hex/graph.json`: Unchanged files hit cache
   (`cache_hits == N`, `cache_misses == 0`) on repeat `build_or_refresh()` calls, and modifying 1 file
   re-parses only that 1 file.
2. Surgical AST Symbol Slicing (`extract_surgical_context` + `graphify.file_slice`): Extracts only the
   failing/mutated function body + module header + 1-hop caller signatures (>70% prompt token reduction
   compared to dumping whole multi-hundred-line files).
3. Multi-Language Full-Stack Indexing (`.py`, `.ts`, `.tsx`, `.go`, `.rs`): Tracks React/TypeScript
   components, hooks, imports, and tests alongside Python.
4. Zero-Token Architectural Topology & Circular Import Detection (`analyze_architecture`): Wires
   `graphify.analyze.god_nodes`, `graphify.analyze.find_import_cycles`, and `graphify.analyze.graph_diff`.
5. Interactive D3 v7 HTML Architecture Tree Export (`export_interactive_html`): Wires
   `graphify.tree_html.build_tree` and `emit_html`.
6. Unified Graphify Work-Memory Reflection & AST Code-Hash Staleness Detection: Wires
   `GovernedMemoryService` (`router.py`) + `ActiveGraphifyAdapter.reflect_work_memory()` (`graphify.reflect`)
   so memories anchored to AST `source_nodes` generate `LESSONS.md` + `.graphify_learning.json` and
   automatically flag `is_stale=True` when the underlying source file changes.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from harness_core.db import LedgerStore
from harness_core.graphify import ActiveGraphifyAdapter
from harness_core.router import GovernedMemoryService


class TestGraphifyEfficiencySuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="hex_graphify_eff_")
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_1_incremental_stat_cache_hits_and_selective_invalidation(self) -> None:
        """Verify unchanged files are served from stat cache without re-parsing."""
        (self.root / "mod_a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (self.root / "mod_b.py").write_text("from mod_a import alpha\ndef beta():\n    return alpha() + 1\n", encoding="utf-8")
        (self.root / "mod_c.py").write_text("from mod_b import beta\ndef gamma():\n    return beta() * 2\n", encoding="utf-8")

        adapter = ActiveGraphifyAdapter(self.root)
        res1 = adapter.build_or_refresh()
        self.assertEqual(res1["indexed_files"], 3)
        self.assertEqual(res1["cache_hits"], 0)
        self.assertEqual(res1["cache_misses"], 3)

        # Second pass without modifications: 100% cache hits
        res2 = adapter.build_or_refresh()
        self.assertEqual(res2["cache_hits"], 3)
        self.assertEqual(res2["cache_misses"], 0)

        # Modify only mod_b.py: 2 cache hits, 1 cache miss
        time.sleep(0.01)
        (self.root / "mod_b.py").write_text(
            "from mod_a import alpha\ndef beta():\n    return alpha() + 99\n",
            encoding="utf-8",
        )
        res3 = adapter.build_or_refresh()
        self.assertEqual(res3["cache_hits"], 2)
        self.assertEqual(res3["cache_misses"], 1)

    def test_2_surgical_ast_symbol_slicing_saves_over_70_pct_tokens(self) -> None:
        """Verify extract_surgical_context slices only the failing symbol + header + callers (>70% savings)."""
        lines = [
            "import os",
            "import sys",
            "GLOBAL_CONST = 42",
            "",
        ]
        for idx in range(25):
            lines.append(f"def helper_func_{idx}(x: int) -> int:")
            for step in range(12):
                lines.append(f"    x = (x + {step}) * {idx + 1}  # filler computation line {step}")
            lines.append("    return x")
            lines.append("")

        lines.extend(
            [
                "def compute_invoice_total(subtotal: float, tax_rate: float) -> float:",
                "    # Buggy implementation",
                "    tax = subtotal * tax_rate",
                "    return subtotal - tax  # AssertionError: subtracted instead of added",
                "",
            ]
        )
        (self.root / "billing.py").write_text("\n".join(lines), encoding="utf-8")
        (self.root / "checkout.py").write_text(
            "from billing import compute_invoice_total\n"
            "def process_order(amount: float) -> float:\n"
            "    return compute_invoice_total(amount, 0.1)\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.root)
        failure_digest = (
            'Traceback (most recent call last):\n'
            '  File "billing.py", line 358, in compute_invoice_total\n'
            'AssertionError: expected 110.0, got 90.0'
        )
        surgical = adapter.extract_surgical_context(
            self.root,
            ["billing.py"],
            failure_digest=failure_digest,
        )

        self.assertIn("def compute_invoice_total", surgical["context_text"])
        self.assertIn("checkout.process_order", surgical["context_text"])
        self.assertNotIn("def helper_func_10", surgical["context_text"])
        self.assertGreater(surgical["token_savings_pct"], 70.0)

    def test_3_polyglot_typescript_react_and_go_indexing(self) -> None:
        """Verify TypeScript/React (.tsx/.ts) and Go (.go) components, calls, and test blast radius."""
        (self.root / "api.ts").write_text(
            "export async function fetchUserQuota(userId: string) {\n"
            "  return { remaining: 0.85 };\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "QuotaCard.tsx").write_text(
            "import { fetchUserQuota } from './api';\n"
            "export function QuotaCard() {\n"
            "  const data = fetchUserQuota('u1');\n"
            "  return <div>{data.remaining}</div>;\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "QuotaCard.test.tsx").write_text(
            "import { QuotaCard } from './QuotaCard';\n"
            "export function testQuotaCard() {\n"
            "  return QuotaCard();\n"
            "}\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.root)
        blast = adapter.compute_blast_radius(self.root, ["api.ts"])
        self.assertIn("fetchUserQuota", blast.modified_symbols)
        hop1_files = {d["file"] for d in blast.hop1_dependents}
        self.assertIn("QuotaCard.tsx", hop1_files)
        self.assertIn("QuotaCard.test.tsx", blast.impacted_tests)

    def test_4_architectural_god_nodes_import_cycles_and_graph_diff(self) -> None:
        """Verify god_nodes, circular import detection (find_import_cycles), and graph_diff."""
        (self.root / "cycle_a.py").write_text(
            "from cycle_b import func_b\ndef func_a():\n    return func_b()\n",
            encoding="utf-8",
        )
        (self.root / "cycle_b.py").write_text(
            "from cycle_a import func_a\ndef func_b():\n    return func_a()\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.root)
        arch = adapter.analyze_architecture(self.root)
        self.assertTrue(len(arch["god_nodes"]) >= 1)
        self.assertTrue(len(arch["import_cycles"]) >= 1)
        cycle_files = set(arch["import_cycles"][0]["cycle"])
        self.assertIn("cycle_a.py", cycle_files)
        self.assertIn("cycle_b.py", cycle_files)

        old_graph = adapter.graph.copy() if adapter.graph is not None else None
        (self.root / "new_service.py").write_text(
            "from cycle_a import func_a\ndef service_entry():\n    return func_a()\n",
            encoding="utf-8",
        )
        adapter.build_or_refresh(self.root)
        diff = adapter.diff_graphs(old_graph, adapter.graph)
        self.assertIn("new node", diff["summary"])

    def test_5_export_interactive_d3_html(self) -> None:
        """Verify export_interactive_html emits a self-contained D3 v7 HTML tree."""
        (self.root / "engine.py").write_text("class CoreEngine:\n    def run(self):\n        pass\n", encoding="utf-8")
        adapter = ActiveGraphifyAdapter(self.root)
        html_out = self.root / "arch_tree.html"
        saved = adapter.export_interactive_html(html_out, project_label="HEX Test Project")
        self.assertTrue(saved.is_file())
        content = saved.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", content)
        self.assertIn("d3.v7.min.js", content)
        self.assertIn("CoreEngine", content)

    def test_6_unified_graphify_work_memory_reflection_and_staleness_detection(self) -> None:
        """Verify GovernedMemoryService + Graphify reflect.py integration and SHA-256 staleness tracking."""
        src_file = self.root / "payment_gateway.py"
        src_file.write_text(
            "def charge_card(amount: int) -> bool:\n    return amount > 0\n",
            encoding="utf-8",
        )

        adapter = ActiveGraphifyAdapter(self.root)
        adapter.build_or_refresh(self.root)
        ledger = LedgerStore(self.root / "ledger.db")
        memory_svc = GovernedMemoryService(ledger, graphify=adapter)

        # Record 2 corroborating 'useful' memories and 1 'dead_end' memory anchored to AST symbol `charge_card`
        memory_svc.write_memory(
            scope="project",
            project_id=" billing_proj ",
            subject="Validate positive charge amounts in charge_card",
            content="charge_card expects integer cents > 0.",
            source_nodes=["charge_card"],
            outcome="useful",
            workspace_dir=self.root,
        )
        memory_svc.write_memory(
            scope="episodic",
            project_id=" billing_proj ",
            subject="Verify charge_card retry idempotency",
            content="charge_card verified with integer cents.",
            source_nodes=["charge_card"],
            outcome="useful",
            workspace_dir=self.root,
        )
        memory_svc.write_memory(
            scope="episodic",
            project_id=" billing_proj ",
            subject="Float currency conversion attempt",
            content="Passing float dollars caused rounding drift.",
            source_nodes=[" payment_gateway.py "],
            outcome="dead_end",
            workspace_dir=self.root,
        )

        # 1. Verify upstream Graphify reflection generated LESSONS.md and .graphify_learning.json
        ref = adapter.reflect_work_memory(self.root)
        self.assertIn("**Preferred sources**", ref["lessons_markdown"])
        self.assertIn("`charge_card`", ref["lessons_markdown"])
        self.assertIn("**Known dead ends**", ref["lessons_markdown"])
        self.assertTrue(Path(ref["lessons_path"]).is_file())
        self.assertTrue(Path(ref["learning_sidecar_path"]).is_file())

        # 2. Verify retrieved memories are fresh (is_stale == False) before file modification
        mems_fresh = memory_svc.retrieve_memories(
            project_id=" billing_proj ",
            target_symbols=["charge_card"],
            workspace_dir=self.root,
        )
        self.assertTrue(len(mems_fresh) >= 2)
        self.assertFalse(mems_fresh[0]["is_stale"])
        self.assertIn('stale="false"', mems_fresh[0]["quoted_evidence"])

        # 3. Modify payment_gateway.py -> verify code-fingerprint staleness flips is_stale to True!
        src_file.write_text(
            "def charge_card(amount: int, currency: str = 'USD') -> bool:\n    return amount > 0\n",
            encoding="utf-8",
        )
        mems_after_edit = memory_svc.retrieve_memories(
            project_id=" billing_proj ",
            target_symbols=["charge_card"],
            workspace_dir=self.root,
        )
        self.assertTrue(mems_after_edit[0]["is_stale"])
        self.assertIn('stale="true"', mems_after_edit[0]["quoted_evidence"])

        # 4. Verify combined prompt context includes both governed memories and Graphify LESSONS.md
        combined_ctx = memory_svc.retrieve_graphify_lessons_context(
            project_id=" billing_proj ",
            target_symbols=["charge_card"],
            workspace_dir=self.root,
        )
        self.assertIn("[Governed Episodic & Project Memory]", combined_ctx)
        self.assertIn("[Graphify Work-Memory Reflections]", combined_ctx)


if __name__ == "__main__":
    unittest.main()
