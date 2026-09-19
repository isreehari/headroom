"""Tests for the optional Jev retention decision layer."""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from headroom import settings_store
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev import (
    DEFAULT_JEV_ENDPOINT,
    JevCandidate,
    JevConfig,
    JevDecision,
    JevPlan,
    JevPlanner,
    JevResponse,
    JevStats,
    apply_active_decisions,
    extract_openai_candidates,
    revision_for_messages,
)
from headroom.proxy.models import ProxyConfig
from headroom.proxy.server import _proxy_config_from_env, create_app


def _candidate(**overrides: object) -> JevCandidate:
    values: dict[str, object] = {
        "candidate_id": "t1",
        "tool_name": "Read",
        "tool_input": {"file_path": "src/app.py"},
        "result_chars": 1200,
        "call_tokens": 18,
        "result_tokens": 300,
    }
    values.update(overrides)
    return JevCandidate(**values)


def test_jev_is_off_by_default_and_does_not_need_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_JEV_MODE", raising=False)
    monkeypatch.delenv("HEADROOM_JEV_API_KEY", raising=False)

    config = JevConfig.from_env()

    assert config.mode == "off"
    assert config.api_key is None
    assert config.endpoint == DEFAULT_JEV_ENDPOINT


def test_jev_modes_require_a_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY"):
        JevConfig(mode="shadow").validate()

    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY"):
        JevConfig(mode="active").validate()

    assert JevConfig(mode="active", api_key="local-test-key").validate() is None


def test_config_reads_jev_settings_without_exposing_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "shadow")
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")
    monkeypatch.setenv("HEADROOM_JEV_TIMEOUT_MS", "750")
    monkeypatch.setenv("HEADROOM_JEV_THRESHOLD_PERCENT", "82")

    config = JevConfig.from_env()

    assert config.validate() is None
    assert config.timeout_ms == 750
    assert config.threshold_percent == 82
    assert "local-test-key" not in repr(config)


@pytest.mark.asyncio
@respx.mock
async def test_shadow_planner_uses_jev_contract_without_sending_raw_tool_result() -> None:
    route = respx.post(DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "call_t1": {"noul": 0.1},
                    "result_t1": {"noul": 0.1},
                }
            },
        )
    )
    config = JevConfig(mode="shadow", api_key="local-test-key")
    planner = JevPlanner(config)

    plan = await planner.plan(
        provider="anthropic",
        model="claude-sonnet",
        session_id="session-1",
        branch_id="branch-1",
        revision="revision-1",
        goal="Fix the failing test",
        candidates=[_candidate()],
    )

    assert plan.called is True
    assert plan.fallback_reason is None
    assert plan.decisions[0].action == "keep"
    assert plan.decisions[0].reason == "insufficient_evidence"
    assert plan.projected_tokens_saved == 0

    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer local-test-key"
    assert body["model"] == "jev-latest"
    assert body["state"]["revision"] == "revision-1"
    assert (
        body["state"]["history"][0]["tool_calls"][0]["result"]
        == "1200 chars (omitted; status unknown)"
    )
    assert "SECRET_TOOL_OUTPUT" not in request.content.decode()


@pytest.mark.asyncio
@respx.mock
async def test_shadow_planner_fails_open_on_malformed_jev_response() -> None:
    respx.post(DEFAULT_JEV_ENDPOINT).mock(return_value=httpx.Response(200, json={"bad": True}))
    planner = JevPlanner(JevConfig(mode="shadow", api_key="local-test-key"))

    plan = await planner.plan(
        provider="openai",
        model="gpt-5",
        session_id="session-1",
        branch_id="branch-1",
        revision="revision-1",
        goal="Continue the task",
        candidates=[_candidate()],
    )

    assert plan.called is True
    assert plan.decisions == ()
    assert plan.projected_tokens_saved == 0
    assert plan.fallback_reason == "invalid_response"


@pytest.mark.asyncio
async def test_shadow_planner_skips_when_no_candidates_are_eligible() -> None:
    planner = JevPlanner(JevConfig(mode="shadow", api_key="local-test-key"))

    plan = await planner.plan(
        provider="anthropic",
        model="claude-sonnet",
        session_id="session-1",
        branch_id="branch-1",
        revision="revision-1",
        goal="No-op",
        candidates=[_candidate(protected=True)],
    )

    assert plan.called is False
    assert plan.decisions == ()
    assert plan.fallback_reason == "no_eligible_candidates"


