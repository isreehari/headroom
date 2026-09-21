"""POST /v1/compress honours ``config.jev_compaction_boundary``.

This is a ROUTE test: the app is the real one, the pipeline is the real one,
the CCR store is a real :class:`CompressionStore` over an in-memory backend,
and the active path runs the real orchestrator over a real ``JevClient`` whose
only stub is its HTTP transport. What is pinned here is the wiring the handler
owns and nothing below it:

* the gate runs for EVERY request, whatever ``HEADROOM_JEV_MODE`` says, so a
  malformed boundary is a 400 even on a proxy with Jev switched off;
* a non-boundary turn is byte-identical to today (no ``jev`` key at all);
* a boundary turn reports its outcome, and when something was retained the
  retained bytes reach the response, the hash list AND the session replay
  state — the caller forwards the retained messages, so the next turn has to
  replay those.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.cache.backends import InMemoryBackend  # noqa: E402
from headroom.cache.compression_store import CompressionStore  # noqa: E402
from headroom.proxy.jev.client import JevClient  # noqa: E402
from headroom.proxy.jev.config import JevConfig  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402

SESSION_KEY_PREFIX = "compress\x00"


def _client(jev: JevConfig | None = None) -> TestClient:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        **({"jev": jev} if jev is not None else {}),
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


def _active_jev() -> JevConfig:
    return JevConfig(
        mode="active",
        api_key="sk-jev-test-key",
        endpoint="https://jev.example/v1/decide",
        model="jev-test",
        timeout_ms=500,
        max_candidate_tokens=4000,
        max_state_tokens=200_000,
    )


def _blob(seed: str, rows: int = 60) -> str:
    return json.dumps([{"id": i, "seed": seed, "blob": "z" * 200} for i in range(rows)])


def _messages() -> list[dict[str, Any]]:
    """One eligible tool result, then six messages of untouchable recent tail."""
    return [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": _blob("a")},
        *[{"role": "assistant", "content": f"step {i}"} for i in range(6)],
    ]


def _post(client: TestClient, **config: Any) -> httpx.Response:
    return client.post(
        "/v1/compress",
        json={"model": "gpt-4o", "messages": _messages(), "config": config},
    )


# --------------------------------------------------------------------------
# The gate: it runs for every request, whatever the Jev mode is.
# --------------------------------------------------------------------------


def test_a_malformed_boundary_flag_is_a_400() -> None:
    with _client() as client:
        resp = _post(client, mode="ccr", session_id="s1", jev_compaction_boundary="true")
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["type"] == "invalid_request"
    assert "config.jev_compaction_boundary" in error["message"]
    assert "'true'" in error["message"]


def test_a_boundary_without_ccr_mode_is_a_400() -> None:
    with _client() as client:
        resp = _post(client, session_id="s1", jev_compaction_boundary=True)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["type"] == "invalid_request"
    assert 'config.mode="ccr"' in error["message"]


def test_a_boundary_without_a_session_id_is_a_400() -> None:
    with _client() as client:
        resp = _post(client, mode="ccr", jev_compaction_boundary=True)
    assert resp.status_code == 400, resp.text
    assert "config.session_id" in resp.json()["error"]["message"]


# --------------------------------------------------------------------------
# Non-boundary turns are exactly what they are today.
# --------------------------------------------------------------------------


def test_a_non_boundary_turn_reports_no_jev_block() -> None:
    with _client(_active_jev()) as client:
        resp = _post(client, mode="ccr", session_id="s1")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "jev" not in payload
    assert payload["messages"][2]["role"] == "tool"


def test_a_boundary_turn_with_jev_off_changes_nothing_but_reports_itself() -> None:
    with _client() as client:
        baseline = _post(client, mode="ccr", session_id="s-off-a")
        assert baseline.status_code == 200, baseline.text
        resp = _post(client, mode="ccr", session_id="s-off-b", jev_compaction_boundary=True)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["jev"] == {
        "boundary": True,
        "called": False,
        "candidates": 0,
        "applied": 0,
        "reason": "jev_inactive",
        "hashes": [],
    }
    assert payload["messages"] == baseline.json()["messages"]
    assert payload["tokens_after"] == baseline.json()["tokens_after"]
    assert payload["ccr_hashes"] == baseline.json()["ccr_hashes"]


# --------------------------------------------------------------------------
# The active path, end to end through the route.
# --------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


class _Jev:
    """A real ``JevClient`` over a mock transport, plus what it was asked."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[dict[str, Any]] = []
        self._handler = handler

    def _wrapped(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return self._handler(request)

    def __call__(self, config: JevConfig) -> JevClient:
        return JevClient(
            config,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._wrapped)),
        )


