import tempfile
from pathlib import Path
import pytest

from harness_core.soul import (
    DEFAULT_SOUL_MD,
    DEFAULT_SOUL_NAME,
    DEFAULT_USER_MD,
    MAX_SOUL_CHARS,
    SoulManager,
    _LEGACY_TEMPLATE_SOULS,
    normalize_soul,
)


@pytest.fixture
def temp_hex_home(tmp_path):
    home = tmp_path / ".hex"
    home.mkdir()
    return home


def test_soul_manager_initialization_seeds_defaults(temp_hex_home):
    mgr = SoulManager(global_home=temp_hex_home)
    assert mgr.global_soul_path.exists()
    assert mgr.global_user_path.exists()

    content = mgr.global_soul_path.read_text(encoding="utf-8")
    assert "You are HEX" in content
    assert normalize_soul(content) == normalize_soul(DEFAULT_SOUL_MD)

    dna = mgr.load_developer_dna()
    assert "Developer Profile & Conventions" in dna


def test_soul_manager_resolution_hierarchy(temp_hex_home, tmp_path):
    mgr = SoulManager(global_home=temp_hex_home)
    ws = tmp_path / "my_project"
    ws.mkdir()

    # 1. Without workspace .hex/SOUL.md, resolves global
    content, source = mgr.resolve_soul(workspace_root=ws)
    assert "You are HEX" in content
    assert source == "global"

    # 2. With workspace .hex/SOUL.md, workspace takes precedence
    ws_hex = ws / ".hex"
    ws_hex.mkdir()
    ws_soul = ws_hex / "SOUL.md"
    ws_soul.write_text("You are ProjectBot, tuned for this specific repo.", encoding="utf-8")

    content, source = mgr.resolve_soul(workspace_root=ws)
    assert "You are ProjectBot" in content
    assert source == "workspace"


def test_soul_manager_upgrades_legacy_template(temp_hex_home):
    # Seed a legacy template
    legacy_text = _LEGACY_TEMPLATE_SOULS[0]
    soul_file = temp_hex_home / "SOUL.md"
    soul_file.write_text(legacy_text, encoding="utf-8")

    mgr = SoulManager(global_home=temp_hex_home)
    # The constructor calls _ensure_global_files -> maybe_upgrade_soul
    updated = soul_file.read_text(encoding="utf-8")
    assert normalize_soul(updated) == normalize_soul(DEFAULT_SOUL_MD)
    assert "You are HEX" in updated


def test_soul_manager_preserves_custom_user_soul(temp_hex_home):
    custom_text = "You are CustomAssistant. Speak entirely in pirate dialect: Ahoy!"
    soul_file = temp_hex_home / "SOUL.md"
    soul_file.write_text(custom_text, encoding="utf-8")

    mgr = SoulManager(global_home=temp_hex_home)
    # Must NOT be overwritten
    current = soul_file.read_text(encoding="utf-8")
    assert current == custom_text
    content, source = mgr.resolve_soul()
    assert content == custom_text


def test_soul_manager_enforces_budget(temp_hex_home):
    # Oversized content > MAX_SOUL_CHARS
    oversized = "A" * (MAX_SOUL_CHARS + 500)
    soul_file = temp_hex_home / "SOUL.md"
    soul_file.write_text(oversized, encoding="utf-8")

    mgr = SoulManager(global_home=temp_hex_home)
    content, _ = mgr.resolve_soul()
    assert len(content) <= MAX_SOUL_CHARS + 100
    assert "... [truncated to fit 500-token budget]" in content


def test_soul_manager_rename_identity(temp_hex_home, tmp_path):
    mgr = SoulManager(global_home=temp_hex_home)
    assert mgr.get_active_name() == DEFAULT_SOUL_NAME

    # Rename globally to "Athena"
    mgr.rename_identity("Athena", scope="global")
    assert mgr.get_active_name() == "Athena"
    content, _ = mgr.resolve_soul()
    assert "You are Athena," in content

    # Rename in workspace scope
    ws = tmp_path / "workspace_test"
    ws.mkdir()
    mgr.rename_identity("Jarvis", scope="workspace", workspace_root=ws)
    assert mgr.get_active_name(workspace_root=ws) == "Jarvis"

    ws_content, source = mgr.resolve_soul(workspace_root=ws)
    assert source == "workspace"
    assert "You are Jarvis," in ws_content
