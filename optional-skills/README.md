# `optional-skills` (`harness_core.resources.skills.optional`)

**Extended catalog of 115 specialized optional `SKILL.md` bundles.**

`optional-skills/` maps to `harness_core.resources.skills.optional` in [`pyproject.toml`](../pyproject.toml) (packaged in full as wheel data) and is resolved at runtime via `harness_core.resources.bundled_skills_dir(optional=True)`.

## Catalog Summary (115 Bundles)

- **Root**: `yuanbao/` (1)
- **Category Directories (20 categories, 114 bundles)**:
  - `autonomous-ai-agents/` (5): `antigravity-cli`, `blackbox`, `grok`, `honcho`, `openhands`
  - `blockchain/` (3): `evm`, `hyperliquid`, `solana` (with Python helper `scripts/`)
  - `communication/` (1): `one-three-one-rule`
  - `creative/` (14): `audiocraft-audio-generation`, `baoyu-article-illustrator`, `baoyu-comic`, `blender-mcp`, `concept-diagrams`, `creative-ideation`, `heartmula`, `hyperframes`, `kanban-video-orchestrator`, `meme-generation`, `pixel-art`, `social-media-content-calendar`, `tldraw-offline`, `unreal-mcp`
  - `data-science/` (1): `jupyter-notebook`
  - `devops/` (6): `actual-setup`, `docker-management`, `hex-s6-container-supervision`, `inference-sh-cli`, `pinggy-tunnel`, `watchers`
  - `dogfood/` (1): `adversarial-ux-test`
  - `email/` (1): `agentmail`
  - `finance/` (9): `3-statement-model`, `comps-analysis`, `dcf-model`, `excel-author`, `lbo-model`, `merger-model`, `polymarket`, `pptx-author`, `stocks`
  - `gaming/` (2): `minecraft-modpack-server`, `pokemon-player`
  - `health/` (2): `fitness-nutrition`, `neuroskill-bci`
  - `mcp/` (3): `fastmcp`, `mcp-oauth-remote-gateway`, `mcporter`
  - `migration/` (1): `openclaw-migration` (`scripts/openclaw_to_hex.py`)
  - `mlops/` (31): `accelerate`, `chroma`, `clip`, `faiss`, `flash-attention`, `guidance`, `huggingface-tokenizers`, `inference/outlines`, `instructor`, `lambda-labs`, `llava`, `modal`, `models/segment-anything-model`, `nemo-curator`, `obliteratus`, `peft`, `pinecone`, `pytorch-fsdp`, `pytorch-lightning`, `qdrant`, `research/dspy`, `saelens`, `simpo`, `slime`, `stable-diffusion`, `tensorrt-llm`, `torchtitan`, `training/{axolotl,trl-fine-tuning,unsloth}`, `whisper`
  - `payments/` (3): `mpp-agent`, `stripe-link-cli`, `stripe-projects`
  - `productivity/` (7): `canvas`, `here-now`, `memento-flashcards`, `shop`, `shopify`, `siyuan`, `telephony`
  - `research/` (12): `bioinformatics`, `darwinian-evolver`, `domain-intel`, `drug-discovery`, `duckduckgo-search`, `gitnexus-explorer`, `osint-investigation`, `parallel-cli`, `pinecone-research`, `qmd`, `scrapling`, `searxng-search`
  - `security/` (6): `1password`, `godmode`, `oss-forensics`, `sherlock`, `unbroker`, `web-pentest`
  - `software-development/` (4): `ast-grep`, `code-wiki`, `rest-graphql-debug`, `subagent-driven-development`
  - `web-development/` (2): `cloudflare-temporary-deploy`, `page-agent`

## Notes

- All imported/activated optional skills still pass through `SkillSpectorScanner` (`cognition/skillspector.py`) before execution.
- Legacy bytecode artifacts under `harness_core/optional_skills/` are unused leftovers from the open-root relocation; active resources live here.
