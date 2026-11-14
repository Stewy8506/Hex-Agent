# Supported Models & Providers Guide

HEX is completely provider- and model-agnostic. It features a unified dynamic model routing engine supporting **26 frontier, cloud, enterprise, and local offline providers** with zero vendor lock-in.

You can switch models or providers on the fly at any time via:
```bash
# In the interactive cockpit:
/model

# From the shell:
hex --provider openrouter --model deepseek/deepseek-r1
hex --provider anthropic --model claude-3-7-sonnet-20250219
hex --provider ollama --model qwen2.5-coder:7b
```

---

## Provider Directory & Curated Models

### 1. Frontier & Multi-Model Providers

| Provider | ID | Default Model | Curated Models | Authentication |
| :--- | :--- | :--- | :--- | :--- |
| **Google Antigravity** | `antigravity` | `claude-opus-4-6-thinking` | `claude-opus-4-6-thinking`<br>`gemini-2.5-pro`<br>`gemini-2.5-flash` | OAuth ($0 Day-1 Marginal Billing) |
| **OpenRouter** | `openrouter` | `anthropic/claude-3.7-sonnet` | `anthropic/claude-3.7-sonnet`<br>`deepseek/deepseek-r1`<br>`deepseek/deepseek-chat`<br>`openai/gpt-4o`<br>`google/gemini-2.5-pro`<br>`meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` |
| **Anthropic** | `anthropic` | `claude-3-7-sonnet-20250219` | `claude-3-7-sonnet-20250219` (Hybrid reasoning)<br>`claude-3-5-sonnet-20241022` (v2)<br>`claude-3-5-haiku-20241022` | `ANTHROPIC_API_KEY` |
| **OpenAI** | `openai` | `gpt-4o` | `gpt-4o` (Omni)<br>`gpt-4o-mini`<br>`o1` (Full reasoning)<br>`o3-mini` (High-speed reasoning) | `OPENAI_API_KEY` |
| **Google Gemini** | `gemini` | `gemini-2.5-pro` | `gemini-2.5-pro` (Long context)<br>`gemini-2.5-flash`<br>`gemini-2.0-flash` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| **DeepSeek** | `deepseek` | `deepseek-chat` | `deepseek-chat` (DeepSeek V3)<br>`deepseek-reasoner` (DeepSeek R1) | `DEEPSEEK_API_KEY` |
| **xAI Grok** | `xai` | `grok-2-latest` | `grok-2-latest`<br>`grok-beta` | `XAI_API_KEY` |

---

### 2. High-Speed Open-Weight Cloud

Ultra-low-latency inference engines for open-weight models:

| Provider | ID | Default Model | Curated Models | Authentication |
| :--- | :--- | :--- | :--- | :--- |
| **Groq LPU** | `groq` | `llama-3.3-70b-versatile` | `llama-3.3-70b-versatile`<br>`llama-3.1-8b-instant`<br>`qwen-qwq-32b`<br>`mixtral-8x7b-32768` | `GROQ_API_KEY` |
| **Mistral AI** | `mistral` | `mistral-large-latest` | `mistral-large-latest`<br>`codestral-latest` (Code-specialized)<br>`mistral-small-latest` | `MISTRAL_API_KEY` |
| **Together AI** | `together` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | `Llama-3.3-70B-Instruct-Turbo`<br>`deepseek-ai/DeepSeek-R1`<br>`Qwen/Qwen2.5-Coder-32B-Instruct` | `TOGETHER_API_KEY` |
| **Cerebras** | `cerebras` | `llama3.3-70b` | `llama3.3-70b`<br>`llama3.1-8b` (2,000+ tokens/sec) | `CEREBRAS_API_KEY` |
| **Fireworks AI** | `fireworks` | `llama-v3p3-70b-instruct` | `accounts/fireworks/models/llama-v3p3-70b-instruct`<br>`accounts/fireworks/models/deepseek-r1` | `FIREWORKS_API_KEY` |
| **NVIDIA NIM** | `nvidia` | `meta/llama-3.3-70b-instruct` | `meta/llama-3.3-70b-instruct`<br>`deepseek-ai/deepseek-r1`<br>`nvidia/llama-3.1-nemotron-70b-instruct` | `NVIDIA_API_KEY` |
| **Alibaba / Qwen** | `alibaba` | `qwen-plus` | `qwen-max`<br>`qwen-plus`<br>`qwen-turbo`<br>`qwen2.5-coder-32b-instruct` | `DASHSCOPE_API_KEY` |
| **MiniMax** | `minimax` | `MiniMax-Text-01` | `MiniMax-Text-01` (1M Context)<br>`abab6.5s-chat` | `MINIMAX_API_KEY` |
| **Kimi / Moonshot** | `kimi` | `moonshot-v1-32k` | `moonshot-v1-128k`<br>`moonshot-v1-32k`<br>`moonshot-v1-8k` | `MOONSHOT_API_KEY` |
| **Z.AI / GLM** | `zai` | `glm-4-plus` | `glm-4-plus`<br>`glm-4-air`<br>`glm-4-flash` | `ZAI_API_KEY` |

