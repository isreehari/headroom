"""Regression tests for evidence, admission and accounting boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import respx

from headroom.proxy import jev


def candidate(**overrides):
    return replace(
        jev.JevCandidate("t1", "Read", {"path": "app.py"}, 1200, 18, 300),
        **overrides,
    )


async def plan(planner, **overrides):
    args = {
        "provider": "openai",
        "model": "gpt-5",
        "session_id": "session",
        "branch_id": "main",
        "revision": "revision-1",
        "goal": "Fix the test",
        "candidates": [candidate()],
    }
    args.update(overrides)
    return await planner.plan(**args)


def response(probability=0.01, **extra):
    return httpx.Response(
        200,
        json={
            "answers": {"call_t1": {"noul": probability}, "result_t1": {"noul": probability}},
            **extra,
        },
    )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "probability,reason", [(0.49, "uncertain"), (0.01, "insufficient_evidence")]
)
@pytest.mark.parametrize("mode", ["shadow", "active"])
async def test_missing_evidence_or_uncertainty_never_authorizes_removal(probability, reason, mode):
    respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response(probability))
    planner = jev.JevPlanner(jev.JevConfig(mode=mode, api_key="test"))
    result = await plan(planner)
    assert result.decisions[0].action == "keep"
    assert result.decisions[0].reason == reason
    assert result.projected_tokens_saved == 0
    assert planner.stats.snapshot(planner.config)["abstentions"][reason] == 1


def test_payload_does_not_invent_success_and_questions_reference_explicit_paths():
    payload = jev.build_jev_payload(
        provider="openai",
        model="gpt-5",
        session_id="s",
        branch_id="b",
        revision="r",
        goal="Fix it",
        candidates=[candidate()],
    )
    tool = payload["state"]["history"][0]["tool_calls"][0]
    assert tool["result"] == "1200 chars (omitted; status unknown)"
    for question in payload["questions"].values():
        assert "`history[0].tool_calls[0]" in question["instructions"]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("field", ["goal", "input"])
async def test_budget_includes_serialized_goal_inputs_and_questions(field):
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(
        jev.JevConfig(mode="shadow", api_key="test", max_candidate_tokens=2000)
    )
    overrides = (
        {"goal": "x" * 3000}
        if field == "goal"
        else {"candidates": [candidate(tool_input={"payload": "x" * 3000})]}
    )
    result = await plan(planner, **overrides)
    assert result.fallback_reason == "request_budget_exceeded"
    assert not result.called
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_original_result_size_is_not_the_remote_request_budget():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    result = await plan(planner, candidates=[candidate(result_tokens=50000, result_chars=200000)])
    assert result.called
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_cooldown_and_retries_do_not_make_extra_calls_or_repeat_accounting():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test", cooldown_turns=2))
    assert (await plan(planner)).called
    assert (await plan(planner)).fallback_reason == "duplicate_revision"
    assert (await plan(planner, revision="r2")).fallback_reason == "cooldown"
    assert (await plan(planner, revision="r2")).fallback_reason == "duplicate_revision"
    assert (await plan(planner, revision="r3")).fallback_reason == "cooldown"
    assert (await plan(planner, revision="r4")).called
    assert (await plan(planner)).fallback_reason == "duplicate_revision"
    assert route.call_count == 2
    stats = planner.stats.snapshot(planner.config)
    assert stats["calls_completed"] == 2
    assert stats["skips"]["cooldown"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_retry_is_admitted_only_once():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    results = await asyncio.gather(plan(planner), plan(planner))
    assert sum(result.called for result in results) == 1
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_missing_session_identity_skips_remote_call():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    assert (await plan(planner, session_id="")).fallback_reason == "identity_unavailable"
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_api_usage_is_preserved_even_when_decisions_are_invalid():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {},
                "model": "jev-test",
                "usage": {"input_tokens": 123, "output_tokens": 7},
            },
        )
    )
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    result = await plan(planner)
    assert result.fallback_reason == "invalid_response"
    assert result.input_tokens == 123
    assert result.output_tokens == 7
    assert result.response_model == "jev-test"
    usage = planner.stats.snapshot(planner.config)["api_usage"]
    assert usage == {"input_tokens": 123, "output_tokens": 7, "known_calls": 1, "unknown_calls": 0}
    assert "authorization" not in route.calls[0].request.content.decode()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": -1, "output_tokens": 2},
    ],
)
async def test_unknown_or_invalid_usage_is_not_reported_as_zero(usage):
    respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response(usage=usage))
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    result = await plan(planner)
    assert result.input_tokens is None
    assert planner.stats.snapshot(planner.config)["api_usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "known_calls": 0,
        "unknown_calls": 1,
    }


@pytest.mark.asyncio
async def test_deadline_wraps_the_whole_await_not_just_http_phases():
    class SlowClient:
        async def ask(self, state, questions):
            await asyncio.sleep(1)

    planner = jev.JevPlanner(
        jev.JevConfig(mode="shadow", api_key="test", timeout_ms=10), client=SlowClient()
    )
    result = await plan(planner)
    assert result.fallback_reason == "timeout"
    assert result.called


@pytest.mark.asyncio
@respx.mock
async def test_expired_before_dispatch_does_not_count_an_api_attempt(monkeypatch):
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    times = iter([0.0, 1.0, 1.0])
    monkeypatch.setattr(jev, "time", SimpleNamespace(perf_counter=lambda: next(times)))
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test", timeout_ms=10))
    result = await plan(planner)
    assert result.fallback_reason == "timeout"
    assert not result.called
    assert not route.called
    assert planner.stats.snapshot(planner.config)["calls_attempted"] == 0


def test_failed_active_commit_never_counts_applied_savings():
    stats = jev.JevStats()
    stats.record_active(
        jev.JevApplyResult([], applied_tokens=500, fallback_reason="session_revision_changed")
    )
    assert stats.snapshot(jev.JevConfig())["active_applied_tokens"] == 0


def test_latency_excludes_skips_and_uses_all_attempted_calls():
    stats = jev.JevStats()
    stats.record(jev.JevPlan(called=True), 10)
    stats.record(jev.JevPlan(called=True, fallback_reason="timeout"), 100)
    stats.record(jev.JevPlan(fallback_reason="cooldown"), 50)
    assert stats.snapshot(jev.JevConfig())["average_latency_ms"] == 55


@pytest.mark.asyncio
@respx.mock
async def test_admission_isolated_by_session_and_branch():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    assert (await plan(planner)).called
    assert (await plan(planner, branch_id="fork")).called
    assert (await plan(planner, session_id="other")).called
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_admission_capacity_does_not_evict_retry_protection(monkeypatch):
    monkeypatch.setattr(jev, "JEV_MAX_TRACKED_SESSIONS", 1)
    monkeypatch.setattr(jev, "JEV_MAX_TRACKED_REVISIONS", 1)
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=response())
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    assert (await plan(planner)).called
    assert (await plan(planner, revision="r2")).fallback_reason == "admission_capacity"
    assert (await plan(planner, session_id="new")).fallback_reason == "admission_capacity"
    assert (await plan(planner)).fallback_reason == "duplicate_revision"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_failed_remote_attempt_still_enforces_cooldown():
    route = respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(return_value=httpx.Response(503))
    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"))
    assert (await plan(planner)).fallback_reason == "http_error"
    assert (await plan(planner, revision="r2")).fallback_reason == "cooldown"
    assert route.call_count == 1


@pytest.mark.parametrize("failure", ["ccr_write_failed", "active_no_token_reduction"])
def test_active_failure_is_returned_and_does_not_inflate_stats(monkeypatch, failure):
    from fastapi.testclient import TestClient

    from headroom.proxy.models import ProxyConfig
    from headroom.proxy.server import create_app

    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")

    async def fake_plan(self, **kwargs):
        return jev.JevPlan(called=True)

    def fake_apply(**kwargs):
        messages = list(kwargs["messages"])
        if failure == "active_no_token_reduction":
            messages.append({"role": "assistant", "content": "extra tokens " * 10000})
        return jev.JevApplyResult(
            messages,
            applied_tokens=999,
            fallback_reason=failure if failure == "ccr_write_failed" else None,
        )

    monkeypatch.setattr(jev.JevPlanner, "plan", fake_plan)
    monkeypatch.setattr("headroom.proxy.handlers.openai.apply_active_decisions", fake_apply)
    app = create_app(
        ProxyConfig(
            jev_mode="active",
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
    )
    messages = [{"role": "user", "content": "Keep this output " * 500}]
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        result = client.post(
            "/v1/compress",
            json={
                "messages": messages,
                "model": "gpt-5",
                "token_budget": 10,
                "config": {"mode": "ccr", "session_id": "test", "jev_compaction_boundary": True},
            },
        )
        stats = client.get("/stats").json()["jev"]
    assert result.status_code == 200, result.text
    assert result.json()["jev"]["fallback_reason"] == failure
    assert result.json()["messages"] == messages
    assert stats["active_applied_tokens"] == 0


@pytest.mark.asyncio
async def test_late_response_keeps_usage_but_does_not_count_stale_decisions():
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedClient:
        async def ask(self, state, questions):
            started.set()
            await release.wait()
            return jev.JevResponse(
                {"call_t1": {"noul": 0.01}, "result_t1": {"noul": 0.01}},
                input_tokens=123,
                output_tokens=7,
            )

    planner = jev.JevPlanner(jev.JevConfig(mode="shadow", api_key="test"), client=DelayedClient())
    first = asyncio.create_task(plan(planner))
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        assert (await plan(planner, revision="newer")).fallback_reason == "cooldown"
    finally:
        release.set()
    result = await first
    assert result.fallback_reason == "stale_revision"
    assert not result.decisions
    stats = planner.stats.snapshot(planner.config)
    assert stats["abstentions"] == {}
    assert stats["api_usage"]["input_tokens"] == 123


@respx.mock
@pytest.mark.parametrize("malformed", [False, True])
def test_compress_exposes_usage_and_model_even_for_invalid_decisions(monkeypatch, malformed):
    from fastapi.testclient import TestClient

    from headroom.proxy.models import ProxyConfig
    from headroom.proxy.server import create_app

    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")
    respx.post(jev.DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {}
                if malformed
                else {"call_t1": {"noul": 0.01}, "result_t1": {"noul": 0.01}},
                "usage": {"input_tokens": 123, "output_tokens": 7},
                "model": "jev-test",
            },
        )
    )
    app = create_app(
        ProxyConfig(
            jev_mode="shadow",
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
    )
    messages = [
        {"role": "user", "content": "Read the file"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "file contents " * 100},
        *[{"role": "user", "content": "Continue"} for _ in range(8)],
    ]
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        result = client.post(
            "/v1/compress",
            headers={"x-headroom-session-id": "s"},
            json={
                "messages": messages,
                "model": "gpt-5",
                "token_budget": 10,
            },
        )
    assert result.status_code == 200, result.text
    report = result.json()["jev"]
    assert report["called"]
    assert report["input_tokens"] == 123
    assert report["output_tokens"] == 7
    assert report["response_model"] == "jev-test"
    assert report["fallback_reason"] == ("invalid_response" if malformed else None)
