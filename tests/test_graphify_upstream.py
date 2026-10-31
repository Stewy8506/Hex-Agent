import tempfile
import unittest
from pathlib import Path

from harness_core.graphify import (
    ActiveGraphifyAdapter,
    UPSTREAM_GRAPHIFY_AVAILABLE,
)


class TestGraphifyUpstreamIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

        # Create a mini codebase with 1-hop and 2-hop dependencies and a test file
        (self.ws / "math_lib.py").write_text(
            "def calculate_discount(price: float, rate: float) -> float:\n"
            "    return price * (1.0 - rate)\n",
            encoding="utf-8",
        )
        (self.ws / "checkout.py").write_text(
            "from math_lib import calculate_discount\n\n"
            "def finalize_cart(cart: dict) -> float:\n"
            "    return calculate_discount(cart['total'], 0.05)\n",
            encoding="utf-8",
        )
        (self.ws / "app.py").write_text(
            "from checkout import finalize_cart\n\n"
            "def handle_order(order: dict) -> float:\n"
            "    return finalize_cart(order['cart'])\n",
            encoding="utf-8",
        )
        (self.ws / "test_checkout.py").write_text(
            "import unittest\n"
            "from checkout import finalize_cart\n\n"
            "class TestCheckout(unittest.TestCase):\n"
            "    def test_cart(self):\n"
            "        self.assertEqual(finalize_cart({'total': 100}), 95.0)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_vendor_graphify_detected_and_active(self) -> None:
        self.assertTrue(UPSTREAM_GRAPHIFY_AVAILABLE, "Upstream vendor/graphify must be available")
        adapter = ActiveGraphifyAdapter(self.ws)
        self.assertTrue(adapter.upstream_available)

    def test_build_or_refresh_constructs_upstream_networkx_graph(self) -> None:
        adapter = ActiveGraphifyAdapter(self.ws)
        stats = adapter.build_or_refresh(self.ws)

        self.assertTrue(stats.get("upstream_graphify_active"))
        self.assertIsNotNone(adapter.graph)
        self.assertGreater(len(adapter.graph.nodes), 0)

        # Check nodes
        self.assertIn("calculate_discount", adapter.graph.nodes)
        self.assertIn("finalize_cart", adapter.graph.nodes)

    def test_compute_blast_radius_with_upstream_affected_nodes(self) -> None:
        adapter = ActiveGraphifyAdapter(self.ws)

        # Modify math_lib.py -> calculate_discount
        report = adapter.compute_blast_radius(
            workspace_dir=self.ws,
            modified_rel_paths=["math_lib.py"],
        )

        self.assertIn("calculate_discount", report.modified_symbols)

        # 1-hop should include finalize_cart
        hop1_symbols = {d["qualified_name"].split(".")[-1] for d in report.hop1_dependents}
        self.assertIn("finalize_cart", hop1_symbols)

        # Impacted tests must pin test_checkout.py
        self.assertIn("test_checkout.py", report.impacted_tests)

    def test_fallback_parity_when_upstream_disabled(self) -> None:
        # Simulate environment where upstream networkx/graphify is disabled
        adapter = ActiveGraphifyAdapter(self.ws)
        adapter.upstream_available = False
        adapter.graph = None

        report = adapter.compute_blast_radius(
            workspace_dir=self.ws,
            modified_rel_paths=["math_lib.py"],
        )

        self.assertIn("calculate_discount", report.modified_symbols)
        hop1_symbols = {d["qualified_name"].split(".")[-1] for d in report.hop1_dependents}
        self.assertIn("finalize_cart", hop1_symbols)
        self.assertIn("test_checkout.py", report.impacted_tests)


if __name__ == "__main__":
    unittest.main()
