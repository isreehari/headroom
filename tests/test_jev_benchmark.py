"""Tests for the seeded Jev benchmark accounting."""

from __future__ import annotations

import httpx
import pytest
import respx

from benchmarks.jev_proof_table import (
    apply_jev_actions,
    build_benchmark_messages,
    calculate_provider_projection,
    run_scenario,
)
from headroom.providers.openai_compatible import OpenAICompatibleTokenCounter
from headroom.proxy.jev import DEFAULT_JEV_ENDPOINT, JevConfig, JevPlanner


def _tool_payload() -> list[dict]:
    return [
        {
            "tool": "filesystem_search",
            "result": {"matches": [{"path": "src/app.py", "line": 42}] * 20},
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_run_scenario_separates_headroom_and_jev_savings() -> None:
    respx.post(DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "call_call_0": {"noul": 0.1},
                    "result_call_0": {"noul": 0.1},
                },
                "usage": {"input_tokens": 321, "output_tokens": 12},
                "model": "jev-test",
            },
        )
    )
    planner = JevPlanner(JevConfig(mode="shadow", api_key="test-key"))
    tok = OpenAICompatibleTokenCounter(model="gpt-5.6")

    row = await run_scenario("Example", _tool_payload(), tok, planner)

    assert row.headroom_saved == row.before_tokens - row.after_tokens
    assert row.jev_called is True
    assert row.jev_projected_saved == 0
    assert row.jev_projected_provider_saved == 0
    assert row.jev_input_tokens == 321
    assert row.jev_output_tokens == 12
    assert row.jev_response_model == "jev-test"
    assert row.jev_abstentions == {"insufficient_evidence": 1}
    assert row.jev_applied_saved == 0
    assert row.actual_total_saved == row.headroom_saved


def test_build_messages_puts_tool_results_outside_recent_protection() -> None:
    messages = build_benchmark_messages("Example", _tool_payload())

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[3]["role"] == "tool"
    assert len(messages) > 9


def test_apply_jev_actions_truncates_result_without_orphaning_tool_call() -> None:
    messages = build_benchmark_messages("Example", _tool_payload())
    messages[2]["tool_calls"] = [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": "filesystem_search", "arguments": "{}"},
        }
    ]

    applied = apply_jev_actions(messages, {"call_0": "drop_result"}, truncate_head_chars=12)

    assistant = next(message for message in applied if message.get("role") == "assistant")
    result = next(message for message in applied if message.get("role") == "tool")
    assert len(assistant["tool_calls"]) == 1
    assert result["content"].endswith("[truncated by Jev]")
    assert len(result["content"]) < len(messages[3]["content"])


def test_apply_jev_actions_removes_call_and_matching_result() -> None:
    messages = build_benchmark_messages("Example", _tool_payload())
    messages[2]["tool_calls"] = [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": "filesystem_search", "arguments": "{}"},
        }
    ]

    applied = apply_jev_actions(messages, {"call_0": "drop_call"})

    assert not any(message.get("role") == "tool" for message in applied)
    assert not any(
        call.get("id") == "call_0" for message in applied for call in message.get("tool_calls", [])
    )


def test_provider_projection_counts_only_decisions_and_not_headroom_savings() -> None:
    messages = build_benchmark_messages("Example", _tool_payload())
    tok = OpenAICompatibleTokenCounter(model="gpt-5.6")

    projected = calculate_provider_projection(
        messages,
        tok,
        {"call_0": "drop_result"},
    )

    assert projected > 0


@pytest.mark.asyncio
@respx.mock
async def test_benchmark_latency_is_null_for_skips_and_measured_for_failed_attempts():
    tok = OpenAICompatibleTokenCounter(model="gpt-5.6")
    disabled = await run_scenario("Example", _tool_payload(), tok, JevPlanner(JevConfig()))
    assert disabled.jev_latency_ms is None
    assert disabled.jev_planning_latency_ms >= 0

    respx.post(DEFAULT_JEV_ENDPOINT).mock(return_value=httpx.Response(503))
    planner = JevPlanner(JevConfig(mode="shadow", api_key="test"))
    attempted = await run_scenario("Example", _tool_payload(), tok, planner)
    duplicate = await run_scenario("Example", _tool_payload(), tok, planner)
    changed_payload = [{"tool": "Read", "result": "different"}]
    cooldown = await run_scenario("Example", changed_payload, tok, planner)
    assert attempted.jev_called
    assert attempted.jev_latency_ms >= 0
    assert duplicate.jev_fallback_reason == "duplicate_revision"
    assert duplicate.jev_latency_ms is None
    assert cooldown.jev_fallback_reason == "cooldown"
    assert cooldown.jev_latency_ms is None
