"""F10 regression: one product version across packaging, runtime, and ACP."""

from __future__ import annotations

import re
from pathlib import Path

import harness_core
from harness_core.client.acp import ACPServer

from tests import REPO_ROOT


def test_pyproject_and_runtime_versions_match():
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no project version"
    declared = match.group(1)
    assert harness_core.__version__ == declared, (
        f"runtime version {harness_core.__version__} != pyproject {declared}"
    )


def test_acp_handshake_reports_product_version():
    server = ACPServer(orchestrator=object(), workspace_root=REPO_ROOT)
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["version"] == harness_core.__version__
