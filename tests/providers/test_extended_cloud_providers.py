"""Unit tests for the extended CloudModelProvider protocol handling (OpenAI Chat, Anthropic Messages, Gemini REST)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from harness_core.providers.cloud_provider import (
    CloudModelProvider,
    CloudProviderType,
    ProviderUnavailableError,
    fetch_available_models,
)


class TestExtendedCloudProviders(unittest.TestCase):
    """Test suite verifying request building and response parsing across all transports."""

    def test_openai_chat_request_and_response(self) -> None:
        """Verify OpenAI Chat completions request building and response parsing for OpenRouter / DeepSeek / Groq."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}):
            provider = CloudModelProvider(
                provider_type="openrouter",
                model_id="deepseek/deepseek-r1",
            )
            self.assertEqual(provider.model_id, "deepseek/deepseek-r1")
            self.assertEqual(provider.base_url, "https://openrouter.ai/api/v1")

            url, headers, body_bytes = provider._build_request(
                clean_prompt="Write a Python hello world function.",
                system_prompt="You are an expert engineer.",
                temperature=0.2,
                max_tokens=500,
                stop_sequences=None,
            )
            self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
            # Headers are normalized lowercase in internal builder
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["authorization"], "Bearer sk-or-test-key")
            self.assertEqual(norm_headers["content-type"], "application/json")

            payload = json.loads(body_bytes.decode("utf-8"))
            self.assertEqual(payload["model"], "deepseek/deepseek-r1")
            self.assertEqual(payload["temperature"], 0.2)
            self.assertEqual(payload["max_tokens"], 500)
            self.assertEqual(len(payload["messages"]), 2)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["messages"][1]["role"], "user")

            mock_response_data = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "def hello():\n    return 'Hello, World!'",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 25,
                    "total_tokens": 40,
                },
            }

            text, in_tok, out_tok, cached_tok, finish = provider._parse_response(
                mock_response_data, prompt="Write a Python hello world function."
            )
            self.assertIn("def hello()", text)
            self.assertEqual(in_tok, 15)
            self.assertEqual(out_tok, 25)

    def test_anthropic_messages_request_and_response(self) -> None:
        """Verify Anthropic Messages request format for Anthropic / MiniMax."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}):
            provider = CloudModelProvider(
                provider_type="anthropic",
                model_id="claude-3-5-sonnet-20241022",
            )
            url, headers, body_bytes = provider._build_request(
                clean_prompt="Refactor this code.",
                system_prompt="Concise refactoring instructions.",
                temperature=0.0,
                max_tokens=1000,
                stop_sequences=None,
            )
            self.assertEqual(url, "https://api.anthropic.com/v1/messages")
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["x-api-key"], "sk-ant-test-key")
            self.assertEqual(norm_headers["anthropic-version"], "2023-06-01")

            payload = json.loads(body_bytes.decode("utf-8"))
            self.assertEqual(payload["model"], "claude-3-5-sonnet-20241022")
            self.assertEqual(payload["system"], "Concise refactoring instructions.")
            self.assertEqual(payload["messages"], [{"role": "user", "content": "Refactor this code."}])

            mock_response_data = {
                "content": [{"type": "text", "text": "// Refactored solution"}],
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 45,
                },
            }

            text, in_tok, out_tok, cached_tok, finish = provider._parse_response(
                mock_response_data, prompt="Refactor this code."
            )
            self.assertEqual(text, "// Refactored solution")
            self.assertEqual(in_tok, 30)
            self.assertEqual(out_tok, 45)

    def test_custom_endpoint_override(self) -> None:
        """Verify that a custom endpoint with base_url overrides default endpoint."""
        with patch.dict(os.environ, {"HEX_CUSTOM_API_KEY": "sk-proxy-key"}):
            provider = CloudModelProvider(
                provider_type="custom",
                model_id="meta-llama/Llama-3.3-70B-Instruct",
                base_url="http://10.0.0.5:8000/v1",
            )
            url, headers, body_bytes = provider._build_request(
                clean_prompt="Hello server",
                system_prompt=None,
                temperature=0.7,
                max_tokens=256,
                stop_sequences=None,
            )
            self.assertEqual(url, "http://10.0.0.5:8000/v1/chat/completions")
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["authorization"], "Bearer sk-proxy-key")

    def test_fetch_available_models_network_failure(self) -> None:
        """Verify fetch_available_models raises ProviderUnavailableError on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            with self.assertRaises(ProviderUnavailableError):
                fetch_available_models("groq", api_key="test-key")

    def test_missing_api_key_raises_unavailable(self) -> None:
        """Verify unconfigured provider raises ProviderUnavailableError on generation."""
        with patch.dict(os.environ, {}, clear=True):
            provider = CloudModelProvider(
                provider_type="deepseek",
                model_id="deepseek-chat",
            )
            with self.assertRaises(ProviderUnavailableError):
                provider.generate(prompt="Test prompt")

    def test_openai_native_tools_and_tool_calls_roundtrip(self) -> None:
        """Verify OpenAI transport serializes JSON-schema tools, tool_calls, and tool results."""
        captured_payloads: list[dict] = []

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            captured_payloads.append(json.loads(body.decode("utf-8")))
            resp = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12},
            }
            return 200, json.dumps(resp).encode("utf-8")

        provider = CloudModelProvider(
            provider_type="openrouter",
            model_id="openai/gpt-4o-mini",
            api_key="sk-or-test",
            http_transport=mock_transport,
        )
        tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Inspect README.md"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_prev", "name": "list_files", "arguments": {"path": "."}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev",
                "name": "list_files",
                "content": '{"files": ["README.md"]}',
                "observation": "strip-me",
            },
        ]
        resp = provider.generate(messages=messages, tools=tools)
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0]["id"], "call_abc_1")
        self.assertEqual(resp.tool_calls[0]["name"], "read_file")
        self.assertEqual(resp.tool_calls[0]["arguments"], {"path": "README.md"})

        sent = captured_payloads[0]
        self.assertEqual(sent["tools"][0]["type"], "function")
        self.assertEqual(sent["tools"][0]["function"]["name"], "read_file")
        assistant_msg = [m for m in sent["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "list_files")
        tool_msg = [m for m in sent["messages"] if m["role"] == "tool"][0]
        self.assertEqual(tool_msg, {"role": "tool", "tool_call_id": "call_prev", "content": '{"files": ["README.md"]}'})

    def test_anthropic_and_gemini_native_tool_calls(self) -> None:
        """Verify Anthropic Messages and Gemini REST transports encode tools and parse tool calls."""
        ant_captured: dict = {}

        def ant_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            ant_captured["payload"] = json.loads(body.decode("utf-8"))
            return 200, json.dumps({
                "content": [
                    {"type": "text", "text": "Checking file"},
                    {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {"path": "src"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 8},
                "stop_reason": "tool_use",
            }).encode("utf-8")

        ant = CloudModelProvider(
            provider_type="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            api_key="sk-ant-test",
            http_transport=ant_transport,
        )
        ant_resp = ant.generate(
            messages=[
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t0", "name": "list_files", "arguments": {}}]},
                {"role": "tool", "tool_call_id": "t0", "content": "[]"},
            ],
            tools=[{"name": "list_files", "description": "List", "parameters": {"type": "object", "properties": {}}}],
        )
        self.assertEqual(ant_captured["payload"]["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(ant_resp.tool_calls, [{"id": "toolu_1", "name": "list_files", "arguments": {"path": "src"}}])

        gem_captured: dict = {}

        def gem_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            gem_captured["payload"] = json.loads(body.decode("utf-8"))
            return 200, json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"functionCall": {"name": "read_file", "args": {"path": "Cargo.toml"}}}]
                    },
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 6},
            }).encode("utf-8")

        gem = CloudModelProvider(
            provider_type="gemini",
            model_id="gemini-2.5-pro",
            api_key="gem-test",
            http_transport=gem_transport,
        )
        gem_resp = gem.generate(
            messages=[{"role": "user", "content": "Inspect Cargo.toml"}],
            tools=[{"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}],
            tool_choice={"type": "function", "function": {"name": "read_file"}},
        )
        self.assertEqual(gem_captured["payload"]["tools"][0]["functionDeclarations"][0]["name"], "read_file")
        self.assertEqual(
            gem_captured["payload"]["toolConfig"],
            {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["read_file"]}},
        )
        self.assertEqual(gem_resp.tool_calls[0]["name"], "read_file")
        self.assertEqual(gem_resp.tool_calls[0]["arguments"], {"path": "Cargo.toml"})

    def test_structured_messages_redact_secrets_across_all_cloud_transports(self) -> None:
        """Verify secrets in structured tool outputs, assistant calls, and user messages are redacted."""
        secret_tok = "sk-ant-api03-secretvalue1234567890"
        gh_tok = "ghp_1234567890abcdefghijklmnop"
        gem_tok = "AIzaSyD-1234567890abcdefghijklmno"
        tool_spec = [{"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"max_tokens": {"type": "integer"}}}}]
        base_msgs = [
            {"role": "user", "content": f"Check token {secret_tok}"},
            {
                "role": "assistant",
                "content": f"Reading {gh_tok}",
                "thinking": f"thought {secret_tok}",
                "signature": "sk-sig-opaque-thinking-1234567890",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "read_file",
                        "arguments": {
                            "path": ".env",
                            "max_tokens": 50,
                            "name": secret_tok,
                            "id": gh_tok,
                            "type": gem_tok,
                            "role": secret_tok,
                            "tool_name": gh_tok,
                            "signature": secret_tok,
                            "nested_thinking": {"type": "thinking", "signature": secret_tok},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": f"API_KEY={secret_tok}"},
        ]

        for ptype, model, key in (
            ("openrouter", "openai/gpt-4o-mini", "sk-or-test"),
            ("anthropic", "claude-3-5-sonnet-20241022", "sk-ant-test"),
            ("gemini", "gemini-2.5-pro", "gem-test"),
        ):
            captured: list[bytes] = []

            def transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
                captured.append(body)
                if ptype == "anthropic":
                    payload = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 2}}
                elif ptype == "gemini":
                    payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}
                else:
                    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
                return 200, json.dumps(payload).encode("utf-8")

            prov = CloudModelProvider(provider_type=ptype, model_id=model, api_key=key, http_transport=transport)
            prov.generate(messages=base_msgs, tools=tool_spec)
            raw_sent = captured[0].decode("utf-8")
            self.assertNotIn(secret_tok, raw_sent)
            self.assertNotIn(gh_tok, raw_sent)
            self.assertNotIn(gem_tok, raw_sent)
            self.assertIn("[REDACTED_SECRET]", raw_sent)
            parsed_sent = json.loads(raw_sent)
            if ptype == "anthropic":
                blocks = parsed_sent["messages"][1]["content"]
                self.assertEqual(blocks[0]["signature"], "sk-sig-opaque-thinking-1234567890")
                tool_input = blocks[-1]["input"]
                self.assertEqual(tool_input["max_tokens"], 50)
                for k in ("name", "id", "type", "role", "tool_name", "signature"):
                    self.assertEqual(tool_input[k], "[REDACTED_SECRET]")
                self.assertEqual(tool_input["nested_thinking"]["signature"], "[REDACTED_SECRET]")
            elif ptype == "gemini":
                fn_args = parsed_sent["contents"][1]["parts"][-1]["functionCall"]["args"]
                self.assertEqual(fn_args["max_tokens"], 50)
                for k in ("name", "id", "type", "role", "tool_name", "signature"):
                    self.assertEqual(fn_args[k], "[REDACTED_SECRET]")
                self.assertEqual(fn_args["nested_thinking"]["signature"], "[REDACTED_SECRET]")
            else:
                tc_args = json.loads(parsed_sent["messages"][1]["tool_calls"][0]["function"]["arguments"])
                self.assertEqual(tc_args["max_tokens"], 50)
                for k in ("name", "id", "type", "role", "tool_name", "signature"):
                    self.assertEqual(tc_args[k], "[REDACTED_SECRET]")
                self.assertEqual(tc_args["nested_thinking"]["signature"], "[REDACTED_SECRET]")

        # Verify Anthropic content-list thinking block preserves its signature while redacting tool_use inputs
        ant_block_captured: list[bytes] = []

        def ant_block_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            ant_block_captured.append(body)
            return 200, json.dumps({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 2}}).encode("utf-8")

        ant_block_prov = CloudModelProvider(
            provider_type="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            api_key="sk-ant-test",
            http_transport=ant_block_transport,
        )
        ant_block_prov.generate(
            messages=[{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": f"secret {secret_tok}", "signature": "sk-sig-block-99999999999"},
                    {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"name": secret_tok, "id": gh_tok, "type": gem_tok, "signature": secret_tok}},
                ],
            }],
            tools=tool_spec,
        )
        ant_block_raw = ant_block_captured[0].decode("utf-8")
        self.assertNotIn(secret_tok, ant_block_raw)
        self.assertNotIn(gh_tok, ant_block_raw)
        self.assertNotIn(gem_tok, ant_block_raw)
        ant_blocks = json.loads(ant_block_raw)["messages"][0]["content"]
        self.assertEqual(ant_blocks[0]["signature"], "sk-sig-block-99999999999")
        self.assertEqual(ant_blocks[0]["thinking"], "secret [REDACTED_SECRET]")
        self.assertEqual(ant_blocks[1]["input"]["signature"], "[REDACTED_SECRET]")

    def test_antigravity_and_local_providers_preserve_messages_tools_and_redact(self) -> None:
        """Verify Antigravity and LocalModelProvider (Ollama chat + MLX) preserve ReAct messages and tools."""
        from harness_core.providers.antigravity_provider import AntigravityModelProvider
        from harness_core.providers.local_provider import LocalModelProvider

        secret_tok = "ghp_1234567890abcdefghijklmnop"
        sk_tok = "sk-ant-api03-nestedarg1234567890"
        msgs = [
            {"role": "user", "content": "Inspect Cargo.toml"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "Cargo.toml", "name": sk_tok, "id": secret_tok, "type": sk_tok}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": f"token={secret_tok}; name='hex'"},
        ]
        tools = [{"name": "read_file", "description": "Read file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]

        # 1. Antigravity delegation via CloudModelProvider
        ag_captured: dict = {}

        def ag_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            ag_captured["body"] = body.decode("utf-8")
            ag_captured["json"] = json.loads(ag_captured["body"])
            return 200, json.dumps({
                "response": {
                    "candidates": [{"content": {"parts": [{"functionCall": {"name": "read_file", "args": {"path": "README.md"}}}]}}],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
                }
            }).encode("utf-8")

        ag = AntigravityModelProvider(model_id="gemini-2.5-pro", access_token="ya29.test", project_id="proj-1", http_transport=ag_transport)
        ag_cloud = CloudModelProvider(provider_type="antigravity", model_id="gemini-2.5-pro", api_key="ya29.test", http_transport=ag_transport)
        ag_cloud._antigravity_delegate = ag
        ag_resp = ag_cloud.generate(messages=msgs, tools=tools, tool_choice="required", system_prompt="System")
        self.assertNotIn(secret_tok, ag_captured["body"])
        self.assertNotIn(sk_tok, ag_captured["body"])
        self.assertIn("[REDACTED_SECRET]", ag_captured["body"])
        inner_req = ag_captured["json"]["request"]
        self.assertEqual(len(inner_req["contents"]), 3)
        self.assertEqual(inner_req["tools"][0]["functionDeclarations"][0]["name"], "read_file")
        self.assertEqual(inner_req["toolConfig"], {"functionCallingConfig": {"mode": "ANY"}})
        self.assertEqual(ag_resp.tool_calls[0]["name"], "read_file")
        self.assertEqual(ag_resp.tool_calls[0]["arguments"], {"path": "README.md"})

        # 2. Local Ollama /api/chat path
        local = LocalModelProvider(model_id="qwen2.5-coder:7b")
        ollama_calls: list[tuple[str, dict]] = []

        class _FakeResp:
            def __init__(self, status: int, data: bytes) -> None:
                self.status = status
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=30):
            body_obj = json.loads(req.data.decode("utf-8"))
            ollama_calls.append((req.full_url, body_obj))
            return _FakeResp(200, json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "list_files", "arguments": {"path": "."}}}],
                },
                "prompt_eval_count": 15,
                "eval_count": 7,
            }).encode("utf-8"))

        with patch.object(local, "is_ollama_available", return_value=True), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ol_resp = local.generate(messages=msgs, tools=tools, system_prompt="Be concise")
        self.assertEqual(len(ollama_calls), 1)
        self.assertTrue(ollama_calls[0][0].endswith("/api/chat"))
        sent_ollama = json.dumps(ollama_calls[0][1])
        self.assertNotIn(secret_tok, sent_ollama)
        self.assertNotIn(sk_tok, sent_ollama)
        self.assertIn("[REDACTED_SECRET]", sent_ollama)
        self.assertTrue(any(m.get("role") == "tool" for m in ollama_calls[0][1]["messages"]))
        self.assertEqual(ollama_calls[0][1]["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(ol_resp.tool_calls[0]["name"], "list_files")
        self.assertEqual(ol_resp.tool_calls[0]["arguments"], {"path": "."})

        # 3. Local MLX multi-turn fallback formatting
        mlx_prompts: list[str] = []

        def fake_mlx_gen(model, tokenizer, prompt: str, **kwargs):
            mlx_prompts.append(prompt)
            return "MLX reply"

        with patch.object(local, "is_ollama_available", return_value=False), \
             patch.object(local, "is_mlx_available", return_value=True), \
             patch.object(local, "load_model", return_value=(object(), object())), \
             patch.dict("sys.modules", {"mlx_lm": MagicMock(generate=fake_mlx_gen)}):
            mlx_resp = local.generate(messages=msgs, tools=tools, system_prompt="Be concise")
        self.assertEqual(mlx_resp.text, "MLX reply")
        self.assertEqual(len(mlx_prompts), 1)
        self.assertNotIn(secret_tok, mlx_prompts[0])
        self.assertNotIn(sk_tok, mlx_prompts[0])
        self.assertIn("[REDACTED_SECRET]", mlx_prompts[0])
        self.assertIn("name='hex'", mlx_prompts[0])
        self.assertIn("read_file", mlx_prompts[0])


if __name__ == "__main__":
    unittest.main()
