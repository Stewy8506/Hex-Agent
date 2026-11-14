"""Background Reflection Synthesizer for HEX Autonomous Learning Loop.

Synthesizes structured post-mortem lessons from multi-step debugging and repair friction,
formats lessons into .hex/reflections/LESSONS.md, auto-populates negative guardrails
into GUARDRAILS.md, and coordinates with MemoryLifecycleManager for hit-tracking and promotion.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class FrictionLesson:
    """Structured post-mortem lesson synthesized from multi-step friction."""

    lesson_id: str
    task_id: str
    symptom: str
    root_cause: str
    verified_solution: str
    avoid_invariant: str
    source_nodes: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "task_id": self.task_id,
            "symptom": self.symptom,
            "root_cause": self.root_cause,
            "verified_solution": self.verified_solution,
            "avoid_invariant": self.avoid_invariant,
            "source_nodes": self.source_nodes,
            "created_at": self.created_at,
        }

    def format_markdown(self) -> str:
        nodes_str = ", ".join(self.source_nodes) if self.source_nodes else "None"
        inv = self.avoid_invariant
        if inv and not inv.startswith("- [AVOID]"):
            if inv.startswith("[AVOID]"):
                inv = f"- {inv}"
            else:
                inv = f"- [AVOID] {inv.lstrip('- ').strip()}"

        lines = [
            f"### Lesson: {self.lesson_id} (Task: {self.task_id})",
            f"- **Created At**: {self.created_at}",
            f"- **Linked Nodes**: {nodes_str}",
            f"- **Symptom**: {self.symptom}",
            f"- **Root Cause**: {self.root_cause}",
            f"- **Verified Solution**: {self.verified_solution}",
        ]
        if inv:
            lines.append(f"- **Negative Guardrail**: {inv}")
        return "\n".join(lines) + "\n"


class ReflectionSynthesizer:
    """Synthesizes actionable post-mortem lessons from friction traces and patches."""

    def __init__(
        self,
        router: Any | None = None,
        workspace_root: Path | str | None = None,
        lifecycle_mgr: Any | None = None,
        guardrails_mgr: Any | None = None,
    ) -> None:
        self.router = router
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        )
        self._lifecycle_mgr = lifecycle_mgr
        self._guardrails_mgr = guardrails_mgr

    @property
    def lifecycle_mgr(self) -> Any:
        if self._lifecycle_mgr is None:
            try:
                from .memory import MemoryLifecycleManager

                self._lifecycle_mgr = MemoryLifecycleManager(workspace_root=self.workspace_root)
            except Exception:
                self._lifecycle_mgr = None
        return self._lifecycle_mgr

    @property
    def guardrails_mgr(self) -> Any:
        if self._guardrails_mgr is None:
            try:
                from .memory import GuardrailsManager

                self._guardrails_mgr = GuardrailsManager(workspace_root=self.workspace_root)
            except Exception:
                self._guardrails_mgr = None
        return self._guardrails_mgr

    def synthesize_lesson(
        self,
        task_id: str,
        goal: str,
        error_traces: list[str] | None = None,
        final_patch: str = "",
        modified_files: list[str] | None = None,
        source_nodes: list[str] | None = None,
        router: Any | None = None,
        workspace_root: Path | str | None = None,
    ) -> FrictionLesson:
        """Synthesize a structured FrictionLesson using cloud LLM or deterministic fallback."""
        ws = Path(workspace_root).resolve() if workspace_root else self.workspace_root
        active_router = router or self.router
        traces = error_traces or []
        files = modified_files or []
        nodes = list(source_nodes or files)

        # 1. Attempt LLM generation if router available
        if active_router is not None and hasattr(active_router, "execute_routed_request"):
            try:
                prompt_text = (
                    "You are the HEX Reflection Synthesizer. A coding task succeeded after encountering multi-step friction.\n"
                    "Synthesize a concise post-mortem lesson following the JSON template below.\n\n"
                    f"Task ID: {task_id}\n"
                    f"Goal: {goal}\n"
                    f"Error Traces:\n" + ("\n".join(traces[:5]) if traces else "None reported") + "\n"
                    f"Modified Files: {', '.join(files) if files else 'None'}\n"
                    f"Final Patch:\n{final_patch[:1000] if final_patch else 'None'}\n\n"
                    "Return a JSON object with strictly these keys:\n"
                    '{\n  "symptom": "<concise summary of failure symptom/error>",\n'
                    '  "root_cause": "<concise root cause of the failure>",\n'
                    '  "verified_solution": "<concise verified resolution that fixed it>",\n'
                    '  "avoid_invariant": "- [AVOID] <negative constraint to prevent recurrence>"\n}'
                )
                decision, model_resp = active_router.execute_routed_request(
                    prompt=prompt_text,
                    task_class="reflection_synthesis",
                    risk_class="R1",
                    system_prompt="You are HEX Reflection Synthesizer. Output only valid JSON.",
                    workspace_dir=ws,
                    max_tokens=1024,
                )
                if model_resp and getattr(model_resp, "text", None):
                    raw = model_resp.text.strip()
                    m = re.search(r"\{.*\}", raw, re.DOTALL)
                    if m:
                        data = json.loads(m.group(0))
                        symptom = str(data.get("symptom") or "").strip()
                        root_cause = str(data.get("root_cause") or "").strip()
                        verified_solution = str(data.get("verified_solution") or "").strip()
                        avoid_invariant = str(data.get("avoid_invariant") or "").strip()
                        if symptom and root_cause and verified_solution:
                            lesson_hash = hashlib.sha256(
                                f"{task_id}:{symptom}:{verified_solution}".encode("utf-8")
                            ).hexdigest()[:10]
                            return FrictionLesson(
                                lesson_id=f"lesson_{lesson_hash}",
                                task_id=task_id,
                                symptom=symptom,
                                root_cause=root_cause,
                                verified_solution=verified_solution,
                                avoid_invariant=avoid_invariant,
                                source_nodes=nodes,
                            )
            except Exception as e:
                logger.warning("LLM lesson synthesis failed, using deterministic fallback: %s", e)

        # 2. Deterministic Structural Fallback
        symptom = ""
        if traces:
            first_err = traces[0].strip().split("\n")[0]
            clean_err = re.sub(r"^(?:Error:\s*)+", "", first_err).strip()
            symptom = clean_err[:180]
        else:
            symptom = f"Multi-step friction resolving goal '{goal[:80]}'"

        if files:
            root_cause = f"Defect or incompatibility in {', '.join(files[:3])}"
        else:
            root_cause = "Execution or configuration friction during task execution"

        if final_patch:
            patch_preview = final_patch.strip().split("\n")[0][:120]
            verified_solution = f"Corrected implementation in {', '.join(files or ['workspace'])}: {patch_preview}"
        else:
            verified_solution = f"Resolved via multi-step repair and verification in {', '.join(files or ['workspace'])}"

        if traces:
            first_err = traces[0].strip().split("\n")[0]
            clean_sig = re.sub(r"^(?:Error:\s*)+", "", first_err).strip()[:100]
            avoid_invariant = f"- [AVOID] Do not repeat pattern causing '{clean_sig}' in {', '.join(files or ['workspace'])}"
        else:
            avoid_invariant = f"- [AVOID] Avoid unverified edits in {', '.join(files or ['workspace'])}"

        lesson_hash = hashlib.sha256(
            f"{task_id}:{symptom}:{verified_solution}".encode("utf-8")
        ).hexdigest()[:10]

        return FrictionLesson(
            lesson_id=f"lesson_{lesson_hash}",
            task_id=task_id,
            symptom=symptom,
            root_cause=root_cause,
            verified_solution=verified_solution,
            avoid_invariant=avoid_invariant,
            source_nodes=nodes,
        )

    def append_to_lessons_md(
        self,
        lesson: FrictionLesson,
        workspace_root: Path | str | None = None,
    ) -> Path:
        """Append structured lesson to .hex/reflections/LESSONS.md and sync with memory lifecycle."""
        ws = Path(workspace_root).resolve() if workspace_root else self.workspace_root
        reflections_dir = ws / ".hex" / "reflections"
        reflections_dir.mkdir(parents=True, exist_ok=True)
        lessons_file = reflections_dir / "LESSONS.md"

        header = (
            "# Autonomous Friction Lessons & Post-Mortems\n\n"
            "Curated repository lessons synthesized from multi-step debugging and repair friction.\n\n"
        )

        formatted = lesson.format_markdown()

        if not lessons_file.is_file():
            lessons_file.write_text(header + formatted + "\n---\n\n", encoding="utf-8")
        else:
            with lessons_file.open("a", encoding="utf-8") as f:
                f.write(formatted + "\n---\n\n")

        # Automatically sync into MemoryLifecycleManager and GuardrailsManager
        self.record_lesson_in_lifecycle(lesson, workspace_root=ws)

        return lessons_file

    def record_lesson_in_lifecycle(
        self,
        lesson: FrictionLesson,
        workspace_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Record lesson into MemoryLifecycleManager and negative guardrail into GUARDRAILS.md."""
        ws = Path(workspace_root).resolve() if workspace_root else self.workspace_root
        result: dict[str, Any] = {"reflection": None, "guardrail": False}

        # 1. Record reflection in MemoryLifecycleManager
        lifecycle = self.lifecycle_mgr
        if lifecycle is not None and hasattr(lifecycle, "record_reflection"):
            try:
                from .memory import extract_wiki_tags

                tags = extract_wiki_tags(lesson.verified_solution)
                if lesson.source_nodes:
                    tags.extend([f"node:{n}" for n in lesson.source_nodes])
                tags.append(f"task:{lesson.task_id}")
                ref_record = lifecycle.record_reflection(
                    content=lesson.verified_solution,
                    source="LESSONS.md",
                    tags=list(dict.fromkeys(tags)),
                )
                result["reflection"] = ref_record
            except Exception as e:
                logger.warning("Failed to record reflection in MemoryLifecycleManager: %s", e)

        # 2. Record avoid invariant into GuardrailsManager
        if lesson.avoid_invariant:
            guardrails = self.guardrails_mgr
            if guardrails is not None and hasattr(guardrails, "record_avoid_invariant"):
                try:
                    res = guardrails.record_avoid_invariant(lesson.avoid_invariant)
                    result["guardrail"] = bool(res)
                except Exception as e:
                    logger.warning("Failed to record avoid invariant in GuardrailsManager: %s", e)

        return result
