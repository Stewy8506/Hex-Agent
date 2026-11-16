"""Asynchronous Background LLM Session Title Synthesis Engine.

Derives concise, informative 3-5 word session titles for interactive workbench
sessions. Employs a 2-stage lifecycle:
1. Stage 1 (Instant): Deterministic heuristic title derived instantly from
   the opening prompt line (<= 48 chars).
2. Stage 2 (LLM Background): Asynchronous non-blocking worker executing
   structured JSON title generation through local Tier-1 or fast cloud model,
   safeguarded by anti-chatter and answer-rejection heuristics.

Maintains strict provenance hierarchy: USER > LLM > DERIVED.
"""

from __future__ import annotations

from enum import Enum
import json
import logging
import re
import threading
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from harness_core.storage.db import LedgerStore

logger = logging.getLogger(__name__)


class TitleProvenance(str, Enum):
    """Hierarchy of title authority: USER > LLM > DERIVED."""
    DERIVED = "derived"
    LLM = "llm"
    USER = "user"

    @property
    def rank(self) -> int:
        if self == TitleProvenance.USER:
            return 3
        if self == TitleProvenance.LLM:
            return 2
        return 1

    def can_overwrite(self, other: TitleProvenance | str) -> bool:
        """Return True if this provenance may overwrite another."""
        if isinstance(other, TitleProvenance):
            other_rank = other.rank
        else:
            val = str(other).lower()
            if val == "user":
                other_rank = 3
            elif val == "llm":
                other_rank = 2
            else:
                other_rank = 1
        return self.rank >= other_rank


def derive_instant_title(user_text: str, max_chars: int = 48) -> str:
    """Extract Stage 1 instant title from opening prompt text.
    
    Extracts the first non-empty line of the user's prompt, removes markdown
    headers, bullet markers, and surrounding quotes, trims punctuation,
    and cleanly truncates up to max_chars.
    """
    if not user_text or not user_text.strip():
        return "New Session"

    # Find first non-empty line
    lines = [line.strip() for line in user_text.splitlines() if line.strip()]
    if not lines:
        return "New Session"

    raw = lines[0]

    # Strip markdown headers (#, ##), bullets (*, -, >), numbers (1., 2.)
    cleaned = re.sub(r"^[\s#*>\-]+", "", raw)
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
    cleaned = cleaned.strip()

    # Strip enclosing quotes or backticks
    cleaned = cleaned.strip("\"'`“”‘’")

    # Trim trailing sentence punctuation
    cleaned = re.sub(r"[\s.,;:!?]+$", "", cleaned)

    if not cleaned:
        return "New Session"

    # Clean truncation at word boundary if exceeding max_chars
    if len(cleaned) > max_chars:
        limit = max(1, max_chars - 3)
        truncated = cleaned[:limit].rstrip()
        # If space exists, truncate at last complete word
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        cleaned = truncated.rstrip(".,;:!?") + "..."

    return cleaned


# Forbidden verbatim echoes from the prompt instruction
FORBIDDEN_ECHOES = {
    "generate a 3 to 5 word title",
    "3 to 5 word title",
    "summarizing the user's objective",
    "output json",
    "do not answer the query",
    "return title only",
    "user objective",
    "untitled",
    "title",
    "new session",
}

# Heuristics for assistant answer chat rather than concise title
ANSWER_SHAPED_PREFIXES = (
    "yes",
    "certainly",
    "here is",
    "here are",
    "sure",
    "i can",
    "of course",
    "as an ai",
    "to accomplish this",
    "in this task",
    "i will",
    "let me",
    "let's",
    "first,",
    "okay,",
)


def validate_synthesized_title(candidate: str, user_prompt: str = "") -> str | None:
    """Validate a candidate LLM title against anti-chatter, length, and format guards."""
    if not candidate or not candidate.strip():
        return None

    raw = candidate.strip()

    # Strip markdown code blocks if present
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()

    # Attempt JSON extraction {"title": "..."}
    if "{" in raw and "}" in raw:
        try:
            # Find json substring
            start = raw.index("{")
            end = raw.rindex("}") + 1
            payload = json.loads(raw[start:end])
            if isinstance(payload, dict) and "title" in payload:
                raw = str(payload["title"]).strip()
        except Exception:
            pass

    # Strip surrounding quotes, asterisks, backticks
    raw = raw.strip("\"'`“”‘’*# ")
    raw = re.sub(r"^title\s*:\s*", "", raw, flags=re.IGNORECASE).strip()
    raw = raw.strip("\"'`“”‘’*# ")

    if not raw:
        return None

    lower = raw.lower()

    # 1. Echo rejection
    for echo in FORBIDDEN_ECHOES:
        if lower == echo or lower.startswith(echo):
            return None

    # 2. Answer-shaped rejection
    for prefix in ANSWER_SHAPED_PREFIXES:
        if lower.startswith(prefix):
            return None

    # Reject multi-sentence or conversational paragraphs
    if "\n" in raw:
        # If multiple lines, pick first line only if short, else reject
        raw = raw.splitlines()[0].strip()
        if not raw:
            return None

    # 3. Word count ceiling: <= 12 words
    words = raw.split()
    if len(words) > 12 or len(words) == 0:
        return None

    # 4. CJK character limit: <= 20 chars
    cjk_chars = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", raw)
    if len(cjk_chars) > 20:
        return None

    # Strip trailing punctuation
    raw = re.sub(r"[\s.,;:!?]+$", "", raw).strip()

    # 5. Length ceiling
    if len(raw) < 2 or len(raw) > 70:
        return None

    return raw


