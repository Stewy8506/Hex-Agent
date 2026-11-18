"""B52 regression: browser file:// navigation obeys workspace path authority.

The same resolved-path containment used by read_file applies to browser
navigation; escapes (sibling paths, traversal, symlinks) are refused before
any file read, and unscoped sessions cannot use file:// at all.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.capabilities.browser import BrowserManager
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.storage.db import LedgerStore

PAGE = "<html><head><title>Contained Page</title></head><body><p>inside</p></body></html>"


class TestBrowserPathAuthority(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp()).resolve()
        self.ws = self.temp_dir / "workspace"
        self.ws.mkdir()
        (self.ws / "page.html").write_text(PAGE, encoding="utf-8")
        self.secret = self.temp_dir / "outside-secret.html"
        self.secret.write_text("<html><body>TOP SECRET</body></html>", encoding="utf-8")
        self.ledger = LedgerStore(db_path=self.temp_dir / "ledger.db")
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _navigate(self, url: str) -> dict:
        return self.gateway._tool_browser(self.ws, {"action": "navigate", "url": url})

    def test_gateway_refuses_sibling_file_url(self) -> None:
        with self.assertRaises(PolicyBlockedError):
            self._navigate(f"file://{self.secret}")

    def test_gateway_refuses_traversal_file_url(self) -> None:
        with self.assertRaises(PolicyBlockedError):
            self._navigate(f"file://{self.ws}/../outside-secret.html")

    def test_gateway_refuses_symlink_escape(self) -> None:
        link = self.ws / "escape.html"
        try:
            link.symlink_to(self.secret)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaises(PolicyBlockedError):
            self._navigate(f"file://{link}")

    def test_gateway_allows_contained_file_url(self) -> None:
        res = self._navigate(f"file://{self.ws / 'page.html'}")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["title"], "Contained Page")

    def test_unscoped_session_refuses_file_url(self) -> None:
        mgr = BrowserManager()
        try:
            session = mgr.get_or_create_session("unscoped")
            res = session.navigate(f"file://{self.ws / 'page.html'}")
            self.assertFalse(res.ok)
            self.assertIn("workspace-scoped", str(res.error))
        finally:
            mgr.close_all()

    def test_scoped_session_refuses_outside_and_allows_inside(self) -> None:
        mgr = BrowserManager()
        try:
            session = mgr.get_or_create_session("scoped", workspace_root=self.ws)
            outside = session.navigate(f"file://{self.secret}")
            self.assertFalse(outside.ok)
            self.assertNotIn("TOP SECRET", outside.content)
            inside = session.navigate(f"file://{self.ws / 'page.html'}")
            self.assertTrue(inside.ok)
            self.assertEqual(inside.title, "Contained Page")
        finally:
            mgr.close_all()


if __name__ == "__main__":
    unittest.main()