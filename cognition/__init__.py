"""Cognition and memory subpackage: soul, memory tiers, reflections, SkillSpector, synthesis, prompts, and context compression."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'reflection', 'skillspector', 'soul', 'context_compressor', 'prompt_builder', 'skill_synthesizer', 'memory', 'title_synthesizer', 'repetition_sentry'}
_SYMBOL_MAP: dict[str, str] = {
    'RepetitionSentry': 'repetition_sentry',
    'ActionLoopDetector': 'repetition_sentry',
    'ActionRecord': 'repetition_sentry',
    'strip_thinking_tags': 'repetition_sentry',
    'is_repetition_dominated': 'repetition_sentry',
    'is_runaway_repetition': 'repetition_sentry',
    'generate_steering_prompt': 'repetition_sentry',
    'REPETITION_LOOP_INTERRUPTED': 'repetition_sentry',
    'TitleProvenance': 'title_synthesizer',
    'derive_instant_title': 'title_synthesizer',
    'validate_synthesized_title': 'title_synthesizer',
    'SessionTitleSynthesizer': 'title_synthesizer',
    'DEFAULT_SOUL_NAME': 'soul', 'DEFAULT_SOUL_MD': 'soul', 'DEFAULT_USER_MD': 'soul', 'MAX_SOUL_CHARS': 'soul', 'TRUNCATION_MARKER': 'soul', 'SoulProvenance': 'soul', 'normalize_soul': 'soul', 'get_hex_home': 'soul', 'SoulManager': 'soul', 'ENTRY_DELIMITER': 'memory', 'USER_CHAR_LIMIT': 'memory', 'MEMORY_CHAR_LIMIT': 'memory', 'GUARDRAIL_CHAR_LIMIT': 'memory', 'sanitize_context': 'memory', 'StreamingContextScrubber': 'memory', 'CuratedMemoryStore': 'memory', 'MemorySnapshot': 'memory', 'is_transient_error': 'memory', 'GuardrailsManager': 'memory', 'ASTNodeMemory': 'memory', 'resolve_symbol_span': 'memory', 'compute_code_fingerprint': 'memory', 'is_fingerprint_stale': 'memory', 'ASTNodeMemoryStore': 'memory', 'query_jit_memories_for_file': 'memory', 'format_jit_memories_block': 'memory', 'extract_wiki_tags': 'memory', 'MemoryItem': 'memory', 'AMEMController': 'memory', 'MemoryLifecycleManager': 'memory', 'HistorySnapshotManager': 'memory', 'FrictionLesson': 'reflection', 'ReflectionSynthesizer': 'reflection', 'VENDOR_SKILLSPECTOR': 'skillspector', 'RISK_ORDER': 'skillspector', 'CompiledRule': 'skillspector', 'STATIC_RULES_REGISTRY': 'skillspector', 'SkillScanReport': 'skillspector', 'parse_skill_frontmatter': 'skillspector', 'compute_mcp_manifest_sha256': 'skillspector', 'compute_bundle_sha256': 'skillspector', 'get_approved_skill_hash': 'skillspector', 'SkillSpectorScanner': 'skillspector', 'SynthesizedSkill': 'skill_synthesizer', 'SkillSynthesizer': 'skill_synthesizer', 'scan_for_threats': 'prompt_builder', 'scan_context_content': 'prompt_builder', 'build_tools_summary': 'prompt_builder', 'build_skills_index': 'prompt_builder', 'PromptBuilder': 'prompt_builder', 'HEX_COMPRESSION_MARKER_PREFIX': 'context_compressor', 'HEX_COMPRESSION_MARKER_TEMPLATE': 'context_compressor', 'CHARS_PER_TOKEN': 'context_compressor', 'DEFAULT_KEEP_TOOL_ROUNDS': 'context_compressor', 'DEFAULT_MIN_DEMOTE_CHARS': 'context_compressor', 'estimate_messages_tokens': 'context_compressor', 'CompactionConfig': 'context_compressor', 'CompactionResult': 'context_compressor', 'ContextCompactor': 'context_compressor'}

__all__ = [
    "soul",
    "memory",
    "reflection",
    "skillspector",
    "skill_synthesizer",
    "prompt_builder",
    "context_compressor",
    "title_synthesizer",
    "repetition_sentry",
    "RepetitionSentry",
    "ActionLoopDetector",
    "ActionRecord",
    "strip_thinking_tags",
    "is_repetition_dominated",
    "is_runaway_repetition",
    "generate_steering_prompt",
    "REPETITION_LOOP_INTERRUPTED",
    "TitleProvenance",
    "derive_instant_title",
    "validate_synthesized_title",
    "SessionTitleSynthesizer",
    "DEFAULT_SOUL_NAME",
    "DEFAULT_SOUL_MD",
    "DEFAULT_USER_MD",
    "MAX_SOUL_CHARS",
    "TRUNCATION_MARKER",
    "SoulProvenance",
    "normalize_soul",
    "get_hex_home",
    "SoulManager",
    "ENTRY_DELIMITER",
    "USER_CHAR_LIMIT",
    "MEMORY_CHAR_LIMIT",
    "GUARDRAIL_CHAR_LIMIT",
    "sanitize_context",
    "StreamingContextScrubber",
    "CuratedMemoryStore",
    "MemorySnapshot",
    "is_transient_error",
    "GuardrailsManager",
    "ASTNodeMemory",
    "resolve_symbol_span",
    "compute_code_fingerprint",
    "is_fingerprint_stale",
    "ASTNodeMemoryStore",
    "query_jit_memories_for_file",
    "format_jit_memories_block",
    "extract_wiki_tags",
    "MemoryItem",
    "AMEMController",
    "MemoryLifecycleManager",
    "HistorySnapshotManager",
    "FrictionLesson",
    "ReflectionSynthesizer",
    "VENDOR_SKILLSPECTOR",
    "RISK_ORDER",
    "CompiledRule",
    "STATIC_RULES_REGISTRY",
    "SkillScanReport",
    "parse_skill_frontmatter",
    "compute_mcp_manifest_sha256",
    "compute_bundle_sha256",
    "get_approved_skill_hash",
    "SkillSpectorScanner",
    "SynthesizedSkill",
    "SkillSynthesizer",
    "scan_for_threats",
    "scan_context_content",
    "build_tools_summary",
    "build_skills_index",
    "PromptBuilder",
    "HEX_COMPRESSION_MARKER_PREFIX",
    "HEX_COMPRESSION_MARKER_TEMPLATE",
    "CHARS_PER_TOKEN",
    "DEFAULT_KEEP_TOOL_ROUNDS",
    "DEFAULT_MIN_DEMOTE_CHARS",
    "estimate_messages_tokens",
    "CompactionConfig",
    "CompactionResult",
    "ContextCompactor",
]

def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _SYMBOL_MAP:
        target_mod = _SYMBOL_MAP[name]
        mod = importlib.import_module(f".{target_mod}", __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(__all__)
