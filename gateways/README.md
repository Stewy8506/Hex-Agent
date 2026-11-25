# `gateways` (`harness_core.gateways`)

**Backward-compatibility module alias forwarding to `harness_core.capabilities.gateways`.**

`gateways/` maps to `harness_core.gateways` in [`pyproject.toml`](../pyproject.toml). Every file in this folder (`__init__.py`, `discord_bot.py`, `manager.py`, `telegram_bot.py`) aliases `sys.modules[__name__]` directly to the canonical implementation in [`capabilities/gateways/`](../capabilities/README.md) so imports and `unittest.mock.patch` targets keep identical module identity.

> Note: `reaction_listener.py` (`MobileApprovalBridge`, `ApprovalRequest`, `ApprovalDecision`) resides only under [`capabilities/gateways/`](../capabilities/gateways/reaction_listener.py) and is re-exported through `__init__.py`.

## Integration

- Imported by `hex_cli/cli.py` (`hex gateway start|status`).
- Gateway tests live in [`tests/capabilities/`](../tests/capabilities).