@pytest.mark.parametrize("env_name", ["HEADROOM_JEV_API_KEY", "TYPESAFE_API_KEY"])
def test_live_key_is_never_required_by_default(
    monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    monkeypatch.delenv("HEADROOM_JEV_MODE", raising=False)
    monkeypatch.setenv(env_name, "local-test-key")

    assert JevConfig.from_env().mode == "off"


def test_proxy_config_carries_non_secret_jev_settings() -> None:
    config = ProxyConfig(jev_mode="shadow", jev_threshold_percent=82)

    assert config.jev_mode == "shadow"
    assert config.jev_threshold_percent == 82
    assert "local-test-key" not in repr(config)


def test_jev_config_can_be_derived_from_proxy_config_without_serializing_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")
    proxy_config = ProxyConfig(jev_mode="shadow", jev_timeout_ms=650)

    jev_config = JevConfig.from_proxy_config(proxy_config)

    assert jev_config.mode == "shadow"
    assert jev_config.api_key == "local-test-key"
    assert jev_config.timeout_ms == 650
    assert "local-test-key" not in repr(proxy_config)


def test_proxy_config_from_env_reads_jev_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "shadow")
    monkeypatch.setenv("HEADROOM_JEV_ENDPOINT", "https://jev.example.test/decide")
    monkeypatch.setenv("HEADROOM_JEV_MODEL", "jev-test")
    monkeypatch.setenv("HEADROOM_JEV_TIMEOUT_MS", "600")

    config = _proxy_config_from_env()

    assert config.jev_mode == "shadow"
    assert config.jev_endpoint == "https://jev.example.test/decide"
    assert config.jev_model == "jev-test"
    assert config.jev_timeout_ms == 600


def test_jev_settings_are_available_in_the_existing_file_backed_registry() -> None:
    fields = {field.key: field for field in settings_store.SETTINGS}

    assert fields["jev_mode"].env == "HEADROOM_JEV_MODE"
    assert fields["jev_mode"].choices == ("off", "shadow", "active")
    assert fields["jev_api_key"].env == "HEADROOM_JEV_API_KEY"
    assert fields["jev_api_key"].secret is True


def test_jev_api_key_is_masked_by_settings_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))

    settings_store.save({"jev_api_key": "local-test-key"})

    assert settings_store.stored_values()["jev_api_key"] == settings_store._MASK


def test_openai_tool_candidates_pair_calls_with_results_and_pin_recent_messages() -> None:
    messages = [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"file_path":"src/app.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "old tool output"},
    ]

    candidates = extract_openai_candidates(messages, preserve_recent_messages=1)

    assert len(candidates) == 1
    assert candidates[0].candidate_id == "call_1"
    assert candidates[0].tool_name == "Read"
    assert candidates[0].result_chars == len("old tool output")
    assert candidates[0].pinned is True


def test_revision_for_messages_is_stable_and_changes_with_content() -> None:
    messages = [{"role": "user", "content": "hello"}]

    first = revision_for_messages(messages)
    second = revision_for_messages(messages)

    assert first == second
    assert first != revision_for_messages([{**messages[0], "content": "goodbye"}])


def test_proxy_accepts_active_mode_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")
    proxy = create_app(ProxyConfig(jev_mode="active"))

    assert proxy is not None


def test_proxy_rejects_shadow_mode_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY"):
        create_app(ProxyConfig(jev_mode="shadow"))


@pytest.mark.asyncio
@respx.mock
async def test_jev_stats_report_shadow_projection_without_active_or_ccr_savings() -> None:
    respx.post(DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"answers": {"call_t1": {"noul": 0.1}, "result_t1": {"noul": 0.1}}},
        )
    )
    config = JevConfig(mode="shadow", api_key="local-test-key")
    stats = JevStats()
    planner = JevPlanner(config, stats=stats)

    await planner.plan(
        provider="openai",
        model="gpt-5",
        session_id="session-1",
        branch_id="main",
        revision="revision-1",
        goal="Inspect",
        candidates=[_candidate()],
    )

    snapshot = stats.snapshot(config)
    assert snapshot["calls_completed"] == 1
    assert snapshot["projected_tokens_saved"] == 0
    assert snapshot["abstentions"]["insufficient_evidence"] == 1
    assert snapshot["active_applied_tokens"] == 0
    assert snapshot["ccr"]["acknowledged"] == 0


def test_active_decision_stores_before_replacing_result_and_preserves_call() -> None:
    source_messages = [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "secret source contents"},
    ]
    candidates = extract_openai_candidates(source_messages, preserve_recent_messages=0)
    plan = JevPlan(
        decisions=(
            JevDecision(
                candidate_id="t1",
                tool_name="Read",
                keep_call=0.9,
                keep_result=0.1,
                action="drop_result",
            ),
        ),
        called=True,
    )
    store = CompressionStore()

    applied = apply_active_decisions(
        messages=source_messages,
        source_messages=source_messages,
        candidates=candidates,
        plan=plan,
        store=store,
    )

    assert applied.fallback_reason is None
    assert applied.acknowledged == 1
    assert applied.messages[1]["tool_calls"] == source_messages[1]["tool_calls"]
    assert "Retrieve more: hash=" in applied.messages[2]["content"]
    hash_key = applied.ccr_hashes[0]
    assert store.retrieve(hash_key).original_content == "secret source contents"


