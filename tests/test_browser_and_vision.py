"""Comprehensive tests for Tier B Headless Browser and Multimodal Vision Engine.

Tests:
- Headless browser navigation, accessibility tree snapshot (ariaSnapshot with @e refs).
- Element interaction: clicking and typing.
- DOM screenshot / capture artifact generation.
- DOM evaluate expression queries.
- Multimodal VisionEngine image inspection with dimension extraction and base64 encoding.
- ToolGateway integration for 'browser' and 'image_inspect'.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.browser import BrowserAction, BrowserManager, HeadlessBrowserSession
from harness_core.db import LedgerStore
from harness_core.gateway import ToolGateway
from harness_core.vision import VisionEngine, get_image_metadata


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>HEX Workbench Test App</title>
</head>
<body>
    <header>
        <h1>Welcome to HEX Web Console</h1>
        <nav>
            <a href="/dashboard">Dashboard</a>
            <a href="/settings">Settings</a>
        </nav>
    </header>
    <main>
        <form action="/search" method="get">
            <input type="text" name="q" id="search-input" placeholder="Search tasks..." />
            <button type="submit" id="search-btn">Search</button>
        </form>
        <section>
            <p>Active task list is currently empty.</p>
        </section>
    </main>
</body>
</html>
"""

# Valid 1x1 transparent PNG bytes
TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestBrowserAndVision(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.html_file = self.workspace_dir / "test_app.html"
        self.html_file.write_text(SAMPLE_HTML, encoding="utf-8")

        self.png_file = self.workspace_dir / "sample.png"
        self.png_file.write_bytes(TINY_PNG_BYTES)

        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.gateway = ToolGateway(self.ledger)
        self.browser_manager = BrowserManager()
        self.vision_engine = VisionEngine()

    def tearDown(self) -> None:
        self.browser_manager.close_all()
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_browser_navigation_and_aria_snapshot(self) -> None:
        """Verify browser navigates to file URL and generates ariaSnapshot with element refs."""
        session = self.browser_manager.get_or_create_session("task_nav_01")
        file_url = f"file://{self.html_file.resolve()}"
        res = session.navigate(file_url)

        self.assertTrue(res.ok)
        self.assertEqual(res.title, "HEX Workbench Test App")
        self.assertIn("Page Title: HEX Workbench Test App", res.content)
        self.assertIn("--- Accessibility & Interactive Elements ---", res.content)

        # Check interactive element refs
        refs = [e["ref"] for e in res.elements]
        self.assertIn("@e1", refs)
        self.assertIn("@e2", refs)
        self.assertTrue(len(res.elements) >= 4)  # 2 links, 1 input, 1 button, 1 form

    def test_browser_element_interaction_and_type(self) -> None:
        """Verify clicking elements and typing into inputs."""
        session = self.browser_manager.get_or_create_session("task_interact_01")
        session.navigate(f"file://{self.html_file.resolve()}")

        # Type into search input
        type_res = session.type("@e3", "autonomous reasoning")
        self.assertTrue(type_res.ok)
        self.assertIn("Typed text", type_res.content)
        self.assertEqual(type_res.elements[0].get("value"), "autonomous reasoning")

        # Click button
        click_res = session.click("@e4")
        self.assertTrue(click_res.ok)
        self.assertIn("Clicked element", click_res.content)

    def test_browser_screenshot_and_evaluate(self) -> None:
        """Verify DOM screenshot artifact creation and expression evaluation."""
        session = self.browser_manager.get_or_create_session("task_shot_01")
        session.navigate(f"file://{self.html_file.resolve()}")

        # Screenshot capture
        shot_path = self.workspace_dir / "capture.html"
        shot_res = session.screenshot(shot_path)
        self.assertTrue(shot_res.ok)
        self.assertTrue(shot_path.exists())
        self.assertIn("HEX Workbench Test App", shot_path.read_text(encoding="utf-8"))

        # Evaluate expression
        eval_title = session.evaluate("title")
        self.assertTrue(eval_title.ok)
        self.assertEqual(eval_title.content, "HEX Workbench Test App")

        eval_elems = session.evaluate("elements")
        self.assertTrue(eval_elems.ok)
        self.assertTrue(int(eval_elems.content) >= 4)

    def test_vision_engine_metadata_and_inspection(self) -> None:
        """Verify VisionEngine parses PNG dimensions and generates visual report."""
        meta = get_image_metadata(TINY_PNG_BYTES, ".png")
        self.assertIsNotNone(meta)
        self.assertEqual(meta, (1, 1))

        # Inspect local image
        inspect_res = self.vision_engine.inspect_image(
            str(self.png_file),
            prompt="Describe this image",
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(inspect_res["ok"])
        self.assertEqual(inspect_res["mime_type"], "image/png")
        self.assertEqual(inspect_res["dimensions"], (1, 1))
        self.assertIn("Visual Inspection Summary", inspect_res["analysis"])

    def test_vision_engine_nonexistent_or_unsupported_file(self) -> None:
        """Verify VisionEngine gracefully rejects invalid or non-image files."""
        res_missing = self.vision_engine.inspect_image("nonexistent.png", "query", workspace_dir=self.workspace_dir)
        self.assertFalse(res_missing["ok"])
        self.assertIn("not found", res_missing["error"])

        res_unsupported = self.vision_engine.inspect_image(str(self.html_file), "query", workspace_dir=self.workspace_dir)
        self.assertFalse(res_unsupported["ok"])
        self.assertIn("Unsupported image extension", res_unsupported["error"])

    def test_gateway_browser_and_image_inspect_tools(self) -> None:
        """Verify ToolGateway execution for browser and image_inspect."""
        file_url = f"file://{self.html_file.resolve()}"

        # 1. Gateway browser tool navigate
        nav_res = self.gateway._tool_browser(
            self.workspace_dir,
            {"action": "navigate", "url": file_url, "task_id": "gw_browser_01"},
        )
        self.assertTrue(nav_res["ok"])
        self.assertEqual(nav_res["result"]["title"], "HEX Workbench Test App")

        # 2. Gateway browser tool snapshot
        snap_res = self.gateway._tool_browser(
            self.workspace_dir,
            {"action": "snapshot", "task_id": "gw_browser_01"},
        )
        self.assertTrue(snap_res["ok"])
        self.assertIn("Accessibility", snap_res["result"]["content"])

        # 3. Gateway browser tool screenshot
        shot_res = self.gateway._tool_browser(
            self.workspace_dir,
            {"action": "screenshot", "output_path": "shot.html", "task_id": "gw_browser_01"},
        )
        self.assertTrue(shot_res["ok"])
        self.assertTrue((self.workspace_dir / "shot.html").exists())

        # 4. Gateway browser close
        close_res = self.gateway._tool_browser(
            self.workspace_dir,
            {"action": "close", "task_id": "gw_browser_01"},
        )
        self.assertTrue(close_res["ok"])

        # 5. Gateway image_inspect tool
        vision_res = self.gateway._tool_image_inspect(
            self.workspace_dir,
            {"image_path": str(self.png_file), "prompt": "Check UI color balance"},
        )
        self.assertTrue(vision_res["ok"])
        self.assertEqual(vision_res["mime_type"], "image/png")
        self.assertIn("Visual Inspection Summary", vision_res["analysis"])


if __name__ == "__main__":
    unittest.main()