def _answers(decision: str = "drop") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    cid: {"type": "choice", "choice": decision} for cid in body["questions"]
                },
            },
        )

    return handler


def _install_jev(monkeypatch: pytest.MonkeyPatch) -> _Jev:
    jev = _Jev(_answers())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", jev)
    store = CompressionStore(default_ttl=600, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: store)
    return jev


def _session_state(client: TestClient, session_id: str) -> tuple[Any, Any]:
    """The very cache + tracker objects the handler will use for this session."""
    proxy = client.app.state.proxy
    key = f"{SESSION_KEY_PREFIX}{session_id}"
    return proxy._get_compression_cache(key), proxy.session_tracker_store.get_or_create(
        key, "openai"
    )


def test_a_boundary_turn_retains_and_rerecords_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jev = _install_jev(monkeypatch)

    with _client(_active_jev()) as client:
        baseline = _post(client, mode="ccr", session_id="s-active-a")
        assert baseline.status_code == 200, baseline.text
        resp = _post(client, mode="ccr", session_id="s1", jev_compaction_boundary=True)
        assert resp.status_code == 200, resp.text
        payload = resp.json()

        assert jev.requests, "the route must reach the real Jev client"
        info = payload["jev"]
        assert info["boundary"] is True
        assert info["called"] is True
        assert info["reason"] == "applied"
        assert info["applied"] >= 1
        assert info["candidates"] >= info["applied"]
        assert len(info["hashes"]) == info["applied"]

        hash_key = info["hashes"][0]
        # The marker reached the caller, and the hash is advertised as
        # retrievable alongside Headroom's own.
        assert f"hash={hash_key}" in json.dumps(payload["messages"])
        assert hash_key in payload["ccr_hashes"]
        # Headroom's own hashes are additive, never replaced.
        for existing in baseline.json()["ccr_hashes"]:
            assert existing in payload["ccr_hashes"]
        assert len(payload["ccr_hashes"]) == len(set(payload["ccr_hashes"]))
        # Retention is incremental on top of Headroom's own compression.
        assert payload["tokens_after"] < baseline.json()["tokens_after"]
        assert payload["tokens_saved"] == max(0, payload["tokens_before"] - payload["tokens_after"])

        # The session tracker must hold the RETAINED bytes: it is what the
        # caller forwards, so it is what the next turn has to replay.
        proxy = client.app.state.proxy
        tracker = proxy.session_tracker_store.get_or_create(f"{SESSION_KEY_PREFIX}s1", "openai")
        assert f"hash={hash_key}" in json.dumps(tracker.get_last_forwarded_messages())


# --------------------------------------------------------------------------
# The retained-state re-record is fail-open and never clobbers a newer turn.
# --------------------------------------------------------------------------


def _after_retention(monkeypatch: pytest.MonkeyPatch, hook: Callable[[], None]) -> None:
    """Run ``hook`` in the window the turn lock is free, after the Jev call.

    That window is exactly where a concurrent same-session turn lands: the
    turn released ``session_turn_lock`` in its ``finally`` before the
    orchestrator was awaited, and the re-record has not re-acquired it yet.
    """
    from headroom.proxy.jev import active_hook

    real = active_hook.run_jev_active_retention

    async def _wrapped(**kwargs: Any) -> Any:
        result = await real(**kwargs)
        hook()
        return result

    monkeypatch.setattr(active_hook, "run_jev_active_retention", _wrapped)


def test_a_concurrent_turn_is_not_overwritten_by_the_rerecord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_jev(monkeypatch)
    newer = [{"role": "user", "content": "a newer same-session turn already recorded this"}]
    state: dict[str, Any] = {}

    def _a_concurrent_turn_completes() -> None:
        cache, tracker = state["cache"], state["tracker"]
        with cache.session_turn_lock:
            cache.update_from_result(newer, newer)
            tracker.record_returned(newer, newer)

    _after_retention(monkeypatch, _a_concurrent_turn_completes)

    with _client(_active_jev()) as client:
        baseline = _post(client, mode="ccr", session_id="s-race-baseline")
        assert baseline.status_code == 200, baseline.text
        state["cache"], state["tracker"] = _session_state(client, "s-race")
        resp = _post(client, mode="ccr", session_id="s-race", jev_compaction_boundary=True)
        assert resp.status_code == 200, resp.text
        payload = resp.json()

        # The newer turn's replay state is intact: the older boundary turn's
        # retained messages did NOT overwrite it.
        assert state["tracker"].get_last_forwarded_messages() == newer

    # Jev ran, but its mutation was discarded, so the caller is given exactly
    # what a non-boundary turn would have returned.
    info = payload["jev"]
    assert info["called"] is True
    assert info["candidates"] >= 1
    assert info["applied"] == 0
    assert info["hashes"] == []
    assert info["reason"] == "rerecord_failed"
    assert payload["messages"] == baseline.json()["messages"]
    assert payload["tokens_after"] == baseline.json()["tokens_after"]
    assert payload["ccr_hashes"] == baseline.json()["ccr_hashes"]


