"""OS Keychain secure credential vault and plaintext configuration migration.

Securely manages credentials in the native OS credential store (macOS Keychain,
Windows DPAPI / Credential Manager, Linux Secret Service / KWallet via keyring)
and provides automated migration of plaintext credentials from configuration and
environment files.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SERVICE: str = "HEX Autonomous AI Workbench"

# Keyring backends that are NOT authentic OS keychains
_NOT_A_KEYCHAIN = (
    "keyring.backends.fail",
    "keyring.backends.null",
    "keyrings.alt",
)


class KeychainUnavailable(Exception):
    """No authentic OS keychain is reachable from this session."""


def _is_os_keychain(backend: Any) -> bool:
    """Ensure backend is a genuine OS credential store, rejecting insecure stubs."""
    if backend is None:
        return False
    # ChainerBackend writes to its highest-priority child
    children = getattr(backend, "backends", None)
    if isinstance(children, (list, tuple)) and children:
        return _is_os_keychain(children[0])

    backend_module = getattr(type(backend), "__module__", "")
    backend_name = getattr(type(backend), "__name__", "")

    if any(backend_module.startswith(prefix) for prefix in _NOT_A_KEYCHAIN):
        return False

    name_lower = backend_name.lower()
    if "fail" in name_lower or "null" in name_lower:
        return False

    return True


def os_keyring() -> Any:
    """Return the active OS credential store keyring, or raise KeychainUnavailable."""
    try:
        import keyring
    except ImportError as exc:
        raise KeychainUnavailable(
            "The 'keyring' package is not installed."
        ) from exc

    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise KeychainUnavailable(
            f"Keyring could not discover a backend: {exc}"
        ) from exc

    if not _is_os_keychain(backend):
        backend_cls = f"{type(backend).__module__}.{type(backend).__name__}"
        raise KeychainUnavailable(
            f"No authentic OS keychain is reachable (backend: {backend_cls})"
        )

    return backend


def is_keychain_available() -> bool:
    """Return True if an authentic OS keychain backend is available and operational."""
    try:
        os_keyring()
        return True
    except KeychainUnavailable:
        return False
    except Exception as exc:
        logger.debug("Keychain availability probe failed: %s", exc)
        return False


def backend_label() -> str:
    """Human-readable name of the active keychain backend."""
    try:
        backend = os_keyring()
        return str(getattr(backend, "name", "") or type(backend).__name__)
    except KeychainUnavailable as exc:
        return f"unavailable: {exc}"


def store_secret(service: str, account: str, secret: str) -> None:
    """Store secret in OS Keychain under service and account, validating writeback."""
    backend = os_keyring()
    try:
        backend.set_password(service, account, secret)
        readback = backend.get_password(service, account)
    except Exception as exc:
        raise KeychainUnavailable(
            f"OS keychain refused the secret ({type(exc).__name__}): {exc}"
        ) from exc
    if readback != secret:
        raise KeychainUnavailable("OS keychain did not return the secret it was given")


def get_secret(service: str, account: str) -> Optional[str]:
    """Retrieve secret from OS Keychain, returning None if missing or unavailable."""
    try:
        backend = os_keyring()
        return backend.get_password(service, account)
    except KeychainUnavailable:
        return None
    except Exception as exc:
        logger.debug("Failed to retrieve secret from keychain for %s/%s: %s", service, account, exc)
        return None


def delete_secret(service: str, account: str) -> bool:
    """Delete secret from OS Keychain. Returns True once removed or already absent."""
    try:
        backend = os_keyring()
        try:
            backend.delete_password(service, account)
        except Exception:
            pass
        return backend.get_password(service, account) is None
    except KeychainUnavailable:
        return False
    except Exception as exc:
        logger.debug("Failed to delete secret from keychain for %s/%s: %s", service, account, exc)
        return False


def _derive_account_name(key_name: str, parent: str = "") -> str:
    """Derive standardized keychain account name (e.g. 'openai_api_key')."""
    clean = key_name.strip().lower()
    if parent and clean in ("api_key", "key", "token", "secret"):
        return f"{parent.strip().lower()}_api_key"
    if clean.endswith("_api_key"):
        return clean
    if clean.endswith("_key"):
        prefix = clean[:-4]
        return f"{prefix}_api_key"
    if clean.endswith("_token"):
        return clean
    if parent:
        return f"{parent.strip().lower()}_api_key"
    return f"{clean}_api_key"


def _atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text atomically with explicit POSIX permissions."""
    temp_path = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        temp_path.write_text(content, encoding="utf-8")
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _is_secret_key(key: str) -> bool:
    """Check if a config or env key name represents an API key or secret token."""
    k = key.upper()
    return (
        k.endswith("_API_KEY")
        or k.endswith("_KEY")
        or k.endswith("_TOKEN")
        or k.endswith("_SECRET")
        or k == "API_KEY"
    )


