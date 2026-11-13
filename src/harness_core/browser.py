"""Headless Browser Automation Engine for HEX Autonomous AI Workbench.

Provides interactive headless browsing capabilities with accessibility snapshots (ariaSnapshot),
element interaction via selectors (@e1, @e2, CSS), screenshot captures, and per-task session isolation.
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

    def __init__(self, task_id: str, timeout: float = 30.0) -> None:
        self.task_id = task_id
        self.timeout = timeout
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
                    file_p = Path(urllib.parse.unquote(target_url[7:]))
                    content = file_p.read_text(encoding="utf-8", errors="replace")
                else:
                    req = urllib.request.Request(
                        target_url,
                        headers={"User-Agent": "HEX-Autonomous-Workbench/1.0 (Headless)"},
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

    def click(self, selector: str) -> BrowserActionResult:
        """Click an element specified by ref (@e1, @e2) or CSS selector."""
        with self._lock:
            sel = selector.strip()
            target_el = None
            if sel.startswith("@e"):
                target_el = next((e for e in self.elements if e["ref"] == sel), None)
            else:
                target_el = next(
                    (e for e in self.elements if e.get("id") == sel.lstrip("#") or e.get("name") == sel),
                    None,
                )

            if target_el is None:
                return BrowserActionResult(
                    action=BrowserAction.CLICK.value,
                    url=self.current_url,
                    title=self.title,
                    ok=False,
                    error=f"Element '{selector}' not found in current page snapshot",
                )

            # If it's a link with href, perform navigation
            href = target_el.get("href")
            if href and not href.startswith("javascript:"):
                dest = urllib.parse.urljoin(self.current_url, href)
                return self.navigate(dest)

            return BrowserActionResult(
                action=BrowserAction.CLICK.value,
                url=self.current_url,
                title=self.title,
                content=f"Clicked element {target_el['ref']} ({target_el['tag']})",
                elements=[target_el],
                ok=True,
            )

    def type(self, selector: str, text: str) -> BrowserActionResult:
        """Type text into an input or textarea element."""
        with self._lock:
            sel = selector.strip()
            target_el = None
            if sel.startswith("@e"):
                target_el = next((e for e in self.elements if e["ref"] == sel), None)
            else:
                target_el = next(
                    (e for e in self.elements if e.get("id") == sel.lstrip("#") or e.get("name") == sel),
                    None,
                )

            if target_el is None:
                return BrowserActionResult(
                    action=BrowserAction.TYPE.value,
                    url=self.current_url,
                    title=self.title,
                    ok=False,
                    error=f"Input element '{selector}' not found in current page snapshot",
                )

            target_el["value"] = text
            return BrowserActionResult(
                action=BrowserAction.TYPE.value,
                url=self.current_url,
                title=self.title,
                content=f"Typed text into {target_el['ref']}: {text}",
                elements=[target_el],
                ok=True,
            )

    def screenshot(self, output_path: Path | str | None = None) -> BrowserActionResult:
        """Capture rendered HTML or save DOM snapshot file."""
        with self._lock:
            if output_path is not None:
                out_p = Path(output_path).resolve()
            else:
                temp_f = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
                out_p = Path(temp_f.name)
                temp_f.close()

            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(self.raw_html or "<html><body>Empty</body></html>", encoding="utf-8")
            return BrowserActionResult(
                action=BrowserAction.SCREENSHOT.value,
                url=self.current_url,
                title=self.title,
                screenshot_path=str(out_p),
                content=f"Captured page snapshot at {out_p}",
                ok=True,
            )

    def evaluate(self, script: str) -> BrowserActionResult:
        """Evaluate expression or query elements in page."""
        with self._lock:
            # Simple expression evaluator over parsed DOM
            expr = script.strip().lower()
            if "title" in expr:
                res = self.title
            elif "url" in expr:
                res = self.current_url
            elif "elements" in expr:
                res = str(len(self.elements))
            else:
                # Regex search in raw HTML
                matches = re.findall(script, self.raw_html, re.I)
                res = f"Found {len(matches)} match(es): {matches[:5]}"

            return BrowserActionResult(
                action=BrowserAction.EVALUATE.value,
                url=self.current_url,
                title=self.title,
                content=str(res),
                ok=True,
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

    def get_or_create_session(self, task_id: str) -> HeadlessBrowserSession:
        """Retrieve existing session or spawn a fresh one for the task."""
        with self._lock:
            if task_id not in self._sessions:
                self._sessions[task_id] = HeadlessBrowserSession(task_id)
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