def test_a_busy_session_lock_fails_open_instead_of_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_jev(monkeypatch)
    # The re-record's timed acquire, shortened so the test does not sit out
    # the production 10s wait.
    monkeypatch.setattr("headroom.proxy.handlers.openai._SESSION_TURN_LOCK_TIMEOUT_SECONDS", 0.05)
    state: dict[str, Any] = {}

    def _hold_the_turn_lock() -> None:
        state["cache"].session_turn_lock.acquire()

    _after_retention(monkeypatch, _hold_the_turn_lock)

    with _client(_active_jev()) as client:
        baseline = _post(client, mode="ccr", session_id="s-busy-baseline")
        assert baseline.status_code == 200, baseline.text
        state["cache"], state["tracker"] = _session_state(client, "s-busy")
        try:
            resp = _post(client, mode="ccr", session_id="s-busy", jev_compaction_boundary=True)
        finally:
            state["cache"].session_turn_lock.release()

    # A Jev step must never turn a SUCCESSFUL compression into an error.
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    info = payload["jev"]
    assert info["called"] is True
    assert info["applied"] == 0
    assert info["hashes"] == []
    assert info["reason"] == "rerecord_failed"
    assert payload["messages"] == baseline.json()["messages"]
    assert payload["tokens_after"] == baseline.json()["tokens_after"]
    assert payload["ccr_hashes"] == baseline.json()["ccr_hashes"]


# --------------------------------------------------------------------------
# message_shape describes the list Jev is actually shown, not the model name.
# --------------------------------------------------------------------------


def _anthropic_messages() -> list[dict[str, Any]]:
    """The same conversation in Anthropic shape: a ``tool_result`` block."""
    return [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "get", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": _blob("a")}],
        },
        *[{"role": "assistant", "content": f"step {i}"} for i in range(6)],
    ]


def _shape_of(jev: _Jev) -> str:
    assert jev.requests, "the route must reach the real Jev client"
    return str(jev.requests[0]["state"]["message_shape"])


def test_message_shape_reads_an_anthropic_list_under_an_openai_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jev = _install_jev(monkeypatch)
    with _client(_active_jev()) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _anthropic_messages(),
                "config": {
                    "mode": "ccr",
                    "session_id": "s-shape-a",
                    "jev_compaction_boundary": True,
                },
            },
        )
    assert resp.status_code == 200, resp.text
    assert _shape_of(jev) == "anthropic"


def test_message_shape_reads_an_openai_list_under_a_claude_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jev = _install_jev(monkeypatch)
    with _client(_active_jev()) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "messages": _messages(),
                "config": {
                    "mode": "ccr",
                    "session_id": "s-shape-b",
                    "jev_compaction_boundary": True,
                },
            },
        )
    assert resp.status_code == 200, resp.text
    assert _shape_of(jev) == "openai"


# --------------------------------------------------------------------------
# A gateway-claimed boundary turn: the provider body and the turn context the
# re-drive replays must describe the SAME conversation.
# --------------------------------------------------------------------------


def test_a_gateway_boundary_turn_keeps_its_ctx_in_step_with_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_jev(monkeypatch)
    with _client(_active_jev()) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {
                    "mode": "ccr",
                    "session_id": "s-gateway",
                    "jev_compaction_boundary": True,
                },
                "gateway": {
                    "can_redrive": True,
                    "can_relay_response": True,
                    "session_affinity": True,
                    "plugin_version": "test",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["jev"]["reason"] == "applied"
        hash_key = payload["jev"]["hashes"][0]

        pending = client.app.state.proxy.gateway_turns.get(payload["turn_id"])
        assert pending is not None, "a relaying gateway turn must be registered"
        # The redrive path re-calls the model with `ctx.messages`
        # (`gateway_turn.py:1014`), while the provider was given
        # `fields["body"]`. A redrive that replays the PRE-retention bytes
        # after the provider saw the retention marker is a torn conversation.
        assert f"hash={hash_key}" in json.dumps(pending.body["messages"])
        assert pending.ctx is not None
        assert pending.ctx.messages == pending.body["messages"]
