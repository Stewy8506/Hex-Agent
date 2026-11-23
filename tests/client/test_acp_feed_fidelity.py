"""DX4 feed-fidelity tests: thought streaming, tool diffs, and attachments.

Covers the pieces that make the desktop feed reflect what actually happened:
``<think>`` routing through the ACP stream callback, bounded unified diffs
attached to file-edit tool results, and the attachment renderer that guards
client-picked files before they reach the model.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_core.gateway import ToolGateway
from hex_cli.acp import (
    _MAX_ATTACHMENTS,
    _MAX_ATTACHMENT_BYTES,
    _MAX_ATTACHMENT_TOTAL_BYTES,
    _TOOL_DIFF_MAX_CHARS,
    _ThinkSplitter,
    _extract_tool_diff,
    _render_attachments,
)
from harness_core.tools.filesystem import DIFF_MAX_LINES, unified_diff_bounded


class ThinkSplitterTests(unittest.TestCase):
    def test_plain_text_streams_entirely_as_answer(self):
        splitter = _ThinkSplitter()
        out = splitter.feed("just answer text")
        out += splitter.flush()
        self.assertEqual(out, [("answer", "just answer text")])
        self.assertFalse(splitter.in_thought)

    def test_simple_think_block_is_split(self):
        splitter = _ThinkSplitter()
        out = splitter.feed("<think>reasoning here</think>final answer")
        out += splitter.flush()
        self.assertEqual(
            out,
            [("thought", "reasoning here"), ("answer", "final answer")],
        )

    def test_tag_split_across_chunk_boundaries(self):
        splitter = _ThinkSplitter()
        pieces = []
        for chunk in ("<thi", "nk>hid", "den</th", "ink>answ", "er"):
            pieces.extend(splitter.feed(chunk))
        pieces.extend(splitter.flush())
        # Deltas are per-chunk; the contract is that concatenation equals the
        # original text minus the tags, with no partial tags leaking through.
        self.assertEqual(
            "".join(t for kind, t in pieces if kind == "thought"), "hidden"
        )
        self.assertEqual(
            "".join(t for kind, t in pieces if kind == "answer"), "answer"
        )
        for _, text in pieces:
            self.assertNotIn("<", text)

    def test_unterminated_thought_flushes_as_thought(self):
        splitter = _ThinkSplitter()
        out = splitter.feed("<think>still deciding")
        out += splitter.flush()
        self.assertEqual(out, [("thought", "still deciding")])
        self.assertTrue(splitter.in_thought)

    def test_adjacent_tag_boundary_keeps_tail(self):
        # A chunk ending exactly on the open tag must not leak it as text.
        splitter = _ThinkSplitter()
        out = splitter.feed("<think>")
        self.assertEqual(out, [])
        out = splitter.feed("inside</think>")
        self.assertEqual(out, [("thought", "inside")])

    def test_multiple_blocks_per_chunk(self):
        splitter = _ThinkSplitter()
        out = splitter.feed(
            "<think>a</think>b<think>c</think>d"
        )
        self.assertEqual(
            out,
            [
                ("thought", "a"),
                ("answer", "b"),
                ("thought", "c"),
                ("answer", "d"),
            ],
        )


class ExtractToolDiffTests(unittest.TestCase):
    def test_non_dict_payload_yields_none(self):
        self.assertIsNone(_extract_tool_diff("plain output"))
        self.assertIsNone(_extract_tool_diff(None))

    def test_diff_is_popped_from_payload(self):
        payload = {"ok": True, "diff": "--- a/x\n+++ b/x\n@@\n-old\n+new"}
        diff = _extract_tool_diff(payload)
        self.assertEqual(diff, "--- a/x\n+++ b/x\n@@\n-old\n+new")
        self.assertNotIn("diff", payload)

    def test_blank_or_non_string_diff_yields_none(self):
        self.assertIsNone(_extract_tool_diff({"diff": "   "}))
        self.assertIsNone(_extract_tool_diff({"diff": 42}))
        self.assertIsNone(_extract_tool_diff({}))

    def test_oversized_diff_is_bounded(self):
        diff = "-" * (_TOOL_DIFF_MAX_CHARS + 500)
        bounded = _extract_tool_diff({"diff": diff})
        self.assertTrue(bounded.startswith("-" * _TOOL_DIFF_MAX_CHARS))
        self.assertTrue(bounded.endswith("[...diff truncated]"))
        self.assertLessEqual(len(bounded), _TOOL_DIFF_MAX_CHARS + 32)


class UnifiedDiffBoundedTests(unittest.TestCase):
    def test_unchanged_content_yields_none(self):
        self.assertIsNone(unified_diff_bounded("x.txt", "same", "same"))

    def test_changed_content_yields_unified_diff(self):
        diff = unified_diff_bounded("notes.txt", "old\n", "new\n")
        self.assertIn("--- a/notes.txt", diff)
        self.assertIn("+++ b/notes.txt", diff)
        self.assertIn("-old", diff)
        self.assertIn("+new", diff)

    def test_truncation_note_at_max_lines(self):
        before = "\n".join(f"line{i}" for i in range(500))
        after = "\n".join(f"line{i}" for i in range(400, 900))
        diff = unified_diff_bounded("big.txt", before, after, max_lines=10)
        self.assertTrue(diff.endswith("[...diff truncated at 10 lines]"))
        self.assertLessEqual(len(diff.splitlines()), 11)

    def test_default_budget_is_240_lines(self):
        self.assertEqual(DIFF_MAX_LINES, 240)


class ToolResultDiffTests(unittest.TestCase):
    """File-edit tools attach their diff at the write moment."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.gateway = ToolGateway(ledger=None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_file_new_file_has_diff(self):
        res = self.gateway.tools["write_file"].handler(
            self.workspace, {"path": "drafts/new.txt", "content": "hello\n"}
        )
        self.assertTrue(res["ok"])
        self.assertIn("diff", res)
        self.assertIn("+hello", res["diff"])

    def test_write_file_noop_has_no_diff(self):
        (self.workspace / "same.txt").write_text("stable\n", encoding="utf-8")
        res = self.gateway.tools["write_file"].handler(
            self.workspace, {"path": "same.txt", "content": "stable\n"}
        )
        self.assertTrue(res["ok"])
        self.assertNotIn("diff", res)

    def test_write_file_overwrite_diffs_against_previous_content(self):
        (self.workspace / "notes.txt").write_text("old\n", encoding="utf-8")
        res = self.gateway.tools["write_file"].handler(
            self.workspace, {"path": "notes.txt", "content": "new\n"}
        )
        self.assertIn("-old", res["diff"])
        self.assertIn("+new", res["diff"])

    def test_patch_find_replace_has_diff(self):
        (self.workspace / "code.py").write_text("value = 1\n", encoding="utf-8")
        res = self.gateway.tools["patch"].handler(
            self.workspace,
            {"path": "code.py", "find": "value = 1", "replace": "value = 2"},
        )
        self.assertTrue(res["ok"])
        self.assertIn("-value = 1", res["diff"])
        self.assertIn("+value = 2", res["diff"])

    def test_failed_patch_has_no_diff(self):
        (self.workspace / "code.py").write_text("value = 1\n", encoding="utf-8")
        res = self.gateway.tools["patch"].handler(
            self.workspace,
            {"path": "code.py", "find": "missing-marker", "replace": "x"},
        )
        self.assertFalse(res["ok"])
        self.assertNotIn("diff", res)


class RenderAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> str:
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def test_none_renders_empty(self):
        self.assertEqual(_render_attachments(None), "")

    def test_empty_list_renders_empty(self):
        self.assertEqual(_render_attachments([]), "")

    def test_valid_file_is_fenced_with_header(self):
        path = self._write("a.py", b"value = 1")
        rendered = _render_attachments([path])
        self.assertTrue(rendered.startswith("\n\n"))
        self.assertIn(f"[Attached file: {Path(path)}]", rendered)
        self.assertIn("```text\nvalue = 1\n```", rendered)

    def test_non_list_or_non_string_raises(self):
        with self.assertRaises(ValueError):
            _render_attachments("not-a-list")
        with self.assertRaises(ValueError):
            _render_attachments([1, 2])
        with self.assertRaises(ValueError):
            _render_attachments({"path": "x"})

    def test_unreadable_file_raises(self):
        with self.assertRaises(ValueError):
            _render_attachments([str(self.dir / "missing.txt")])

    def test_too_many_files_raises(self):
        paths = [
            self._write(f"f{i}.txt", b"x") for i in range(_MAX_ATTACHMENTS + 1)
        ]
        with self.assertRaises(ValueError):
            _render_attachments(paths)

    def test_per_file_budget_truncates_content(self):
        data = b"A" * (_MAX_ATTACHMENT_BYTES + 100)
        path = self._write("big.txt", data)
        rendered = _render_attachments([path])
        self.assertNotIn("A" * (_MAX_ATTACHMENT_BYTES + 1), rendered)

    def test_total_budget_enforced(self):
        half = _MAX_ATTACHMENT_TOTAL_BYTES // 2 + 10
        first = self._write("one.txt", b"B" * half)
        second = self._write("two.txt", b"C" * half)
        with self.assertRaises(ValueError):
            _render_attachments([first, second])


if __name__ == "__main__":
    unittest.main()
