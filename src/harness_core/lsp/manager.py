"""Polyglot LSP Manager and Workspace Coordinator.

Coordinates multiple language server instances (one per language family)
across a workspace, provides lazy server instantiation on file access,
aggregates real-time diagnostics, and handles graceful teardown.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from harness_core.lsp.client import LSPClient, LSPDiagnostic
from harness_core.lsp.registry import LanguageServerRegistry, LanguageServerSpec

logger = logging.getLogger(__name__)


class LSPManager:
    """Manages active LSPClient instances keyed by language ID for a workspace."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        registry: LanguageServerRegistry | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.registry = registry or LanguageServerRegistry()
        self._clients: dict[str, LSPClient] = {}
        self._lock = threading.Lock()

    @property
    def active_languages(self) -> list[str]:
        """Return list of languages with currently running server instances."""
        with self._lock:
            return [lang for lang, c in self._clients.items() if c.is_alive]

    def get_client_for_language(
        self,
        language_id: str,
        auto_start: bool = True,
        timeout: float = 30.0,
    ) -> LSPClient | None:
        """Retrieve or lazily spawn an LSPClient for the specified language ID."""
        with self._lock:
            existing = self._clients.get(language_id)
            if existing and existing.is_alive:
                return existing

            if not auto_start:
                return None

            specs = self.registry.get_specs_for_language(language_id)
            # Find first spec with an installed binary
            chosen_spec: LanguageServerSpec | None = None
            for s in specs:
                if s.resolve_binary() is not None:
                    chosen_spec = s
                    break

            if not chosen_spec:
                logger.info("No installed LSP server found for language: %s", language_id)
                return None

            client = LSPClient(
                command=chosen_spec.get_command(),
                workspace_root=self.workspace_root,
                spec=chosen_spec,
            )
            try:
                client.initialize(self.workspace_root, timeout=timeout)
            except Exception as exc:
                logger.error("Failed to initialize LSP server %s: %s", chosen_spec.server_name, exc)
                client.shutdown()
                return None

            self._clients[language_id] = client
            return client

    def get_client_for_file(
        self,
        file_path: Path | str,
        auto_start: bool = True,
        timeout: float = 30.0,
    ) -> LSPClient | None:
        """Find the matching language server spec for a file and return its active client."""
        spec = self.registry.find_server_for_file(
            file_path,
            workspace_root=self.workspace_root,
            require_installed=True,
        )
        if not spec:
            return None

        with self._lock:
            existing = self._clients.get(spec.language_id)
            if existing and existing.is_alive:
                return existing

        return self.get_client_for_language(spec.language_id, auto_start=auto_start, timeout=timeout)

    def sync_file(
        self,
        file_path: Path | str,
        text: str | None = None,
        auto_start: bool = True,
    ) -> LSPClient | None:
        """Synchronize file contents with its language server via didOpen/didChange."""
        client = self.get_client_for_file(file_path, auto_start=auto_start)
        if client:
            client.did_open(file_path, text=text)
        return client

    def get_diagnostics(
        self,
        file_path: Path | str | None = None,
        severity: str | int | None = None,
    ) -> list[LSPDiagnostic]:
        """Aggregate diagnostics across all running servers or for a specific file."""
        with self._lock:
            active_clients = [c for c in self._clients.values() if c.is_alive]

        results: list[LSPDiagnostic] = []
        seen: set[tuple[str, int, int, str]] = set()

        for c in active_clients:
            diags = c.get_diagnostics(file_path=file_path, severity=severity)
            for d in diags:
                key = (d.file_path, d.line, d.character, d.message)
                if key not in seen:
                    seen.add(key)
                    results.append(d)

        return results

    def close_all(self, timeout: float = 3.0) -> None:
        """Gracefully shut down all running language servers."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                client.shutdown(timeout=timeout)
            except Exception as exc:
                logger.debug("Error shutting down client: %s", exc)

    def __enter__(self) -> LSPManager:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_all()
