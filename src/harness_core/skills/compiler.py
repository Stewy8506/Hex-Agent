"""Explicit /learn Skill Compiler with Hermes Hardline Authoring Invariants and SkillSpector Gate.

Implements SkillCompiler according to:
- PRD Section 7.3 & FR-039..FR-039e
- Hermes Skill Authoring Standards (HARDLINE):
  * name: lowercase-hyphenated, <=64 chars, no spaces
  * description: ONE sentence, strictly <=60 chars, ends with a period, no marketing words
  * version: 0.1.0
  * author: "HEX" (never host username or git config)
  * Standard body section order (Title, When to Use, Prerequisites, How to Run,
    Quick Reference, Procedure, Pitfalls, Verification)
  * Knowledge-base layout (lean SKILL.md index + auxiliary references/ and scripts/)
- Deterministic verify: YAML frontmatter hooks
- Mandatory NVIDIA SkillSpector AST and security gate before disk persistence
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from harness_core.db import LedgerStore
from harness_core.skillspector import (
    SkillScanReport,
    SkillSpectorScanner,
    parse_skill_frontmatter,
)
from harness_core.soul import get_hex_home

_MARKETING_WORDS_RE = re.compile(
    r"\b(powerful|comprehensive|seamless|advanced|robust)\b",
    re.IGNORECASE,
)

_HERMES_STANDARD_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]


class SkillSecurityViolationError(PermissionError):
    """Raised when a candidate skill bundle violates SkillSpector safety checks."""

    def __init__(
        self,
        message: str,
        scan_report: Optional[SkillScanReport] = None,
        quarantine_path: Optional[Path] = None,
    ):
        super().__init__(message)
        self.scan_report = scan_report
        self.quarantine_path = quarantine_path


@dataclass
class SkillCompileResult:
    """Structured result returned by SkillCompiler."""

    name: str
    description: str
    skill_dir: Path
    is_global: bool
    frontmatter: dict[str, Any]
    bundle_sha256: str
    scan_report: SkillScanReport
    quarantined: bool = False
    quarantine_path: Optional[Path] = None
    references: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "skill_dir": str(self.skill_dir),
            "is_global": self.is_global,
            "frontmatter": self.frontmatter,
            "bundle_sha256": self.bundle_sha256,
            "quarantined": self.quarantined,
            "quarantine_path": str(self.quarantine_path) if self.quarantine_path else None,
            "scan_report": self.scan_report.to_dict() if self.scan_report else None,
            "references": self.references,
            "scripts": self.scripts,
        }


def normalize_skill_name(raw_name: str) -> str:
    """Normalize skill name adhering to Hermes HARDLINE standards:
    lowercase-hyphenated, <=64 chars, no spaces, no non-alphanumeric except hyphen.
    """
    cleaned = raw_name.lower().strip()
    cleaned = re.sub(r"[^a-z0-9\-]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "synthesized-skill"
    if len(cleaned) > 64:
        cleaned = cleaned[:64].rstrip("-")
    return cleaned


def normalize_skill_description(desc: str, skill_name: str = "") -> str:
    """Normalize skill description adhering to Hermes HARDLINE standards:
    - Exactly ONE sentence.
    - Strictly <= 60 characters total.
    - Ends with a period.
    - Strips marketing buzzwords (powerful, comprehensive, seamless, advanced, robust).
    - Avoids echoing the skill name.
    """
    cleaned = _MARKETING_WORDS_RE.sub("", desc)
    cleaned = " ".join(cleaned.split()).strip()

    if skill_name:
        # Strip leading skill name repetition
        cleaned = re.sub(
            rf"^(?:the\s+)?{re.escape(skill_name)}\s*[:\-]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            rf"^(?:the\s+)?{re.escape(skill_name.replace('-', ' '))}\s*[:\-]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    if "." in cleaned:
        first_sentence = cleaned.split(".")[0].strip()
    else:
        first_sentence = cleaned.rstrip("!?;: ")

    if not first_sentence:
        first_sentence = "Execute automated skill workflow"

    # Enforce strictly <= 60 characters with trailing period
    if len(first_sentence) > 59:
        truncated = first_sentence[:59]
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        first_sentence = truncated.rstrip(" ,;:-")

    result = f"{first_sentence}."
    if len(result) > 60:
        result = f"{result[:59]}."
    return result


def format_frontmatter_yaml(meta: dict[str, Any]) -> str:
    """Format frontmatter metadata into clean YAML string without PyYAML dependency."""
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, str):
            # Quote if contains colon or special characters
            if ":" in v or "#" in v or '"' in v or "'" in v:
                escaped = v.replace('"', '\\"')
                lines.append(f'{k}: "{escaped}"')
            else:
                lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            if all(isinstance(x, (str, int, float, bool)) for x in v):
                items_str = ", ".join(f'"{x}"' if isinstance(x, str) and ":" in x else str(x) for x in v)
                lines.append(f"{k}: [{items_str}]")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
        elif isinstance(v, dict):
            lines.append(f"{k}:")
            for sub_k, sub_v in v.items():
                if sub_v is None:
                    continue
                if isinstance(sub_v, bool):
                    lines.append(f"  {sub_k}: {'true' if sub_v else 'false'}")
                elif isinstance(sub_v, (int, float)):
                    lines.append(f"  {sub_k}: {sub_v}")
                elif isinstance(sub_v, str):
                    if ":" in sub_v or '"' in sub_v or "'" in sub_v:
                        escaped = sub_v.replace('"', '\\"')
                        lines.append(f'  {sub_k}: "{escaped}"')
                    else:
                        lines.append(f"  {sub_k}: {sub_v}")
                elif isinstance(sub_v, list):
                    items_str = ", ".join(str(x) for x in sub_v)
                    lines.append(f"  {sub_k}: [{items_str}]")
                elif isinstance(sub_v, dict):
                    lines.append(f"  {sub_k}:")
                    for ssk, ssv in sub_v.items():
                        lines.append(f"    {ssk}: {ssv}")
    lines.append("---")
    return "\n".join(lines)


class SkillCompiler:
    """Explicit /learn skill compilation engine enforcing Hermes standards and SkillSpector security."""

    def __init__(
        self,
        scanner: Optional[SkillSpectorScanner] = None,
        ledger: Optional[LedgerStore] = None,
        global_skills_dir: Optional[Path | str] = None,
        default_workspace_root: Optional[Path | str] = None,
        quarantine_dir: Optional[Path | str] = None,
    ):
        self.ledger = ledger
        self.scanner = scanner or SkillSpectorScanner(ledger=ledger)
        self.global_skills_dir = (
            Path(global_skills_dir) if global_skills_dir else (get_hex_home() / "skills")
        ).resolve()
        self.default_workspace_root = (
            Path(default_workspace_root).resolve()
            if default_workspace_root
            else Path.cwd().resolve()
        )
        self.quarantine_dir = (
            Path(quarantine_dir).resolve() if quarantine_dir else None
        )

    def _normalize_verify_hook(
        self, hook: Any
    ) -> Optional[dict[str, Any]]:
        """Normalize verify hook into structured dictionary."""
        if not hook:
            return None
        if isinstance(hook, str):
            return {
                "command": hook.strip(),
                "timeout_seconds": 30.0,
                "must_pass": True,
            }
        if hasattr(hook, "command"):
            d = {
                "command": str(hook.command).strip(),
                "timeout_seconds": float(getattr(hook, "timeout_seconds", 30.0)),
                "must_pass": bool(getattr(hook, "must_pass", True)),
            }
            if getattr(hook, "ast_invariant", None):
                d["ast_invariant"] = str(hook.ast_invariant)
            if getattr(hook, "test_filter", None):
                d["test_filter"] = str(hook.test_filter)
            if getattr(hook, "env_vars", None):
                d["env_vars"] = dict(hook.env_vars)
            return d
        if isinstance(hook, dict):
            cmd = str(hook.get("command", "")).strip()
            d = {
                "command": cmd,
                "timeout_seconds": float(hook.get("timeout_seconds", 30.0)),
                "must_pass": bool(hook.get("must_pass", True)),
            }
            if hook.get("ast_invariant"):
                d["ast_invariant"] = str(hook["ast_invariant"])
            if hook.get("test_filter"):
                d["test_filter"] = str(hook["test_filter"])
            if hook.get("env_vars"):
                d["env_vars"] = dict(hook["env_vars"])
            return d
        return None

    def compile_skill(
        self,
        source_input: str | Path | dict[str, Any],
        name: Optional[str] = None,
        description: Optional[str] = None,
        verify_hook: Optional[str | dict[str, Any]] = None,
        is_global: bool = False,
        workspace_root: Optional[Path | str] = None,
        references: Optional[dict[str, str]] = None,
        scripts: Optional[dict[str, str]] = None,
        platforms: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        allowed_tools: Optional[list[str]] = None,
        risk_class: Optional[str] = None,
        dry_run: bool = False,
    ) -> SkillCompileResult:
        """Synthesize and validate a reusable SKILL.md bundle.

        Enforces:
        - Hermes hardline authoring invariants:
          * name <= 64 chars, lowercase-hyphenated.
          * description <= 60 chars, 1 sentence, ends with '.', no marketing words.
          * author: 'HEX'.
          * version: '0.1.0'.
          * Standard section structure.
        - Deterministic verify: YAML frontmatter hooks.
        - Knowledge-base layout for reference docs and scripts.
        - Mandatory SkillSpector AST and security gate check before disk persistence.
        """
        extracted_name: Optional[str] = None
        extracted_desc: Optional[str] = None
        extracted_verify: Any = None
        extracted_body: str = ""
        aux_references: dict[str, str] = dict(references or {})
        aux_scripts: dict[str, str] = dict(scripts or {})

        # 1. Parse source input
        if isinstance(source_input, (Path, str)) and Path(str(source_input)).exists():
            input_path = Path(str(source_input))
            if input_path.is_file():
                raw_text = input_path.read_text(encoding="utf-8", errors="replace")
                fm, extracted_body = parse_skill_frontmatter(raw_text)
                extracted_name = fm.get("name")
                extracted_desc = fm.get("description") or fm.get("purpose")
                extracted_verify = fm.get("verify")
            elif input_path.is_dir():
                skill_file = input_path / "SKILL.md"
                if skill_file.is_file():
                    raw_text = skill_file.read_text(encoding="utf-8", errors="replace")
                    fm, extracted_body = parse_skill_frontmatter(raw_text)
                    extracted_name = fm.get("name") or input_path.name
                    extracted_desc = fm.get("description") or fm.get("purpose")
                    extracted_verify = fm.get("verify")
                # collect references and scripts if present
                ref_dir = input_path / "references"
                if ref_dir.is_dir():
                    for f in ref_dir.glob("**/*"):
                        if f.is_file():
                            rel = str(f.relative_to(ref_dir))
                            aux_references.setdefault(rel, f.read_text(encoding="utf-8", errors="replace"))
                scr_dir = input_path / "scripts"
                if scr_dir.is_dir():
                    for f in scr_dir.glob("**/*"):
                        if f.is_file():
                            rel = str(f.relative_to(scr_dir))
                            aux_scripts.setdefault(rel, f.read_text(encoding="utf-8", errors="replace"))
        elif isinstance(source_input, dict):
            extracted_name = source_input.get("name")
            extracted_desc = source_input.get("description")
            extracted_verify = source_input.get("verify")
            extracted_body = str(source_input.get("body") or source_input.get("procedure") or "")
            if source_input.get("references"):
                aux_references.update(source_input["references"])
            if source_input.get("scripts"):
                aux_scripts.update(source_input["scripts"])
        elif isinstance(source_input, str):
            if source_input.strip().startswith("---"):
                fm, extracted_body = parse_skill_frontmatter(source_input)
                extracted_name = fm.get("name")
                extracted_desc = fm.get("description") or fm.get("purpose")
                extracted_verify = fm.get("verify")
            else:
                extracted_body = source_input.strip()

        # 2. Normalize metadata invariants
        final_name = normalize_skill_name(name or extracted_name or "synthesized-skill")
        final_desc = normalize_skill_description(
            description or extracted_desc or f"Execute {final_name} procedures.",
            skill_name=final_name,
        )
        final_verify = self._normalize_verify_hook(verify_hook or extracted_verify)

        # 3. Construct frontmatter
        fm_meta: dict[str, Any] = {
            "name": final_name,
            "description": final_desc,
            "version": "0.1.0",
            "author": "HEX",
        }
        if risk_class:
            fm_meta["risk_class"] = risk_class
        if allowed_tools:
            fm_meta["allowed_tools"] = allowed_tools
        if platforms:
            fm_meta["platforms"] = platforms
        if final_verify:
            fm_meta["verify"] = final_verify
        if metadata:
            fm_meta["metadata"] = metadata

        # 4. Construct standard body sections
        body_text = extracted_body.strip()
        human_title = final_name.replace("-", " ").title()

        if not body_text or not body_text.startswith("#"):
            # Construct standard 8-section Hermes document
            verify_cmd = final_verify.get("command") if final_verify else f"pytest tests/test_{final_name.replace('-', '_')}.py"
            overview_intro = (
                f"{final_desc} This skill defines canonical workflows and regression invariants "
                f"for autonomous execution under the HEX workbench."
            )
            sections = [
                f"# {human_title}\n\n{overview_intro}\n",
                "## When to Use\n- Triggered when operating on related tasks and procedures.\n- Use during automated regression checks.\n",
                "## Prerequisites\n- Standard workbench environment and verified dependencies.\n",
                "## How to Run\n- Execute commands through the `terminal` tool.\n- Inspect files using `read_file`.\n",
                "## Quick Reference\n- Canonical commands and endpoints.\n",
                f"## Procedure\n1. Review workspace requirements.\n2. Execute procedures cleanly.\n3. Run verification check.\n\n{body_text}\n" if body_text else "## Procedure\n1. Review workspace requirements.\n2. Execute procedures cleanly.\n3. Run verification check.\n",
                "## Pitfalls\n- Ensure all sandbox and path constraints are respected.\n- Avoid unvetted dependencies.\n",
                f"## Verification\nRun verification command:\n```bash\n{verify_cmd}\n```\n",
            ]
            final_body = "\n".join(sections).strip()
        else:
            # Ensure standard sections exist
            final_body = body_text
            for sec in _HERMES_STANDARD_SECTIONS:
                if sec not in final_body:
                    if sec == "## Verification" and final_verify:
                        final_body += f"\n\n{sec}\nRun verification command:\n```bash\n{final_verify.get('command')}\n```"
                    else:
                        sec_name = sec.replace("## ", "")
                        final_body += f"\n\n{sec}\n- Standard {sec_name.lower()} guidelines.\n"

        # If references exist, add index section
        if aux_references and "### Reference Index" not in final_body:
            ref_index = "\n\n### Reference Index\n"
            for ref_name in sorted(aux_references.keys()):
                ref_index += f"- `references/{ref_name}`: Extended knowledge documentation.\n"
            final_body += ref_index

        full_skill_md = f"{format_frontmatter_yaml(fm_meta)}\n\n{final_body}\n"

        # 5. Resolve target and quarantine directories
        ws_root = (
            Path(workspace_root).resolve()
            if workspace_root
            else self.default_workspace_root
        )
        if is_global:
            target_dir = self.global_skills_dir / final_name
            quar_dir = (
                (self.quarantine_dir / final_name)
                if self.quarantine_dir
                else (get_hex_home() / "quarantine" / final_name).resolve()
            )
        else:
            target_dir = ws_root / ".hex" / "skills" / final_name
            quar_dir = (
                (self.quarantine_dir / final_name)
                if self.quarantine_dir
                else (ws_root / ".hex" / "quarantine" / final_name).resolve()
            )

        # 6. Stage candidate bundle into isolated staging directory
        stage_dir = Path(tempfile.mkdtemp(prefix="hex_skill_stage_"))
        try:
            skill_md_path = stage_dir / "SKILL.md"
            skill_md_path.write_text(full_skill_md, encoding="utf-8")

            if aux_references:
                stage_ref_dir = stage_dir / "references"
                for rel_path, content in aux_references.items():
                    dest = stage_ref_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content, encoding="utf-8")

            if aux_scripts:
                stage_scr_dir = stage_dir / "scripts"
                for rel_path, content in aux_scripts.items():
                    dest = stage_scr_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content, encoding="utf-8")
                    try:
                        dest.chmod(0o755)
                    except OSError:
                        pass

            # 7. Scan staged bundle with SkillSpector
            report = self.scanner.scan_bundle(stage_dir)

            # 8. Deterministic gate enforcement
            if report.gate_decision == "QUARANTINE":
                if not dry_run:
                    if quar_dir.exists():
                        shutil.rmtree(quar_dir)
                    quar_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(stage_dir, quar_dir)
                    audit_file = quar_dir / "audit.json"
                    audit_file.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

                raise SkillSecurityViolationError(
                    f"Skill '{final_name}' failed SkillSpector security gate "
                    f"({report.severity.upper()} risk score {report.risk_score}): {report.gate_decision}. "
                    f"Quarantined to {quar_dir}.",
                    scan_report=report,
                    quarantine_path=quar_dir,
                )

            # Clean path (ALLOW / REQUIRE_APPROVAL)
            if not dry_run:
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(stage_dir, target_dir)

            return SkillCompileResult(
                name=final_name,
                description=final_desc,
                skill_dir=target_dir,
                is_global=is_global,
                frontmatter=fm_meta,
                bundle_sha256=report.bundle_sha256,
                scan_report=report,
                quarantined=False,
                references=sorted(list(aux_references.keys())),
                scripts=sorted(list(aux_scripts.keys())),
            )
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