def migrate_plaintext_credentials(
    config_path: Optional[Path] = None,
    credentials_path: Optional[Path] = None,
) -> dict[str, str]:
    """Scan configuration and env files for plaintext secrets and migrate to OS Keychain.

    When OS Keychain is available:
    - Scans config.yaml and credentials.env for API keys and tokens.
    - Stores each secret into the OS Keychain under account f"{provider}_api_key".
    - Scrubs plaintext values from the files on disk, setting storage: keychain.

    When OS Keychain is unavailable (headless Linux, no Secret Service daemon):
    - Leaves files intact and logs an informational notice.

    Returns:
        Mapping of migrated key/account names to their keychain account ids.
    """
    if not is_keychain_available():
        logger.info(
            "OS Keychain is not available in this environment; skipping credential migration."
        )
        return {}

    migrated: dict[str, str] = {}

    # Determine paths to inspect
    target_files: list[tuple[Path, str]] = []  # (path, format: "yaml" | "env")

    if config_path is not None or credentials_path is not None:
        if config_path is not None:
            target_files.append((Path(config_path), "yaml"))
        if credentials_path is not None:
            target_files.append((Path(credentials_path), "env"))
    else:
        default_config = Path.home() / ".hex" / "config.yaml"
        if default_config.is_file():
            target_files.append((default_config, "yaml"))
        default_env = Path.home() / ".hex" / "credentials.env"
        if default_env.is_file():
            target_files.append((default_env, "env"))
        local_env = Path(".hex") / "credentials.env"
        if local_env.is_file() and local_env.resolve() != default_env.resolve():
            target_files.append((local_env, "env"))

    for file_path, fmt in target_files:
        if not file_path.is_file():
            continue
        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read credentials file %s: %s", file_path, exc)
            continue

        updated_lines: list[str] = []
        file_modified = False

        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                updated_lines.append(line)
                continue

            if fmt == "env" and "=" in line:
                key, val = line.split("=", 1)
                k_clean = key.strip()
                v_clean = val.strip().strip("'\"")
                if _is_secret_key(k_clean) and v_clean and v_clean not in ("keychain", "vault") and not v_clean.startswith("storage:"):
                    account = _derive_account_name(k_clean)
                    try:
                        store_secret(SERVICE, account, v_clean)
                        migrated[k_clean] = account
                        updated_lines.append(f"{key.split('=')[0]}=keychain")
                        file_modified = True
                        continue
                    except Exception as exc:
                        logger.warning("Failed to store secret for %s in keychain: %s", k_clean, exc)

            elif fmt == "yaml" and ":" in line:
                key, val = line.split(":", 1)
                k_clean = key.strip().strip("- ")
                v_clean = val.strip().strip("'\"")
                if _is_secret_key(k_clean) and v_clean and v_clean not in ("keychain", "vault") and not v_clean.startswith("storage:"):
                    account = _derive_account_name(k_clean)
                    try:
                        store_secret(SERVICE, account, v_clean)
                        migrated[k_clean] = account
                        indent = line[: len(line) - len(line.lstrip())]
                        updated_lines.append(f"{indent}{k_clean}: keychain")
                        file_modified = True
                        continue
                    except Exception as exc:
                        logger.warning("Failed to store secret for %s in keychain: %s", k_clean, exc)

            updated_lines.append(line)

        if file_modified:
            new_content = "\n".join(updated_lines)
            if raw_text.endswith("\n"):
                new_content += "\n"
            _atomic_write_text(file_path, new_content, mode=0o600)
            logger.info("Scrubbed and migrated plaintext secrets in %s to OS Keychain", file_path)

    return migrated
