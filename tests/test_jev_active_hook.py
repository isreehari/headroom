"""The orchestrator: decide -> stage -> apply, fail-open at every step.

Only the HTTP transport is stubbed. ``JevClient`` itself is the production
class, built over an ``httpx.MockTransport``, so the request construction, the
response parsing, the error scrubbing, the per-call timeout and ``aclose`` are
all the real code. Candidate selection, the request budget, the tokenizer, the
CCR store, the staging sequence and the write-back are real too, so the round
trip these tests pin -- stage a candidate, rewrite its slot, read the original
back out of the store under the hash in the marker -- is the round trip
production runs. A hand-written fake client survives in exactly one test, the
one that needs ``aclose`` itself to fail.

The store is a real :class:`CompressionStore` over an in-memory backend, not a
mock: the CCR contract this task exists to honour ("write -> acknowledged
read-back -> lease, any failed step keeps the original") is only meaningfully
exercised against a store that can actually refuse.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionEntry, CompressionStore
from headroom.proxy.jev.active_hook import JevActiveResult, run_jev_active_retention
from headroom.proxy.jev.client import JevClient
from headroom.proxy.jev.config import JevConfig

ENDPOINT = "https://user:pw@jev.example/v1/decide?token=abc"
API_KEY = "sk-jev-secret-key"
MODEL = "gpt-4o"
SESSION = "s1"


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


Handler = Callable[[httpx.Request], httpx.Response]


class _Jev:
    """A real ``JevClient`` over a mock transport, plus what it was asked."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[dict[str, Any]] = []
        self.clients: list[JevClient] = []
        self._handler = handler

    def _wrapped(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return self._handler(request)

    def __call__(self, config: JevConfig) -> JevClient:
        client = JevClient(
            config,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._wrapped)),
        )
        self.clients.append(client)
        return client

    @property
    def candidate_ids(self) -> list[str]:
        return [cid for body in self.requests for cid in body["questions"]]

    async def is_closed(self, index: int = 0) -> bool:
        """``JevClient`` refuses to dial after ``aclose``; that is the signal.

        An INJECTED httpx client is not owned by ``JevClient`` and so is not
        itself closed — the guard that matters is the one that stops a decide()
        during shutdown from building a fresh pool and dialling the live
        endpoint, and that guard is what this observes.
        """
        answer = await self.clients[index].decide(state={}, questions={}, candidate_ids=["x"])
        return answer.error is not None and "closed" in answer.error


