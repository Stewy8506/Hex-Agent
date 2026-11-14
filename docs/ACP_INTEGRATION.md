# Agent Client Protocol (ACP) Integration

HEX implements the **Agent Client Protocol (ACP)** over standard JSON-RPC 2.0 stdio transport, allowing you to use HEX as an intelligent, autonomous partner directly inside your favorite code editors.

---

## 1. Zed Editor

Add HEX as an external assistant in your `.zed/settings.json` (or global `~/.config/zed/settings.json`):

```json
{
  "agent": {
    "name": "HEX",
    "command": "hex",
    "args": ["acp"],
    "protocol": "acp"
  }
}
```

Once configured, toggle the assistant in Zed (`Cmd+?` or `Ctrl+?`) and select **HEX**.

---

## 2. Neovim (`avante.nvim` / `codecompanion`)

In your Neovim Lua configuration:

```lua
-- avante.nvim configuration
require('avante').setup({
  provider = "acp",
  acp_command = { "hex", "acp" },
})
```

---

## 3. Diagnostics & Environment Setup

Verify that the ACP server is operational and your environment is ready:

```bash
# Verify ACP readiness and environment health
hex acp --check

# Generate ready-to-paste editor configuration snippets
hex acp --setup

# Start ACP server manually over stdio (JSON-RPC 2.0)
hex acp
```

### Supported ACP Features:
- Multi-turn conversational chat with live token streaming.
- Workspace file reading and editing in isolated Copy-on-Write staging.
- Over 20+ slash commands accessible directly through editor prompts.
- Interactive operator policy approvals for high-risk tool actions (R3/R4).
