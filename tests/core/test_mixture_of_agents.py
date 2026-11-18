"""Unit and integration tests for Mixture-of-Agents (MoA) synthesis engine."""

from __future__ import annotations

import threading
import time
from typing import Any
import pytest

from harness_core.core.models import TaskSubmitRequest
from harness_core.core.moa import (
    MixtureOfAgents,
    MoAConfig,
    MoAResult,
    ProposerConfig,
    ProposerOutput,
)
from harness_core.core.react_loop import ReActConfig, ReActLoop, ReActTurn
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.storage.db import LedgerStore


class TestMixtureOfAgents:
    """Tests for MixtureOfAgents core engine."""

    def test_parallel_proposer_execution_and_synthesis(self):
        """Test parallel proposer querying and aggregator prompt synthesis."""
        call_log = []

        def mock_caller(messages, proposer: ProposerConfig | None = None):
            model_name = proposer.model if proposer else "unknown"
            call_log.append(model_name)
            if model_name == "proposer-1":
                time.sleep(0.02)
                return "Proposal from model 1: Use binary search."
            elif model_name == "proposer-2":
                time.sleep(0.01)
                return "Proposal from model 2: Use hash map lookup."
            elif model_name == "aggregator":
                return "<finish>Synthesized best solution: Combined hash map and binary search.</finish>"
            return f"Response from {model_name}"

        cfg = MoAConfig(
            proposers=[
                ProposerConfig(model="proposer-1", temperature=0.7),
                ProposerConfig(model="proposer-2", temperature=0.5),
            ],
            aggregator=ProposerConfig(model="aggregator", temperature=0.2),
            min_proposers_required=1,
            max_parallel_workers=2,
        )

        moa = MixtureOfAgents(cfg, model_caller=mock_caller)
        res: MoAResult = moa.synthesize(prompt="Find optimal search algorithm")

        assert not res.fallback_used
        assert res.aggregator_model == "aggregator"
        assert len(res.proposer_outputs) == 2
        assert res.proposer_outputs[0].model == "proposer-1"
        assert "binary search" in res.proposer_outputs[0].content
        assert res.proposer_outputs[1].model == "proposer-2"
        assert "hash map" in res.proposer_outputs[1].content
        assert "Combined hash map and binary search" in res.synthesized_text
        assert res.total_tokens > 0
        assert res.total_latency_ms > 0

        # Verify both proposers and the aggregator were called
        assert "proposer-1" in call_log
        assert "proposer-2" in call_log
        assert "aggregator" in call_log

    def test_partial_proposer_failure_resilience(self):
        """Test that MoA proceeds with valid proposers when one proposer fails."""
        def mock_caller(messages, proposer: ProposerConfig | None = None):
            model_name = proposer.model if proposer else "unknown"
            if model_name == "failing-proposer":
                raise RuntimeError("503 Service Unavailable")
            elif model_name == "working-proposer":
                return "Valid proposal from working model."
            elif model_name == "aggregator":
                return "Aggregated response using working model proposal."
            return "ok"

        cfg = MoAConfig(
            proposers=[
                ProposerConfig(model="failing-proposer"),
                ProposerConfig(model="working-proposer"),
            ],
            aggregator=ProposerConfig(model="aggregator"),
            min_proposers_required=1,
        )

        moa = MixtureOfAgents(cfg, model_caller=mock_caller)
        res = moa.synthesize(prompt="Solve problem")

        assert not res.fallback_used
        assert len(res.proposer_outputs) == 2
        assert res.proposer_outputs[0].is_error
        assert "503 Service Unavailable" in (res.proposer_outputs[0].error_message or "")
        assert not res.proposer_outputs[1].is_error
        assert "Valid proposal" in res.proposer_outputs[1].content
        assert "Aggregated response" in res.synthesized_text

    def test_timeout_proposer_handled_gracefully(self):
        """Test that a proposer exceeding its timeout is flagged as an error without crashing."""
        def mock_caller(messages, proposer: ProposerConfig | None = None):
            model_name = proposer.model if proposer else "unknown"
            if model_name == "slow-proposer":
                time.sleep(0.15)
                return "Slow response"
            elif model_name == "fast-proposer":
                return "Fast response"
            return "Aggregated output"

        cfg = MoAConfig(
            proposers=[
                ProposerConfig(model="slow-proposer", timeout_seconds=0.05),
                ProposerConfig(model="fast-proposer", timeout_seconds=2.0),
            ],
            aggregator=ProposerConfig(model="aggregator"),
            min_proposers_required=1,
        )

        moa = MixtureOfAgents(cfg, model_caller=mock_caller)
        res = moa.synthesize(prompt="Quick query")

        assert len(res.proposer_outputs) == 2
        slow_out = next(p for p in res.proposer_outputs if p.model == "slow-proposer")
        fast_out = next(p for p in res.proposer_outputs if p.model == "fast-proposer")

        assert slow_out.is_error
        assert "Timeout" in (slow_out.error_message or "")
        assert not fast_out.is_error
        assert "Fast response" in fast_out.content

    def test_fallback_when_all_proposers_fail(self):
        """Test fallback directly to primary aggregator when all proposers fail."""
        def mock_caller(messages, proposer: ProposerConfig | None = None):
            model_name = proposer.model if proposer else "unknown"
            if model_name.startswith("proposer"):
                raise ConnectionResetError("Connection refused")
            return "Direct aggregator fallback response"

        cfg = MoAConfig(
            proposers=[
                ProposerConfig(model="proposer-1"),
                ProposerConfig(model="proposer-2"),
            ],
            aggregator=ProposerConfig(model="aggregator"),
            min_proposers_required=1,
        )

        moa = MixtureOfAgents(cfg, model_caller=mock_caller)
        res = moa.synthesize(prompt="Help with architecture")

        assert res.fallback_used
        assert all(p.is_error for p in res.proposer_outputs)
        assert res.synthesized_text == "Direct aggregator fallback response"

    def test_build_aggregator_prompt_structure(self):
        """Test that build_aggregator_prompt constructs correct authoritative prompt."""
        cfg = MoAConfig()
        moa = MixtureOfAgents(cfg)

        proposers = [
            ProposerOutput(
                candidate_index=0,
                model="claude-3-5-sonnet",
                content="Consider microservices architecture.",
                latency_ms=120.0,
                tokens_used=45,
            ),
            ProposerOutput(
                candidate_index=1,
                model="gpt-4o",
                content="Modular monolith is simpler and lower latency.",
                latency_ms=95.0,
                tokens_used=50,
            ),
            ProposerOutput(
                candidate_index=2,
                model="failed-model",
                content="",
                latency_ms=10.0,
                tokens_used=0,
                is_error=True,
            ),
        ]

        messages = moa.build_aggregator_prompt("System design question", proposers)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "Synthesize the most accurate, cohesive, and complete solution" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert "claude-3-5-sonnet" in messages[1]["content"]
        assert "Consider microservices architecture." in messages[1]["content"]
        assert "gpt-4o" in messages[1]["content"]
        assert "Modular monolith" in messages[1]["content"]
        # Error candidate should not be in the candidate body
        assert "failed-model" not in messages[1]["content"]

    def test_react_loop_with_moa_integration(self, tmp_path):
        """Test ReActLoop executing turns routed through MixtureOfAgents."""
        db_path = tmp_path / "ledger.db"
        ledger = LedgerStore(db_path)
        task_rec = ledger.submit_task(
            TaskSubmitRequest(
                goal="Perform complex reasoning task",
                workspace_root=str(tmp_path),
                execution_mode="react_loop",
            )
        )
        task_id = task_rec["task_id"]

        def mock_caller(messages, proposer: ProposerConfig | None = None):
            model = proposer.model if proposer else "unknown"
            if model == "prop-a":
                return "Proposer A says write the file."
            elif model == "prop-b":
                return "Proposer B agrees."
            # Aggregator: emits final finish
            return '<finish>Successfully completed task via MoA synthesis</finish>'

        moa_cfg = MoAConfig(
            proposers=[
                ProposerConfig(model="prop-a"),
                ProposerConfig(model="prop-b"),
            ],
            aggregator=ProposerConfig(model="aggregator"),
            enabled=True,
        )

        react_cfg = ReActConfig(
            max_turns=3,
            moa_config=moa_cfg,
        )

        loop = ReActLoop(
            model_caller=mock_caller,
            config=react_cfg,
            ledger=ledger,
            workspace_root=tmp_path,
        )

        res = loop.run(prompt="Perform complex reasoning task", task_id=task_id)
        assert res.success
        assert len(res.turns) == 1
        turn = res.turns[0]
        assert turn.moa_result is not None
        assert turn.moa_result.aggregator_model == "aggregator"
        assert len(turn.moa_result.proposer_outputs) == 2
        assert "Successfully completed task via MoA synthesis" in res.final_answer

        # Verify ledger event recorded
        cur = ledger.conn.cursor()
        cur.execute("SELECT event_type, payload_json FROM task_events WHERE task_id = ?", (task_id,))
        events = cur.fetchall()
        assert any(row[0] == "MOA_SYNTHESIS_COMPLETED" for row in events)

    def test_orchestrator_run_agent_loop_wires_moa(self, tmp_path):
        """Test HarnessOrchestrator.run_agent_loop passes moa_config to ReActLoop."""
        data_dir = tmp_path / "data"
        ws_dir = tmp_path / "ws"
        orchestrator = HarnessOrchestrator(data_dir=data_dir, default_workspace=ws_dir)

        moa_cfg = MoAConfig(
            proposers=[ProposerConfig(model="p1")],
            aggregator=ProposerConfig(model="agg"),
            enabled=True,
        )

        orchestrator.router = None
        # Deterministic execution is explicitly opt-in.
        res = orchestrator.run_agent_loop(
            goal="Test MoA orchestrator wiring",
            demo_mode=True,
            workspace_root=ws_dir,
            max_turns=2,
            moa_config=moa_cfg,
        )

        assert "turns" in res
        assert len(res["turns"]) >= 1
        first_turn = res["turns"][0]
        # In dict form, moa_result is present
        assert "moa_result" in first_turn
        assert first_turn["moa_result"] is not None
        assert first_turn["moa_result"]["aggregator_model"] == "agg"


