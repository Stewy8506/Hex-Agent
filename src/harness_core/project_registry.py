"""Persistent Project Workspace Registry for HEX (`~/.hex/projects.json`).

Tracks project workspace roots, stack profiles, task telemetry, and last active timestamps.
Provides automatic enclosing project detection and complete folder-scoped context isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ProjectRecord:
    name: str
    path: str
    stack: str
    last_active: str
    task_count: int = 0
    config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "stack": self.stack,
            "last_active": self.last_active,
            "task_count": self.task_count,
            "config_path": self.config_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectRecord:
        return cls(
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            stack=str(data.get("stack", "generic")),
            last_active=str(data.get("last_active", "")),
            task_count=int(data.get("task_count", 0)),
            config_path=str(data.get("config_path", "")),
        )


class ProjectRegistry:
    """Manages the global project registry in ~/.hex/projects.json."""

    def __init__(self, registry_file: Path | str | None = None) -> None:
        if registry_file is not None:
            self.registry_file = Path(registry_file)
        elif os.environ.get("HEX_PROJECTS_REGISTRY"):
            self.registry_file = Path(os.environ["HEX_PROJECTS_REGISTRY"])
        elif (
            "unittest" in sys.modules
            or "pytest" in sys.modules
            or bool(os.environ.get("PYTEST_CURRENT_TEST"))
        ):
            # Isolate tests to a temporary registry file so test runs never pollute ~/.hex/projects.json
            test_reg = Path(tempfile.gettempdir()) / f"hex_test_projects_{os.getpid()}.json"
            self.registry_file = test_reg
        else:
            cand = Path.home() / ".hex" / "projects.json"
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                test_file = cand.parent / ".perm_check"
                test_file.touch()
                test_file.unlink()
                self.registry_file = cand
            except (PermissionError, OSError):
                self.registry_file = Path(".hex").resolve() / "projects.json"
        self._projects: dict[str, ProjectRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.registry_file.is_file():
            return
        try:
            content = self.registry_file.read_text(encoding="utf-8")
            data = json.loads(content)
            items = []
            if isinstance(data, dict):
                items = data.get("projects", [])
            elif isinstance(data, list):
                items = data
            for item in items:
                if isinstance(item, dict):
                    rec = ProjectRecord.from_dict(item)
                    if rec.path:
                        self._projects[rec.path] = rec
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.registry_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1.0",
                "projects": [p.to_dict() for p in self.list_projects(validate_paths=False)],
            }
            content = json.dumps(payload, indent=2) + "\n"
            temp_file = tempfile.NamedTemporaryFile(
                "w",
                dir=str(self.registry_file.parent),
                delete=False,
                encoding="utf-8",
            )
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()
            os.replace(temp_file.name, str(self.registry_file))
        except Exception:
            try:
                self.registry_file.write_text(content, encoding="utf-8")
            except Exception:
                pass

    def prune_stale(self) -> list[str]:
        """Prune projects whose directory paths no longer exist on disk."""
        stale = [path for path, rec in self._projects.items() if not os.path.exists(rec.path)]
        for path in stale:
            del self._projects[path]
        if stale:
            self._save()
        return stale

    def list_projects(self, validate_paths: bool = True) -> list[ProjectRecord]:
        if validate_paths:
            self.prune_stale()
        return sorted(
            self._projects.values(),
            key=lambda p: p.last_active or "",
            reverse=True,
        )

    def get_project(self, name_or_path: str) -> ProjectRecord | None:
        if not name_or_path:
            return None

        # 1. Exact match by stored path
        if name_or_path in self._projects:
            return self._projects[name_or_path]

        # 2. Match by normalized abspath
        norm_path = os.path.abspath(name_or_path)
        if norm_path in self._projects:
            return self._projects[norm_path]

        # 3. Match by resolved path
        try:
            res_path = str(Path(name_or_path).resolve())
            for p in self._projects.values():
                if str(Path(p.path).resolve()) == res_path:
                    return p
        except Exception:
            pass

        # 4. Match by exact name
        for p in self._projects.values():
            if p.name == name_or_path:
                return p

        # 5. Case-insensitive name match
        name_lower = name_or_path.lower()
        for p in self._projects.values():
            if p.name.lower() == name_lower:
                return p

        return None

    def register_project(
        self,
        path: Path | str,
        name: str | None = None,
        stack: str | None = None,
    ) -> ProjectRecord:
        abs_path_str = os.path.abspath(str(path))
        proj_dir = Path(abs_path_str)
        proj_dir.mkdir(parents=True, exist_ok=True)

        proj_name = name or proj_dir.name
        if not proj_name:
            proj_name = "project"

        if stack is None:
            try:
                from .onboard import detect_project_stack
                st_info = detect_project_stack(proj_dir)
                stack = st_info.get("stack", "generic")
            except Exception:
                stack = "generic"

        # Ensure folder-scoped context isolation under <project_dir>/.hex/
        hex_dir = proj_dir / ".hex"
        hex_dir.mkdir(parents=True, exist_ok=True)

        config_path = hex_dir / "config.yaml"
        if not config_path.is_file():
            try:
                from .onboard import init_project

                init_project(proj_dir, name=proj_name, force=False)
            except Exception:
                pass

        reflections_dir = hex_dir / "reflections"
        reflections_dir.mkdir(parents=True, exist_ok=True)
        lessons_path = reflections_dir / "LESSONS.md"
        if not lessons_path.is_file():
            lessons_path.write_text(
                f"# Project Reflections & Work Memory: {proj_name}\n\n## Lessons Learned\n",
                encoding="utf-8",
            )

        data_dir = hex_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Preserve task_count if already registered
        existing = self.get_project(abs_path_str)
        task_count = existing.task_count if existing else 0

        now_utc = datetime.now(timezone.utc).isoformat()
        record = ProjectRecord(
            name=proj_name,
            path=abs_path_str,
            stack=stack,
            last_active=now_utc,
            task_count=task_count,
            config_path=str(config_path),
        )
        self._projects[abs_path_str] = record
        self._save()
        return record

    def touch_active(self, path: Path | str, increment_tasks: bool = False) -> None:
        rec = self.get_project(str(path))
        if rec:
            rec.last_active = datetime.now(timezone.utc).isoformat()
            if increment_tasks:
                rec.task_count += 1
            self._projects[rec.path] = rec
            self._save()
        else:
            self.register_project(path)

    def detect_enclosing_project(self, start_dir: Path | str) -> ProjectRecord | None:
        curr = Path(os.path.abspath(str(start_dir)))
        home = Path.home().resolve()
        while True:
            is_git = (curr / ".git").is_dir()
            is_hex = (curr / ".hex").is_dir()
            # If curr is HOME, do not treat it as a project unless it has a git repo
            if is_git or (is_hex and curr.resolve() != home):
                rec = self.get_project(str(curr))
                if not rec:
                    rec = self.register_project(curr)
                return rec

            parent = curr.parent
            if parent == curr:
                break
            curr = parent
        return None
