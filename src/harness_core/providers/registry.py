"""Single source of truth for AI model providers in HEX Workbench.

Parity with Hermes Agent provider specifications, providing unified metadata,
transports (OpenAI Chat, Anthropic Messages, Gemini REST, Antigravity OAuth, Local),
default base URLs, environment variable priority chains, signup URLs, and curated models.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class ProviderSpec:
    """Metadata specification for an AI model provider backend."""

    id: str
    name: str
    category: str  # "featured", "fast_cloud", "aggregator", "enterprise", "local"
    transport: str  # "openai_chat", "anthropic_messages", "gemini_rest", "antigravity", "local"
    default_base_url: str
    env_vars: tuple[str, ...]
    base_url_env_var: str = ""
    signup_url: str = ""
    description: str = ""
    default_model: str = ""
    curated_models: tuple[tuple[str, str], ...] = ()
    requires_api_key: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """User-friendly display name."""
        return self.name

    @property
    def is_local(self) -> bool:
        """Whether this provider runs locally or offline without cloud egress."""
        return self.category == "local" or self.transport == "local"

    def resolve_api_key(self, custom_creds: dict[str, str] | None = None) -> str | None:
        """Resolve API key using custom credentials dictionary then process environment variables."""
        creds = custom_creds or {}
        for var in self.env_vars:
            val = creds.get(var) or os.getenv(var)
            if val and str(val).strip():
                return str(val).strip()
        if not self.requires_api_key:
            return ""
        return None

    def resolve_base_url(self, custom_creds: dict[str, str] | None = None) -> str:
        """Resolve API base URL honoring base_url_env_var overrides."""
        creds = custom_creds or {}
        candidates: list[str] = []
        if self.base_url_env_var:
            candidates.append(self.base_url_env_var)
        if self.id == "custom":
            candidates.extend(["HEX_CUSTOM_BASE_URL", "CUSTOM_BASE_URL", "OPENAI_BASE_URL"])

        for var in candidates:
            val = creds.get(var) or os.getenv(var)
            if val and str(val).strip():
                return str(val).strip().rstrip("/")

        active_prov = (creds.get("HEX_CLOUD_PROVIDER") or os.getenv("HEX_CLOUD_PROVIDER") or "").strip().lower()
        if active_prov == self.id:
            val = creds.get("HEX_CLOUD_BASE_URL") or os.getenv("HEX_CLOUD_BASE_URL")
            if val and str(val).strip():
                return str(val).strip().rstrip("/")

        return self.default_base_url.rstrip("/")


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    # -------------------------------------------------------------------------
    # 1. Featured Frontier Providers
    # -------------------------------------------------------------------------
    ProviderSpec(
        id="antigravity",
        name="Google Antigravity OAuth",
        category="featured",
        transport="antigravity",
        default_base_url="https://cloudcode-pa.googleapis.com",
        env_vars=("HEX_ANTIGRAVITY_TOKEN",),
        signup_url="https://cloudcode-oauth.google.com",
        description="Zero-cost Day-1 routing via Antigravity OAuth Quota (Claude 3.7/Opus, Gemini 2.5 Pro)",
        default_model="claude-opus-4-6-thinking",
        curated_models=(
            ("claude-opus-4-6-thinking", "Claude 3.7 / Opus 4.6 Thinking (Antigravity Frontier)"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro (Google Antigravity)"),
            ("gemini-2.5-flash", "Gemini 2.5 Flash (Ultra-fast $0)"),
        ),
        requires_api_key=False,
    ),
    ProviderSpec(
        id="openrouter",
        name="OpenRouter",
        category="featured",
        transport="openai_chat",
        default_base_url="https://openrouter.ai/api/v1",
        env_vars=("OPENROUTER_API_KEY", "HEX_OPENROUTER_API_KEY"),
        base_url_env_var="OPENROUTER_BASE_URL",
        signup_url="https://openrouter.ai/keys",
        description="Unified aggregator with 300+ models, live pricing, and automated failover",
        default_model="anthropic/claude-3.7-sonnet",
        curated_models=(
            ("anthropic/claude-3.7-sonnet", "Claude 3.7 Sonnet (Anthropic via OpenRouter)"),
            ("deepseek/deepseek-r1", "DeepSeek R1 (Reasoning Open-Weight)"),
            ("deepseek/deepseek-chat", "DeepSeek V3 (High-performance chat)"),
            ("openai/gpt-4o", "OpenAI GPT-4o"),
            ("google/gemini-2.5-pro", "Google Gemini 2.5 Pro"),
            ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B Instruct"),
        ),
    ),
    ProviderSpec(
        id="anthropic",
        name="Anthropic",
        category="featured",
        transport="anthropic_messages",
        default_base_url="https://api.anthropic.com/v1",
        env_vars=("ANTHROPIC_API_KEY", "HEX_ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        base_url_env_var="ANTHROPIC_BASE_URL",
        signup_url="https://console.anthropic.com/settings/keys",
        description="Direct API keys for Claude 3.7 Sonnet, Claude 3.5 Sonnet, and Claude 3.5 Haiku",
        default_model="claude-3-7-sonnet-20250219",
        curated_models=(
            ("claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet (Latest frontier with hybrid reasoning)"),
            ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet v2"),
            ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku (Fast & efficient)"),
        ),
    ),
    ProviderSpec(
        id="openai",
        name="OpenAI",
        category="featured",
        transport="openai_chat",
        default_base_url="https://api.openai.com/v1",
        env_vars=("OPENAI_API_KEY", "HEX_OPENAI_API_KEY"),
        base_url_env_var="OPENAI_BASE_URL",
        signup_url="https://platform.openai.com/api-keys",
        description="Official OpenAI API keys for GPT-4o, o1, and o3-mini",
        default_model="gpt-4o",
        curated_models=(
            ("gpt-4o", "GPT-4o (Omni flagship)"),
            ("gpt-4o-mini", "GPT-4o Mini (Fast & cost-effective)"),
            ("o1", "OpenAI o1 (Full reasoning)"),
            ("o3-mini", "OpenAI o3-mini (High-speed reasoning)"),
        ),
    ),
    ProviderSpec(
        id="gemini",
        name="Google Gemini / AI Studio",
        category="featured",
        transport="gemini_rest",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        env_vars=("GEMINI_API_KEY", "HEX_GEMINI_API_KEY", "GOOGLE_API_KEY"),
        base_url_env_var="GEMINI_BASE_URL",
        signup_url="https://aistudio.google.com/app/apikey",
        description="Direct Google AI Studio API key (Gemini 2.5 Pro / Flash)",
        default_model="gemini-2.5-pro",
        curated_models=(
            ("gemini-2.5-pro", "Gemini 2.5 Pro (Long context, reasoning)"),
            ("gemini-2.5-flash", "Gemini 2.5 Flash (Ultra-fast multimodal)"),
            ("gemini-2.0-flash", "Gemini 2.0 Flash"),
        ),
    ),
    ProviderSpec(
        id="deepseek",
        name="DeepSeek",
        category="featured",
        transport="openai_chat",
        default_base_url="https://api.deepseek.com/v1",
        env_vars=("DEEPSEEK_API_KEY", "HEX_DEEPSEEK_API_KEY"),
        base_url_env_var="DEEPSEEK_BASE_URL",
        signup_url="https://platform.deepseek.com/api_keys",
        description="Direct DeepSeek API (DeepSeek-V3 chat and DeepSeek-R1 reasoning)",
        default_model="deepseek-chat",
        curated_models=(
            ("deepseek-chat", "DeepSeek V3 (deepseek-chat)"),
            ("deepseek-reasoner", "DeepSeek R1 (deepseek-reasoner)"),
        ),
    ),
    ProviderSpec(
        id="xai",
        name="xAI Grok",
        category="featured",
        transport="openai_chat",
        default_base_url="https://api.x.ai/v1",
        env_vars=("XAI_API_KEY", "HEX_XAI_API_KEY"),
        base_url_env_var="XAI_BASE_URL",
        signup_url="https://console.x.ai/",
        description="Official xAI API key for Grok-2 and Grok-3 models",
        default_model="grok-2-latest",
        curated_models=(
            ("grok-2-latest", "Grok 2 (Latest flagship)"),
            ("grok-beta", "Grok Beta"),
        ),
    ),

    # -------------------------------------------------------------------------
    # 2. High-Speed Open-Weight Cloud Inference
    # -------------------------------------------------------------------------
    ProviderSpec(
        id="groq",
        name="Groq LPU",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.groq.com/openai/v1",
        env_vars=("GROQ_API_KEY", "HEX_GROQ_API_KEY"),
        base_url_env_var="GROQ_BASE_URL",
        signup_url="https://console.groq.com/keys",
        description="Ultra-low latency inference engine (500+ tok/s on Llama 3.3 70B & QwQ)",
        default_model="llama-3.3-70b-versatile",
        curated_models=(
            ("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile"),
            ("llama-3.1-8b-instant", "Llama 3.1 8B Instant (Ultra-fast)"),
            ("qwen-qwq-32b", "Qwen QwQ 32B (Reasoning preview)"),
            ("mixtral-8x7b-32768", "Mixtral 8x7B (32k context)"),
        ),
    ),
    ProviderSpec(
        id="mistral",
        name="Mistral AI",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.mistral.ai/v1",
        env_vars=("MISTRAL_API_KEY", "HEX_MISTRAL_API_KEY"),
        base_url_env_var="MISTRAL_BASE_URL",
        signup_url="https://console.mistral.ai/api-keys/",
        description="Direct Mistral API for Mistral Large, Codestral, and Pixtral",
        default_model="mistral-large-latest",
        curated_models=(
            ("mistral-large-latest", "Mistral Large (Flagship)"),
            ("codestral-latest", "Codestral (Specialized code model)"),
            ("mistral-small-latest", "Mistral Small"),
        ),
    ),
    ProviderSpec(
        id="together",
        name="Together AI",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.together.xyz/v1",
        env_vars=("TOGETHER_API_KEY", "HEX_TOGETHER_API_KEY"),
        base_url_env_var="TOGETHER_BASE_URL",
        signup_url="https://api.together.xyz/settings/api-keys",
        description="Fast open-source model inference on Together GPU clusters",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        curated_models=(
            ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "Llama 3.3 70B Instruct Turbo"),
            ("deepseek-ai/DeepSeek-R1", "DeepSeek R1 (Together AI)"),
            ("Qwen/Qwen2.5-Coder-32B-Instruct", "Qwen 2.5 Coder 32B"),
        ),
    ),
    ProviderSpec(
        id="cerebras",
        name="Cerebras",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.cerebras.ai/v1",
        env_vars=("CEREBRAS_API_KEY", "HEX_CEREBRAS_API_KEY"),
        base_url_env_var="CEREBRAS_BASE_URL",
        signup_url="https://cloud.cerebras.ai/",
        description="Wafer-scale compute engine delivering 1800+ tokens/sec",
        default_model="llama3.3-70b",
        curated_models=(
            ("llama3.3-70b", "Cerebras Llama 3.3 70B"),
            ("llama3.1-8b", "Cerebras Llama 3.1 8B"),
        ),
    ),
    ProviderSpec(
        id="fireworks",
        name="Fireworks AI",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.fireworks.ai/inference/v1",
        env_vars=("FIREWORKS_API_KEY", "HEX_FIREWORKS_API_KEY"),
        base_url_env_var="FIREWORKS_BASE_URL",
        signup_url="https://fireworks.ai/account/api-keys",
        description="Ultra-fast compound AI and function-calling platform",
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        curated_models=(
            ("accounts/fireworks/models/llama-v3p3-70b-instruct", "Fireworks Llama 3.3 70B"),
            ("accounts/fireworks/models/deepseek-r1", "Fireworks DeepSeek R1"),
        ),
    ),
    ProviderSpec(
        id="nvidia",
        name="NVIDIA NIM",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://integrate.api.nvidia.com/v1",
        env_vars=("NVIDIA_API_KEY", "HEX_NVIDIA_API_KEY"),
        base_url_env_var="NVIDIA_BASE_URL",
        signup_url="https://build.nvidia.com/",
        description="Enterprise NVIDIA Inference Microservices (NIM)",
        default_model="meta/llama-3.3-70b-instruct",
        curated_models=(
            ("meta/llama-3.3-70b-instruct", "NVIDIA Llama 3.3 70B Instruct"),
            ("deepseek-ai/deepseek-r1", "NVIDIA DeepSeek R1"),
            ("nvidia/llama-3.1-nemotron-70b-instruct", "Nemotron 70B Instruct"),
        ),
    ),
    ProviderSpec(
        id="alibaba",
        name="Alibaba / Qwen",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_vars=("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY"),
        base_url_env_var="DASHSCOPE_BASE_URL",
        signup_url="https://dashscope.console.aliyun.com/",
        description="Alibaba Cloud DashScope international endpoint (Qwen 2.5 Coder, QwQ)",
        default_model="qwen-plus",
        curated_models=(
            ("qwen-max", "Qwen Max (Flagship)"),
            ("qwen-plus", "Qwen Plus (Balanced)"),
            ("qwen-turbo", "Qwen Turbo (Fast)"),
            ("qwen2.5-coder-32b-instruct", "Qwen 2.5 Coder 32B"),
        ),
    ),
    ProviderSpec(
        id="minimax",
        name="MiniMax",
        category="fast_cloud",
        transport="anthropic_messages",
        default_base_url="https://api.minimax.io/anthropic",
        env_vars=("MINIMAX_API_KEY", "HEX_MINIMAX_API_KEY"),
        base_url_env_var="MINIMAX_BASE_URL",
        signup_url="https://platform.minimaxi.com/",
        description="MiniMax language model via Anthropic-compatible protocol",
        default_model="MiniMax-Text-01",
        curated_models=(
            ("MiniMax-Text-01", "MiniMax Text 01 (1M Context)"),
            ("abab6.5s-chat", "MiniMax abab6.5s"),
        ),
    ),
    ProviderSpec(
        id="kimi",
        name="Kimi / Moonshot",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.moonshot.ai/v1",
        env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY", "MOONSHOT_API_KEY"),
        base_url_env_var="KIMI_BASE_URL",
        signup_url="https://platform.moonshot.ai/",
        description="Moonshot AI Kimi models with ultra-long context windows",
        default_model="moonshot-v1-32k",
        curated_models=(
            ("moonshot-v1-128k", "Moonshot Kimi v1 128k"),
            ("moonshot-v1-32k", "Moonshot Kimi v1 32k"),
            ("moonshot-v1-8k", "Moonshot Kimi v1 8k"),
        ),
    ),
    ProviderSpec(
        id="zai",
        name="Z.AI / GLM",
        category="fast_cloud",
        transport="openai_chat",
        default_base_url="https://api.z.ai/api/paas/v4",
        env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
        signup_url="https://open.bigmodel.cn/",
        description="Zhipu AI GLM-4 Plus and general language models",
        default_model="glm-4-plus",
        curated_models=(
            ("glm-4-plus", "GLM-4 Plus (Flagship)"),
            ("glm-4-air", "GLM-4 Air (Fast)"),
            ("glm-4-flash", "GLM-4 Flash (Free tier)"),
        ),
    ),

    # -------------------------------------------------------------------------
    # 3. Aggregators & Cloud Gateways
    # -------------------------------------------------------------------------
    ProviderSpec(
        id="ai-gateway",
        name="Vercel AI Gateway",
        category="aggregator",
        transport="openai_chat",
        default_base_url="https://ai-gateway.vercel.sh/v1",
        env_vars=("AI_GATEWAY_API_KEY", "HEX_AI_GATEWAY_API_KEY"),
        base_url_env_var="AI_GATEWAY_BASE_URL",
        signup_url="https://vercel.com/d/ai-gateway",
        description="Edge gateway with centralized caching and billing across frontier models",
        default_model="openai/gpt-4o",
        curated_models=(
            ("openai/gpt-4o", "GPT-4o (via Vercel AI Gateway)"),
            ("anthropic/claude-3-5-sonnet", "Claude 3.5 Sonnet (via Vercel)"),
        ),
    ),
    ProviderSpec(
        id="huggingface",
        name="Hugging Face Serverless",
        category="aggregator",
        transport="openai_chat",
        default_base_url="https://router.huggingface.co/v1",
        env_vars=("HF_TOKEN", "HUGGINGFACE_API_KEY"),
        base_url_env_var="HF_BASE_URL",
        signup_url="https://huggingface.co/settings/tokens",
        description="Hugging Face Router with access to hosted open models",
        default_model="meta-llama/Llama-3.3-70B-Instruct",
        curated_models=(
            ("meta-llama/Llama-3.3-70B-Instruct", "Llama 3.3 70B Instruct"),
            ("deepseek-ai/DeepSeek-R1", "DeepSeek R1"),
            ("Qwen/Qwen2.5-72B-Instruct", "Qwen 2.5 72B Instruct"),
        ),
    ),

    # -------------------------------------------------------------------------
    # 4. Enterprise Cloud Platforms
    # -------------------------------------------------------------------------
    ProviderSpec(
        id="azure",
        name="Azure OpenAI / Foundry",
        category="enterprise",
        transport="openai_chat",
        default_base_url="https://models.inference.ai.azure.com",
        env_vars=("AZURE_OPENAI_API_KEY", "AZURE_FOUNDRY_API_KEY", "HEX_AZURE_API_KEY"),
        base_url_env_var="AZURE_OPENAI_ENDPOINT",
        signup_url="https://portal.azure.com/",
        description="Microsoft Azure AI Foundry & Azure OpenAI Service",
        default_model="gpt-4o",
        curated_models=(
            ("gpt-4o", "Azure GPT-4o"),
            ("DeepSeek-R1", "Azure DeepSeek R1"),
        ),
    ),
    ProviderSpec(
        id="bedrock",
        name="AWS Bedrock",
        category="enterprise",
        transport="anthropic_messages",
        default_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        env_vars=("AWS_ACCESS_KEY_ID", "HEX_AWS_ACCESS_KEY_ID"),
        base_url_env_var="BEDROCK_BASE_URL",
        signup_url="https://aws.amazon.com/bedrock/",
        description="Amazon Web Services Bedrock (Claude 3.5, Llama 3, Amazon Nova)",
        default_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        curated_models=(
            ("anthropic.claude-3-5-sonnet-20241022-v2:0", "AWS Claude 3.5 Sonnet v2"),
            ("amazon.nova-pro-v1:0", "Amazon Nova Pro"),
        ),
    ),
    ProviderSpec(
        id="vertex",
        name="Google Cloud Vertex AI",
        category="enterprise",
        transport="gemini_rest",
        default_base_url="https://aiplatform.googleapis.com/v1",
        env_vars=("VERTEX_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
        base_url_env_var="VERTEX_BASE_URL",
        signup_url="https://cloud.google.com/vertex-ai",
        description="Google Cloud Platform enterprise Vertex AI managed models",
        default_model="gemini-2.5-pro",
        curated_models=(
            ("gemini-2.5-pro", "Vertex Gemini 2.5 Pro"),
            ("gemini-2.5-flash", "Vertex Gemini 2.5 Flash"),
            ("claude-3-5-sonnet-v2@20241022", "Vertex Claude 3.5 Sonnet v2"),
        ),
    ),

    # -------------------------------------------------------------------------
    # 5. Local Offline & Custom Endpoints
    # -------------------------------------------------------------------------
    ProviderSpec(
        id="ollama",
        name="Ollama Local",
        category="local",
        transport="openai_chat",
        default_base_url="http://localhost:11434/v1",
        env_vars=("OLLAMA_API_KEY",),
        base_url_env_var="OLLAMA_HOST",
        signup_url="https://ollama.com",
        description="Local Ollama server (http://localhost:11434/v1) — Zero egress & free",
        default_model="llama3.3",
        curated_models=(
            ("llama3.3", "Ollama Llama 3.3"),
            ("qwen2.5-coder:7b", "Ollama Qwen 2.5 Coder 7B"),
            ("deepseek-r1:8b", "Ollama DeepSeek R1 8B"),
        ),
        requires_api_key=False,
    ),
    ProviderSpec(
        id="lmstudio",
        name="LM Studio",
        category="local",
        transport="openai_chat",
        default_base_url="http://localhost:1234/v1",
        env_vars=("LM_API_KEY",),
        base_url_env_var="LM_BASE_URL",
        signup_url="https://lmstudio.ai",
        description="Local LM Studio inference server (http://localhost:1234/v1)",
        default_model="local-model",
        curated_models=(
            ("local-model", "LM Studio Active Model"),
        ),
        requires_api_key=False,
    ),
    ProviderSpec(
        id="local",
        name="Local MLX / Host LLM",
        category="local",
        transport="local",
        default_base_url="local://mlx",
        env_vars=(),
        signup_url="https://github.com/ml-explore/mlx",
        description="In-process local hardware acceleration via Apple Silicon MLX / host compute",
        default_model="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        curated_models=(
            ("mlx-community/Qwen2.5-Coder-7B-Instruct-4bit", "Qwen 2.5 Coder 7B (MLX 4-bit)"),
            ("mlx-community/Llama-3.2-3B-Instruct-4bit", "Llama 3.2 3B (MLX 4-bit)"),
        ),
        requires_api_key=False,
    ),
    ProviderSpec(
        id="custom",
        name="Custom OpenAI-Compatible Endpoint",
        category="local",
        transport="openai_chat",
        default_base_url="http://localhost:8000/v1",
        env_vars=("HEX_CUSTOM_API_KEY", "CUSTOM_API_KEY"),
        base_url_env_var="HEX_CUSTOM_BASE_URL",
        signup_url="https://vllm.ai",
        description="Any custom vLLM, SGLang, LiteLLM, llama.cpp, or proxy endpoint",
        default_model="custom-model",
        curated_models=(
            ("custom-model", "Custom Model"),
        ),
        requires_api_key=False,
    ),
)


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {p.id: p for p in PROVIDER_SPECS}

CATEGORY_LABELS: dict[str, str] = {
    "featured": "Frontier & Multi-Model Providers (Antigravity, OpenRouter, Claude, GPT-4o, Gemini, DeepSeek, Grok)",
    "fast_cloud": "High-Speed Open-Weight Cloud (Groq, Mistral, Together, Cerebras, Fireworks, NVIDIA NIM, Qwen)",
    "aggregator": "Aggregators & Gateways (Vercel AI Gateway, Hugging Face Serverless)",
    "enterprise": "Enterprise Cloud Platforms (Azure Foundry, AWS Bedrock, Vertex AI)",
    "local": "Local Offline & Custom Endpoints (Ollama, LM Studio, Local MLX, Custom Endpoint)",
}


def get_provider_spec(provider_id: str | None) -> ProviderSpec | None:
    """Retrieve ProviderSpec by normalized ID or return None."""
    if not provider_id:
        return None
    norm = str(provider_id).strip().lower()
    # Support common aliases
    alias_map = {
        "google-antigravity": "antigravity",
        "cloudcode": "antigravity",
        "google": "gemini",
        "google-gemini": "gemini",
        "claude": "anthropic",
        "qwen": "alibaba",
        "dashscope": "alibaba",
        "moonshot": "kimi",
        "glm": "zai",
        "azure-foundry": "azure",
        "azure-openai": "azure",
        "ai_gateway": "ai-gateway",
    }
    resolved_id = alias_map.get(norm, norm)
    return PROVIDER_REGISTRY.get(resolved_id)


def list_providers(category: str | None = None) -> list[ProviderSpec]:
    """Return all provider specifications, optionally filtered by category."""
    if category:
        return [p for p in PROVIDER_SPECS if p.category == category]
    return list(PROVIDER_SPECS)


def list_provider_ids() -> list[str]:
    """Return list of all registered provider IDs."""
    return [p.id for p in PROVIDER_SPECS]


def get_all_provider_env_vars() -> dict[str, str]:
    """Map all possible environment variables back to their canonical provider ID."""
    mapping: dict[str, str] = {}
    for p in PROVIDER_SPECS:
        for ev in p.env_vars:
            mapping[ev] = p.id
        if p.base_url_env_var:
            mapping[p.base_url_env_var] = p.id
    return mapping
