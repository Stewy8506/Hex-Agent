"""HTML fetch and parse capability for HEX Agent.

Fetches pages over HTTP(S) or reads workspace-scoped file:// documents, then
parses them into readable text and an accessibility-style list of interactive
elements. There is no JavaScript execution, DOM event dispatch, or rendering:
click/type/screenshot/evaluate report that limitation instead of simulating
success (B53).
"""

from __future__ import annotations

import enum
import html
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BrowserAction(str, enum.Enum):
    """Supported browser operations."""

    NAVIGATE = "navigate"
    SNAPSHOT = "snapshot"
    CLICK = "click"
    TYPE = "type"
    SCREENSHOT = "screenshot"
    EVALUATE = "evaluate"
    CLOSE = "close"


@dataclass
class BrowserActionResult:
    """Standardized output structure for browser actions."""

    action: str
    url: str = ""
    title: str = ""
    content: str = ""
    screenshot_path: str = ""
    ok: bool = True
    error: str | None = None
    elements: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "screenshot_path": self.screenshot_path,
            "ok": self.ok,
            "error": self.error,
            "elements": self.elements,
        }


class _AriaHTMLParser(HTMLParser):
    """Parses HTML into readable text, title, and interactive element refs (@e1, @e2...)."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._in_script = False
        self._in_style = False
        self.text_lines: list[str] = []
        self.elements: list[dict[str, Any]] = []
        self._element_counter = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in ("script", "style", "noscript"):
            self._in_script = True
        elif tag in ("a", "button", "input", "textarea", "select", "form"):
            self._element_counter += 1
            ref = f"@e{self._element_counter}"
            el_type = attr_dict.get("type", "text") if tag == "input" else tag
            label = attr_dict.get("aria-label") or attr_dict.get("placeholder") or attr_dict.get("name") or attr_dict.get("id") or ""
            href = attr_dict.get("href") or attr_dict.get("action") or ""
            self.elements.append({
                "ref": ref,
                "tag": tag,
                "type": el_type,
                "label": label,
                "href": href,
                "id": attr_dict.get("id", ""),
                "name": attr_dict.get("name", ""),
            })

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "noscript"):
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        elif not self._in_script:
            txt = data.strip()
            if txt:
                self.text_lines.append(txt)
                if self.elements and not self.elements[-1].get("text"):
                    # Tag the nearest preceding interactive element with this inner text
                    self.elements[-1]["text"] = txt[:120]


class HeadlessBrowserSession:
    """Manages an isolated browser session for a single task."""

    def __init__(self, task_id: str, timeout: float = 30.0, workspace_root: Path | str | None = None) -> None:
        self.task_id = task_id
        self.timeout = timeout
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.current_url = "about:blank"
        self.title = ""
        self.raw_html = ""
        self.text_content = ""
        self.elements: list[dict[str, Any]] = []
        self.history: list[str] = []
        self._lock = threading.Lock()

    def navigate(self, url: str) -> BrowserActionResult:
        """Navigate to a target URL or load local HTML file."""
        with self._lock:
            target_url = url.strip()
            if not target_url.startswith(("http://", "https://", "file://", "data:")):
                target_url = "https://" + target_url

            self.history.append(target_url)
            self.current_url = target_url

            try:
                if target_url.startswith("file://"):
                    # B52: file:// navigation obeys the same resolved-path
                    # workspace containment as read_file; without a workspace
                    # scope it is refused outright.
                    file_p = Path(urllib.parse.unquote(target_url[7:]))
                    if self.workspace_root is None:
                        raise PermissionError(
                            "file:// navigation is disabled without a workspace-scoped browser session"
                        )
                    if ".." in file_p.parts or ".." in str(file_p):
                        raise PermissionError(f"Path escapes workspace boundary: {file_p}")
                    resolved_file = file_p.resolve()
                    try:
                        resolved_file.relative_to(self.workspace_root)
                    except ValueError:
                        raise PermissionError(
                            f"file:// path outside workspace containment: {file_p}"
                        )
                    content = resolved_file.read_text(encoding="utf-8", errors="replace")
                else:
                    req = urllib.request.Request(
                        target_url,
                        headers={"User-Agent": "HEX-Agent/1.0 (Headless)"},
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw_bytes = resp.read()
                        charset = resp.headers.get_content_charset() or "utf-8"
                        content = raw_bytes.decode(charset, errors="replace")

                self.raw_html = content
                self._parse_page(content)
                return BrowserActionResult(
                    action=BrowserAction.NAVIGATE.value,
                    url=self.current_url,
                    title=self.title,
                    content=self._build_aria_snapshot(),
                    elements=list(self.elements),
                    ok=True,
                )
            except Exception as exc:
                logger.warning("Browser navigation failed for %s: %s", target_url, exc)
                self.raw_html = f"<html><body><h1>Navigation Error</h1><p>{html.escape(str(exc))}</p></body></html>"
                self._parse_page(self.raw_html)
                return BrowserActionResult(
                    action=BrowserAction.NAVIGATE.value,
                    url=self.current_url,
                    title="Navigation Error",
                    content=str(exc),
                    ok=False,
                    error=str(exc),
                )

    def snapshot(self) -> BrowserActionResult:
        """Generate structured text and accessibility tree snapshot."""
        with self._lock:
            return BrowserActionResult(
                action=BrowserAction.SNAPSHOT.value,
                url=self.current_url,
                title=self.title,
                content=self._build_aria_snapshot(),
                elements=list(self.elements),
                ok=True,
            )

    def _unsupported(self, action: BrowserAction, detail: str) -> BrowserActionResult:
        """B53: this capability fetches and parses HTML; it is not a browser engine."""
        return BrowserActionResult(
            action=action.value,
            url=self.current_url,
            title=self.title,
            ok=False,
            error=(
                f"Unsupported: the '{action.value}' action needs a JavaScript-capable "
                "rendering engine. This capability only fetches and parses HTML "
                f"({detail}); nothing was executed or reported as successful."
            ),
        )

    def click(self, selector: str) -> BrowserActionResult:
        """Clicks require a rendering engine; report the limitation instead of faking it."""
        return self._unsupported(BrowserAction.CLICK, f"selector '{selector}' was not dispatched")

    def type(self, selector: str, text: str) -> BrowserActionResult:
        """Typing requires a rendering engine; no input state is mutated."""
        return self._unsupported(BrowserAction.TYPE, f"no text was entered into '{selector}'")

    def screenshot(self, output_path: Path | str | None = None) -> BrowserActionResult:
        """No image can be captured without a renderer; use snapshot for page text."""
        return self._unsupported(
            BrowserAction.SCREENSHOT, "use action='snapshot' for the parsed page text instead"
        )

    def evaluate(self, script: str) -> BrowserActionResult:
        """Arbitrary JavaScript is not executed by this capability."""
        return self._unsupported(
            BrowserAction.EVALUATE, f"script was not executed: {str(script)[:80]}"
        )

    def close(self) -> None:
        """Terminate the browser session."""
        with self._lock:
            self.raw_html = ""
            self.elements.clear()
            self.text_content = ""

    def _parse_page(self, content: str) -> None:
        parser = _AriaHTMLParser()
        try:
            parser.feed(content)
        except Exception:
            pass
        self.title = parser.title or "Untitled Page"
        self.text_content = "\n".join(parser.text_lines)
        self.elements = parser.elements

    def _build_aria_snapshot(self) -> str:
        """Build accessibility tree snapshot with interactive element refs."""
        lines = [
            f"Page Title: {self.title}",
            f"URL: {self.current_url}",
            "--- Accessibility & Interactive Elements ---",
        ]
        if not self.elements:
            lines.append("  (No interactive elements found)")
        else:
            for el in self.elements[:50]:
                tag = el["tag"].upper()
                ref = el["ref"]
                label = el.get("text") or el.get("label") or el.get("name") or el.get("id") or ""
                href = f" -> {el['href']}" if el.get("href") else ""
                lines.append(f"  [{ref}] {tag} \"{label}\"{href}")

        lines.append("--- Page Text Summary ---")
        lines.append(self.text_content[:2000] if self.text_content else "(Empty page body)")
        return "\n".join(lines)


class BrowserManager:
    """Manages pool of HeadlessBrowserSession instances per task."""

    def __init__(self) -> None:
        self._sessions: dict[str, HeadlessBrowserSession] = {}
        self._lock = threading.Lock()

    def get_or_create_session(
        self, task_id: str, workspace_root: Path | str | None = None
    ) -> HeadlessBrowserSession:
        """Retrieve existing session or spawn a fresh one for the task."""
        with self._lock:
            if task_id not in self._sessions:
                self._sessions[task_id] = HeadlessBrowserSession(task_id, workspace_root=workspace_root)
            elif workspace_root is not None and self._sessions[task_id].workspace_root is None:
                self._sessions[task_id].workspace_root = Path(workspace_root).resolve()
            return self._sessions[task_id]

    def close_session(self, task_id: str) -> None:
        """Close and discard a task's browser session."""
        with self._lock:
            session = self._sessions.pop(task_id, None)
            if session:
                session.close()

    def close_all(self) -> None:
        """Close all active sessions."""
        with self._lock:
            for s in self._sessions.values():
                s.close()
            self._sessions.clear()