def _answers(decision: str = "drop", status: int = 200) -> Handler:
    """A Jev response answering every question in the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            status,
            json={
                "model": "jev-test",
                "answers": {
                    cid: {"type": "choice", "choice": decision} for cid in body["questions"]
                },
            },
        )

    return handler


def _install_jev(monkeypatch: pytest.MonkeyPatch, handler: Handler | None = None) -> _Jev:
    """Point the hook at a real ``JevClient`` speaking to ``handler``."""
    jev = _Jev(handler if handler is not None else _answers())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", jev)
    return jev


class _RefusingStore(CompressionStore):
    """A store whose read-back refuses the first ``refuse`` candidates.

    That is the CCR failure the plan names: the write returned a key but the
    entry is not retrievable, so no lease may be taken and the original has to
    stay in the conversation.
    """

    def __init__(self, *args: Any, refuse: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._refuse = refuse
        self.peeks = 0

    def peek(self, hash_key: str) -> CompressionEntry | None:
        self.peeks += 1
        if self.peeks <= self._refuse:
            return None
        return super().peek(hash_key)


class _BrokenTokenizer:
    def count_text(self, text: str) -> int:
        raise RuntimeError(f"tokenizer exploded with {API_KEY} at {ENDPOINT}")

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        raise RuntimeError(f"tokenizer exploded with {API_KEY} at {ENDPOINT}")


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


def _jev(mode: str = "active", **overrides: Any) -> JevConfig:
    values: dict[str, Any] = {
        "mode": mode,
        "api_key": API_KEY,
        "endpoint": ENDPOINT,
        "model": "jev-test",
        "timeout_ms": 500,
        "threshold_percent": 80,
        "cooldown_turns": 5,
        "max_candidate_tokens": 4000,
        "max_candidates": 12,
        "max_state_tokens": 200_000,
    }
    values.update(overrides)
    return JevConfig(**values)


def _blob(seed: str, rows: int = 60) -> str:
    return json.dumps([{"id": i, "seed": seed, "blob": "z" * 200} for i in range(rows)])


def _messages() -> list[dict[str, Any]]:
    """Two eligible tool results, then six messages of untouchable recent tail."""
    tail = [{"role": "assistant", "content": f"step {i}"} for i in range(6)]
    return [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_0", "content": _blob("a")},
        {"role": "tool", "tool_call_id": "call_1", "content": _blob("b")},
        *tail,
    ]


def _no_candidates() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> CompressionStore:
    s = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: s)
    return s


def _use_store(monkeypatch: pytest.MonkeyPatch, s: CompressionStore) -> CompressionStore:
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: s)
    return s


async def _run(proxy: _Proxy, messages: list[dict[str, Any]], **kwargs: Any) -> JevActiveResult:
    return await run_jev_active_retention(
        proxy=proxy, messages=messages, model=MODEL, session_id=SESSION, **kwargs
    )


# --------------------------------------------------------------------------
# Inactive modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_inactive_config_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore, mode: str
) -> None:
    jev = _install_jev(monkeypatch)
    proxy = _Proxy(_jev(mode=mode))
    messages = _messages()

    result = await _run(proxy, messages)

    assert result.applied == 0
    assert result.reason == "jev_inactive"
    assert result.called is False
    assert result.messages is messages
    # No client, no Jev call, and no metric noise for a mode the operator
    # switched off.
    assert jev.clients == []
    assert jev.requests == []
    assert proxy.metrics.events == []


async def test_a_missing_jev_config_is_a_no_op(store: CompressionStore) -> None:
    class _Bare:
        config = None
        metrics = _Metrics()

    proxy = _Bare()
    messages = _messages()
    result = await run_jev_active_retention(
        proxy=proxy, messages=messages, model=MODEL, session_id=SESSION
    )
    assert result.reason == "jev_inactive"
    assert result.messages is messages
    assert proxy.metrics.events == []


# --------------------------------------------------------------------------
# Nothing to do
# --------------------------------------------------------------------------


async def test_no_candidates_is_reported_without_a_call(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    jev = _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _no_candidates()

    result = await _run(proxy, messages)

    assert result.reason == "no_candidates"
    assert result.applied == 0
    assert result.candidates == 0
    assert result.called is False
    assert result.messages is messages
    assert jev.requests == []
    assert await jev.is_closed()
    assert proxy.metrics.events == ["active_attempted", "active_no_candidates"]


async def test_an_all_keep_answer_moves_nothing(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    jev = _install_jev(monkeypatch, _answers("keep"))
    proxy = _Proxy(_jev())
    messages = _messages()

    result = await _run(proxy, messages)

    assert jev.candidate_ids == ["cand_0000", "cand_0001"]
    assert result.reason == "all_keep"
    assert result.applied == 0
    assert result.candidates == 2
    assert result.called is True
    assert result.messages is messages
    assert result.hashes == []
    # Nothing was staged: an all-keep answer must not write to the CCR store.
    assert store.get_stats()["entry_count"] == 0
    assert proxy.metrics.events == ["active_attempted", "active_no_candidates"]


# --------------------------------------------------------------------------
# A failed Jev call
# --------------------------------------------------------------------------


async def test_an_http_error_keeps_everything(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    jev = _install_jev(monkeypatch, handler)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.reason == "call_failed"
    assert result.applied == 0
    assert result.candidates == 2
    assert result.called is True
    assert result.messages is messages
    assert messages == before
    # Nothing was staged for a turn whose answer never arrived.
    assert store.get_stats()["entry_count"] == 0
    assert await jev.is_closed()
    assert proxy.metrics.events == ["active_attempted", "active_call_failed"]


async def test_a_dead_transport_keeps_everything(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    """The client swallows transport errors into ``JevAnswer.error``, so this
    is a ``call_failed`` turn and not a fail-open one."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_jev(monkeypatch, handler)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.reason == "call_failed"
    assert result.applied == 0
    assert result.called is True
    assert messages == before
    assert store.get_stats()["entry_count"] == 0
    assert proxy.metrics.events == ["active_attempted", "active_call_failed"]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_drop_stages_ccr_then_rewrites(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    jev = _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert jev.candidate_ids == ["cand_0000", "cand_0001"]
    assert result.reason == "applied"
    assert result.applied == 2
    assert result.candidates == 2
    assert result.called is True
    assert len(result.hashes) == 2

    # The rewritten slots carry the marker for their own staged hash...
    for offset, hash_key in enumerate(result.hashes):
        content = result.messages[1 + offset]["content"]
        assert f"hash={hash_key}" in content
        assert before[1 + offset]["content"] not in content
        # ...and the original is retrievable under it.
        entry = store.retrieve(hash_key)
        assert entry is not None
        assert entry.original_content == before[1 + offset]["content"]

    # The recent tail and the leading user turn are untouched.
    assert result.messages[0] == before[0]
    assert result.messages[3:] == before[3:]

    # The caller's list is byte-identical, so a later failure can still fall back.
    assert messages == before
    assert result.messages is not messages

    assert result.tokens_after > 0
    assert result.tokens_after < 1000
    assert await jev.is_closed()
    assert proxy.metrics.events == ["active_attempted", "active_applied"]


async def test_truncate_keeps_a_readable_head(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    _install_jev(monkeypatch, _answers("truncate"))
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.applied == 2
    rewritten = result.messages[1]["content"]
    assert rewritten.startswith(before[1]["content"][:400])
    assert f"hash={result.hashes[0]}" in rewritten
    assert len(rewritten) < len(before[1]["content"])


# --------------------------------------------------------------------------
# CCR refusals
# --------------------------------------------------------------------------


async def test_a_refused_lease_keeps_that_candidate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _use_store(
        monkeypatch,
        _RefusingStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend(), refuse=1),
    )
    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.reason == "applied"
    assert result.applied == 1
    assert result.candidates == 2
    assert len(result.hashes) == 1
    # The refused candidate still has its original content...
    assert result.messages[1]["content"] == before[1]["content"]
    # ...and the leased one was rewritten.
    assert f"hash={result.hashes[0]}" in result.messages[2]["content"]
    assert store.retrieve(result.hashes[0]) is not None
    assert proxy.metrics.events == ["active_attempted", "active_applied"]


async def test_every_refused_lease_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(
        monkeypatch,
        _RefusingStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend(), refuse=99),
    )
    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.reason == "no_lease"
    assert result.applied == 0
    assert result.hashes == []
    assert result.messages == before
    assert messages == before
    assert proxy.metrics.events == ["active_attempted", "active_no_lease"]