---

### 3. Aggregators & Gateways

| Provider | ID | Default Model | Curated Models | Authentication |
| :--- | :--- | :--- | :--- | :--- |
| **Vercel AI Gateway** | `ai-gateway` | `openai/gpt-4o` | `openai/gpt-4o`<br>`anthropic/claude-3-5-sonnet` | `AI_GATEWAY_API_KEY` |
| **Hugging Face** | `huggingface` | `meta-llama/Llama-3.3-70B-Instruct` | `meta-llama/Llama-3.3-70B-Instruct`<br>`deepseek-ai/DeepSeek-R1`<br>`Qwen/Qwen2.5-72B-Instruct` | `HF_TOKEN` |

---

### 4. Enterprise Hyperscalers

| Provider | ID | Default Model | Curated Models | Authentication |
| :--- | :--- | :--- | :--- | :--- |
| **Azure OpenAI & Foundry** | `azure` | `gpt-4o` | `gpt-4o`<br>`DeepSeek-R1` | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` |
| **AWS Bedrock** | `bedrock` | `claude-3-5-sonnet-v2` | `anthropic.claude-3-5-sonnet-20241022-v2:0`<br>`amazon.nova-pro-v1:0` | AWS IAM Credentials (`AWS_REGION`, etc.) |
| **Google Cloud Vertex AI** | `vertex` | `gemini-2.5-pro` | `gemini-2.5-pro`<br>`gemini-2.5-flash`<br>`claude-3-5-sonnet-v2@20241022` | GCP Application Default Credentials (`gcloud auth`) |

---

### 5. Local Offline & Custom Runtimes

100% offline, private inference with zero telemetry or data leaving your machine:

| Provider | ID | Default Model | Description | Endpoint |
| :--- | :--- | :--- | :--- | :--- |
| **Ollama Local** | `ollama` | `llama3.3` | Native Ollama engine (`llama3.3`, `qwen2.5-coder:7b`, `deepseek-r1:8b`) | `http://localhost:11434/v1` |
| **LM Studio** | `lmstudio` | `local-model` | Connects directly to active LM Studio server | `http://localhost:1234/v1` |
| **Local Apple Silicon (MLX)** | `local` | `Qwen2.5-Coder-7B-Instruct-4bit` | Native Apple Metal acceleration via MLX on Apple Silicon | Direct in-process |
| **Custom OpenAI-Compatible** | `custom` | `custom-model` | Connects to vLLM, SGLang, TGI, LocalAI, or custom proxies | Configurable via `--base-url` |

---

## Zero-Friction Setup: Google Antigravity OAuth

If you already use Google Antigravity in your editor, HEX automatically detects your active local credentials on startup:
- **$0 Marginal Quota**: Uses your included frontier daily reasoning budget.
- **Zero API Keys**: No manual token generation or billing setup required.
- **Quota Telemetry**: Run `hex quota` at any time to inspect token buckets, remaining percentages, and reset windows.
