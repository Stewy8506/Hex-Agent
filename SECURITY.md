# Security Policy

## Supported Versions

Security updates and patches are actively maintained for the latest minor release series of **HEX Agent** (`hex-ai`):

| Version | Supported |
| :--- | :---: |
| `0.22.x` | Yes |
| `< 0.22.0` | No |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report vulnerabilities privately using **GitHub Security Advisories** on the `Stewy8506/Harness` repository (**Security** tab -> **Advisories** -> **Report a vulnerability**).

### What to Include
- Affected component (`hex-core`, `hex-desktop`, `hex-tui`, `hex-protocol`, `hex-cli`, `skill_engine`, `SkillSpector`, `storage/cow.py`, or `cloud/`)
- Exact version (`hex --version`) and OS/architecture
- Minimal reproduction steps or proof-of-concept payload
- Potential impact (e.g., sandbox escape, R0–R4 approval bypass, prompt injection / Unicode smuggling bypass, credential leak)

## Response Timeline

- **Initial acknowledgement:** Within 48 hours
- **Triage & severity assessment:** Within 5 business days
- **Fix & coordinated disclosure:** Target 14–30 days depending on severity

## Security Scope

We are especially interested in reports affecting:
- **R0–R4 Tool Gateway & Approval Leases** (`harness_core/gateway.py`, `agent/approval_lease.py`)
- **Copy-on-Write (CoW) Sandbox Isolation** (`storage/cow.py`, `storage/sandbox.py`)
- **SkillSpector AST & Prompt-Injection Gate** (`cognition/skillspector.py`)
- **Credential & OS Keychain Vault** (`storage/vault_keychain.py`, `models/credential_pool.py`)
- **ACP Server & Cloud/P2P Protocol** (`hex_cli/acp.py`, `crates/hex-protocol/`, `cloud/`)
- **Cryptographic Event Ledger Integrity** (`storage/db.py`, `capabilities/proof.py`)

Out of scope: vulnerabilities requiring already-compromised local root/administrator access on the host machine, or third-party LLM provider outages.
