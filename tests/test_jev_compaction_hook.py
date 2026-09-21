"""Track C: every gate of the compaction orchestrator fails open to the original bytes.

This module is the fail-open matrix. ``apply_jev_compaction_boundary`` composes
detection (Task 20), extraction (Task 21), the recovery-tool gate (Task 22), the
single-candidate decision (Task 23), the revision store (Task 24) and the shared
CCR sequence (Task 13) behind one contract:

* it returns ``(frame_to_forward, reason)`` and never raises, except
  :class:`asyncio.CancelledError`, which is the relay tearing the connection
  down and must propagate;
* the forwarded frame is the **unchanged input string** -- byte-identical, not a
  re-serialization -- for every reason but ``jev_compaction_dropped``.

So every test below asserts BOTH halves: the exact reason string, and ``out is
raw`` / ``out == raw``. A test that only checked the reason would pass against
an implementation that quietly round-tripped the frame through ``json.dumps``
and reordered the client's keys on the way to the provider.

Ordering is asserted too, not just outcomes. The recovery-tool gate has to run
before any mutation and before any CCR write, because a marker the model cannot
redeem is permanent data loss rather than compression; ``claim``'s ``False``
return has to be honoured as the authoritative gate rather than treated as a
formality after ``seen``; and a failed ``stage_retention`` has to forward the
original with no marker anywhere in it.

The store is a real ``CompressionStore(backend=InMemoryBackend())`` running the
real staging sequence. Only the Jev decision client is stubbed -- it is the one
component that would otherwise talk to a network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.compaction_hook import (
    JEV_COMPACTION_REASONS,
    apply_jev_compaction_boundary,
    resolve_jev_client,
)
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore
from headroom.proxy.jev.retention_ccr import (
    JEV_RETENTION_LEASE_SECONDS,
    candidate_retention_hash,
)


@dataclass
class _Config:
    mode: str = "active"
    timeout_ms: int = 5000
    max_candidate_tokens: int = 0
    model: str | None = "jev-latest"


@dataclass
class _Answer:
    decisions: dict[str, Any]
    error: str | None = None


class _Client:
    def __init__(self, decision: str = "drop") -> None:
        self.decision = decision
        self.calls = 0

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
        self.calls += 1
        return _Answer(decisions={candidate_ids[0]: self.decision})


class _Metrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


@dataclass
class _SpyStore:
    """Records whether the CCR sequence was reached at all."""

    calls: list[str] = field(default_factory=list)

    def store(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("store")
        raise RuntimeError("spy store never persists")

    def peek(self, hash_key: str) -> Any:
        self.calls.append("peek")
        return None

    def extend_ttl(self, hash_key: str, ttl: int) -> bool:
        self.calls.append("extend_ttl")
        return False


def _frame(*, with_tool: bool = True) -> str:
    tools = [{"type": "function", "name": CCR_TOOL_NAME}] if with_tool else []
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.6-sol",
                "previous_response_id": "resp_abc123",
                "tools": tools,
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_9",
                        "output": "stdout body",
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )


async def _run(raw: str, **kwargs: Any) -> tuple[str, str]:
    defaults: dict[str, Any] = {
        "jev_config": _Config(),
        "client": _Client(),
        "session_id": "ws1",
        "request_id": "req1",
        "revisions": JevCompactionRevisionStore(),
        "metrics": None,
        "store": CompressionStore(backend=InMemoryBackend()),
    }
    defaults.update(kwargs)
    out, reason = await apply_jev_compaction_boundary(raw, **defaults)
    # The reason vocabulary is a closed contract: Task 26 branches on it, Task 28
    # reports it and Task 29 documents it. A typo'd reason must fail here rather
    # than reach a dashboard as a silent new bucket.
    assert reason in JEV_COMPACTION_REASONS, reason
    return out, reason


# --------------------------------------------------------------------------
# The drop path: the only path allowed to change the bytes.
# --------------------------------------------------------------------------


async def test_drop_replaces_only_the_candidate_body() -> None:
    raw = _frame()
    store = CompressionStore(backend=InMemoryBackend())
    metrics = _Metrics()
    out, reason = await _run(raw, store=store, metrics=metrics)
    assert reason == "jev_compaction_dropped"

    sent = json.loads(out)["response"]
    items = sent["input"]
    assert items[1] == {"type": "compaction_trigger"}
    assert items[0]["type"] == "custom_tool_call_output"
    assert items[0]["call_id"] == "call_9"
    marker = items[0]["output"]
    assert marker.startswith("[") and "Retrieve more: hash=" in marker
    # The envelope around the rewritten item survives untouched.
    assert sent["previous_response_id"] == "resp_abc123"
    assert sent["model"] == "gpt-5.6-sol"
    assert json.loads(out)["type"] == "response.create"

    hash_key = marker.split("hash=")[1].rstrip("]")
    entry = store.retrieve(hash_key)
    assert entry is not None and entry.original_content == "stdout body"
    assert "compaction_dropped" in metrics.events


async def test_the_retained_entry_is_bound_to_the_boundary_branch() -> None:
    # The CCR key is bound to (session, BRANCH, content), and the branch is the
    # boundary's `previous_response_id` -- the anchor Codex hangs this
    # compaction off -- not the session id again. Binding it to the session
    # would let two different branch points of one conversation that produced
    # byte-identical tool output share a single entry, a single lease and a
    # single TTL, so the first branch's expiry would take the second's original
    # with it.
    store = CompressionStore(backend=InMemoryBackend())
    out, reason = await _run(_frame(), session_id="ws-7", store=store)
    assert reason == "jev_compaction_dropped"
    hash_key = json.loads(out)["response"]["input"][0]["output"].split("hash=")[1].rstrip("]")

    assert hash_key == candidate_retention_hash("ws-7", "resp_abc123", "stdout body")
    assert hash_key != candidate_retention_hash("ws-7", "ws-7", "stdout body")

    # Same session, same bytes, a DIFFERENT branch point: a distinct entry.
    other = json.loads(_frame())
    other["response"]["previous_response_id"] = "resp_zzz999"
    out2, reason2 = await _run(json.dumps(other), session_id="ws-7", store=store)
    assert reason2 == "jev_compaction_dropped"
    key2 = json.loads(out2)["response"]["input"][0]["output"].split("hash=")[1].rstrip("]")
    assert key2 != hash_key
    assert store.peek(hash_key) is not None and store.peek(key2) is not None


async def test_a_lease_longer_than_the_store_default_is_taken() -> None:
    # After active retention the CCR entry is the ONLY copy of the original, so
    # it has to outlive the store's short default TTL.
    store = CompressionStore(backend=InMemoryBackend())
    out, reason = await _run(_frame(), store=store)
    assert reason == "jev_compaction_dropped"
    hash_key = json.loads(out)["response"]["input"][0]["output"].split("hash=")[1].rstrip("]")
    assert store.extend_ttl(hash_key, JEV_RETENTION_LEASE_SECONDS) is True


async def test_drop_does_not_touch_a_second_non_candidate_item() -> None:
    # `trigger_index`/`candidate_index` address the ORIGINAL input list, junk
    # included, so a rewrite must never be done by filtering and re-indexing.
    payload = json.loads(_frame())
    payload["response"]["input"].insert(0, {"type": "message", "content": "junk"})
    payload["response"]["input"].insert(2, "a bare string, not an object")
    raw = json.dumps(payload)

    out, reason = await _run(raw)
    assert reason == "jev_compaction_dropped"
    items = json.loads(out)["response"]["input"]
    assert items[0] == {"type": "message", "content": "junk"}
    assert items[2] == "a bare string, not an object"
    assert "Retrieve more: hash=" in items[1]["output"]


async def test_keep_forwards_the_original_bytes_unchanged() -> None:
    raw = _frame()
    out, reason = await _run(raw, client=_Client(decision="keep"))
    assert reason == "jev_compaction_keep"
    assert out is raw


# --------------------------------------------------------------------------
# The thirteen reasons, one gate at a time.
# --------------------------------------------------------------------------


async def test_disabled_when_mode_is_not_active() -> None:
    raw = _frame()
    for mode in ("off", "shadow", "", "  OFF  "):
        out, reason = await _run(raw, jev_config=_Config(mode=mode))
        assert (out, reason) == (raw, "jev_compaction_disabled")
        assert out is raw


async def test_disabled_reaches_its_reason_before_doing_any_work() -> None:
    # An unconfigured proxy must pay nothing: no client call, no store touch,
    # and no revision recorded.
    client = _Client()
    revisions = JevCompactionRevisionStore()
    spy = _SpyStore()
    out, reason = await _run(
        _frame(),
        jev_config=_Config(mode="off"),
        client=client,
        revisions=revisions,
        store=spy,
    )
    assert reason == "jev_compaction_disabled"
    assert client.calls == 0
    assert spy.calls == []
    assert revisions.tracked_revisions == 0
    assert out == _frame()


async def test_not_json() -> None:
    assert await _run("not json") == ("not json", "jev_compaction_not_json")
    assert await _run("") == ("", "jev_compaction_not_json")


async def test_not_response_create_covers_every_shape_decline() -> None:
    # `unwrap_response_create` declines for four distinct causes; all four are
    # this one reason, and all four forward the original bytes.
    bare = json.dumps({"foo": 1})
    assert await _run(bare) == (bare, "jev_compaction_not_response_create")

    cancel = json.dumps({"type": "response.cancel"})
    assert await _run(cancel) == (cancel, "jev_compaction_not_response_create")

    malformed = json.dumps({"type": "response.create", "response": "not an object"})
    assert await _run(malformed) == (malformed, "jev_compaction_not_response_create")

    flattened = json.dumps({"type": "response.create", "input": "not a list"})
    assert await _run(flattened) == (flattened, "jev_compaction_not_response_create")

    not_an_object = json.dumps([1, 2, 3])
    assert await _run(not_an_object) == (not_an_object, "jev_compaction_not_response_create")


async def test_shape_declines_are_distinguishable_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The deferred review item: a stricter unwrap makes non-detection SILENT.
    # The reason string cannot grow a 14th member (Tasks 26/28/29 consume the
    # closed vocabulary), so the cause is carried on the log line instead and an
    # operator can tell the four apart.
    cases = {
        json.dumps({"foo": 1}): "bare_payload_without_input_list",
        json.dumps({"type": "response.cancel"}): "frame_type_is_not_response_create",
        json.dumps({"type": "response.create", "response": 7}): (
            "response_envelope_is_not_an_object"
        ),
        json.dumps({"type": "response.create", "input": "x"}): (
            "flattened_create_without_input_list"
        ),
        json.dumps([1]): "frame_is_not_a_json_object",
    }
    for raw, cause in cases.items():
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="headroom.proxy.jev.compaction_hook"):
            assert (await _run(raw))[1] == "jev_compaction_not_response_create"
        assert cause in caplog.text, (raw, caplog.text)


async def test_no_boundary_for_an_ordinary_create_frame() -> None:
    ordinary = json.dumps({"type": "response.create", "response": {"input": [{"type": "message"}]}})
    assert await _run(ordinary) == (ordinary, "jev_compaction_no_boundary")

    # A well-formed envelope whose `input` is not a list unwraps fine and is
    # declined by DETECTION, so it lands on `no_boundary`, not on
    # `not_response_create`. Documented so the split is deliberate.
    odd_input = json.dumps({"type": "response.create", "response": {"input": "x"}})
    assert await _run(odd_input) == (odd_input, "jev_compaction_no_boundary")

    # Two candidates is a shape Track C did not observe and does not act on.
    payload = json.loads(_frame())
    payload["response"]["input"].append(
        {"type": "function_call_output", "call_id": "call_10", "output": "second"}
    )
    two = json.dumps(payload)
    assert await _run(two) == (two, "jev_compaction_no_boundary")


async def test_missing_identity() -> None:
    raw = _frame()
    assert await _run(raw, session_id="") == (raw, "jev_compaction_missing_identity")


async def test_missing_identity_is_checked_before_the_revision_is_touched() -> None:
    revisions = JevCompactionRevisionStore()
    assert (await _run(_frame(), session_id="", revisions=revisions))[1] == (
        "jev_compaction_missing_identity"
    )
    assert revisions.seen("resp_abc123") is False


async def test_missing_recovery_tool() -> None:
    raw = _frame(with_tool=False)
    assert await _run(raw) == (raw, "jev_compaction_missing_recovery_tool")


async def test_the_recovery_tool_gate_runs_before_any_mutation_or_ccr_write() -> None:
    # The single most important ordering in this module. A marker the model
    # cannot redeem is permanent data loss, so the gate has to close before the
    # decision is asked, before the store is touched, and before anything in the
    # frame is rewritten.
    raw = _frame(with_tool=False)
    client = _Client()
    spy = _SpyStore()
    out, reason = await _run(raw, client=client, store=spy)
    assert reason == "jev_compaction_missing_recovery_tool"
    assert out is raw
    assert client.calls == 0
    assert spy.calls == []


async def test_recovery_tool_is_honoured_from_the_additional_tools_carrier() -> None:
    # Codex >= 0.149.0 carries the declaration inside `input`, not `tools`.
    payload = json.loads(_frame(with_tool=False))
    payload["response"]["input"].append(
        {
            "type": "additional_tools",
            "tools": [{"type": "function", "name": f"mcp__Headroom__{CCR_TOOL_NAME}"}],
        }
    )
    out, reason = await _run(json.dumps(payload))
    assert reason == "jev_compaction_dropped"
    assert "Retrieve more: hash=" in json.loads(out)["response"]["input"][0]["output"]


async def test_no_candidate_when_over_the_token_ceiling() -> None:
    big = json.dumps(
        {
            "type": "response.create",
            "response": {
                "previous_response_id": "resp_abc123",
                "tools": [{"type": "function", "name": CCR_TOOL_NAME}],
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_9",
                        "output": "x" * 5000,
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )
    out, reason = await _run(big, jev_config=_Config(max_candidate_tokens=100))
    assert (out, reason) == (big, "jev_compaction_no_candidate")


async def test_no_candidate_when_the_item_has_no_usable_body() -> None:
    payload = json.loads(_frame())
    payload["response"]["input"][0].pop("output")
    raw = json.dumps(payload)
    out, reason = await _run(raw)
    assert (out, reason) == (raw, "jev_compaction_no_candidate")


async def test_no_client() -> None:
    raw = _frame()
    assert await _run(raw, client=None) == (raw, "jev_compaction_no_client")


async def test_stale_revision_on_a_replay() -> None:
    raw = _frame()
    revisions = JevCompactionRevisionStore()
    first_out, first_reason = await _run(raw, revisions=revisions)
    assert first_reason == "jev_compaction_dropped"
    assert first_out != raw
    assert await _run(raw, revisions=revisions) == (raw, "jev_compaction_stale_revision")


async def test_a_reconnect_replay_is_stale_under_a_new_session_id() -> None:
    # The WS handler mints a fresh uuid4 session id per socket, so a reconnect
    # replay of the same boundary arrives with a DIFFERENT session_id and the
    # same previous_response_id. It must not be dropped a second time.
    raw = _frame()
    revisions = JevCompactionRevisionStore()
    _out, reason = await _run(raw, session_id="ws-connection-1", revisions=revisions)
    assert reason == "jev_compaction_dropped"
    assert await _run(raw, session_id="ws-connection-2", revisions=revisions) == (
        raw,
        "jev_compaction_stale_revision",
    )


async def test_claim_is_the_authoritative_gate_not_the_earlier_seen() -> None:
    # `seen` is a pure read taken early so a known-stale replay costs nothing;
    # `claim` is the locked test-and-set taken late. Treating `seen` as the
    # decision and firing `claim` for its side effect is a TOCTOU that defeats
    # the lock. A store whose `seen` always reports False proves the claim's
    # False return is what actually stops the second drop.
    class _AmnesiacSeen(JevCompactionRevisionStore):
        def seen(self, revision: str) -> bool:
            return False

    raw = _frame()
    revisions = _AmnesiacSeen()
    assert (await _run(raw, revisions=revisions))[1] == "jev_compaction_dropped"

    client = _Client()
    spy = _SpyStore()
    out, reason = await _run(raw, revisions=revisions, client=client, store=spy)
    assert (out, reason) == (raw, "jev_compaction_stale_revision")
    # And the refusal happened BEFORE the decision and before the store.
    assert client.calls == 0
    assert spy.calls == []


async def test_an_unusable_revision_is_refused_by_the_claim() -> None:
    # `claim` declines a revision it will never remember (over-length), and the
    # hook must honour that False rather than proceed unguarded.
    payload = json.loads(_frame())
    payload["response"]["previous_response_id"] = "r" * 600
    raw = json.dumps(payload)
    out, reason = await _run(raw)
    assert (out, reason) == (raw, "jev_compaction_stale_revision")


async def test_a_gate_before_the_decision_does_not_burn_the_revision() -> None:
    # A missing recovery tool, an oversized candidate or a momentarily absent
    # client are all transient: nothing was decided and nothing reached CCR, so
    # the same boundary must still be decidable when it is retried.
    revisions = JevCompactionRevisionStore()
    raw = _frame()

    assert (await _run(_frame(with_tool=False), revisions=revisions))[1] == (
        "jev_compaction_missing_recovery_tool"
    )
    assert (await _run(raw, jev_config=_Config(max_candidate_tokens=1), revisions=revisions)) == (
        raw,
        "jev_compaction_no_candidate",
    )
    assert await _run(raw, client=None, revisions=revisions) == (
        raw,
        "jev_compaction_no_client",
    )
    assert revisions.seen("resp_abc123") is False

    out, reason = await _run(raw, revisions=revisions)
    assert reason == "jev_compaction_dropped"
    assert out != raw
    assert revisions.seen("resp_abc123") is True


async def test_a_keep_answer_still_spends_the_claim() -> None:
    # Everything from the claim onwards is single-shot by design: a retry that
    # is refused as stale loses an optimisation, never content.
    revisions = JevCompactionRevisionStore()
    raw = _frame()
    assert (await _run(raw, client=_Client(decision="keep"), revisions=revisions))[1] == (
        "jev_compaction_keep"
    )
    assert revisions.seen("resp_abc123") is True
    assert await _run(raw, revisions=revisions) == (raw, "jev_compaction_stale_revision")


async def test_ccr_failure_keeps_the_original() -> None:
    class _BrokenStore:
        def store(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("ccr down")

        def exists(self, hash_key: str, clean_expired: bool = False) -> bool:
            return False

    raw = _frame()
    out, reason = await _run(raw, store=_BrokenStore())
    assert (out, reason) == (raw, "jev_compaction_ccr_failed")
    assert out is raw


async def test_ccr_read_back_failure_keeps_the_original() -> None:
    # The write "succeeded" but the acknowledged read-back did not: the shared
    # sequence returns None and the original must survive with no marker.
    class _UnacknowledgedStore:
        def store(self, *args: Any, **kwargs: Any) -> str:
            return str(kwargs["explicit_hash"])

        def peek(self, hash_key: str) -> Any:
            return None

        def extend_ttl(self, hash_key: str, ttl: int) -> bool:  # pragma: no cover
            raise AssertionError("lease must not be reached without acknowledgement")

    raw = _frame()
    out, reason = await _run(raw, store=_UnacknowledgedStore())
    assert (out, reason) == (raw, "jev_compaction_ccr_failed")
    assert "Retrieve more: hash=" not in out


async def test_a_refused_lease_keeps_the_original() -> None:
    real = CompressionStore(backend=InMemoryBackend())

    class _NoLeaseStore:
        def store(self, *args: Any, **kwargs: Any) -> str:
            return str(real.store(*args, **kwargs))

        def peek(self, hash_key: str) -> Any:
            return real.peek(hash_key)

        def extend_ttl(self, hash_key: str, ttl: int) -> bool:
            return False

    raw = _frame()
    out, reason = await _run(raw, store=_NoLeaseStore())
    assert (out, reason) == (raw, "jev_compaction_ccr_failed")
    assert out is raw


async def test_a_refused_rewrite_keeps_the_original(monkeypatch: pytest.MonkeyPatch) -> None:
    # `replace_candidate_output` is content-bound and returns False without
    # mutating if the slot drifted between extraction and commit. The frame that
    # is forwarded then has to be the original string, not a partly rewritten one.
    monkeypatch.setattr(
        "headroom.proxy.jev.compaction_hook.replace_candidate_output",
        lambda *args, **kwargs: False,
    )
    raw = _frame()
    out, reason = await _run(raw)
    assert (out, reason) == (raw, "jev_compaction_ccr_failed")
    assert out is raw


async def test_hook_never_raises() -> None:
    class _Exploding:
        @property
        def mode(self) -> str:
            raise RuntimeError("config blew up")

    raw = _frame()
    assert await _run(raw, jev_config=_Exploding()) == (raw, "jev_compaction_error")


async def test_a_hostile_revision_store_fails_open() -> None:
    class _Hostile:
        def seen(self, revision: str) -> bool:
            raise RuntimeError("revision store blew up")

        def claim(self, revision: str) -> bool:  # pragma: no cover
            raise AssertionError("unreachable")

    raw = _frame()
    assert await _run(raw, revisions=_Hostile()) == (raw, "jev_compaction_error")


async def test_cancelled_error_propagates() -> None:
    class _Cancelling:
        def seen(self, revision: str) -> bool:
            raise asyncio.CancelledError

        def claim(self, revision: str) -> bool:  # pragma: no cover
            raise AssertionError("unreachable")

    with pytest.raises(asyncio.CancelledError):
        await _run(_frame(), revisions=_Cancelling())


# --------------------------------------------------------------------------
# Credentials, metrics and client resolution.
# --------------------------------------------------------------------------


async def test_no_credential_reaches_a_log_line_on_the_error_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _LeakyConfig:
        mode = "active"
        timeout_ms = 5000
        model = "jev-latest"
        endpoint = "https://jev.example.internal/v1/decide"
        api_key = "sk-jev-super-secret-123"

        @property
        def max_candidate_tokens(self) -> int:
            raise RuntimeError(
                "POST https://jev.example.internal/v1/decide failed "
                "(Authorization: Bearer sk-jev-super-secret-123)"
            )

    raw = _frame()
    with caplog.at_level(logging.DEBUG):
        out, reason = await _run(raw, jev_config=_LeakyConfig())
    assert (out, reason) == (raw, "jev_compaction_error")
    assert "sk-jev-super-secret-123" not in caplog.text
    assert "jev.example.internal" not in caplog.text
    # The exception type still survives, so the failure is diagnosable.
    assert "RuntimeError" in caplog.text


async def test_an_exception_detail_is_withheld_when_there_is_nothing_to_scrub_against(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A config double with no endpoint/api_key cannot be scrubbed against, so
    # only the exception TYPE may be logged -- a message that cannot be proven
    # clean is not worth the leak.
    class _Plain:
        mode = "active"

        @property
        def timeout_ms(self) -> int:
            raise RuntimeError("secret-looking-detail-xyz")

    with caplog.at_level(logging.DEBUG):
        assert (await _run(_frame(), jev_config=_Plain()))[1] == "jev_compaction_error"
    assert "secret-looking-detail-xyz" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_metrics_are_optional_and_a_broken_recorder_is_not_an_error() -> None:
    class _BrokenMetrics:
        def record_jev_event(self, event: str) -> None:
            raise RuntimeError("counter exploded")

    raw = _frame()
    assert (await _run(raw, metrics=None))[1] == "jev_compaction_dropped"
    assert (await _run(raw, metrics=object()))[1] == "jev_compaction_dropped"
    assert (await _run(raw, metrics=_BrokenMetrics()))[1] == "jev_compaction_dropped"


async def test_metrics_name_each_gate() -> None:
    expected = {
        "jev_compaction_missing_identity": "compaction_missing_identity",
        "jev_compaction_missing_recovery_tool": "compaction_missing_recovery_tool",
        "jev_compaction_no_candidate": "compaction_no_candidate",
        "jev_compaction_no_client": "compaction_no_client",
        "jev_compaction_keep": "compaction_keep",
        "jev_compaction_ccr_failed": "compaction_ccr_failed",
        "jev_compaction_dropped": "compaction_dropped",
    }
    raw = _frame()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("jev_compaction_missing_identity", {"session_id": ""}),
        ("jev_compaction_no_candidate", {"jev_config": _Config(max_candidate_tokens=1)}),
        ("jev_compaction_no_client", {"client": None}),
        ("jev_compaction_keep", {"client": _Client(decision="keep")}),
        ("jev_compaction_dropped", {}),
    ]
    for reason, kwargs in cases:
        metrics = _Metrics()
        _out, got = await _run(raw, metrics=metrics, **kwargs)
        assert got == reason
        assert expected[reason] in metrics.events

    metrics = _Metrics()
    assert (await _run(_frame(with_tool=False), metrics=metrics))[1] == (
        "jev_compaction_missing_recovery_tool"
    )
    assert "compaction_missing_recovery_tool" in metrics.events


def test_resolve_jev_client_probes_both_attachment_points() -> None:
    class _WithDecide:
        def decide(self, **kwargs: Any) -> Any:
            return None

    class _Proxy:
        pass

    proxy = _Proxy()
    assert resolve_jev_client(proxy) is None

    # Track A's JevShadowRunner holds its bounded JevClient on `_client`.
    shadow_client = _WithDecide()
    proxy.jev_shadow = type("S", (), {"_client": shadow_client})()
    assert resolve_jev_client(proxy) is shadow_client

    direct = _WithDecide()
    proxy.jev_client = direct
    assert resolve_jev_client(proxy) is direct


def test_resolve_jev_client_ignores_an_object_without_decide() -> None:
    class _Proxy:
        pass

    proxy = _Proxy()
    proxy.jev_client = object()
    proxy.jev_shadow = type("S", (), {"_client": object()})()
    assert resolve_jev_client(proxy) is None


def test_resolve_jev_client_never_raises_on_a_hostile_proxy() -> None:
    class _Hostile:
        @property
        def jev_client(self) -> Any:
            raise RuntimeError("attribute blew up")

        @property
        def jev_shadow(self) -> Any:
            raise RuntimeError("attribute blew up")

    assert resolve_jev_client(_Hostile()) is None
    assert resolve_jev_client(None) is None
