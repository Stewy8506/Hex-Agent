"""Comprehensive unit test suite for local server socket and model introspection."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx
import pytest

from harness_core.models.credential_pool import (
    _clear_endpoint_blackholes,
    _endpoint_blackholed,
)
from harness_core.models.model_metadata import (
    _clear_endpoint_model_metadata_cache,
    get_model_context_length,
    probe_local_server,
)


@pytest.fixture(autouse=True)
def clean_state():
    _clear_endpoint_model_metadata_cache()
    _clear_endpoint_blackholes()
    yield
    _clear_endpoint_model_metadata_cache()
    _clear_endpoint_blackholes()


class TestLocalSocketIntrospection(unittest.TestCase):
    def setUp(self):
        _clear_endpoint_model_metadata_cache()
        _clear_endpoint_blackholes()

    def tearDown(self):
        _clear_endpoint_model_metadata_cache()
        _clear_endpoint_blackholes()

    def test_probe_ollama_num_ctx(self):
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "parameters": "parameter num_ctx 32768\nstop \"<|im_end|>\"",
            "model_info": {"general.context_length": 8192},
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = mock_response

            res = probe_local_server("http://localhost:11434", model="llama3:latest")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "ollama")
            self.assertEqual(res["context_length"], 32768)
            self.assertEqual(res["model"], "llama3:latest")

    def test_probe_ollama_gguf_context_fallback(self):
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "parameters": "stop \"<|im_end|>\"",
            "model_info": {"llama.context_length": 65536},
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = mock_response

            res = probe_local_server("http://localhost:11434", model="qwen2.5:32b")
            self.assertIsNotNone(res)
            self.assertEqual(res["context_length"], 65536)

    def test_probe_ollama_tags_and_ps_discovery(self):
        tags_resp = MagicMock(status_code=200)
        tags_resp.json.return_value = {"models": [{"name": "llama3:8b"}]}
        ps_resp = MagicMock(status_code=200)
        ps_resp.json.return_value = {"models": [{"name": "mistral:7b"}]}
        show_resp = MagicMock(status_code=200)
        show_resp.json.return_value = {"parameters": "num_ctx 16384"}

        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.get.side_effect = lambda url: ps_resp if "/api/ps" in url else tags_resp
            client.post.return_value = show_resp

            res = probe_local_server("http://localhost:11434")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "ollama")
            self.assertEqual(res["context_length"], 16384)
            self.assertEqual(res["model"], "mistral:7b")

    def test_probe_lmstudio_v0_models(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": "qwen2.5-coder-7b",
                    "state": "loaded",
                    "loaded_context_length": 32768,
                    "max_context_length": 65536,
                }
            ]
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = MagicMock(status_code=404)
            client.get.side_effect = lambda url: mock_resp if "/api/v0/models" in url else MagicMock(status_code=404)

            res = probe_local_server("http://localhost:1234", model="qwen2.5-coder-7b")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "lm-studio")
            self.assertEqual(res["context_length"], 32768)

    def test_probe_lmstudio_v1_loaded_instances(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "models": [
                {
                    "id": "deepseek-coder",
                    "loaded_instances": [
                        {"config": {"context_length": 24576}}
                    ],
                }
            ]
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = MagicMock(status_code=404)
            client.get.side_effect = lambda url: mock_resp if "/api/v1/models" in url else MagicMock(status_code=404)

            res = probe_local_server("http://localhost:1234", model="deepseek-coder")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "lm-studio")
            self.assertEqual(res["context_length"], 24576)

    def test_probe_llamacpp_props(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "default_generation_settings": {
                "n_ctx": 16384,
                "params": {},
            },
            "model_alias": "hermes-3-llama-3.1-8b",
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = MagicMock(status_code=404)
            client.get.side_effect = lambda url: mock_resp if "/props" in url else MagicMock(status_code=404)

            res = probe_local_server("http://localhost:8080")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "llamacpp")
            self.assertEqual(res["context_length"], 16384)

    def test_probe_vllm_models(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "object": "list",
            "data": [
                {
                    "id": "meta-llama/Llama-3-8B-Instruct",
                    "owned_by": "vllm",
                    "max_model_len": 65536,
                }
            ],
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = MagicMock(status_code=404)
            client.get.side_effect = lambda url: mock_resp if url.endswith("/v1/models") else MagicMock(status_code=404)

            res = probe_local_server("http://localhost:8000", model="meta-llama/Llama-3-8B-Instruct")
            self.assertIsNotNone(res)
            self.assertEqual(res["server_type"], "vllm")
            self.assertEqual(res["context_length"], 65536)

    def test_connect_timeout_triggers_blackhole(self):
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.side_effect = httpx.ConnectTimeout("Connect timed out")
            client.get.side_effect = httpx.ConnectTimeout("Connect timed out")

            # First probe pays connect timeout and blackholes endpoint
            res1 = probe_local_server("http://192.168.1.99:11434", model="llama3")
            self.assertIsNone(res1)
            self.assertTrue(_endpoint_blackholed("http://192.168.1.99:11434"))

            # Second probe short-circuits with zero network I/O
            client.reset_mock()
            res2 = probe_local_server("http://192.168.1.99:11434", model="llama3")
            self.assertIsNone(res2)
            client.post.assert_not_called()
            client.get.assert_not_called()

    def test_probe_result_caching(self):
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "parameters": "num_ctx 8192",
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = mock_response

            res1 = probe_local_server("http://localhost:11434", model="llama3")
            self.assertIsNotNone(res1)
            self.assertEqual(res1["context_length"], 8192)
            self.assertEqual(client.post.call_count, 1)

            # Second call uses cache
            client.post.reset_mock()
            res2 = probe_local_server("http://localhost:11434", model="llama3")
            self.assertIsNotNone(res2)
            self.assertEqual(res2["context_length"], 8192)
            client.post.assert_not_called()

    def test_get_model_context_length_with_base_url(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "default_generation_settings": {
                "n_ctx": 40960,
            },
        }
        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.return_value = MagicMock(status_code=404)
            client.get.side_effect = lambda url: mock_resp if "/props" in url else MagicMock(status_code=404)

            ctx = get_model_context_length("custom-local-model", base_url="http://localhost:8080")
            self.assertEqual(ctx, 40960)
