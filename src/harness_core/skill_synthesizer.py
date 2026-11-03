"""Hermes-Style Self-Synthesizing Skills Engine with Mandatory NVIDIA SkillSpector Gate.

Inspects completed engineering workflows (tool journal, Proof-Carrying Artifacts, AST diffs),
distills reusable patterns into structured SKILL.md bundles, and enforces static, OSV, and
semantic alignment validation through NVIDIA SkillSpector before saving to active registry.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import LedgerStore
from .skillspector import (
    RISK_ORDER,
    SkillScanReport,
    SkillSecurityScanner,
    parse_skill_frontmatter,
)


@dataclass
class SynthesizedSkill:
    """Represents a self-synthesized skill generated from task execution history."""

    name: str
    purpose: str
    description: str
    risk_class: str = "R1"
    allowed_tools: list[str] = field(default_factory=list)
    trigger: str = ""
    content: str = ""
    status: str = "candidate"  # "active" | "quarantined" | "candidate"
    scan_report: dict[str, Any] = field(default_factory=dict)
    disk_path: str | None = None
    quarantine_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "description": self.description,
            "risk_class": self.risk_class,
            "allowed_tools": list(self.allowed_tools),
            "trigger": self.trigger,
            "content": self.content,
            "status": self.status,
            "scan_report": self.scan_report,
            "disk_path": self.disk_path,
            "quarantine_reason": self.quarantine_reason,
        }


TOOL_RISK_MAP: dict[str, str] = {
    "read_file": "R0",
    "list_files": "R0",
    "query_graphify": "R1",
    "inspect_code": "R1",
    "run_tests": "R2",
    "write_file": "R2",
    "patch_file": "R2",
    "apply_diff": "R2",
    "shell_exec": "R2",
    "git_commit": "R3",
    "git_push": "R3",
    "git_push_remote": "R3",
    "external_webhook": "R3",
    "modify_system_policy": "R4",
}


def _determine_risk_class(tools_used: list[str]) -> str:
    """Computes the minimum sufficient risk class required for the given toolset."""
    if not tools_used:
        return "R1"

    level = 0
    risk_names = ["R0", "R1", "R2", "R3", "R4"]
    for t in tools_used:
        r = TOOL_RISK_MAP.get(t)
        if r is None:
            t_lower = t.lower()
            if any(k in t_lower for k in ("policy", "admin", "format")):
                r = "R4"
            elif any(k in t_lower for k in ("push", "remote", "deploy", "webhook")):
                r = "R3"
            elif any(k in t_lower for k in ("write", "patch", "edit", "exec", "run", "test", "delete", "rm")):
                r = "R2"
            elif any(k in t_lower for k in ("graph", "inspect", "ast", "query")):
                r = "R1"
            else:
                r = "R0"
        lvl = RISK_ORDER.get(r, 1)
        if lvl > level:
            level = lvl

    return risk_names[level]


def _slugify(text: str) -> str:
    """Converts a phrase into a clean snake_case identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "synthesized_skill"


