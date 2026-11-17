"""Behavior contracts for shared platform and onboarding services."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness_core.client import doctor, onboard
from harness_core.client.onboarding.verification import run_first_task_verification
from harness_core.config_loader import parse_config_yaml, update_config_yaml_model
from harness_core.platform.diagnostics import DoctorCheckResult, SystemDiagnostics
from harness_core.platform.hardware import HardwareProfile
from harness_core.storage import backup
from harness_core.storage.project_init import detect_project_stack, init_project
from harness_core.storage.project_registry import ProjectRegistry


def test_public_onboard_names_share_implementations():
    assert onboard.HardwareProfile is HardwareProfile
    assert onboard.update_config_yaml_model is update_config_yaml_model
    assert onboard.detect_project_stack is detect_project_stack
    assert onboard.init_project is init_project
    assert onboard.run_first_task_verification is run_first_task_verification
    assert doctor.DoctorCheckResult is DoctorCheckResult
    assert issubclass(doctor.HexDoctor, SystemDiagnostics)


def test_hardware_detection_selects_backends(tmp_path):
    with patch(
        "harness_core.platform.hardware.get_default_snapshot_provider",
        return_value=SimpleNamespace(backend_name="test_cow"),
    ) as cow, patch(
        "harness_core.platform.hardware.get_default_sandbox",
        return_value=SimpleNamespace(backend_name="test_sandbox"),
    ) as sandbox:
        profile = HardwareProfile.detect(tmp_path)
    cow.assert_called_once_with(tmp_path)
    sandbox.assert_called_once_with()
    assert profile.os_name
    assert profile.arch
    assert profile.ram_gb > 0
    assert profile.cow_backend == "test_cow"
    assert profile.sandbox_backend == "test_sandbox"


@pytest.mark.parametrize("source", ["explicit", "hex", "harness", "home"])
def test_diagnostics_constructor_precedence(tmp_path, monkeypatch, source):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HEX_DATA_DIR", raising=False)
    monkeypatch.delenv("HARNESS_DATA_DIR", raising=False)
    if source in ("explicit", "hex"):
        monkeypatch.setenv("HEX_DATA_DIR", str(tmp_path / "hex-data"))
    if source in ("explicit", "hex", "harness"):
        monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "harness-data"))
    explicit = tmp_path / "explicit-data" if source == "explicit" else None
    expected = {
        "explicit": tmp_path / "explicit-data",
        "hex": tmp_path / "hex-data",
        "harness": tmp_path / "harness-data",
        "home": tmp_path / ".hex" / "data",
    }[source]
    for cls in (SystemDiagnostics, doctor.HexDoctor):
        service = cls(workspace_root=tmp_path, data_dir=explicit)
        assert service.workspace_root == tmp_path.resolve()
        assert service.hex_dir == tmp_path.resolve() / ".hex"
        assert service.data_dir == expected.resolve()
    assert not expected.exists()


def test_check_result_schema_and_report_formatting():
    result = DoctorCheckResult("Space", "storage", "warning", "Low space", "Free space")
    assert result.to_dict() == {
        "name": "Space",
        "category": "storage",
        "status": "warning",
        "message": "Low space",
        "remediation": "Free space",
        "fixable": False,
    }
    report = doctor.DoctorReport(checks=[result])
    assert report.to_dict()["warnings"] == 1
    assert report.is_healthy
    assert "Remediation: Free space" in report.format_summary(color=False)
    assert "\033[" not in report.format_summary(color=False)


@pytest.mark.parametrize("free_gb,status", [(1, "warning"), (2, "ok")])
def test_storage_threshold_and_missing_directory_dedup(tmp_path, free_gb, status):
    service = SystemDiagnostics(tmp_path, tmp_path / "missing-data")
    usage = SimpleNamespace(free=free_gb * 1024**3, total=10 * 1024**3)
    # Historical doctor stdlib patch points still reach the shared implementation.
    with patch("harness_core.client.doctor.shutil.disk_usage", return_value=usage) as disk:
        results = service.check_storage()
    disk.assert_called_once_with(tmp_path.resolve())
    assert len(results) == 1
    assert results[0].status == status
    assert results[0].category == "storage"


def test_cow_initialization_error_is_reported(tmp_path):
    service = SystemDiagnostics(tmp_path, tmp_path / "data")
    with patch(
        "harness_core.storage.cow.get_default_snapshot_provider",
        side_effect=RuntimeError("backend unavailable"),
    ) as cow:
        results = service.check_cow_providers()
    cow.assert_called_once_with(root_dir=service.data_dir / "cow_snapshots")
    assert len(results) == 1
    assert results[0].status == "error"
    assert "backend unavailable" in results[0].message


@pytest.mark.parametrize("bwrap,container", [(False, False), (True, False), (False, True), (True, True)])
def test_sandbox_checks_keep_default_and_optional_results(tmp_path, bwrap, container):
    service = SystemDiagnostics(tmp_path, tmp_path / "data")
    with patch("harness_core.storage.sandbox.BubblewrapSandbox.is_available", return_value=bwrap), patch(
        "harness_core.storage.sandbox.DockerPodmanSandbox.is_available", return_value=container
    ), patch(
        "harness_core.storage.sandbox.get_default_sandbox",
        return_value=SimpleNamespace(backend_name="selected"),
    ) as default:
        results = service.check_sandboxes()
    default.assert_called_once_with(service.workspace_root)
    assert len(results) == 1 + int(bwrap) + int(container)
    assert results[-1].message == "Active execution sandbox: selected"
    assert all(result.status == "ok" and result.category == "sandbox" for result in results)


def test_dump_consumes_shared_system_checks(tmp_path):
    methods = {
        "check_python": "python",
        "check_hardware_acceleration": "hardware",
        "check_cow_providers": "cow",
        "check_sandboxes": "sandbox",
        "check_storage": "storage",
    }
    with patch("harness_core.storage.backup.SystemDiagnostics", autospec=True) as service:
        for method, category in methods.items():
            getattr(service.return_value, method).return_value = [
                DoctorCheckResult(category, category, "ok", f"shared {category}")
            ]
        output = backup.generate_dump(data_dir=tmp_path / "data", workspace_root=tmp_path)
    service.assert_called_once_with(workspace_root=tmp_path.resolve(), data_dir=(tmp_path / "data").resolve())
    for method, category in methods.items():
        getattr(service.return_value, method).assert_called_once_with()
        assert f"[OK] {category}: shared {category}" in output
    assert "END OF DIAGNOSTIC DUMP" in output


def test_model_patch_preserves_comments_sections_and_optional_removal(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "# operator config\nmodel:\n  # model comment\n  cloud_provider: old\n"
        "  cloud_base_url: https://old.invalid\n  reasoning_effort: high\n"
        "learning:\n  auto_reflect: off\n",
        encoding="utf-8",
    )
    assert update_config_yaml_model(
        config, cloud_provider="openai", cloud_model_id="model", profile="cloud",
        cloud_base_url="", reasoning_effort="", project_override=True,
    )
    parsed = parse_config_yaml(config)
    assert parsed["model"] == {
        "cloud_provider": "openai", "cloud_model_id": "model", "profile": "cloud", "project_override": True,
    }
    assert parsed["learning"] == {"auto_reflect": False}
    assert "# operator config" in config.read_text(encoding="utf-8")
    assert "# model comment" in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", ["model: [unterminated", "- not a mapping\n", "model: scalar\n"])
def test_model_patch_failure_does_not_write(tmp_path, content):
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    assert not update_config_yaml_model(config, cloud_provider="openai", cloud_model_id="model", profile="cloud")
    assert config.read_text(encoding="utf-8") == content


def test_project_registry_uses_stack_initialization_and_preserves_existing(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"
    monkeypatch.setenv("HEX_PROJECTS_REGISTRY", str(registry_file))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Cargo.toml").touch()
    record = ProjectRegistry(registry_file).register_project(repo, name="App")
    assert record.stack == "rust"
    config = repo / ".hex" / "config.yaml"
    assert parse_config_yaml(config)["verification"]["test_command"] == "cargo test"
    assert parse_config_yaml(config)["model"]["project_override"] is False
    config.write_text("custom: preserved\n", encoding="utf-8")
    assert init_project(repo)["created"] is False
    assert config.read_text(encoding="utf-8") == "custom: preserved\n"
    assert init_project(repo, force=True)["created"] is True
    assert parse_config_yaml(config)["project"]["stack"] == "rust"
    assert ProjectRegistry(registry_file).get_project(str(repo.resolve())).stack == "rust"


def test_verification_keeps_onboard_which_patch_seam(tmp_path):
    real_which = onboard.shutil.which

    def without_git(name, *args, **kwargs):
        return None if name == "git" else real_which(name, *args, **kwargs)

    with patch("harness_core.client.onboard.shutil.which", side_effect=without_git):
        result = run_first_task_verification(config_dir=tmp_path, target_dir=tmp_path / "verification")
    assert result["status"] == "failed"
    assert result["tools_summary"]["git"] is False
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["devtools"]["status"] == "failed"
    assert checks["provider_connectivity"]["status"] == "skipped"
    assert "VERIFICATION FAILED" in Path(result["artifact_path"]).read_text(encoding="utf-8")


def test_wizard_seeds_skills_from_resource_lookup(tmp_path, monkeypatch):
    skills = tmp_path / "bundled"
    skill = skills / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    orch = MagicMock()
    monkeypatch.setattr(onboard, "HarnessOrchestrator", MagicMock(return_value=orch))
    monkeypatch.setattr(onboard.HardwareProfile, "detect", lambda: HardwareProfile("Test", "test"))
    monkeypatch.setattr(onboard, "load_hex_environment", MagicMock())
    auth = MagicMock()
    auth.resolve_credential.return_value = None
    monkeypatch.setattr(onboard, "AntigravityOAuthManager", MagicMock(return_value=auth))
    lookup = MagicMock(return_value=skills)
    monkeypatch.setattr(onboard, "bundled_skills_dir", lookup)
    result = onboard.run_onboard_wizard(config_dir=tmp_path / "config", non_interactive=True, defaults_only=True)
    lookup.assert_called_once_with()
    orch.scanner.scan_and_register_skill.assert_called_once_with(skill)
    assert result["seeded_skills"] == 1
