"""Multi-Language Server Registry & Executable Discovery Engine.

Maintains specifications for major language servers (pyright, ruff, pylsp,
typescript-language-server, vtsls, gopls, rust-analyzer, clangd, jdtls),
resolves binaries from PATH and environment variables, and detects
applicable servers for workspaces and files.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LanguageServerSpec:
    """Specification and launcher parameters for a language server."""

    language_id: str
    server_name: str
    binary_candidates: list[str]
    default_args: list[str] = field(default_factory=list)
    file_extensions: tuple[str, ...] = field(default_factory=tuple)
    root_markers: list[str] = field(default_factory=list)
    initialization_options: dict[str, Any] = field(default_factory=dict)
    priority: int = 100

    def resolve_binary(self) -> str | None:
        """Resolve executable binary path checking environment overrides then PATH."""
        # 1. Server-specific override: HEX_LSP_<SERVER>_BIN
        server_key = f"HEX_LSP_{self.server_name.upper().replace('-', '_')}_BIN"
        env_val = os.environ.get(server_key)
        if env_val:
            found = shutil.which(env_val) or (env_val if Path(env_val).is_file() else None)
            if found:
                return found

        # 2. Language-wide override: HEX_LSP_<LANG>_BIN
        lang_key = f"HEX_LSP_{self.language_id.upper()}_BIN"
        env_val = os.environ.get(lang_key)
        if env_val:
            found = shutil.which(env_val) or (env_val if Path(env_val).is_file() else None)
            if found:
                return found

        # 3. Candidate search in PATH
        for cand in self.binary_candidates:
            found = shutil.which(cand)
            if found:
                return found

        return None

    def get_command(self, resolved_binary: str | None = None) -> list[str]:
        """Construct subprocess command invocation."""
        bin_path = resolved_binary or self.resolve_binary() or self.binary_candidates[0]
        return [bin_path] + list(self.default_args)


def _default_specs() -> list[LanguageServerSpec]:
    """Default configurations for 6 major language families."""
    return [
        # Python
        LanguageServerSpec(
            language_id="python",
            server_name="pyright",
            binary_candidates=["pyright-langserver", "pyright"],
            default_args=["--stdio"],
            file_extensions=(".py", ".pyi"),
            root_markers=["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"],
            priority=100,
        ),
        LanguageServerSpec(
            language_id="python",
            server_name="ruff",
            binary_candidates=["ruff"],
            default_args=["server"],
            file_extensions=(".py", ".pyi"),
            root_markers=["pyproject.toml", "ruff.toml", ".ruff.toml", "requirements.txt"],
            priority=90,
        ),
        LanguageServerSpec(
            language_id="python",
            server_name="pylsp",
            binary_candidates=["pylsp", "python-lsp-server"],
            default_args=[],
            file_extensions=(".py", ".pyi"),
            root_markers=["pyproject.toml", "setup.py", "requirements.txt"],
            priority=80,
        ),
        # TypeScript / JavaScript
        LanguageServerSpec(
            language_id="typescript",
            server_name="typescript-language-server",
            binary_candidates=["typescript-language-server"],
            default_args=["--stdio"],
            file_extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
            root_markers=["package.json", "tsconfig.json", "jsconfig.json"],
            priority=100,
        ),
        LanguageServerSpec(
            language_id="typescript",
            server_name="vtsls",
            binary_candidates=["vtsls"],
            default_args=["--stdio"],
            file_extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
            root_markers=["package.json", "tsconfig.json"],
            priority=90,
        ),
        # Go
        LanguageServerSpec(
            language_id="go",
            server_name="gopls",
            binary_candidates=["gopls"],
            default_args=[],
            file_extensions=(".go",),
            root_markers=["go.mod", "go.work"],
            priority=100,
        ),
        # Rust
        LanguageServerSpec(
            language_id="rust",
            server_name="rust-analyzer",
            binary_candidates=["rust-analyzer"],
            default_args=[],
            file_extensions=(".rs",),
            root_markers=["Cargo.toml", "Cargo.lock"],
            priority=100,
        ),
        # C / C++
        LanguageServerSpec(
            language_id="cpp",
            server_name="clangd",
            binary_candidates=["clangd"],
            default_args=[],
            file_extensions=(".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"),
            root_markers=["compile_commands.json", "CMakeLists.txt", "Makefile"],
            priority=100,
        ),
        # Java
        LanguageServerSpec(
            language_id="java",
            server_name="jdtls",
            binary_candidates=["jdtls"],
            default_args=[],
            file_extensions=(".java",),
            root_markers=["pom.xml", "build.gradle", "build.gradle.kts", ".project"],
            priority=100,
        ),
    ]


class LanguageServerRegistry:
    """Registry managing available language server specifications."""

    def __init__(self, include_defaults: bool = True) -> None:
        self._specs: list[LanguageServerSpec] = []
        if include_defaults:
            for spec in _default_specs():
                self.register_server(spec)

    def register_server(
        self,
        spec: LanguageServerSpec,
        priority: int | None = None,
    ) -> None:
        """Register a new server spec, maintaining priority order."""
        if priority is not None:
            spec.priority = priority
        # Remove existing spec with same server_name if present
        self._specs = [s for s in self._specs if s.server_name != spec.server_name]
        self._specs.append(spec)
        self._specs.sort(key=lambda s: s.priority, reverse=True)

    def get_all_specs(self) -> list[LanguageServerSpec]:
        """Return list of all registered specifications."""
        return list(self._specs)

    def get_specs_for_language(self, language_id: str) -> list[LanguageServerSpec]:
        """Return all specs configured for a specific language ID."""
        return [s for s in self._specs if s.language_id == language_id]

    def find_server_for_file(
        self,
        file_path: Path | str,
        workspace_root: Path | str | None = None,
        require_installed: bool = True,
    ) -> LanguageServerSpec | None:
        """Find the best language server spec for a given file.

        Matches file extension, verifies executable presence via PATH / environment,
        and optionally checks workspace root markers.
        """
        ext = Path(file_path).suffix.lower()
        if not ext:
            return None

        # Filter candidate specs by file extension
        candidates = [s for s in self._specs if ext in s.file_extensions]
        if not candidates:
            return None

        # Check if workspace root has root markers
        ws_path = Path(workspace_root).resolve() if workspace_root else None

        def marker_score(s: LanguageServerSpec) -> int:
            if not ws_path or not ws_path.is_dir():
                return 0
            for marker in s.root_markers:
                if (ws_path / marker).exists():
                    return 10
            return 0

        # Sort candidates by marker score then priority
        candidates.sort(key=lambda s: (marker_score(s), s.priority), reverse=True)

        for spec in candidates:
            if require_installed:
                if spec.resolve_binary() is not None:
                    return spec
            else:
                return spec

        # If require_installed was True and none found, return None
        return None

    def detect_workspace_servers(
        self,
        workspace_root: Path | str,
    ) -> list[LanguageServerSpec]:
        """Discover all language servers applicable to the repository layout.

        Cross-references with project recipes and root markers.
        """
        root = Path(workspace_root).resolve()
        if not root.is_dir():
            return []

        detected_languages: set[str] = set()

        # 1. Try detect_recipe if available
        try:
            from harness_core.recipes import detect_recipe

            recipe = detect_recipe(root)
            if recipe:
                if recipe.kind == "python":
                    detected_languages.add("python")
                elif recipe.kind == "nodejs":
                    detected_languages.add("typescript")
                elif recipe.kind == "rust":
                    detected_languages.add("rust")
                elif recipe.kind == "go":
                    detected_languages.add("go")
        except Exception as exc:
            logger.debug("Failed checking recipes for workspace LSP detection: %s", exc)

        # 2. Check root markers directly
        for spec in self._specs:
            for marker in spec.root_markers:
                if (root / marker).exists():
                    detected_languages.add(spec.language_id)
                    break

        # 3. Scan top-level and 1st-level directories for file extensions if still empty
        if not detected_languages:
            try:
                for entry in root.iterdir():
                    if entry.is_file():
                        ext = entry.suffix.lower()
                        for spec in self._specs:
                            if ext in spec.file_extensions:
                                detected_languages.add(spec.language_id)
            except Exception:
                pass

        # For each detected language, pick best candidate
        results: list[LanguageServerSpec] = []
        for lang in detected_languages:
            candidates = [s for s in self._specs if s.language_id == lang]
            # Try to find one with an installed binary first
            installed = [s for s in candidates if s.resolve_binary() is not None]
            if installed:
                results.append(installed[0])
            elif candidates:
                results.append(candidates[0])

        return results