class SkillSynthesizer:
    """Hermes-style learning loop engine that synthesizes reusable SKILL.md bundles from tasks."""

    def __init__(
        self,
        ledger: LedgerStore,
        scanner: SkillSecurityScanner | None = None,
        custom_skills_dir: Path | str | None = None,
        quarantine_dir: Path | str | None = None,
    ):
        self.ledger = ledger
        self.scanner = scanner or SkillSecurityScanner(ledger)

        # Locate default repository directories
        repo_root = Path(__file__).resolve().parents[2]
        if custom_skills_dir:
            self.custom_skills_dir = Path(custom_skills_dir).resolve()
        else:
            if (repo_root / "skills").is_dir():
                self.custom_skills_dir = (repo_root / "skills" / "custom").resolve()
            else:
                self.custom_skills_dir = (Path.home() / ".hex" / "skills" / "custom").resolve()

        if quarantine_dir:
            self.quarantine_dir = Path(quarantine_dir).resolve()
        else:
            self.quarantine_dir = (repo_root / ".hex" / "quarantine").resolve()

    def extract_task_context(self, task_id: str) -> dict[str, Any]:
        """Inspects completed task records, proof bundle, and tool journal."""
        task = self.ledger.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if task.get("status") != "COMPLETED":
            raise ValueError(f"Task {task_id} is not completed (current status: {task.get('status')})")

        goal = str(task.get("goal") or "").strip()
        project_id = str(task.get("project_id") or "default")

        # Retrieve proof bundle
        proof = self.ledger.get_proof_bundle(task_id) or {}

        # Retrieve tool call journal
        if hasattr(self.ledger, "get_call_journal"):
            calls = self.ledger.get_call_journal(task_id)
        else:
            calls = self.ledger.list_tool_calls(task_id)

        # Deduplicate tools in execution order
        tools_used: list[str] = []
        for call in calls:
            tname = call.get("tool_name") or call.get("tool_id")
            if tname and tname not in tools_used:
                tools_used.append(tname)

        # Extract touched files from tool inputs and diffs
        files_modified: list[str] = []
        for call in calls:
            inp_raw = call.get("tool_input_json") or call.get("arguments_redacted_json") or call.get("arguments") or "{}"
            if isinstance(inp_raw, str):
                try:
                    inp_dict = json.loads(inp_raw)
                except Exception:
                    inp_dict = {}
            elif isinstance(inp_raw, dict):
                inp_dict = inp_raw
            else:
                inp_dict = {}

            for key in ("path", "file_path", "file", "filename", "workspace_path"):
                val = inp_dict.get(key)
                if val and isinstance(val, str) and val not in files_modified:
                    files_modified.append(val)

        # Extract modified files from git diff
        git_diff = str(proof.get("git_diff_text") or "")
        diff_files = re.findall(r"^\+\+\+ b/(.*)$", git_diff, re.M)
        for df in diff_files:
            clean_df = df.strip()
            if clean_df and clean_df not in files_modified:
                files_modified.append(clean_df)

        # Verification result evaluation
        verif_raw = proof.get("verification_results_json")
        verification_passed = True
        if verif_raw:
            try:
                verif_dict = json.loads(verif_raw) if isinstance(verif_raw, str) else verif_raw
                if isinstance(verif_dict, dict):
                    if "passed" in verif_dict:
                        verification_passed = bool(verif_dict["passed"])
                    elif "exit_code" in verif_dict:
                        verification_passed = (verif_dict["exit_code"] == 0)
            except Exception:
                pass

        ast_impact = proof.get("ast_impact_json") or {}
        if isinstance(ast_impact, str):
            try:
                ast_impact = json.loads(ast_impact)
            except Exception:
                ast_impact = {}

        code_summary = ""
        if files_modified:
            code_summary = f"Workflow modified {len(files_modified)} file(s): {', '.join(files_modified[:5])}"

        return {
            "task_id": task_id,
            "project_id": project_id,
            "goal": goal,
            "tools_used": tools_used,
            "files_modified": files_modified,
            "verification_passed": verification_passed,
            "ast_impact": ast_impact,
            "git_diff": git_diff,
            "code_summary": code_summary,
        }

    def generate_skill_markdown(
        self,
        name: str,
        goal: str,
        tools_used: list[str],
        files_modified: list[str],
        verification_passed: bool,
        code_summary: str = "",
    ) -> str:
        """Generates structured SKILL.md bundle content with frontmatter and markdown body."""
        clean_name = _slugify(name)
        risk_class = _determine_risk_class(tools_used)

        # Sanitize single-line purpose & description for YAML
        distilled_purpose = (goal or f"Automated workflow for {clean_name}").replace("\n", " ").strip()
        distilled_purpose = re.sub(r"\s+", " ", distilled_purpose)

        # Allowed tools array representation
        tools_repr = "[" + ", ".join(tools_used) + "]"

        # Frontmatter block
        lines = [
            "---",
            f"name: {clean_name}",
            f"purpose: {distilled_purpose}",
            f"description: {distilled_purpose}",
            f"risk_class: {risk_class}",
            f"allowed_tools: {tools_repr}",
            f"trigger: {clean_name}",
            "---",
            f"# {clean_name.replace('_', ' ').title()}",
            "",
            "## Overview & Problem Pattern",
            f"Distilled workflow pattern addressing: {distilled_purpose}.",
        ]

        if code_summary:
            lines.append(f"{code_summary}.")
        lines.append("")

        lines.extend([
            "## Required Toolchain",
            "The following authorized tools are required for this pattern:",
        ])
        if tools_used:
            for t in tools_used:
                r = TOOL_RISK_MAP.get(t, "R1")
                lines.append(f"- `{t}`: Authorized under risk class {r}")
        else:
            lines.append("- No explicit tools required (informational pattern)")
        lines.append("")

        if files_modified:
            lines.extend([
                "Target artifacts and files referenced:",
                *[f"- `{f}`" for f in files_modified[:10]],
                "",
            ])

        lines.extend([
            "## Step-by-Step Execution Workflow",
            "1. **Inspection**: Examine repository context, AST dependencies, and target components.",
            "2. **Execution**: Apply necessary code changes or operations using authorized tools.",
            "3. **Verification**: Confirm syntax, AST boundaries, and test assertions are satisfied.",
            "",
            "## Verification Standards",
            "- Proof-Carrying Artifact verification must record passing assertions.",
            f"- Test suite verification passing status: `{'PASSED' if verification_passed else 'PENDING'}`.",
            "- Workspace AST and snapshot integrity must be verified before promotion.",
            "",
        ])

        return "\n".join(lines)

    def synthesize_skill(
        self,
        task_id: str,
        skill_name: str | None = None,
        dry_run: bool = False,
    ) -> SynthesizedSkill:
        """Synthesizes a new skill from task history and gates it through NVIDIA SkillSpector."""
        ctx = self.extract_task_context(task_id)

        # Determine skill identifier
        if skill_name:
            name = _slugify(skill_name)
        else:
            # Slugify the first 4-5 words of the goal
            goal_words = ctx["goal"].split()[:5]
            name = _slugify(" ".join(goal_words)) if goal_words else f"task_{task_id[:8]}"

        md_content = self.generate_skill_markdown(
            name=name,
            goal=ctx["goal"],
            tools_used=ctx["tools_used"],
            files_modified=ctx["files_modified"],
            verification_passed=ctx["verification_passed"],
            code_summary=ctx.get("code_summary", ""),
        )

        # Mandatory NVIDIA SkillSpector Gate
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_bundle = Path(tmpdir) / name
            temp_bundle.mkdir(parents=True, exist_ok=True)
            (temp_bundle / "SKILL.md").write_text(md_content, encoding="utf-8")

            report = self.scanner.scan_bundle(temp_bundle)

        gate_decision = report.gate_decision
        risk_score = report.risk_score

        # Clean skills must have ALLOW gate decision and risk score < 25
        if gate_decision == "ALLOW" and risk_score < 25:
            status = "active"
            quarantine_reason = None
            disk_path = None
            if not dry_run:
                dest_dir = self.custom_skills_dir / name
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file = dest_dir / "SKILL.md"
                dest_file.write_text(md_content, encoding="utf-8")
                # Register in ledger
                self.scanner.scan_and_register_skill(dest_dir)
                disk_path = str(dest_file)
        else:
            status = "quarantined"
            reasons = [r.get("rule_id", "") for r in report.matched_rules if r.get("rule_id")]
            if not report.semantic_alignment_passed and report.semantic_notes:
                reasons.append(report.semantic_notes)
            quarantine_reason = ", ".join(reasons) if reasons else f"Risk score {risk_score} exceeded threshold"
            disk_path = None
            if not dry_run:
                dest_dir = self.quarantine_dir / name
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file = dest_dir / "SKILL.md"
                dest_file.write_text(md_content, encoding="utf-8")
                audit_file = dest_dir / "audit.json"
                audit_file.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
                disk_path = str(dest_file)

        return SynthesizedSkill(
            name=name,
            purpose=ctx["goal"],
            description=ctx["goal"],
            risk_class=_determine_risk_class(ctx["tools_used"]),
            allowed_tools=list(ctx["tools_used"]),
            trigger=name,
            content=md_content,
            status=status,
            scan_report=report.to_dict(),
            disk_path=disk_path,
            quarantine_reason=quarantine_reason,
        )

    def list_skills(self) -> dict[str, list[dict[str, Any]]]:
        """Enumerates built-in, custom, and quarantined skills with SkillSpector badges."""
        results: dict[str, list[dict[str, Any]]] = {
            "built_in": [],
            "custom": [],
            "quarantined": [],
        }

        # 1. Built-in skills
        repo_root = Path(__file__).resolve().parents[2]
        builtin_root = repo_root / "skills"
        if builtin_root.is_dir():
            for sub in sorted(builtin_root.iterdir()):
                if sub.is_dir() and sub.name != "custom" and (sub / "SKILL.md").is_file():
                    fm, _ = parse_skill_frontmatter((sub / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
                    name = str(fm.get("name") or sub.name)
                    results["built_in"].append({
                        "name": name,
                        "purpose": str(fm.get("purpose") or fm.get("description") or ""),
                        "description": str(fm.get("description") or ""),
                        "risk_class": str(fm.get("risk_class") or "R1").upper(),
                        "allowed_tools": fm.get("allowed_tools") or [],
                        "status": "active",
                        "status_badge": "[ACTIVE]",
                        "disk_path": str(sub / "SKILL.md"),
                    })

        # 2. Custom skills
        if self.custom_skills_dir.is_dir():
            for sub in sorted(self.custom_skills_dir.iterdir()):
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    fm, _ = parse_skill_frontmatter((sub / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
                    name = str(fm.get("name") or sub.name)
                    results["custom"].append({
                        "name": name,
                        "purpose": str(fm.get("purpose") or fm.get("description") or ""),
                        "description": str(fm.get("description") or ""),
                        "risk_class": str(fm.get("risk_class") or "R1").upper(),
                        "allowed_tools": fm.get("allowed_tools") or [],
                        "status": "active",
                        "status_badge": "[ACTIVE]",
                        "disk_path": str(sub / "SKILL.md"),
                    })

        # 3. Quarantined skills
        if self.quarantine_dir.is_dir():
            for sub in sorted(self.quarantine_dir.iterdir()):
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    fm, _ = parse_skill_frontmatter((sub / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
                    name = str(fm.get("name") or sub.name)
                    audit_data: dict[str, Any] = {}
                    audit_file = sub / "audit.json"
                    if audit_file.is_file():
                        try:
                            audit_data = json.loads(audit_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass

                    reasons = [r.get("rule_id", "") for r in audit_data.get("matched_rules", []) if r.get("rule_id")]
                    if not reasons and audit_data.get("semantic_notes"):
                        reasons.append(audit_data["semantic_notes"])
                    reason_str = ", ".join(reasons) if reasons else "Quarantined"

                    results["quarantined"].append({
                        "name": name,
                        "purpose": str(fm.get("purpose") or fm.get("description") or ""),
                        "description": str(fm.get("description") or ""),
                        "risk_class": str(fm.get("risk_class") or "R1").upper(),
                        "allowed_tools": fm.get("allowed_tools") or [],
                        "status": "quarantined",
                        "status_badge": "[QUARANTINED]",
                        "quarantine_reason": reason_str,
                        "risk_score": audit_data.get("risk_score", 0),
                        "disk_path": str(sub / "SKILL.md"),
                    })

        return results