async def test_a_lease_nothing_applies_to_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    """A truncate at/below the inline width is a no-op, so nothing is applied."""
    _install_jev(monkeypatch, _answers("truncate"))
    proxy = _Proxy(_jev())
    # Short enough that truncation would add tokens rather than save any.
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_0", "content": "small result"},
        *[{"role": "assistant", "content": f"step {i}"} for i in range(6)],
    ]
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.applied == 0
    assert result.reason == "not_applied"
    assert result.hashes == []
    assert result.messages == before
    assert proxy.metrics.events == ["active_attempted", "active_no_lease"]


# --------------------------------------------------------------------------
# Fail-open
# --------------------------------------------------------------------------


async def test_a_raising_tokenizer_fails_open(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Selection's tokenizer raises: Task 15 propagates, this is the only guard."""
    monkeypatch.setattr(
        "headroom.proxy.jev.active.get_tokenizer",
        lambda model, *a, **k: _BrokenTokenizer(),
    )
    jev = _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    # DEBUG, so the scrubbed traceback line is captured and checked too.
    with caplog.at_level(logging.DEBUG, logger="headroom.proxy.jev.active_hook"):
        result = await _run(proxy, messages)

    assert result.reason == "fail_open"
    assert result.applied == 0
    assert result.messages is messages
    assert messages == before
    assert proxy.metrics.events == ["active_attempted", "active_fail_open"]
    # The client is closed even when the work inside it blew up.
    assert await jev.is_closed()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "tokenizer exploded" in logged
    assert "jev.example" in logged  # still diagnosable
    for secret in (API_KEY, "user:pw", "token=abc"):
        assert secret not in logged
        for record in caplog.records:
            # `exc_info=True` would let the formatter append the ORIGINAL
            # traceback, whose last line is the unscrubbed `str(exc)` (and
            # every chained `__cause__` with it).
            assert record.exc_info is None
            assert secret not in (record.exc_text or "")
            assert secret not in str(record.exc_info)


async def test_the_fail_open_traceback_is_scrubbed_and_debug_only(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A staging step raising with credentials in its message leaks nothing."""
    config = _jev()

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"boom {config.endpoint} {config.api_key}")

    _install_jev(monkeypatch)
    monkeypatch.setattr("headroom.proxy.jev.active_hook.stage_retention", _boom)
    proxy = _Proxy(config)

    with caplog.at_level(logging.DEBUG, logger="headroom.proxy.jev.active_hook"):
        result = await _run(proxy, _messages())

    assert result.reason == "fail_open"
    # The traceback IS rendered -- by us, scrubbed -- and only at DEBUG.
    traces = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert traces and "Traceback" in traces[0].getMessage()
    assert "RuntimeError" in traces[0].getMessage()

    for secret in (config.api_key, "token=abc", "user:pw"):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert record.exc_info is None
            assert secret not in (record.exc_text or "")
            assert secret not in str(record.exc_info)


async def test_a_raising_counter_after_apply_fails_open(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    """The measurement is inside the guard too: a bad count forwards the input."""
    monkeypatch.setattr(
        "headroom.proxy.jev.active_hook.get_tokenizer",
        lambda model, *a, **k: _BrokenTokenizer(),
    )
    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(proxy, messages)

    assert result.reason == "fail_open"
    assert result.applied == 0
    assert result.messages is messages
    assert messages == before
    assert proxy.metrics.events == ["active_attempted", "active_fail_open"]


async def test_a_raising_client_constructor_fails_open(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    def _boom(config: JevConfig) -> Any:
        raise RuntimeError("no transport")

    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", _boom)
    proxy = _Proxy(_jev())
    messages = _messages()

    result = await _run(proxy, messages)

    assert result.reason == "fail_open"
    assert result.messages is messages
    assert proxy.metrics.events == ["active_attempted", "active_fail_open"]


async def test_a_failing_close_does_not_lose_the_turn(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    """``aclose`` is best-effort: a close failure must not undo a good decision.

    The one hand-written double in this module: the real ``JevClient.aclose``
    cannot be made to fail without reaching into a private attribute.
    """
    closes: list[int] = []

    class _UnclosableClient(JevClient):
        async def aclose(self) -> None:
            closes.append(1)
            raise RuntimeError("socket already gone")

    def _factory(config: JevConfig) -> JevClient:
        return _UnclosableClient(
            config,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_answers())),
        )

    monkeypatch.setattr("headroom.proxy.jev.active_hook.JevClient", _factory)
    proxy = _Proxy(_jev())

    result = await _run(proxy, _messages())

    assert result.reason == "applied"
    assert result.applied == 2
    assert closes == [1]


async def test_a_broken_metrics_recorder_never_fails_a_turn(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    class _AngryMetrics:
        def record_jev_event(self, event: str) -> None:
            raise RuntimeError("prometheus is down")

    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    proxy.metrics = _AngryMetrics()  # type: ignore[assignment]

    result = await _run(proxy, _messages())

    assert result.reason == "applied"
    assert result.applied == 2


async def test_a_proxy_without_metrics_is_fine(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    class _NoMetrics:
        def __init__(self) -> None:
            self.config = _Config(_jev())

    _install_jev(monkeypatch)
    result = await run_jev_active_retention(
        proxy=_NoMetrics(), messages=_messages(), model=MODEL, session_id=SESSION
    )
    assert result.applied == 2


# --------------------------------------------------------------------------
# Identity plumbing
# --------------------------------------------------------------------------


async def test_the_branch_id_binds_the_retention_hash(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    """A different branch stages the same bytes under a different key."""
    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())

    default = await _run(proxy, _messages())
    other = await _run(_Proxy(_jev()), _messages(), branch_id="resp_123")

    assert default.hashes and other.hashes
    assert set(default.hashes).isdisjoint(other.hashes)


async def test_the_frozen_prefix_is_honoured(
    monkeypatch: pytest.MonkeyPatch, store: CompressionStore
) -> None:
    _install_jev(monkeypatch)
    proxy = _Proxy(_jev())
    messages = _messages()

    result = await _run(proxy, messages, frozen_prefix=2)

    assert result.candidates == 1
    assert result.applied == 1
    assert result.messages[1]["content"] == messages[1]["content"]
