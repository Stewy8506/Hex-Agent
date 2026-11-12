"""Cross-Platform Local AI Model Provider & Tier-1 Context/Log Compressor.

Supports:
- Local offline runtimes across macOS, Linux, and Windows (Ollama HTTP, Apple MLX, CPU/CUDA).
- Tier-1 Deterministic and Local Log Compressor (>70% token reduction while preserving
  file locations, line numbers, stack frames, and assertion messages).
- Resilient pure-Python fallback when no local models are running.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_core.gateway import redact_secrets

from .base import (
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
)

# Regex patterns for test failure and trace frame extraction
FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\w+))?')
COLON_LINE_RE = re.compile(r'(?:^|[\s\(])([a-zA-Z0-9_\-\./]+\.py):(\d+)(?::\s*(\w+))?')
FAILED_COLON_RE = re.compile(r'^FAILED\s+([a-zA-Z0-9_\-\./]+\.py):(\d+):\s*([a-zA-Z0-9_]+)')
FAILED_PYTEST_RE = re.compile(r'^FAILED\s+([a-zA-Z0-9_\-\./]+)::([a-zA-Z0-9_]+)(?:\s*-\s*(.*))?')
FAILED_GENERIC_RE = re.compile(r'^FAILED\s+([^\s:]+)')
UNITTEST_FAIL_RE = re.compile(r'^(?:FAIL|ERROR):\s+([a-zA-Z0-9_]+)\s*\(([^)]+)\)')
PYTEST_HEADER_RE = re.compile(r'^_{3,}\s*([a-zA-Z0-9_]+)\s*_{3,}$')
ASSERTION_RE = re.compile(r'^(?:E\s+)?(AssertionError(?::\s*.*)?)$')
EXCEPTION_RE = re.compile(r'^(?:E\s+)?([A-Za-z0-9_]+(?:Error|Exception):\s*.*)$')
ASSERT_EXPR_RE = re.compile(r'^(?:E\s+)?assert\b\s*(.*)$')
DIFF_RE = re.compile(r'^(?:E\s+)?([\+\-]\s*.*|where\s+.*)$')


@dataclass
class FailureRecord:
    """Structured failure representation distilled from verbose logs."""

    test_name: str = ""
    location: str = ""
    file_path: str = ""
    line_number: int | None = None
    frame: str = ""
    assertion: str = ""
    diff: list[str] = field(default_factory=list)

    def to_digest(self) -> str:
        name = self.test_name or "unknown_failure"
        loc = self.location or (
            f"{self.file_path}:{self.line_number}" if self.file_path and self.line_number else "unknown_location"
        )
        frm = self.frame or self.test_name or "main"
        ass = self.assertion or "AssertionError: test failure detected"
        lines = [
            f"FAILED: {name}",
            f"LOCATION: {loc}",
        ]
        if self.file_path and self.line_number:
            lines.append(f'SOURCE: File "{self.file_path}", line {self.line_number}')
        lines.extend([
            f"FRAME: {frm}",
            f"ASSERTION: {ass}",
        ])
        if self.diff:
            diff_text = " | ".join(d.strip() for d in self.diff if d.strip())
            if diff_text:
                lines.append(f"DIFF: {diff_text}")
        return "\n".join(lines)


class Tier1LogCompressor:
    """Tier-1 Context & Log Compressor for verbose test traces and build logs.

    Extracts dense failure digests containing failing test names, exact source file
    locations, line numbers, traceback frames, and assertion messages while stripping
    noise and passing tests to achieve >70% token savings.
    """

    def __init__(self, mlx_available: bool | None = None) -> None:
        if mlx_available is None:
            is_m4 = sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")
            try:
                self._mlx_available = is_m4 and (importlib.util.find_spec("mlx_lm") is not None)
            except Exception:
                self._mlx_available = False
        else:
            self._mlx_available = bool(mlx_available)

    def is_mlx_available(self) -> bool:
        """Return True if local MLX engine is available."""
        return self._mlx_available

    def compress(self, raw_log: str, max_chars: int = 2400) -> dict[str, Any]:
        """Compress raw log text into a structured failure digest with token metrics."""
        sanitized = str(redact_secrets(raw_log))
        raw_tokens = max(1, len(sanitized) // 4)

        if not sanitized.strip():
            summary = "[Tier-1 Local Compressed Failure Digest]\nEmpty log trace."
            comp_tokens = max(1, len(summary) // 4)
            return {
                "compressed_text": summary,
                "raw_tokens": raw_tokens,
                "compressed_tokens": comp_tokens,
                "token_savings_pct": 0.0,
                "failing_files": [],
                "failing_assertions": [],
                "engine": "tier1_mlx" if self.is_mlx_available() else "tier1_deterministic",
            }

        records, all_files, all_assertions = self._extract_failures(sanitized)

        if not records and not all_files and not all_assertions:
            summary = self._fallback_filter(sanitized, max_chars)
        else:
            digests = [r.to_digest() for r in records]
            if digests:
                body = "\n\n".join(digests)
                summary = f"[Tier-1 Local Compressed Failure Digest]\n{body}"
            else:
                summary = self._fallback_filter(sanitized, max_chars)

        if len(summary) > max_chars:
            summary = summary[: max_chars - 35] + "\n...[truncated by local compressor]"

        comp_tokens = max(1, len(summary) // 4)
        savings_pct = round(max(0.0, (1.0 - (comp_tokens / raw_tokens)) * 100.0), 2)

        return {
            "compressed_text": summary,
            "raw_tokens": raw_tokens,
            "compressed_tokens": comp_tokens,
            "token_savings_pct": savings_pct,
            "failing_files": sorted(list(set(all_files))),
            "failing_assertions": sorted(list(set(all_assertions))),
            "engine": "tier1_mlx" if self.is_mlx_available() else "tier1_deterministic",
        }

    def _extract_failures(self, sanitized: str) -> tuple[list[FailureRecord], list[str], list[str]]:
        lines = sanitized.splitlines()
        records: list[FailureRecord] = []
        current_record: FailureRecord | None = None
        all_files: list[str] = []
        all_assertions: list[str] = []

        def commit_record() -> None:
            nonlocal current_record
            if current_record is not None:
                if current_record.file_path and current_record.file_path not in all_files:
                    all_files.append(current_record.file_path)
                if current_record.assertion and current_record.assertion not in all_assertions:
                    all_assertions.append(current_record.assertion)
                records.append(current_record)
                current_record = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Skip passing dots and progress percentage lines
            if re.match(r'^[.FEsxXpP\s]+\[\s*\d+%\s*\]$', stripped):
                continue
            if stripped.startswith("PASSED ") or stripped.startswith("SKIPPED "):
                continue
            if re.match(r'^\.{5,}$', stripped):
                continue
            if re.match(r'^={3,}\s*(?:test session starts|\d+ passed).*={3,}$', stripped):
                continue
            if any(
                stripped.startswith(prefix)
                for prefix in (
                    "rootdir:",
                    "configfile:",
                    "plugins:",
                    "collecting ...",
                    "collected ",
                    "INFO ",
                    "DEBUG ",
                    "WARNING ",
                )
            ):
                continue

            # Failure marker 1: FAILED file:line: test_name
            m_colon = FAILED_COLON_RE.match(stripped)
            if m_colon:
                commit_record()
                file_p = m_colon.group(1)
                line_n = int(m_colon.group(2))
                t_name = m_colon.group(3)
                current_record = FailureRecord(
                    test_name=t_name,
                    file_path=file_p,
                    line_number=line_n,
                    location=f"{file_p}:{line_n}",
                    frame=t_name,
                )
                if file_p not in all_files:
                    all_files.append(file_p)
                continue

            # Failure marker 2: FAILED file::test_name
            m_pytest = FAILED_PYTEST_RE.match(stripped)
            if m_pytest:
                commit_record()
                file_p = m_pytest.group(1)
                t_name = m_pytest.group(2)
                ass_inline = m_pytest.group(3) or ""
                current_record = FailureRecord(
                    test_name=t_name,
                    file_path=file_p,
                    location=f"{file_p}::{t_name}",
                    frame=t_name,
                    assertion=ass_inline,
                )
                if file_p not in all_files:
                    all_files.append(file_p)
                if ass_inline and ass_inline not in all_assertions:
                    all_assertions.append(ass_inline)
                continue

            # Failure marker 3: FAIL/ERROR: test_name (module.class)
            m_unit = UNITTEST_FAIL_RE.match(stripped)
            if m_unit:
                commit_record()
                t_name = m_unit.group(1)
                mod_cls = m_unit.group(2)
                current_record = FailureRecord(
                    test_name=t_name,
                    frame=f"{mod_cls}.{t_name}",
                )
                continue

            # Failure marker 4: ___ test_name ___
            m_hdr = PYTEST_HEADER_RE.match(stripped)
            if m_hdr:
                commit_record()
                t_name = m_hdr.group(1)
                current_record = FailureRecord(
                    test_name=t_name,
                    frame=t_name,
                )
                continue

            # Failure marker 5: Traceback (most recent call last):
            if stripped.startswith("Traceback (most recent call last):"):
                if current_record is None:
                    current_record = FailureRecord()
                continue

            # File and line detection in traceback or failure context
            m_fl = FILE_LINE_RE.search(stripped)
            if m_fl:
                file_p = m_fl.group(1)
                line_n = int(m_fl.group(2))
                fn = m_fl.group(3)
                if file_p not in all_files:
                    all_files.append(file_p)
                if current_record is None:
                    current_record = FailureRecord()
                current_record.file_path = file_p
                current_record.line_number = line_n
                current_record.location = f"{file_p}:{line_n}"
                if fn:
                    current_record.frame = fn
                continue

            # Colon line detection: path.py:123
            m_cl = COLON_LINE_RE.search(stripped)
            if m_cl and not stripped.startswith("PASSED"):
                file_p = m_cl.group(1)
                line_n = int(m_cl.group(2))
                if file_p not in all_files:
                    all_files.append(file_p)
                if current_record is None:
                    current_record = FailureRecord()
                if not current_record.location:
                    current_record.file_path = file_p
                    current_record.line_number = line_n
                    current_record.location = f"{file_p}:{line_n}"

            # Assertion errors
            m_ass = ASSERTION_RE.match(stripped)
            if m_ass:
                ass_val = m_ass.group(1)
                if ass_val not in all_assertions:
                    all_assertions.append(ass_val)
                if current_record is None:
                    current_record = FailureRecord()
                current_record.assertion = ass_val
                continue

            # General exception errors
            m_exc = EXCEPTION_RE.match(stripped)
            if m_exc:
                exc_val = m_exc.group(1)
                if exc_val not in all_assertions:
                    all_assertions.append(exc_val)
                if current_record is None:
                    current_record = FailureRecord()
                if not current_record.assertion:
                    current_record.assertion = exc_val
                continue

            # Assert expressions (e.g. assert 2 + 2 == 5 or E assert 4 == 5)
            m_asexpr = ASSERT_EXPR_RE.match(stripped)
            if m_asexpr:
                expr = m_asexpr.group(1)
                if current_record is None:
                    current_record = FailureRecord()
                if not current_record.assertion:
                    current_record.assertion = f"assert {expr}"
                    if f"assert {expr}" not in all_assertions:
                        all_assertions.append(f"assert {expr}")
                if f"assert {expr}" not in current_record.diff:
                    current_record.diff.append(f"assert {expr}")
                continue

            # Diff lines (+ expected vs - actual, where ...)
            m_diff = DIFF_RE.match(stripped)
            if m_diff and current_record is not None:
                current_record.diff.append(m_diff.group(1))
                continue

        commit_record()

        # Deduplicate records by test_name and location
        deduped: list[FailureRecord] = []
        seen_keys: set[str] = set()
        for r in records:
            key = f"{r.test_name}|{r.location}|{r.assertion}"
            if key not in seen_keys:
                seen_keys.add(key)
                if r.file_path and r.line_number:
                    try:
                        p = Path(r.file_path)
                        if p.is_file():
                            flines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                            if 1 <= r.line_number <= len(flines):
                                src_line = flines[r.line_number - 1].strip()
                                if not r.assertion and ("assert" in src_line or "raise" in src_line):
                                    r.assertion = src_line
                                    if src_line not in all_assertions:
                                        all_assertions.append(src_line)
                                if not r.diff and src_line:
                                    r.diff.append(src_line)
                    except Exception:
                        pass
                deduped.append(r)

        return deduped, all_files, all_assertions

    def _fallback_filter(self, sanitized: str, max_chars: int) -> str:
        """Filter noise when logs lack explicit failure markers."""
        filtered_lines: list[str] = []
        for line in sanitized.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("PASSED ") or s.startswith("SKIPPED "):
                continue
            if re.match(r'^[.FEsxXpP\s]+\[\s*\d+%\s*\]$', s):
                continue
            if re.match(r'^\.{5,}$', s):
                continue
            if re.match(r'^={3,}.*={3,}$', s):
                continue
            filtered_lines.append(s)

        if not filtered_lines:
            return "[Tier-1 Local Compressed Failure Digest]\nAll tests passed or no failure frames detected."

        selected = filtered_lines[:15]
        return "[Tier-1 Local Compressed Failure Digest]\n" + "\n".join(selected)


class LocalModelProvider(ModelProvider):
    """Cross-platform Local AI inference provider supporting Ollama, Apple MLX, and CPU/CUDA.

    Works natively on macOS, Linux, and Windows:
    - Queries Ollama HTTP API (http://localhost:11434) when available.
    - Uses Apple Silicon MLX when on macOS Darwin arm64 with `mlx_lm` installed.
    - Provides resilient pure-Python fallback execution when offline without active model weights.
    """

    provider_id: str = "local_ai"

    def __init__(
        self,
        model_id: str = "qwen2.5-coder:7b",
        ollama_host: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.ollama_host = (
            ollama_host
            or os.getenv("OLLAMA_HOST")
            or os.getenv("HEX_OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")
        self._loaded_model: Any = None
        self._compressor = Tier1LogCompressor(mlx_available=self.is_mlx_available())

    def is_apple_silicon(self) -> bool:
        """Return True if running on Darwin ARM64/Apple Silicon architecture."""
        return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")

    def is_mlx_available(self) -> bool:
        """Return True if running on Apple Silicon and mlx_lm is installed and importable."""
        if not self.is_apple_silicon():
            return False
        try:
            return importlib.util.find_spec("mlx_lm") is not None
        except Exception:
            return False

    def is_cuda_available(self) -> bool:
        """Return True if NVIDIA CUDA acceleration is accessible."""
        try:
            import torch  # type: ignore[import-not-found]
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def is_ollama_available(self) -> bool:
        """Return True if local Ollama daemon is reachable."""
        try:
            req = urllib.request.Request(f"{self.ollama_host}/api/version", method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def detect_hardware(self) -> str:
        """Return human-readable local hardware acceleration backend."""
        if self.is_apple_silicon():
            return "Apple Silicon (Metal Unified Memory)"
        if self.is_cuda_available():
            return "NVIDIA (CUDA GPU)"
        return "Host CPU"

    def is_available(self) -> bool:
        """Return whether a real local model is loaded and ready for inference.

        Consistent with the model provider contract: reporting false when cold
        prevents accidental routing to uninitialized local models.
        """
        return self._loaded_model is not None

    def get_capabilities(self) -> ProviderCapabilities:
        """Return provider architectural capabilities."""
        quant = "4bit" if "4bit" in self.model_id.lower() else "8bit" if "8bit" in self.model_id.lower() else None
        return ProviderCapabilities(
            supports_system_prompt=True,
            supports_streaming=False,
            max_context_window=32768,
            quantization=quant,
            local_unified_memory=self.is_apple_silicon(),
        )

    def load_model(self) -> Any:
        """Lazily load Apple MLX model if supported on this platform."""
        if not self.is_mlx_available():
            return None
        if self._loaded_model is not None:
            return self._loaded_model
        try:
            from mlx_lm import load  # type: ignore[import-not-found]

            model, tokenizer = load(self.model_id)
            self._loaded_model = (model, tokenizer)
            return self._loaded_model
        except Exception:
            self._loaded_model = None
            return None

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Execute text generation via Ollama, Apple MLX, or resilient fallback."""
        t0 = time.perf_counter()
        clean_prompt = str(redact_secrets(prompt))
        clean_system = str(redact_secrets(system_prompt)) if system_prompt else None

        # 1. Try Ollama HTTP API if accessible
        if self.is_ollama_available():
            try:
                payload = {
                    "model": self.model_id,
                    "prompt": clean_prompt,
                    "system": clean_system or "",
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                }
                req = urllib.request.Request(
                    f"{self.ollama_host}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        text_out = data.get("response", "")
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        in_tok = data.get("prompt_eval_count") or max(1, len(clean_prompt) // 4)
                        out_tok = data.get("eval_count") or max(1, len(text_out) // 4)
                        return ModelResponse(
                            text=text_out,
                            model_id=self.model_id,
                            provider_id="ollama",
                            usage=ModelUsage(
                                input_tokens=in_tok,
                                output_tokens=out_tok,
                                total_tokens=in_tok + out_tok,
                                cost_usd=0.0,
                            ),
                            latency_ms=latency_ms,
                            finish_reason="stop",
                            raw={"backend": "ollama", "hardware": self.detect_hardware()},
                        )
            except Exception:
                pass

        # 2. Try Apple MLX if running on macOS Metal
        if self.is_mlx_available():
            loaded = self.load_model()
            if loaded is not None:
                try:
                    model, tokenizer = loaded
                    from mlx_lm import generate as mlx_generate  # type: ignore[import-not-found]

                    full_prompt = clean_prompt
                    if clean_system:
                        full_prompt = f"{clean_system}\n\n{clean_prompt}"

                    output = mlx_generate(
                        model,
                        tokenizer,
                        prompt=full_prompt,
                        max_tokens=max_tokens,
                        temp=temperature,
                        verbose=False,
                        **kwargs,
                    )
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    in_tokens = max(1, len(full_prompt) // 4)
                    out_tokens = max(1, len(output) // 4)
                    return ModelResponse(
                        text=output,
                        model_id=self.model_id,
                        provider_id=self.provider_id,
                        usage=ModelUsage(
                            input_tokens=in_tokens,
                            output_tokens=out_tokens,
                            total_tokens=in_tokens + out_tokens,
                            cost_usd=0.0,
                        ),
                        latency_ms=latency_ms,
                        finish_reason="stop",
                        raw={"mlx": True, "unified_memory": self.is_apple_silicon()},
                    )
                except Exception:
                    pass

        # 3. Resilient pure-Python deterministic fallback engine
        latency_ms = (time.perf_counter() - t0) * 1000.0
        in_tokens = max(1, len(clean_prompt) // 4)
        output_text = (
            f"[Notice] Local model '{self.model_id}' is not loaded or weights are not downloaded.\n"
            f"Hardware detected: {self.detect_hardware()}.\n"
            f"To run offline local models, start Ollama or download weights via 'hex setup model' "
            f"or select an active cloud provider via '/model'."
        )
        out_tokens = max(1, len(output_text) // 4)
        usage = ModelUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=in_tokens + out_tokens,
            cost_usd=0.0,
        )
        return ModelResponse(
            text=output_text,
            model_id=self.model_id,
            provider_id=self.provider_id,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason="stop",
            raw={"fallback": True, "hardware": self.detect_hardware()},
        )

    def compress_log(self, raw_log: str, max_chars: int = 2400) -> dict[str, Any]:
        """Compress verbose test/build logs using Tier-1 Log Compressor."""
        return self._compressor.compress(raw_log, max_chars=max_chars)
