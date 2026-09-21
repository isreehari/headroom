"""A boundary turn from an Anthropic-shaped caller is retained too.

Task 18 proves the OpenAI shape end-to-end through ``POST /v1/compress``. This
covers the Anthropic shape at the orchestrator seam, which is where the shape
actually matters: the candidate is a ``tool_result`` BLOCK inside a user
message's content list, not a whole message, so the marker has to land in that
block's ``content`` and the block's envelope (``type``, ``tool_use_id``,
``is_error``) has to survive -- an orphaned ``tool_use`` is a 400 from
Anthropic.

Only the Jev HTTP transport is stubbed. ``JevClient`` is the production class
over an ``httpx.MockTransport``, and candidate selection, the request budget,
the CCR write/acknowledge/lease sequence and the write-back are all the real
chain, against a real :class:`CompressionStore` over an in-memory backend. So
the round trip pinned here -- select the block, stage its original, rewrite the
block in place, read the original back out of the store under the hash in the
marker -- is the round trip production runs.

``message_shape`` is metadata: ``select_candidates`` and ``apply_retention``
detect the shape structurally and never read it. It is asserted on the wire
because Jev is told the truth about the caller, not because it drives anything
here.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev.active_hook import JevActiveResult, run_jev_active_retention
from headroom.proxy.jev.client import JevClient
from headroom.proxy.jev.config import JevConfig

MODEL = "claude-sonnet-4-5-20250929"
SESSION = "caller-owned-session-id"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class _Metrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


class _Config:
    def __init__(self, jev: JevConfig) -> None:
        self.jev = jev


class _Proxy:
    def __init__(self, jev: JevConfig) -> None:
        self.config = _Config(jev)
        self.metrics = _Metrics()


class _Jev:
    """A real ``JevClient`` over a mock transport, plus what it was asked.

    Same doubling strategy as ``tests/test_jev_active_hook.py``: the factory is
    substituted for ``active_hook.JevClient``, so request construction,
    response parsing, error scrubbing, the per-call timeout and ``aclose`` stay
    production code and only the socket is replaced.
    """

    def __init__(self, decision: str = "drop") -> None:
        self.requests: list[dict[str, Any]] = []
        self._decision = decision

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    cid: {"type": "choice", "choice": self._decision} for cid in body["questions"]
                },
            },
        )

    def __call__(self, config: JevConfig) -> JevClient:
        return JevClient(
            config,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )

    @property
    def state(self) -> dict[str, Any]:
        """The single ``state`` object Jev was sent."""
        assert len(self.requests) == 1, f"expected exactly one Jev call, got {len(self.requests)}"
        state: dict[str, Any] = self.requests[0]["state"]
        return state


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


def _jev_config() -> JevConfig:
    return JevConfig(
        mode="active",
        api_key="sk-jev-test-key",
        endpoint="https://jev.example/v1/decide",
        model="jev-test",
        timeout_ms=500,
        # Explicit, so the measured request budget cannot silently trim the one
        # candidate this suite is about into a `not_asked` turn.
        max_candidate_tokens=4000,
        max_state_tokens=200_000,
    )


def _blob(rows: int = 60) -> str:
    return json.dumps([{"id": i, "blob": "z" * 200} for i in range(rows)])


def _anthropic_messages(tool_result_content: Any) -> list[dict[str, Any]]:
    """An Anthropic-shaped turn whose only candidate is a ``tool_result`` block.

    The ``tool_result`` is the SECOND block of its user message, so a rewrite
    that ignored ``block_index`` would hit the sibling ``text`` block instead of
    the candidate. Seven trailing turns push the block outside Track A's
    six-message recent-tail exclusion.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll take a look."},
                {"type": "tool_use", "id": "tu_1", "name": "ls", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here is the output"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "is_error": False,
                    "content": tool_result_content,
                },
            ],
        },
    ]
    messages.extend({"role": "user", "content": f"follow-up {i}"} for i in range(7))
    return messages


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[CompressionStore]:
    """A private CCR store for the hook, torn down even when a test fails.

    ``monkeypatch`` keeps the process-global singleton out of this entirely --
    ``get_compression_store`` is never called -- and the ``yield`` teardown
    empties the instance so nothing survives into another test through a
    lingering reference.
    """
    s = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: s)
    yield s
    s.clear()


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> _Jev:
    client_factory = _Jev()
    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", client_factory)
    return client_factory