def test_active_ccr_lease_prevents_eviction_until_recovery_window() -> None:
    store = CompressionStore(max_entries=1)
    first = store.store("first", "marker", lease_seconds=3600)
    second = store.store("second", "marker")

    assert store.retrieve(first).original_content == "first"
    assert store.retrieve(second).original_content == "second"


def test_active_decision_does_not_touch_frozen_prefix() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "cached result"},
    ]
    candidate = _candidate(result_index=0)
    plan = JevPlan(
        decisions=(
            JevDecision(
                candidate_id="t1",
                tool_name="Read",
                keep_call=1.0,
                keep_result=0.0,
                action="drop_result",
            ),
        ),
        called=True,
    )

    applied = apply_active_decisions(
        messages=messages,
        source_messages=messages,
        candidates=[candidate],
        plan=plan,
        store=CompressionStore(),
        frozen_message_count=1,
    )

    assert applied.fallback_reason == "no_active_candidates"
    assert applied.messages == messages


def test_active_decision_fails_open_when_ccr_write_is_not_acknowledged() -> None:
    class BrokenStore:
        def store(self, **_: object) -> str:
            raise RuntimeError("backend unavailable")

        def exists(self, _: str) -> bool:
            return False

    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "tool output"},
    ]
    candidate = _candidate(result_index=0, pinned=False)
    plan = JevPlan(
        decisions=(
            JevDecision(
                candidate_id="t1",
                tool_name="Read",
                keep_call=1.0,
                keep_result=0.0,
                action="drop_result",
            ),
        ),
        called=True,
    )

    applied = apply_active_decisions(
        messages=messages,
        source_messages=messages,
        candidates=[candidate],
        plan=plan,
        store=BrokenStore(),
    )

    assert applied.fallback_reason == "ccr_write_failed"
    assert applied.failed == 1
    assert applied.messages == messages


def test_active_mode_keeps_unseen_results_even_on_explicit_ccr_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")

    async def fake_ask(self: object, state: object, questions: object) -> JevResponse:
        return JevResponse(
            answers={
                "call_call_1": {"noul": 0.9},
                "result_call_1": {"noul": 0.1},
            }
        )

    monkeypatch.setattr("headroom.proxy.jev.JevClient.ask", fake_ask)
    app = create_app(
        ProxyConfig(
            jev_mode="active",
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
    )
    messages = [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "source " * 500},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "I am continuing."},
        {"role": "user", "content": "Keep going"},
        {"role": "assistant", "content": "Still working."},
        {"role": "user", "content": "Check the next file"},
        {"role": "assistant", "content": "Checking it."},
        {"role": "user", "content": "Summarize the findings"},
        {"role": "assistant", "content": "I will summarize."},
    ]

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        response = client.post(
            "/v1/compress",
            json={
                "messages": messages,
                "model": "gpt-5",
                "token_budget": 50,
                "config": {
                    "mode": "ccr",
                    "session_id": "active-test-session",
                    "jev_compaction_boundary": True,
                },
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "[Jev compacted" not in payload["messages"][2]["content"]
    assert payload["jev"]["mode"] == "active"
    assert payload["jev"]["fallback_reason"] == "no_active_candidates"
    assert payload["jev"]["decisions"][0]["action"] == "keep"
    assert payload["jev"]["decisions"][0]["reason"] == "insufficient_evidence"


def test_stats_exposes_jev_block_without_exposing_configuration_secret() -> None:
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
    )

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        response = client.get("/stats")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["jev"]["mode"] == "off"
    assert "api_key" not in data["jev"]


@respx.mock
def test_compress_endpoint_runs_jev_in_shadow_without_mutating_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "local-test-key")
    route = respx.post(DEFAULT_JEV_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "call_call_1": {"noul": 0.1},
                    "result_call_1": {"noul": 0.1},
                }
            },
        )
    )
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            jev_mode="shadow",
            jev_threshold_percent=1,
        )
    )
    messages = [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "SECRET_TOOL_OUTPUT " * 100},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "Working"},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "Working"},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "Working"},
    ]

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        response = client.post(
            "/v1/compress",
            headers={"x-headroom-session-id": "shadow-test"},
            json={"messages": messages, "model": "gpt-5", "token_budget": 100},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["jev"]["called"] is True
    assert data["jev"]["projected_tokens_saved"] == 0
    assert route.called is True
    assert "SECRET_TOOL_OUTPUT" not in route.calls[0].request.content.decode()


@pytest.mark.live
@pytest.mark.asyncio
async def test_jev_live_smoke() -> None:
    api_key = os.environ.get("HEADROOM_JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("HEADROOM_JEV_API_KEY is not set")

    planner = JevPlanner(JevConfig(mode="shadow", api_key=api_key))
    plan = await planner.plan(
        provider="headroom-smoke",
        model="jev-latest",
        session_id="live-smoke",
        branch_id="main",
        revision="smoke-1",
        goal="Decide whether an old tool result is still needed",
        candidates=[_candidate()],
    )

    assert plan.called is True
    assert plan.fallback_reason is None
