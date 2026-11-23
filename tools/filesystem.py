from __future__ import annotations

import difflib
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

# Display bound for tool-result diffs carried to UI clients.
DIFF_MAX_LINES = 240


def unified_diff_bounded(
    path: str, before: str, after: str, *, max_lines: int = DIFF_MAX_LINES
) -> str | None:
    """Bounded unified diff for UI display; ``None`` when content is unchanged."""
    if before == after:
        return None
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines]
        diff_lines.append(f"[...diff truncated at {max_lines} lines]")
    return "\n".join(diff_lines)


class FileToolsMixin:
    def _tool_read_file(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        # Resolve gateway bindings at call time to preserve existing dependency patches.
        from harness_core.gateway import (
            ActiveGraphifyAdapter,
            format_jit_memories_block,
            query_jit_memories_for_file,
        )

        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        if not target.is_file():
            raise FileNotFoundError(f"File not found in workspace: {args['path']}")
        content = target.read_text(encoding="utf-8", errors="replace")[:32_000]

        # JIT AST-scoped memory & invariant injection
        ws_resolved = Path(workspace_dir).resolve()
        try:
            rel_p = str(target.resolve().relative_to(ws_resolved)).replace("\\", "/")
        except ValueError:
            rel_p = str(args["path"]).lstrip("./").replace("\\", "/")
        if getattr(self, "graphify", None) is None:
            try:
                self.graphify = ActiveGraphifyAdapter(workspace_dir)
                self.graphify.build_or_refresh(workspace_dir)
            except Exception:
                pass

        memories = query_jit_memories_for_file(workspace_dir, rel_p, getattr(self, "graphify", None))
        if memories:
            jit_block = format_jit_memories_block(memories, rel_p)
            if jit_block:
                injected_content = f"{content}\n\n{jit_block}"
                return {
                    "ok": True,
                    "path": str(args["path"]),
                    "content": injected_content,
                    "size": len(injected_content),
                    "ast_memories": memories,
                }

        return {"ok": True, "path": str(args["path"]), "content": content, "size": len(content)}

    def _tool_list_files(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(workspace_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for f in sorted(filenames):
                if not f.startswith("."):
                    rel = str((Path(dirpath) / f).relative_to(workspace_dir)).replace("\\", "/")
                    files.append(rel)
        return {"ok": True, "files": files[:500], "count": len(files)}

    def _tool_search_files(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("pattern", "")).strip()
        if not query:
            return {"ok": True, "matches": [], "total_matches": 0, "query": ""}

        rel_subpath = str(args.get("path", "")).strip()
        search_dir = self._resolve_confined_path(workspace_dir, rel_subpath) if rel_subpath else workspace_dir
        if not search_dir.exists():
            return {"ok": False, "error": f"Path not found: {rel_subpath}", "matches": []}

        file_pat = str(args.get("file_pattern") or args.get("glob", "*"))
        case_sensitive = bool(args.get("case_sensitive", False))
        max_matches = int(args.get("max_matches", 100))

        regex_flags = 0 if case_sensitive else re.IGNORECASE
        use_regex = True
        try:
            compiled_re = re.compile(query, regex_flags)
        except re.error:
            use_regex = False
            query_cmp = query if case_sensitive else query.lower()

        matches: list[dict[str, Any]] = []
        ignored_dirs = {".git", ".venv", ".pytest_cache", ".hex", "__pycache__", "node_modules", ".mypy_cache"}

        for dirpath, dirnames, filenames in os.walk(search_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ignored_dirs)
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                if file_pat != "*" and not fnmatch.fnmatch(fname, file_pat):
                    continue

                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size > 1_000_000:
                        continue
                    with open(fpath, "rb") as bf:
                        sample = bf.read(1024)
                        if b"\x00" in sample:
                            continue

                    rel_path = str(fpath.relative_to(workspace_dir)).replace("\\", "/")
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for l_idx, line in enumerate(f, 1):
                            matched = False
                            if use_regex:
                                if compiled_re.search(line):
                                    matched = True
                            else:
                                if (line if case_sensitive else line.lower()).find(query_cmp) != -1:
                                    matched = True
                            if matched:
                                matches.append({
                                    "path": rel_path,
                                    "line": l_idx,
                                    "text": line.rstrip("\r\n")[:300],
                                })
                                if len(matches) >= max_matches:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
            if len(matches) >= max_matches:
                break

        return {
            "ok": True,
            "matches": matches,
            "total_matches": len(matches),
            "query": query,
        }

    def _tool_write_file(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        from harness_core.gateway import sha256_hex

        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        rel_p = str(args["path"]).replace("\\", "/").lstrip("./")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        before_content = ""
        if target.is_file():
            before_content = target.read_text(encoding="utf-8", errors="replace")
        target.write_text(content, encoding="utf-8")
        res = {
            "ok": True,
            "path": str(args["path"]),
            "bytes_written": len(content.encode("utf-8")),
            "sha256": sha256_hex(content),
        }
        diff = unified_diff_bounded(rel_p, before_content, content)
        if diff:
            res["diff"] = diff
        return res

    @staticmethod
    def _apply_unified_diff(original: str, diff_text: str) -> tuple[bool, str, str]:
        lines = original.splitlines(keepends=True)
        diff_lines = diff_text.splitlines(keepends=True)

        hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
        hunks: list[tuple[int, int, list[str]]] = []
        current_hunk: list[str] = []
        old_start = 0
        old_len = 0

        for line in diff_lines:
            m = hunk_header_re.match(line)
            if m:
                if current_hunk:
                    hunks.append((old_start, old_len, current_hunk))
                    current_hunk = []
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
            elif hunks or old_start > 0:
                current_hunk.append(line)

        if current_hunk:
            hunks.append((old_start, old_len, current_hunk))

        if not hunks:
            return False, original, "No unified diff hunks found in diff text"

        result_lines = list(lines)
        for start, length, hunk_lines in sorted(hunks, key=lambda h: h[0], reverse=True):
            line_idx = max(0, start - 1)
            expected_old: list[str] = []
            replacement: list[str] = []
            for hline in hunk_lines:
                if hline.startswith("-"):
                    expected_old.append(hline[1:])
                elif hline.startswith("+"):
                    replacement.append(hline[1:])
                elif hline.startswith(" ") or not hline.strip():
                    ctx = hline[1:] if hline.startswith(" ") else hline
                    expected_old.append(ctx)
                    replacement.append(ctx)

            old_chunk_size = len(expected_old)
            matched_idx = -1

            def chunk_matches(idx: int) -> bool:
                actual = [l.rstrip("\r\n") for l in result_lines[idx : idx + old_chunk_size]]
                expected = [l.rstrip("\r\n") for l in expected_old]
                return actual == expected

            if 0 <= line_idx <= len(result_lines) - old_chunk_size and chunk_matches(line_idx):
                matched_idx = line_idx
            else:
                search_min = max(0, line_idx - 20)
                search_max = min(len(result_lines) - old_chunk_size, line_idx + 20)
                for cand in range(search_min, search_max + 1):
                    if chunk_matches(cand):
                        matched_idx = cand
                        break

            if matched_idx == -1:
                old_str = "".join(expected_old).strip()
                all_str = "".join(result_lines)
                if old_str and old_str in all_str:
                    new_str = "".join(replacement).strip()
                    all_str = all_str.replace(old_str, new_str, 1)
                    return True, all_str, ""
                return False, original, f"Hunk at line {start} does not match target file contents"

            result_lines[matched_idx : matched_idx + old_chunk_size] = replacement

        return True, "".join(result_lines), ""

    def _tool_patch(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        from harness_core.gateway import sha256_hex

        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        rel_p = str(args["path"]).replace("\\", "/").lstrip("./")
        target.parent.mkdir(parents=True, exist_ok=True)

        before_content = ""
        if target.is_file():
            before_content = target.read_text(encoding="utf-8", errors="replace")

        after_content = before_content

        find_str = args.get("find") or args.get("search")
        if find_str is not None:
            find_str = str(find_str)
            replace_str = str(args.get("replace", ""))
            if find_str in before_content:
                after_content = before_content.replace(find_str, replace_str, 1)
            elif find_str.replace("\r\n", "\n") in before_content.replace("\r\n", "\n"):
                norm_before = before_content.replace("\r\n", "\n")
                norm_find = find_str.replace("\r\n", "\n")
                norm_replace = replace_str.replace("\r\n", "\n")
                after_content = norm_before.replace(norm_find, norm_replace, 1)
            else:
                return {
                    "ok": False,
                    "error": f"Search block not found in {args['path']}",
                    "path": str(args["path"]),
                }
        elif "diff" in args or "patch" in args:
            diff_text = str(args.get("diff") or args.get("patch", ""))
            success, patched_content, err = self._apply_unified_diff(before_content, diff_text)
            if not success:
                return {
                    "ok": False,
                    "error": f"Failed to apply patch: {err}",
                    "path": str(args["path"]),
                }
            after_content = patched_content
        else:
            return {
                "ok": False,
                "error": "Either 'find'/'replace' or 'diff' must be provided to patch",
                "path": str(args["path"]),
            }

        target.write_text(after_content, encoding="utf-8")

        blast_info = None
        if hasattr(self, "blast_interceptor") and self.blast_interceptor is not None:
            try:
                report = self.blast_interceptor.inspect_file_modification(
                    workspace_dir,
                    rel_p,
                    before_sources={rel_p: before_content},
                )
                if report:
                    blast_info = report.to_dict()
            except Exception:
                pass

        res = {
            "ok": True,
            "path": str(args["path"]),
            "bytes_written": len(after_content.encode("utf-8")),
            "sha256": sha256_hex(after_content),
        }
        diff = unified_diff_bounded(rel_p, before_content, after_content)
        if diff:
            res["diff"] = diff
        if blast_info:
            res["blast_radius"] = blast_info
        return res