class SessionTitleSynthesizer:
    """Asynchronous worker that synthesizes 3-5 word session titles via LLM."""

    def __init__(
        self,
        model_caller: Callable[[str], str] | Any | None = None,
        ledger: LedgerStore | None = None,
        router: Any | None = None,
    ) -> None:
        self.model_caller = model_caller
        self.ledger = ledger
        self.router = router
        self._active_threads: list[threading.Thread] = []

    def ensure_instant_title(self, task_id: str, prompt: str) -> str:
        """Derive instant title and write to ledger under DERIVED provenance."""
        title = derive_instant_title(prompt)
        if self.ledger is not None:
            try:
                self.ledger.update_task_goal(task_id, title, provenance="derived")
            except Exception as exc:
                logger.debug("Failed writing instant title: %s", exc)
        return title

    def _call_model(self, prompt_text: str) -> str:
        """Invoke configured model provider or model caller function."""
        # 1. Direct callable
        if callable(self.model_caller):
            try:
                import inspect
                sig = inspect.signature(self.model_caller)
                params = list(sig.parameters.values())
                first_param_name = params[0].name.lower() if params else ""
                if "message" in first_param_name or "msg" in first_param_name:
                    return str(self.model_caller([{"role": "user", "content": prompt_text}]))
            except Exception:
                pass

            try:
                return str(self.model_caller(prompt_text))
            except TypeError:
                try:
                    return str(self.model_caller([{"role": "user", "content": prompt_text}]))
                except Exception as e:
                    logger.debug("model_caller exception: %s", e)
            except Exception as e:
                logger.debug("model_caller exception: %s", e)

        # 2. Router model caller
        if self.router is not None:
            if hasattr(self.router, "route_and_generate"):
                try:
                    res = self.router.route_and_generate(prompt=prompt_text, task_type="title")
                    if hasattr(res, "text"):
                        return res.text
                    if isinstance(res, str):
                        return res
                except Exception as e:
                    logger.debug("router route_and_generate exception: %s", e)

            if hasattr(self.router, "cloud_provider") and self.router.cloud_provider:
                try:
                    res = self.router.cloud_provider.generate(prompt=prompt_text)
                    if hasattr(res, "text"):
                        return res.text
                    if isinstance(res, str):
                        return res
                except Exception as e:
                    logger.debug("router cloud_provider generate exception: %s", e)

        # 3. Model caller method on model_caller object (e.g. ModelProvider)
        if hasattr(self.model_caller, "generate"):
            try:
                res = self.model_caller.generate(prompt=prompt_text)
                if hasattr(res, "text"):
                    return res.text
                if isinstance(res, str):
                    return res
            except Exception as e:
                logger.debug("model_caller.generate exception: %s", e)

        return ""

    def synthesize_title_sync(self, prompt: str) -> str | None:
        """Synchronously request, parse, and validate an LLM title."""
        system_instruction = (
            'Generate a 3 to 5 word title summarizing the user\'s objective. '
            'Output JSON: {"title": "..."}. Do not answer the query. Return title only.\n\n'
            f'User objective:\n{prompt[:500]}'
        )

        raw_response = self._call_model(system_instruction)
        if not raw_response:
            return None

        return validate_synthesized_title(raw_response, user_prompt=prompt)

    def synthesize_async(
        self,
        task_id: str,
        prompt: str,
        on_title_ready: Callable[[str, str], None] | None = None,
        wait: bool = False,
    ) -> threading.Thread:
        """Spawn asynchronous background worker to synthesize title and update ledger."""
        def _worker() -> None:
            try:
                title = self.synthesize_title_sync(prompt)
                if title:
                    if self.ledger is not None:
                        self.ledger.update_task_goal(task_id, title, provenance="llm")
                    if on_title_ready is not None:
                        try:
                            on_title_ready(task_id, title)
                        except Exception as cb_exc:
                            logger.debug("on_title_ready callback exception: %s", cb_exc)
            except Exception as exc:
                logger.debug("Async title synthesis error for task %s: %s", task_id, exc)

        t = threading.Thread(target=_worker, name=f"title-synth-{task_id[:8]}", daemon=True)
        t.start()
        self._active_threads.append(t)

        if wait:
            t.join(timeout=10.0)

        return t