async def _run(messages: list[dict[str, Any]]) -> tuple[_Proxy, JevActiveResult]:
    proxy = _Proxy(_jev_config())
    result = await run_jev_active_retention(
        proxy=proxy,
        messages=messages,
        model=MODEL,
        session_id=SESSION,
        message_shape="anthropic",
    )
    return proxy, result


def _tool_result_block(messages: list[dict[str, Any]]) -> dict[str, Any]:
    block: dict[str, Any] = messages[2]["content"][1]
    return block


# --------------------------------------------------------------------------
# The Anthropic shape
# --------------------------------------------------------------------------


async def test_anthropic_tool_result_block_is_retained_and_retrievable(
    store: CompressionStore, jev: _Jev
) -> None:
    messages = _anthropic_messages(_blob())
    untouched = copy.deepcopy(messages)
    original = untouched[2]["content"][1]["content"]

    proxy, result = await _run(messages)

    # Jev was told the caller's shape (metadata only -- selection and the
    # rewrite below detect it structurally).
    assert jev.state["message_shape"] == "anthropic"
    # Selection found the block, not the whole message, and named its position.
    assert [c["block_index"] for c in jev.state["candidates"]] == [1]

    assert result.reason == "applied"
    assert result.candidates == 1
    assert result.applied == 1
    assert result.called is True
    assert proxy.metrics.events == ["active_attempted", "active_applied"]

    block = _tool_result_block(result.messages)
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"  # the envelope survives
    assert block["is_error"] is False
    assert f"hash={result.hashes[0]}" in block["content"]
    assert "zzz" not in block["content"]  # the payload really left the turn
    # The block stays IN PLACE, so its sibling and the tool_use it answers are
    # both untouched and nothing is orphaned.
    assert result.messages[2]["content"][0] == untouched[2]["content"][0]
    assert result.messages[1] == untouched[1]
    assert len(result.messages) == len(untouched)

    # The caller's own list is never mutated, so a later failure can still fall
    # back to the full conversation.
    assert messages == untouched

    entry = store.retrieve(result.hashes[0])
    assert entry is not None
    assert entry.original_content == original


async def test_a_list_valued_tool_result_block_keeps_its_envelope(
    store: CompressionStore, jev: _Jev
) -> None:
    """The other legal Anthropic spelling: ``content`` as a block list.

    Track A flattens such a block to one deterministic JSON string, and the
    write-back replaces the slot with a plain string -- itself a valid
    ``tool_result.content`` -- so what has to survive is the ENVELOPE and the
    stored original, not the list-ness.
    """
    content_blocks = [{"type": "text", "text": _blob(30)}, {"type": "text", "text": _blob(30)}]
    messages = _anthropic_messages(content_blocks)
    untouched = copy.deepcopy(messages)
    # Computed independently of production code: `text_of` is plain
    # `json.dumps(..., default=str)`, and that is what was hashed and stored.
    flattened = json.dumps(content_blocks)

    _proxy, result = await _run(messages)

    assert result.reason == "applied"
    assert result.applied == 1
    assert jev.state["candidates"][0]["content"] == flattened

    block = _tool_result_block(result.messages)
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"
    assert block["is_error"] is False
    assert isinstance(block["content"], str)
    assert f"hash={result.hashes[0]}" in block["content"]
    assert messages == untouched

    entry = store.retrieve(result.hashes[0])
    assert entry is not None
    assert entry.original_content == flattened


async def test_a_truncated_block_keeps_its_head_and_its_envelope(
    store: CompressionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``truncate`` is the other applicable decision and shares the slot logic."""
    factory = _Jev(decision="truncate")
    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", factory)

    messages = _anthropic_messages(_blob())
    untouched = copy.deepcopy(messages)
    original = untouched[2]["content"][1]["content"]

    _proxy, result = await _run(messages)

    assert result.reason == "applied"
    assert result.applied == 1

    block = _tool_result_block(result.messages)
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"
    assert block["content"].startswith(original[:200])
    assert len(block["content"]) < len(original)
    assert f"hash={result.hashes[0]}" in block["content"]
    assert messages == untouched

    entry = store.retrieve(result.hashes[0])
    assert entry is not None
    assert entry.original_content == original
