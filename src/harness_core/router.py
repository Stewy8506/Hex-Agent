"""Two-Tier Local/Cloud Model Router & Governed Memory Service (PRD Sections 6.4, 6.6, 11, FR-030..FR-035, FR-045..FR-047).

- Tier-1 Local M4 Gatekeeper (MacBook Air M4 MLX profile):
  1. Distills verbose pytest/compiler logs (>20,000 tokens) into compact, provenance-linked
     failure summaries (>70% token reduction while preserving file, line number, and assertion error).
  2. Runs local R0/R1 triage and keeps sensitive/secret data 100% local.
- Tier-2 Cloud / Google Antigravity Reasoner:
  Supports Day-1 Google Antigravity OAuth quota routing (`AntigravityModelProvider` at $0 marginal API billing)
  as well as Anthropic, OpenAI, and Gemini API keys with ephemeral prompt prefix caching.
- Governed Memory Service:
  Enforces strict isolation across `project`, `personal`, and `episodic` scopes with provenance,
  confidence, sensitivity, and expiration controls.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_loader import parse_config_yaml
from .db import LedgerStore, canonical_json, utcnow_iso
from .gateway import redact_secrets
from .models import TaskBudget
from .providers.antigravity_provider import AntigravityModelProvider
from .providers.base import ModelProvider, ModelResponse, PolicyBlockedError, ProviderUnavailableError
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


def _detect_default_local_model() -> str:
    """Dynamically determine default local model from environment, config, or live local discovery."""
    env_local = os.getenv("HEX_LOCAL_MODEL_ID") or os.getenv("HARNESS_LOCAL_MODEL_ID")
    if env_local:
        return env_local

    # Non-Apple Silicon host (Linux, Intel, Windows, Cloud VMs) cannot run local MLX models
    if sys.platform != "darwin":
        return "local_deterministic_compressor"

    # Check local config file if present
    for cfg_candidate in (
        Path.cwd() / ".hex" / "config.yaml",
        Path.home() / ".hex" / "config.yaml",
    ):
        if cfg_candidate.is_file():
            try:
                cfg = parse_config_yaml(cfg_candidate)
                model_sec = cfg.get("model") or cfg.get("models") or {}
                m = model_sec.get("local_model_id")
                if m and isinstance(m, str) and m.strip():
                    return m.strip()
            except Exception:
                pass

    # Live discover installed models from Ollama, HuggingFace Hub, or LM Studio
    try:
        from .providers.cloud_provider import fetch_available_models

        disc = fetch_available_models("local")
        if disc:
            return disc[0][0]
    except Exception:
        pass

    return "local_deterministic_compressor"


class TwoTierModelRouter:
    """Routes between Tier-1 Local M4 MLX Gatekeeper/Compressor and Tier-2 Heavy Reasoner.

    Supports:
    - Day-1 Google Antigravity OAuth quota routing (`cloud_provider_type='antigravity'`).
    - Configurable log compression mode (`compressor_mode`):
      - `'local'` (default): 100% local M4 MLX / deterministic compression at $0 marginal cost.
      - `'cloud'`: Uses `compressor_provider` (or `cloud_provider`) for cloud LLM log compression,
        automatically falling back to `'local'` when `data_classification` is `'sensitive'` or `'secret'`.
    """

    def __init__(
        self,
        local_provider: ModelProvider | None = None,
        cloud_provider: ModelProvider | None = None,
        local_model_id: str | None = None,
        cloud_model_id: str | None = None,
        cloud_provider_type: str | None = None,
        compressor_mode: str | None = None,
        compressor_provider: ModelProvider | None = None,
    ):
        self.profile = os.getenv("HEX_MODEL_PROFILE", "antigravity_hybrid")
        resolved_local_model = (
            local_model_id
            or os.getenv("HEX_LOCAL_MODEL_ID")
            or os.getenv("HARNESS_LOCAL_MODEL_ID")
            or _detect_default_local_model()
        )
        self.local_provider = local_provider or LocalMLXModelProvider(model_id=resolved_local_model)

        resolved_provider_type = (
            cloud_provider_type
            or os.getenv("HEX_CLOUD_PROVIDER")
            or os.getenv("HARNESS_CLOUD_PROVIDER")
            or ""
        ).strip().lower()

        env_prov = (os.getenv("HEX_CLOUD_PROVIDER") or os.getenv("HARNESS_CLOUD_PROVIDER") or "").strip().lower()
        if cloud_model_id:
            resolved_cloud_model = cloud_model_id
        elif cloud_provider_type and env_prov and cloud_provider_type != env_prov:
            resolved_cloud_model = ""
        else:
            resolved_cloud_model = (
                os.getenv("HEX_CLOUD_MODEL_ID")
                or os.getenv("HARNESS_CLOUD_MODEL_ID")
                or ""
            )

        # Safeguard against cross-provider model contamination
        if resolved_provider_type == "anthropic" and (
            resolved_cloud_model.startswith(("gemini", "gpt", "o1", "o3", "chatgpt"))
        ):
            resolved_cloud_model = ""
        elif resolved_provider_type == "openai" and (
            resolved_cloud_model.startswith(("gemini", "claude"))
        ):
            resolved_cloud_model = ""
        elif resolved_provider_type == "gemini" and (
            resolved_cloud_model.startswith(("claude", "gpt", "o1", "o3", "chatgpt"))
        ):
            resolved_cloud_model = ""

        if not resolved_cloud_model:
            # Check config file if present
            for cfg_candidate in (
                Path.cwd() / ".hex" / "config.yaml",
                Path.home() / ".hex" / "config.yaml",
            ):
                if cfg_candidate.is_file():
                    try:
                        cfg = parse_config_yaml(cfg_candidate)
                        model_sec = cfg.get("model") or cfg.get("models") or {}
                        m = model_sec.get("cloud_model_id")
                        if m and isinstance(m, str) and m.strip():
                            resolved_cloud_model = m.strip()
                            break
                    except Exception:
                        pass

        # The configuration fallback above can reintroduce an ID that belongs to
        # another provider. Validate again before discovery/default selection.
        if resolved_provider_type == "anthropic" and (
            resolved_cloud_model.startswith(("gemini", "gpt", "o1", "o3", "chatgpt"))
        ):
            resolved_cloud_model = ""
        elif resolved_provider_type == "openai" and (
            resolved_cloud_model.startswith(("gemini", "claude"))
        ):
            resolved_cloud_model = ""
        elif resolved_provider_type == "gemini" and (
            resolved_cloud_model.startswith(("claude", "gpt", "o1", "o3", "chatgpt"))
        ):
            resolved_cloud_model = ""

        if not resolved_cloud_model:
            # Live discover available models from active provider
            probe_ptype = resolved_provider_type or "antigravity"
            try:
                from .providers.cloud_provider import fetch_available_models

                disc = fetch_available_models(probe_ptype)
                if disc:
                    resolved_cloud_model = disc[0][0]
            except Exception:
                pass

        if not resolved_cloud_model:
            default_provider_models = {
                "antigravity": "gemini-3-flash",
                "anthropic": "claude-3-5-sonnet",
                "openai": "gpt-4o",
                "gemini": "gemini-2.5-pro",
                "local": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
            }
            resolved_cloud_model = default_provider_models.get(resolved_provider_type, "gemini-3-flash")

        if cloud_provider is not None:
            self.cloud_provider = cloud_provider
        elif resolved_provider_type in ("antigravity", "google-antigravity", "google_antigravity", "agy") or (
            resolved_cloud_model.lower().startswith(("antigravity/", "google/antigravity-"))
        ):
            self.cloud_provider = AntigravityModelProvider(model_id=resolved_cloud_model)
        elif resolved_provider_type in ("anthropic", "openai", "gemini"):
            # Explicit provider configured by user; respect authoritative choice
            self.cloud_provider = CloudModelProvider(
                provider_type=resolved_provider_type,
                model_id=resolved_cloud_model,
            )
        else:
            # Auto-detect local Antigravity OAuth session if no provider is explicitly configured
            has_raw_api_key = bool(
                os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            ag_probe = AntigravityModelProvider(model_id=resolved_cloud_model)
            if not has_raw_api_key and ag_probe.is_available():
                self.cloud_provider = ag_probe
            else:
                self.cloud_provider = CloudModelProvider(
                    provider_type=resolved_provider_type or "gemini",
                    model_id=resolved_cloud_model,
                )

        self.compressor_provider = compressor_provider or self.cloud_provider
        self.compressor_mode = (
            compressor_mode
            or os.getenv("HEX_COMPRESSOR_MODE")
            or os.getenv("HARNESS_COMPRESSOR_MODE")
            or "local"
        ).strip().lower()
        self.local_model_id = getattr(self.local_provider, "model_id", resolved_local_model)
        self.cloud_model_id = getattr(self.cloud_provider, "model_id", resolved_cloud_model)

    def set_active_model(self, model_id: str) -> None:
        """Synchronously update the model ID across router and providers."""
        clean_id = model_id.strip()
        if not clean_id:
            return
        self.cloud_model_id = clean_id
        if hasattr(self.cloud_provider, "model_id"):
            self.cloud_provider.model_id = clean_id
        if hasattr(self.compressor_provider, "model_id") and self.compressor_mode == "cloud":
            self.compressor_provider.model_id = clean_id

    def switch_cloud_provider(self, provider_type: str, model_id: str | None = None) -> ModelProvider:
        """Switch the Tier-2 cloud reasoner backend dynamically (e.g., to 'antigravity')."""
        norm = provider_type.strip().lower()
        target_model = model_id or self.cloud_model_id
        if norm in ("antigravity", "google-antigravity", "google_antigravity", "agy"):
            self.cloud_provider = AntigravityModelProvider(model_id=target_model)
        else:
            self.cloud_provider = CloudModelProvider(provider_type=norm, model_id=target_model)
        self.cloud_model_id = getattr(self.cloud_provider, "model_id", target_model)
        return self.cloud_provider

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

        # Explicit local or offline profile pins all requests to local M4
        if self.profile in ("local", "offline"):
            return RouteDecision(
                tier="tier1_local_m4",
                provider="local_mlx",
                model_id=self.local_model_id,
                reason=f"Model profile '{self.profile}' pinned to local execution",
                raw_tokens_estimated=comp["raw_tokens"],
                compressed_tokens_estimated=comp["compressed_tokens"],
                token_savings_pct=comp["token_savings_pct"],
                compressed_context=comp["compressed_text"],
            )

        if not cloud_available or self.cloud_provider is None:
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

        cloud_prov_id = getattr(self.cloud_provider, "provider_id", "cloud_frontier")
        return RouteDecision(
            tier="tier2_cloud_reasoner",
            provider=cloud_prov_id,
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
            try:
                response = self.cloud_provider.generate(
                    prompt=effective_prompt,
                    data_classification=data_classification,
                    budget=budget,
                    **kwargs,
                )
            except ProviderUnavailableError as pue:
                # Fail over to Tier-1 Local M4 if in hybrid profile and local provider is initialized
                if self.profile in ("hybrid", "antigravity_hybrid") and self.local_provider is not None:
                    try:
                        response = self.local_provider.generate(
                            prompt=effective_prompt,
                            data_classification=data_classification,
                            budget=budget,
                            **kwargs,
                        )
                        decision.tier = "tier1_local_m4"
                        decision.provider = self.local_provider.provider_id
                        decision.model_id = self.local_provider.model_id
                        decision.reason = f"Cloud unavailable ({pue}); failed over to Tier 1 Local M4."
                    except Exception:
                        raise pue
                else:
                    raise pue
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
    """Project, Personal, and Episodic Memory unified with Graphify AST Node Provenance & Staleness Detection (FR-030..FR-035).

    Coherently bridges:
    1. SQLite/PostgreSQL `memories` table (strict `project`, `personal`, and `episodic` scope isolation)
    2. Graphify AST symbol & file SHA-256 code fingerprints (automatic staleness detection when code changes)
    3. Upstream `vendor/graphify/graphify/reflect.py` work-memory reflection (`useful`, `dead_end`, `corrected`
       outcomes producing `.hex/reflections/LESSONS.md` and `.hex/.graphify_learning.json`).
    """

    def __init__(self, ledger: LedgerStore, graphify: Any = None):
        self.ledger = ledger
        self.graphify = graphify

    def _compute_code_fingerprints(
        self,
        source_nodes: list[str] | None,
        workspace_dir: Path | str | None = None,
    ) -> dict[str, str]:
        """Compute current file SHA-256 hashes for any cited AST symbols or file paths."""
        if not source_nodes or self.graphify is None:
            return {}
        ws = Path(workspace_dir or getattr(self.graphify, "workspace_root", Path.cwd())).resolve()
        self.graphify.build_or_refresh(ws)
        fingerprints: dict[str, str] = {}
        for node in source_nodes:
            clean = str(node).strip()
            if not clean:
                continue
            rel_file = ""
            if clean in self.graphify.symbols_by_file:
                rel_file = clean
            elif clean in self.graphify.file_of_qualified:
                rel_file = self.graphify.file_of_qualified[clean]
            elif clean in self.graphify.symbols_by_name and self.graphify.symbols_by_name[clean]:
                rel_file = self.graphify.symbols_by_name[clean][0].rel_path

            if rel_file:
                fpath = ws / rel_file
                if fpath.is_file():
                    import hashlib

                    try:
                        fingerprints[clean] = hashlib.sha256(fpath.read_bytes()).hexdigest()
                    except OSError:
                        pass
        return fingerprints

    def _is_memory_stale(
        self,
        prov: dict[str, Any],
        workspace_dir: Path | str | None = None,
    ) -> bool:
        """Return True if any cited AST node's source file has changed its SHA-256 since the memory was recorded."""
        recorded_fps = prov.get("code_fingerprints")
        if not isinstance(recorded_fps, dict) or not recorded_fps or self.graphify is None:
            return False
        current_fps = self._compute_code_fingerprints(list(recorded_fps.keys()), workspace_dir=workspace_dir)
        for node, old_sha in recorded_fps.items():
            cur_sha = current_fps.get(node)
            if cur_sha is None or cur_sha != old_sha:
                return True
        return False

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
        source_nodes: list[str] | None = None,
        outcome: str = "useful",
        correction: str = "",
        workspace_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        if scope not in ("project", "personal", "episodic"):
            raise ValueError(f"Invalid memory scope: {scope}")
        if scope == "project" and not project_id:
            raise ValueError("Project memory requires an explicit project_id")

        sanitized_content = str(redact_secrets(content))
        sanitized_correction = str(redact_secrets(correction)) if correction else ""
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        prov = dict(provenance or {})
        prov.setdefault("recorded_at", now)
        nodes = list(dict.fromkeys(source_nodes or prov.get("source_nodes") or []))
        if nodes:
            prov["source_nodes"] = nodes
            fps = self._compute_code_fingerprints(nodes, workspace_dir=workspace_dir)
            if fps:
                prov["code_fingerprints"] = fps
        if outcome:
            prov["outcome"] = outcome
        if sanitized_correction:
            prov["correction"] = sanitized_correction

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
            saved = self.ledger._row_to_dict(row)  # type: ignore[return-value]

        # Mirror non-personal AST-linked outcomes into Graphify's work-memory reflection store
        if (
            self.graphify is not None
            and scope in ("project", "episodic")
            and sensitivity not in ("sensitive", "secret")
            and hasattr(self.graphify, "record_work_memory_outcome")
        ):
            try:
                self.graphify.record_work_memory_outcome(
                    workspace_dir=workspace_dir,
                    question=subject,
                    outcome=outcome if outcome in ("useful", "dead_end", "corrected") else "useful",
                    source_nodes=nodes,
                    correction=sanitized_correction or sanitized_content,
                    task_id=prov.get("task_id"),
                )
            except Exception:
                pass

        return saved  # type: ignore[return-value]

    def retrieve_memories(
        self,
        *,
        project_id: str,
        query: str = "",
        target_symbols: list[str] | None = None,
        include_personal: bool = False,
        include_episodic: bool = True,
        include_stale: bool = True,
        workspace_dir: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """Enforces cross-project isolation, checks AST code-hash staleness, and ranks by symbol overlap."""
        with self.ledger._lock:
            rows = self.ledger.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
            ).fetchall()

        results: list[dict[str, Any]] = []
        q_lower = query.lower().strip()
        q_tokens = [t for t in re.split(r"\W+", q_lower) if len(t) >= 3]
        target_sym_set = {s.strip() for s in (target_symbols or []) if s.strip()}

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

            prov = item.get("provenance") or {}
            if not isinstance(prov, dict):
                prov = {}
            mem_nodes = set(prov.get("source_nodes") or [])
            node_overlap = len(mem_nodes & target_sym_set)

            subj_l = item["subject"].lower()
            cont_l = item["content"].lower()
            nodes_l = " ".join(mem_nodes).lower()

            if q_lower:
                exact_sub = q_lower in subj_l or q_lower in cont_l or q_lower in nodes_l
                token_hits = sum(1 for tok in q_tokens if tok in subj_l or tok in cont_l or tok in nodes_l)
                if not exact_sub and node_overlap == 0 and (not q_tokens or token_hits == 0):
                    continue

            is_stale = self._is_memory_stale(prov, workspace_dir=workspace_dir)
            if is_stale and not include_stale:
                continue

            outcome = str(prov.get("outcome") or "useful")
            item["is_stale"] = is_stale
            item["outcome"] = outcome
            item["source_nodes"] = sorted(mem_nodes)

            # Quote untrusted retrieved content as data with provenance, outcome & staleness (FR-034)
            stale_attr = f' stale="{str(is_stale).lower()}"' if prov.get("code_fingerprints") else ""
            outcome_attr = f' outcome="{outcome}"'
            nodes_attr = f' nodes="{",".join(sorted(mem_nodes))}"' if mem_nodes else ""
            item["quoted_evidence"] = (
                f"<memory_data id=\"{item['memory_id']}\" scope=\"{scope}\" "
                f"confidence=\"{item['confidence']}\"{outcome_attr}{stale_attr}{nodes_attr}>"
                f"{item['content']}</memory_data>"
            )
            results.append(item)

        return results

    def retrieve_graphify_lessons_context(
        self,
        *,
        project_id: str,
        query: str = "",
        target_symbols: list[str] | None = None,
        workspace_dir: Path | str | None = None,
        max_memories: int = 5,
    ) -> str:
        """Build a unified memory + Graphify work-memory reflection digest for planning and repair prompts."""
        memories = self.retrieve_memories(
            project_id=project_id,
            query=query,
            target_symbols=target_symbols,
            include_personal=False,
            include_episodic=True,
            workspace_dir=workspace_dir,
        )[:max_memories]

        sections: list[str] = []
        if memories:
            mem_lines = [m["quoted_evidence"] for m in memories]
            sections.append("[Governed Episodic & Project Memory]\n" + "\n".join(mem_lines))

        if self.graphify is not None and hasattr(self.graphify, "reflect_work_memory"):
            try:
                ref = self.graphify.reflect_work_memory(workspace_dir=workspace_dir)
                lessons_excerpt = (ref.get("lessons_markdown") or "").strip()
                if lessons_excerpt and "_No marked outcomes yet._" not in lessons_excerpt:
                    sections.append(f"[Graphify Work-Memory Reflections]\n{lessons_excerpt[:1600]}")
            except Exception:
                pass

        return "\n\n".join(sections)

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
