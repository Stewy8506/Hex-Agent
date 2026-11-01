"""Two-Tier Local/Cloud Model Router & Governed Memory Service (PRD Sections 6.4, 6.6, 11, FR-030..FR-035, FR-045..FR-047).

- Tier-1 Local M4 Gatekeeper (MacBook Air M4 MLX profile):
  1. Distills verbose pytest/compiler logs (>20,000 tokens) into compact, provenance-linked
     failure summaries (>70% token reduction while preserving file, line number, and assertion error).
  2. Runs local R0/R1 triage and keeps sensitive/secret data 100% local.
- Governed Memory Service:
  Enforces strict isolation across `project`, `personal`, and `episodic` scopes with provenance,
  confidence, sensitivity, and expiration controls.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .db import LedgerStore, canonical_json, utcnow_iso
from .gateway import redact_secrets
from .models import TaskBudget
from .providers.base import ModelProvider, ModelResponse, PolicyBlockedError
from .providers.cloud_provider import CloudModelProvider
from .providers.mlx_provider import LocalMLXModelProvider


@dataclass
class RouteDecision:
    tier: str  # 'tier1_local_m4' | 'tier2_cloud_reasoner'
    provider: str
    model_id: str
    reason: str
    raw_tokens_estimated: int
    compressed_tokens_estimated: int
    token_savings_pct: float
    compressed_context: str
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    compression_ratio: float = 0.0
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "provider": self.provider,
            "model_id": self.model_id,
            "reason": self.reason,
            "raw_tokens_estimated": self.raw_tokens_estimated,
            "compressed_tokens_estimated": self.compressed_tokens_estimated,
            "token_savings_pct": self.token_savings_pct,
            "compressed_context": self.compressed_context,
            "latency_ms": round(self.latency_ms, 2),
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "compression_ratio": self.compression_ratio,
            "provider_metadata": self.provider_metadata,
        }


class TwoTierModelRouter:
    """Routes between Tier-1 Local M4 MLX Gatekeeper/Compressor and Tier-2 Heavy Reasoner.

    Supports configurable log compression mode (`compressor_mode`):
    - `'local'` (default): 100% local M4 MLX / deterministic compression at $0 marginal cost.
    - `'cloud'`: Uses `compressor_provider` (or `cloud_provider`) for cloud LLM log compression,
      automatically falling back to `'local'` when `data_classification` is `'sensitive'` or `'secret'`.
    """

    def __init__(
        self,
        local_provider: ModelProvider | None = None,
        cloud_provider: ModelProvider | None = None,
        local_model_id: str = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        cloud_model_id: str = "gemini-3.5-pro",
        compressor_mode: str | None = None,
        compressor_provider: ModelProvider | None = None,
    ):
        import os

        self.local_provider = local_provider or LocalMLXModelProvider(model_id=local_model_id)
        self.cloud_provider = cloud_provider or CloudModelProvider(model_id=cloud_model_id)
        self.compressor_provider = compressor_provider or self.cloud_provider
        self.compressor_mode = (
            compressor_mode or os.getenv("HARNESS_COMPRESSOR_MODE") or "local"
        ).strip().lower()
        self.local_model_id = getattr(self.local_provider, "model_id", local_model_id)
        self.cloud_model_id = getattr(self.cloud_provider, "model_id", cloud_model_id)

    def compress_verbose_log(
        self,
        raw_log: str,
        max_chars: int = 1600,
        *,
        mode: str | None = None,
        data_classification: str = "internal",
        budget: TaskBudget | None = None,
    ) -> dict[str, Any]:
        """FR-045a: Configurable Local M4 or Cloud log compressor.

        Strips repetitive pass/info noise from raw test/build logs while preserving exact
        file paths, line numbers, traceback frames, and assertion errors.
        """
        effective_mode = (mode or self.compressor_mode or "local").strip().lower()
        # Privacy guardrail: sensitive/secret logs NEVER leave the local boundary
        if data_classification in ("sensitive", "secret"):
            effective_mode = "local"

        if effective_mode == "cloud" and hasattr(self.compressor_provider, "compress_log"):
            comp = self.compressor_provider.compress_log(
                raw_log,
                max_chars=max_chars,
                data_classification=data_classification,
                budget=budget,
            )
            res = dict(comp)
            res.setdefault("compressor_model", getattr(self.compressor_provider, "model_id", self.cloud_model_id))
            res["compressor_mode"] = "cloud"
            return res

        if hasattr(self.local_provider, "compress_log"):
            comp = self.local_provider.compress_log(raw_log, max_chars=max_chars)
            text = comp.get("compressed_text", "")
            if "[Tier-1 M4 Compressed Failure Digest]" in text:
                text = text.replace(
                    "[Tier-1 M4 Compressed Failure Digest]",
                    "[Tier-1 M4 Local Compressed Failure Digest]",
                )
            elif not text.startswith("[Tier-1 M4 Local Compressed Failure Digest]"):
                text = f"[Tier-1 M4 Local Compressed Failure Digest]\n{text}"
            res = dict(comp)
            res["compressed_text"] = text
            res.setdefault("compressor_model", self.local_model_id)
            res["compressor_mode"] = "local"
            return res

        redacted = str(redact_secrets(raw_log))
        raw_tokens = max(1, len(redacted) // 4)

        lines = redacted.splitlines()
        extracted: list[str] = []
        file_line_pat = re.compile(r'(File "[^"]+", line \d+|[\w./\-]+\.py:\d+)', re.I)
        err_pat = re.compile(
            r"(AssertionError|Error:|Exception:|FAILED|FAIL:|ERROR:|Traceback|E\s+assert\b)",
            re.I,
        )

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if file_line_pat.search(stripped) or err_pat.search(stripped):
                extracted.append(stripped)
                # Include immediate context line if assertion details follow
                if idx + 1 < len(lines) and lines[idx + 1].strip():
                    nxt = lines[idx + 1].strip()
                    if len(nxt) < 240 and nxt not in extracted:
                        extracted.append(nxt)

        if not extracted:
            # Fallback to head + tail summary
            extracted = [l.strip() for l in (lines[:8] + lines[-8:]) if l.strip()]

        deduped: list[str] = []
        seen: set[str] = set()
        for item in extracted:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        summary = "[Tier-1 M4 Local Compressed Failure Digest]\n" + "\n".join(deduped)
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "\n...[truncated by M4 gatekeeper]"

        comp_tokens = max(1, len(summary) // 4)
        savings_pct = round(max(0.0, (1.0 - (comp_tokens / raw_tokens)) * 100.0), 2)
        return {
            "compressed_text": summary,
            "raw_tokens": raw_tokens,
            "compressed_tokens": comp_tokens,
            "token_savings_pct": savings_pct,
            "compressor_model": self.local_model_id,
            "compressor_mode": "local",
        }

    def route_request(
        self,
        *,
        task_class: str,
        risk_class: str = "R1",
        data_classification: str = "internal",
        raw_context: str = "",
        cloud_available: bool = True,
    ) -> RouteDecision:
        """FR-045..FR-047: Selects local M4 vs. cloud tier based on privacy, risk, and availability."""
        comp = (
            self.compress_verbose_log(raw_context, data_classification=data_classification)
            if len(raw_context) > 1200
            else {
                "compressed_text": str(redact_secrets(raw_context)),
                "raw_tokens": max(1, len(raw_context) // 4),
                "compressed_tokens": max(1, len(raw_context) // 4),
                "token_savings_pct": 0.0,
            }
        )

        # Sensitive/secret data never leaves local M4/Desktop boundary (FR-047 / Section 12.2)
        if data_classification in ("sensitive", "secret"):
            return RouteDecision(
                tier="tier1_local_m4",
                provider="local_mlx",
                model_id=self.local_model_id,
                reason=f"Data classification '{data_classification}' pinned to 100% local M4 execution",
                raw_tokens_estimated=comp["raw_tokens"],
                compressed_tokens_estimated=comp["compressed_tokens"],
                token_savings_pct=comp["token_savings_pct"],
                compressed_context=comp["compressed_text"],
            )

        # Routine R0/R1 triage or cloud outage routes to local M4
        if risk_class in ("R0", "R1") and task_class in ("triage", "fast_path", "repo_summary"):
            return RouteDecision(
                tier="tier1_local_m4",
                provider="local_mlx",
                model_id=self.local_model_id,
                reason=f"Fast-Path/triage ({risk_class}) served locally on M4 MLX at $0 marginal cost",
                raw_tokens_estimated=comp["raw_tokens"],
                compressed_tokens_estimated=comp["compressed_tokens"],
                token_savings_pct=comp["token_savings_pct"],
                compressed_context=comp["compressed_text"],
            )

        if not cloud_available:
            return RouteDecision(
                tier="tier1_local_m4",
                provider="local_mlx_fallback",
                model_id=self.local_model_id,
                reason="Approved local fallback on Tier-2 provider unavailability",
                raw_tokens_estimated=comp["raw_tokens"],
                compressed_tokens_estimated=comp["compressed_tokens"],
                token_savings_pct=comp["token_savings_pct"],
                compressed_context=comp["compressed_text"],
            )

        return RouteDecision(
            tier="tier2_cloud_reasoner",
            provider="cloud_frontier",
            model_id=self.cloud_model_id,
            reason="Escalated to Tier-2 heavy reasoner after Tier-1 M4 log pre-compression",
            raw_tokens_estimated=comp["raw_tokens"],
            compressed_tokens_estimated=comp["compressed_tokens"],
            token_savings_pct=comp["token_savings_pct"],
            compressed_context=comp["compressed_text"],
        )

    def execute_routed_request(
        self,
        *,
        prompt: str,
        task_class: str,
        risk_class: str = "R1",
        data_classification: str = "internal",
        budget: TaskBudget | None = None,
        cloud_available: bool = True,
        **kwargs: Any,
    ) -> tuple[RouteDecision, ModelResponse]:
        """Routes prompt to appropriate tier, executes model call, and records telemetry."""
        decision = self.route_request(
            task_class=task_class,
            risk_class=risk_class,
            data_classification=data_classification,
            raw_context=prompt,
            cloud_available=cloud_available,
        )

        effective_prompt = decision.compressed_context if decision.compressed_context else prompt

        if decision.tier == "tier1_local_m4":
            response = self.local_provider.generate(
                prompt=effective_prompt,
                data_classification=data_classification,
                budget=budget,
                **kwargs,
            )
            decision.cost_usd = 0.0
            decision.latency_ms = response.latency_ms
            decision.input_tokens = response.usage.input_tokens
            decision.output_tokens = response.usage.output_tokens

        elif decision.tier == "tier2_cloud_reasoner":
            if data_classification in ("sensitive", "secret"):
                raise PolicyBlockedError(
                    f"Cloud egress forbidden for data classification '{data_classification}'. "
                    "Data must remain within 100% local boundary."
                )
            response = self.cloud_provider.generate(
                prompt=effective_prompt,
                data_classification=data_classification,
                budget=budget,
                **kwargs,
            )
            decision.latency_ms = response.latency_ms
            decision.cost_usd = response.usage.cost_usd
            decision.input_tokens = response.usage.input_tokens
            decision.output_tokens = response.usage.output_tokens
        else:
            raise ValueError(f"Unknown routing tier: {decision.tier}")

        decision.compression_ratio = round(decision.token_savings_pct / 100.0, 4)
        decision.provider_metadata = dict(response.raw)

        return decision, response


class GovernedMemoryService:
    """Project, Personal, and Episodic Memory with strict scope isolation (FR-030..FR-035)."""

    def __init__(self, ledger: LedgerStore):
        self.ledger = ledger

    def write_memory(
        self,
        *,
        scope: str,
        subject: str,
        content: str,
        project_id: str | None = None,
        provenance: dict[str, Any] | None = None,
        confidence: float = 0.95,
        sensitivity: str = "internal",
        status: str = "active",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if scope not in ("project", "personal", "episodic"):
            raise ValueError(f"Invalid memory scope: {scope}")
        if scope == "project" and not project_id:
            raise ValueError("Project memory requires an explicit project_id")

        sanitized_content = str(redact_secrets(content))
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        prov = dict(provenance or {})
        prov.setdefault("recorded_at", now)

        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT INTO memories (
                    memory_id, scope, project_id, subject, content,
                    provenance_json, confidence, sensitivity, status,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mem_id,
                    scope,
                    project_id,
                    subject,
                    sanitized_content,
                    canonical_json(prov),
                    confidence,
                    sensitivity,
                    status,
                    expires_at,
                    now,
                    now,
                ),
            )
            self.ledger.conn.commit()
            row = self.ledger.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (mem_id,)
            ).fetchone()
            return self.ledger._row_to_dict(row)  # type: ignore[return-value]

    def retrieve_memories(
        self,
        *,
        project_id: str,
        query: str = "",
        include_personal: bool = False,
        include_episodic: bool = True,
    ) -> list[dict[str, Any]]:
        """Enforces cross-project isolation: project memories never leak across project_id."""
        with self.ledger._lock:
            rows = self.ledger.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
            ).fetchall()

        results: list[dict[str, Any]] = []
        q_lower = query.lower().strip()
        for r in rows:
            item = self.ledger._row_to_dict(r)
            if not item:
                continue
            scope = item["scope"]
            if scope == "project" and item.get("project_id") != project_id:
                continue
            if scope == "personal" and not include_personal:
                continue
            if scope == "episodic":
                if not include_episodic:
                    continue
                if item.get("project_id") not in (project_id, "general", None):
                    continue

            if q_lower and q_lower not in item["subject"].lower() and q_lower not in item["content"].lower():
                continue

            # Quote untrusted retrieved content as data with provenance (FR-034)
            item["quoted_evidence"] = (
                f"<memory_data id=\"{item['memory_id']}\" scope=\"{scope}\" "
                f"confidence=\"{item['confidence']}\">{item['content']}</memory_data>"
            )
            results.append(item)
        return results

    def forget_memory(self, memory_id: str) -> dict[str, Any]:
        with self.ledger._lock:
            now = utcnow_iso()
            self.ledger.conn.execute(
                "UPDATE memories SET status = 'expired', content = '[FORGOTTEN]', updated_at = ? WHERE memory_id = ?",
                (now, memory_id),
            )
            self.ledger.conn.commit()
            row = self.ledger.conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Memory not found: {memory_id}")
            return self.ledger._row_to_dict(row)  # type: ignore[return-value]