class TestMoARouteAndDeadlineContract:
    def test_caller_body_typeerror_is_not_retried(self):
        calls = []

        def caller(messages, proposer):
            calls.append(proposer.model)
            if proposer.model == "broken":
                raise TypeError("caller body failed")
            return "synthesis"

        result = MixtureOfAgents(
            MoAConfig(proposers=[ProposerConfig("broken")], aggregator=ProposerConfig("agg")),
            model_caller=caller,
        ).synthesize("objective")
        assert calls == ["broken", "agg"]
        assert result.proposer_outputs[0].error_message == "caller body failed"

    def test_route_blind_caller_is_labeled(self):
        result = MixtureOfAgents(
            MoAConfig(proposers=[ProposerConfig("p")]), model_caller=lambda messages: "answer",
        ).synthesize("objective")
        assert not result.proposer_outputs[0].route_applied
        assert not result.to_dict()["proposer_outputs"][0]["route_applied"]
        assert not result.aggregator_route_applied

    def test_hung_proposer_does_not_delay_aggregation(self):
        release = threading.Event()

        def caller(messages, proposer):
            if proposer.model == "hung":
                release.wait(2)
            return "answer"

        moa = MixtureOfAgents(MoAConfig(
            proposers=[ProposerConfig("hung", timeout_seconds=0.04), ProposerConfig("fast")],
            aggregator=ProposerConfig("agg"),
        ), model_caller=caller)
        start = time.monotonic()
        try:
            result = moa.synthesize("objective")
            assert time.monotonic() - start < 0.5
            assert result.proposer_outputs[0].is_error
            assert not result.proposer_outputs[1].is_error
            assert result.synthesized_text == "answer"
        finally:
            release.set()

    def test_timeouts_are_measured_from_fanout_not_sequential_waits(self):
        release = threading.Event()

        def caller(messages, proposer):
            release.wait(2)
            return "answer"

        moa = MixtureOfAgents(MoAConfig(proposers=[
            ProposerConfig("a", timeout_seconds=0.08),
            ProposerConfig("b", timeout_seconds=0.08),
            ProposerConfig("c", timeout_seconds=0.08),
        ]), model_caller=caller)
        start = time.monotonic()
        try:
            outputs = moa.query_proposers("objective")
            assert time.monotonic() - start < 0.18
            assert all(p.is_error for p in outputs)
        finally:
            release.set()

    def test_overall_deadline_includes_aggregator(self):
        from harness_core.core.moa import MoAAggregationError

        release = threading.Event()
        moa = MixtureOfAgents(
            MoAConfig(timeout_seconds=0.04), model_caller=lambda messages, proposer: release.wait(2),
        )
        start = time.monotonic()
        try:
            with pytest.raises(MoAAggregationError, match="Timeout"):
                moa.synthesize("objective")
            assert time.monotonic() - start < 0.5
        finally:
            release.set()

    def test_cancel_during_proposer_wait_skips_queued_calls(self):
        from harness_core.core.moa import MoACancelledError

        canceled = threading.Event()
        release = threading.Event()
        calls = []

        def caller(messages, proposer):
            calls.append(proposer.model)
            canceled.set()
            release.wait(2)
            return "late answer"

        moa = MixtureOfAgents(MoAConfig(
            proposers=[ProposerConfig("running"), ProposerConfig("queued")],
            max_parallel_workers=1,
        ), model_caller=caller)
        start = time.monotonic()
        try:
            with pytest.raises(MoACancelledError):
                moa.synthesize("objective", cancel_check=canceled.is_set)
            assert time.monotonic() - start < 0.5
            assert calls == ["running"]
        finally:
            release.set()

    @pytest.mark.parametrize("answer", ["", None])
    def test_aggregator_failure_is_explicit(self, answer):
        from harness_core.core.moa import MoAAggregationError

        def caller(messages, proposer):
            if answer is None:
                raise RuntimeError("aggregator unavailable")
            return answer

        with pytest.raises(MoAAggregationError):
            MixtureOfAgents(MoAConfig(), model_caller=caller).synthesize("objective")

    def test_react_preserves_dedicated_moa_caller_and_tool_protocol(self, tmp_path):
        calls = []

        def caller(messages, proposer):
            calls.append(proposer)
            assert messages[0] == {"role": "system", "content": "Keep the tool protocol"}
            return "<finish>answer</finish>"

        def wrong_caller(messages):
            pytest.fail("ReAct clobbered the route-aware caller")

        route = ProposerConfig("p", temperature=0.4, provider="local", max_tokens=123)
        loop = ReActLoop(
            model_caller=wrong_caller, moa_caller=caller, workspace_root=tmp_path,
            config=ReActConfig(system_prompt="Keep the tool protocol", moa_config=MoAConfig(proposers=[route])),
        )
        result = loop.run("objective")
        assert result.success
        assert calls[0] is route
        assert result.final_answer == "answer"

    def test_react_reports_moa_cancellation(self, tmp_path):
        canceled = threading.Event()
        release = threading.Event()

        def caller(messages, proposer):
            canceled.set()
            release.wait(2)
            return "<finish>late answer</finish>"

        loop = ReActLoop(
            model_caller=caller, cancel_check=canceled.is_set, workspace_root=tmp_path,
            config=ReActConfig(moa_config=MoAConfig(proposers=[ProposerConfig("p")])),
        )
        try:
            result = loop.run("objective")
            assert not result.success
            assert result.termination_reason == "canceled"
            assert not result.final_answer
        finally:
            release.set()

    def test_orchestrator_routes_model_provider_temperature_and_tokens(self, tmp_path):
        from harness_core.core.router import TwoTierModelRouter
        from harness_core.providers.base import ModelResponse, ModelUsage, ProviderUnavailableError, PolicyBlockedError

        calls = []
        barrier = threading.Barrier(2)

        class Provider:
            def __init__(self, provider_id, model_id):
                self.provider_id = provider_id
                self.model_id = model_id

            def is_available(self):
                return True

            def generate(self, prompt, **kwargs):
                if self.model_id in ("cloud-p", "local-p"):
                    barrier.wait(timeout=2)
                calls.append((self.provider_id, self.model_id, kwargs))
                return ModelResponse("<finish>answer</finish>", self.model_id, self.provider_id, ModelUsage())

        local = Provider("local_ai", "local-primary")
        cloud = Provider("cloud_test", "cloud-primary")
        router = TwoTierModelRouter(local_provider=local, cloud_provider=cloud, cloud_model_id=cloud.model_id)
        orch = HarnessOrchestrator(data_dir=tmp_path / "data", default_workspace=tmp_path / "ws")
        orch.router = router
        try:
            result = orch.run_agent_loop("objective", moa_config=MoAConfig(proposers=[
                ProposerConfig("cloud-p", temperature=0.3, provider="cloud_test", max_tokens=321),
                ProposerConfig("local-p", temperature=0.8, provider="local", max_tokens=789),
            ]), demo_mode=False)
            assert result["success"]
            routed = {model: (provider, kwargs) for provider, model, kwargs in calls}
            assert routed["cloud-p"][0] == "cloud_test"
            assert routed["cloud-p"][1]["temperature"] == 0.3
            assert routed["cloud-p"][1]["max_tokens"] == 321
            assert routed["local-p"][0] == "local_ai"
            assert routed["local-p"][1]["temperature"] == 0.8
            assert routed["local-p"][1]["max_tokens"] == 789
            assert "cloud-primary" in routed
            assert cloud.model_id == "cloud-primary"
            assert local.model_id == "local-primary"
            with pytest.raises(ProviderUnavailableError, match="not configured"):
                router.generate_for_route(prompt="objective", provider="unknown")
            with pytest.raises(PolicyBlockedError):
                router.generate_for_route(prompt="objective", data_classification="secret")
        finally:
            orch.ledger.close()
