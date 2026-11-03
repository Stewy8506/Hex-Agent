"""Multi-Framework Run-Recipe Auto-Detection Engine (`src/harness_core/recipes.py`).

Auto-detects project lifecycle recipes (bootstrap, build, test, start) across:
1. Node.js / TypeScript (pnpm, yarn, bun, npm, Next.js, Vite, NestJS)
2. Python (uv, poetry, pipenv, pytest, django, fastapi, unittest)
3. Rust (cargo)
4. Go (go test)
5. Makefile (make test)
6. Docker Compose (docker compose)
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Recipe:
    """Project lifecycle commands and detected runtime framework."""

    name: str
    kind: str  # "python" | "nodejs" | "rust" | "go" | "makefile" | "docker"
    test: str
    test_cmd_args: list[str]
    bootstrap: str | None = None
    build: str | None = None
    start: str | None = None
    port: int | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "bootstrap": self.bootstrap,
            "build": self.build,
            "test": self.test,
            "test_cmd_args": self.test_cmd_args,
            "start": self.start,
            "port": self.port,
            "evidence": self.evidence,
        }


def detect_recipe(root: Path | str) -> Recipe | None:
    """Auto-detects project recipe in strict precedence: Node.js, Python, Rust, Go, Makefile, Docker."""
    ws = Path(root).resolve()
    if not ws.is_dir():
        return None

    # 1. Node.js / TypeScript (package.json)
    pkg_json = ws / "package.json"
    if pkg_json.is_file():
        evidence = ["package.json"]
        pm = "npm"
        if (ws / "pnpm-lock.yaml").is_file():
            pm = "pnpm"
            evidence.append("pnpm-lock.yaml")
        elif (ws / "yarn.lock").is_file():
            pm = "yarn"
            evidence.append("yarn.lock")
        elif (ws / "bun.lockb").is_file() or (ws / "bun.lock").is_file():
            pm = "bun"
            evidence.append("bun.lockb" if (ws / "bun.lockb").is_file() else "bun.lock")
        elif (ws / "package-lock.json").is_file():
            evidence.append("package-lock.json")

        scripts: dict[str, str] = {}
        deps: dict[str, str] = {}
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        except Exception:
            pass

        # Framework & Port detection
        port = None
        if "next" in deps:
            port = 3000
        elif "vite" in deps:
            port = 5173
        elif "@nestjs/core" in deps:
            port = 3000
        elif "express" in deps:
            port = 8000

        # Commands
        bootstrap = f"{pm} install"
        build = f"{pm} run build" if "build" in scripts else None
        test = f"{pm} test" if "test" in scripts else f"{pm} test"
        test_args = [pm, "test"] if pm != "npm" else ["npm", "test"]
        start = f"{pm} start" if "start" in scripts else (f"{pm} run dev" if "dev" in scripts else None)

        return Recipe(
            name=f"Node.js ({pm})",
            kind="nodejs",
            bootstrap=bootstrap,
            build=build,
            test=test,
            test_cmd_args=test_args,
            start=start,
            port=port,
            evidence=evidence,
        )

    # 2. Python
    py_markers = [
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "pytest.ini",
        "manage.py",
    ]
    found_py = [m for m in py_markers if (ws / m).is_file()]
    has_py_files = any(ws.glob("*.py")) or any((ws / "src").glob("**/*.py")) or any((ws / "tests").glob("**/*.py"))

    if found_py or has_py_files:
        evidence = list(found_py)
        if not evidence:
            evidence.append("*.py")

        # Determine package manager / virtualenv runner
        if "uv.lock" in evidence:
            return Recipe(
                name="Python (uv pytest)",
                kind="python",
                bootstrap="uv pip install -e .",
                build="uv build",
                test="uv run pytest",
                test_cmd_args=["uv", "run", "pytest"],
                start="uv run python -m app",
                evidence=evidence,
            )
        if "poetry.lock" in evidence:
            return Recipe(
                name="Python (poetry pytest)",
                kind="python",
                bootstrap="poetry install",
                build="poetry build",
                test="poetry run pytest",
                test_cmd_args=["poetry", "run", "pytest"],
                start="poetry run python -m app",
                evidence=evidence,
            )
        if "Pipfile" in evidence:
            return Recipe(
                name="Python (pipenv pytest)",
                kind="python",
                bootstrap="pipenv install",
                build=None,
                test="pipenv run pytest",
                test_cmd_args=["pipenv", "run", "pytest"],
                start="pipenv run python -m app",
                evidence=evidence,
            )

        # Django detection
        if "manage.py" in evidence:
            return Recipe(
                name="Python (Django)",
                kind="python",
                bootstrap="pip install -r requirements.txt" if (ws / "requirements.txt").is_file() else None,
                build="python manage.py check",
                test="python manage.py test",
                test_cmd_args=["python", "manage.py", "test"],
                start="python manage.py runserver",
                port=8000,
                evidence=evidence,
            )

        # Pytest vs Unittest detection
        has_pytest_marker = (
            "pytest.ini" in evidence
            or (ws / "setup.cfg").is_file()
            or any((ws / "tests").glob("test_*.py"))
            or (ws / "conftest.py").is_file()
        )
        if (ws / "pyproject.toml").is_file():
            try:
                content = (ws / "pyproject.toml").read_text(encoding="utf-8")
                if "tool.pytest" in content or "pytest" in content:
                    has_pytest_marker = True
            except Exception:
                pass

        # Check if pytest is executable or installed
        py_exe = sys.executable or "python3"
        bootstrap = "pip install -e ." if (ws / "setup.py").is_file() or (ws / "pyproject.toml").is_file() else (
            "pip install -r requirements.txt" if (ws / "requirements.txt").is_file() else None
        )

        test_str = "pytest"
        test_args = ["pytest"]

        return Recipe(
            name="Python (pytest)",
            kind="python",
            bootstrap=bootstrap,
            build="python -m build" if (ws / "pyproject.toml").is_file() else None,
            test=test_str,
            test_cmd_args=test_args,
            start="python -m uvicorn app:main --port 8000" if (ws / "app.py").is_file() else None,
            port=8000 if (ws / "app.py").is_file() else None,
            evidence=evidence,
        )

    # 3. Rust (Cargo.toml)
    if (ws / "Cargo.toml").is_file():
        return Recipe(
            name="Rust (cargo)",
            kind="rust",
            bootstrap="cargo build",
            build="cargo build",
            test="cargo test",
            test_cmd_args=["cargo", "test"],
            start="cargo run",
            evidence=["Cargo.toml"],
        )

    # 4. Go (go.mod)
    if (ws / "go.mod").is_file():
        return Recipe(
            name="Go (go test)",
            kind="go",
            bootstrap="go mod download",
            build="go build ./...",
            test="go test ./...",
            test_cmd_args=["go", "test", "./..."],
            start="go run .",
            port=8080,
            evidence=["go.mod"],
        )

    # 5. Makefile
    makefile = ws / "Makefile" if (ws / "Makefile").is_file() else (ws / "makefile" if (ws / "makefile").is_file() else None)
    if makefile:
        evidence = [makefile.name]
        try:
            content = makefile.read_text(encoding="utf-8")
            has_test = "\ntest:" in f"\n{content}" or "\ntest " in f"\n{content}"
            has_build = "\nbuild:" in f"\n{content}"
            has_start = "\nrun:" in f"\n{content}" or "\nstart:" in f"\n{content}"
        except Exception:
            has_test = True
            has_build = False
            has_start = False

        if has_test:
            return Recipe(
                name="Makefile",
                kind="makefile",
                bootstrap="make setup" if "setup:" in content else None,
                build="make build" if has_build else None,
                test="make test",
                test_cmd_args=["make", "test"],
                start="make run" if "run:" in content else ("make start" if "start:" in content else None),
                evidence=evidence,
            )

    # 6. Docker Compose
    compose_file = None
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
        if (ws / name).is_file():
            compose_file = name
            break

    if compose_file:
        return Recipe(
            name="Docker Compose",
            kind="docker",
            bootstrap="docker compose build",
            build="docker compose build",
            test="docker compose run --rm test",
            test_cmd_args=["docker", "compose", "run", "--rm", "test"],
            start="docker compose up -d",
            port=8000,
            evidence=[compose_file],
        )

    return None

