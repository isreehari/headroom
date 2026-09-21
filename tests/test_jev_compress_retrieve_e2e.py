"""Boundary /v1/compress -> marker -> existing /v1/retrieve, byte-exact.

Only the Jev HTTP call is stubbed (no billed API call in CI). Everything else is
the real chain: candidate selection, the CCR write/acknowledge/lease sequence,
the rewrite, and the retrieval route that already exists.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.cache.compression_store import reset_compression_store  # noqa: E402
from headroom.proxy.jev.config import JevConfig  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


@dataclass
class _Answer:
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 5.0
    jev_model: str | None = "jev-test"


@pytest.fixture
def jev_active(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "active")
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "test-key")
    monkeypatch.setenv("HEADROOM_JEV_ENDPOINT", "https://jev.invalid/v1/decide")
    monkeypatch.setenv("HEADROOM_JEV_MODEL", "jev-test")
    monkeypatch.setenv("HEADROOM_JEV_TIMEOUT_MS", "500")
    monkeypatch.setenv("HEADROOM_JEV_MAX_CANDIDATE_TOKENS", "4000")
    monkeypatch.setenv("HEADROOM_CCR_BACKEND", "memory")

    async def _decide(
        self: Any,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        candidate_ids: Sequence[str],
    ) -> _Answer:
        # Drop everything offered: this test is about the retrieval guarantee
        # that makes dropping safe, not about decision quality.
        return _Answer(decisions=dict.fromkeys(candidate_ids, "drop"))

    monkeypatch.setattr("headroom.proxy.jev.client.JevClient.decide", _decide)
    reset_compression_store()
    yield
    # The store is a process-global singleton; leaving this test's retained
    # originals in it would leak into whatever runs next.
    reset_compression_store()


def _messages() -> list[dict[str, Any]]:
    blob = json.dumps(
        [{"id": i, "status": "ok", "blob": f"payload-{i:04d}-" + "y" * 200} for i in range(120)]
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": blob},
    ]
    # Push the tool result outside Track A's 6-message recent-tail exclusion.
    messages.extend({"role": "user", "content": f"follow-up {i}"} for i in range(8))
    return messages


def test_boundary_marker_resolves_on_v1_retrieve(jev_active: None) -> None:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig.from_env(),
    )
    original_messages = _messages()
    original_tool_content = original_messages[2]["content"]

    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        # The same turn WITHOUT the boundary flag, on its own session: this is
        # the conversation Headroom would forward, and therefore the exact
        # bytes the marker has to stand in for.
        baseline = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {"mode": "ccr", "session_id": "baseline-session-id"},
            },
        )
        assert baseline.status_code == 200, baseline.text
        assert "jev" not in baseline.json()
        forwarded_tool_content = [
            m for m in baseline.json()["messages"] if m.get("role") == "tool"
        ][0]["content"]

        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": original_messages,
                "config": {
                    "mode": "ccr",
                    "session_id": "caller-owned-session-id",
                    "jev_compaction_boundary": True,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["jev"]["applied"] >= 1, payload["jev"]
        hash_key = payload["jev"]["hashes"][0]

        # The forwarded conversation carries the marker, and the tool message
        # itself is still there (its tool_call would otherwise be orphaned).
        tool_messages = [m for m in payload["messages"] if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert f"hash={hash_key}" in tool_messages[0]["content"]
        assert tool_messages[0]["tool_call_id"] == "c1"

        # Retrieval is the EXISTING path, unchanged.
        retrieved = client.post("/v1/retrieve", json={"hash": hash_key})
        assert retrieved.status_code == 200, retrieved.text
        retrieved_content = retrieved.json()["original_content"]
        # Byte-exact with what the marker replaced. Retention is additive on
        # top of Headroom's deterministic pass, so the retained original is the
        # content Headroom was about to forward, not the caller's raw bytes.
        assert retrieved_content == forwarded_tool_content
        # ...and that pass lost nothing on the way there: the retained bytes
        # still carry every item the caller sent.
        assert json.loads(retrieved_content) == json.loads(original_tool_content)


def test_jev_off_leaves_the_boundary_turn_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "off")
    monkeypatch.setenv("HEADROOM_CCR_BACKEND", "memory")
    reset_compression_store()
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig.from_env(),
    )
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {
                    "mode": "ccr",
                    "session_id": "caller-owned-session-id",
                    "jev_compaction_boundary": True,
                },
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["jev"]["reason"] == "jev_inactive"
    assert resp.json()["jev"]["applied"] == 0
    reset_compression_store()
