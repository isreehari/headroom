"""Track C: one candidate, one question, fail open to keep on anything odd.

``decide_single_candidate`` is the only place in Track C that talks to a remote
service on the WebSocket relay's event loop, and it is the one step whose wrong
answer is irreversible: a ``drop`` replaces a tool result with a marker, a
``keep`` changes nothing. So every ambiguity -- a client that raises, times out,
answers ``None``, answers an object without the fields, reports an error,
returns a verdict this track does not offer, or answers about a candidate we
never asked about -- has to land on exactly ``"keep"``.

Every case below asserts the exact returned string. "Did not raise" is not an
assertion here: a function that returned ``None`` on an odd answer would pass
that and then be truthiness-tested into a silent ``keep``-shaped bug by a
caller, or worse, into a drop.

Two invariants that are not about the verdict are asserted too, because this is
the module that could break them: the API key and endpoint must never reach the
state, the questions or a log line; and ``max_content_chars`` must bound the
candidate body *before* it is put in the state, since the state is what leaves
the machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import pytest

from headroom.proxy.jev.compaction import (
    JevCompactionBoundary,
    JevCompactionCandidate,
    detect_compaction_boundary,
    extract_compaction_candidate,
)
from headroom.proxy.jev.compaction_decision import (
    JEV_DECISION_DROP,
    JEV_DECISION_KEEP,
    build_single_candidate_question,
    build_single_candidate_state,
    decide_single_candidate,
)
from headroom.proxy.jev.config import JevConfig


def _candidate_and_boundary(
    output: str = "stdout body",
) -> tuple[JevCompactionCandidate, JevCompactionBoundary]:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_9", "output": output},
            {"type": "compaction_trigger"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    return candidate, boundary


@dataclass
class _Answer:
    decisions: dict[str, Any]
    error: str | None = None


class _Client:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
        self.calls.append({"state": state, "questions": questions, "candidate_ids": candidate_ids})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


async def _decide(client: Any, timeout_seconds: float = 5.0) -> str:
    candidate, boundary = _candidate_and_boundary()
    return await decide_single_candidate(
        client,
        candidate,
        boundary,
        session_id="ws1",
        model=None,
        timeout_seconds=timeout_seconds,
    )


# --------------------------------------------------------------------------
# The state and the question: exactly one candidate, nothing credential-bearing.
# --------------------------------------------------------------------------


def test_state_and_question_carry_exactly_one_candidate() -> None:
    candidate, boundary = _candidate_and_boundary()
    state = build_single_candidate_state(candidate, boundary, session_id="ws1", model="jev-latest")
    assert state["branch_id"] == "resp_abc123"
    assert state["session_id"] == "ws1"
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["id"] == candidate.candidate_id
    assert state["candidates"][0]["content"] == "stdout body"

    questions = build_single_candidate_question(candidate)
    assert list(questions) == [candidate.candidate_id]
    assert set(questions[candidate.candidate_id]["criteria"]) == {"keep", "drop"}


def test_the_question_never_offers_truncate() -> None:
    # Truncating at a compaction boundary would re-summarize content Codex is
    # already summarizing. Track C only forwards or replaces with a marker.
    candidate, _ = _candidate_and_boundary()
    questions = build_single_candidate_question(candidate)
    assert "truncate" not in questions[candidate.candidate_id]["criteria"]


def test_max_content_chars_bounds_the_state_content() -> None:
    candidate, boundary = _candidate_and_boundary("x" * 5000)
    state = build_single_candidate_state(
        candidate, boundary, session_id="ws1", model=None, max_content_chars=100
    )
    entry = state["candidates"][0]
    assert len(entry["content"]) == 100
    assert entry["content_truncated_for_view"] is True
    # The untruncated body must not survive anywhere else in the state.
    assert "x" * 5000 not in _dumps(state)


def test_content_within_the_bound_is_not_flagged_as_truncated() -> None:
    candidate, boundary = _candidate_and_boundary("short")
    state = build_single_candidate_state(
        candidate, boundary, session_id="ws1", model=None, max_content_chars=100
    )
    assert state["candidates"][0]["content"] == "short"
    assert state["candidates"][0]["content_truncated_for_view"] is False


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


async def test_the_state_and_question_sent_carry_no_credential_material() -> None:
    secret_key = "sk-jev-SUPERSECRET-0123456789"
    secret_endpoint = "https://jev.internal.example/v1/decide"
    config = JevConfig(mode="active", api_key=secret_key, endpoint=secret_endpoint)

    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: "drop"}))
    client._config = config  # type: ignore[attr-defined]

    assert (
        await decide_single_candidate(
            client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )
    sent = _dumps(client.calls[0])
    assert secret_key not in sent
    assert secret_endpoint not in sent
    assert "Authorization" not in sent
    assert "Bearer" not in sent


async def test_the_bound_is_applied_before_the_payload_is_built() -> None:
    # The state the client receives is the thing that leaves the machine, so the
    # truncation has to be in it, not applied to it afterwards.
    candidate, boundary = _candidate_and_boundary("y" * 100000)
    client = _Client(_Answer(decisions={candidate.candidate_id: "keep"}))
    await decide_single_candidate(
        client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
    )
    content = client.calls[0]["state"]["candidates"][0]["content"]
    assert len(content) < 100000
    assert len(content) <= 20000


# --------------------------------------------------------------------------
# The two answers that are honored.
# --------------------------------------------------------------------------


async def test_drop_answer_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: "drop"}))
    decision = await decide_single_candidate(
        client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
    )
    assert decision == JEV_DECISION_DROP
    assert client.calls[0]["candidate_ids"] == [candidate.candidate_id]


async def test_structured_choice_answer_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: {"choice": "drop"}}))
    assert (
        await decide_single_candidate(
            client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )


async def test_structured_decision_key_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: {"decision": "DROP "}}))
    assert (
        await decide_single_candidate(
            client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )


async def test_an_explicit_keep_is_keep() -> None:
    candidate, boundary = _candidate_and_boundary()
    assert (
        await _decide(_Client(_Answer(decisions={candidate.candidate_id: "keep"})))
        == JEV_DECISION_KEEP
    )


async def test_a_missing_error_attribute_still_reads_the_decision() -> None:
    # ``error`` absent is indistinguishable from ``error=None``, and Track A
    # seeds ``decisions`` with ``keep`` for every candidate id before it can
    # fail, so an answer that says ``drop`` with no error field is a real drop.
    candidate, _ = _candidate_and_boundary()

    class _NoErrorField:
        decisions = {candidate.candidate_id: "drop"}

    assert await _decide(_Client(_NoErrorField())) == JEV_DECISION_DROP


# --------------------------------------------------------------------------
# The fail-open matrix. Each entry is its own case and asserts exactly "keep".
# --------------------------------------------------------------------------


def _ambiguous_answers() -> list[tuple[str, Any]]:
    candidate, _ = _candidate_and_boundary()
    cid = candidate.candidate_id

    class _NoDecisionsField:
        error = None

    return [
        ("unknown_verdict_truncate", _Answer(decisions={cid: "truncate"})),
        ("unknown_verdict_gibberish", _Answer(decisions={cid: "maybe-later"})),
        ("empty_verdict", _Answer(decisions={cid: ""})),
        ("error_reported", _Answer(decisions={cid: "drop"}, error="http 500")),
        ("no_decision_for_our_id", _Answer(decisions={})),
        ("decision_for_another_id", _Answer(decisions={"other": "drop"})),
        ("verdict_is_none", _Answer(decisions={cid: None})),
        ("verdict_is_int", _Answer(decisions={cid: 1})),
        ("verdict_is_bool", _Answer(decisions={cid: True})),
        ("verdict_is_list", _Answer(decisions={cid: ["drop"]})),
        ("verdict_dict_unknown_key", _Answer(decisions={cid: {"verdict": "drop"}})),
        ("verdict_dict_non_string_choice", _Answer(decisions={cid: {"choice": 3}})),
        ("decisions_is_a_list", _Answer(decisions=[cid, "drop"])),  # type: ignore[arg-type]
        ("decisions_is_a_string", _Answer(decisions="drop")),  # type: ignore[arg-type]
        ("decisions_is_none", _Answer(decisions=None)),  # type: ignore[arg-type]
        ("answer_missing_both_fields", object()),
        ("answer_missing_decisions_field", _NoDecisionsField()),
        ("answer_is_none", None),
        ("client_raises_runtime_error", RuntimeError("boom")),
        ("client_raises_value_error", ValueError("bad json")),
        ("client_raises_os_error", OSError("connection reset")),
    ]


@pytest.mark.parametrize(
    "answer",
    [pytest.param(answer, id=name) for name, answer in _ambiguous_answers()],
)
async def test_every_ambiguous_answer_keeps(answer: Any) -> None:
    assert await _decide(_Client(answer)) == JEV_DECISION_KEEP


async def test_every_ambiguous_answer_keeps_as_a_batch() -> None:
    # The brief's own loop, kept verbatim alongside the parametrized matrix.
    candidate, boundary = _candidate_and_boundary()
    cases: list[Any] = [
        _Answer(decisions={candidate.candidate_id: "truncate"}),
        _Answer(decisions={candidate.candidate_id: "drop"}, error="http 500"),
        _Answer(decisions={}),
        _Answer(decisions={"other": "drop"}),
        object(),
        None,
        RuntimeError("boom"),
    ]
    for case in cases:
        assert (
            await decide_single_candidate(
                _Client(case),
                candidate,
                boundary,
                session_id="ws1",
                model=None,
                timeout_seconds=5.0,
            )
            == JEV_DECISION_KEEP
        )


async def test_a_client_without_a_decide_attribute_keeps() -> None:
    class _NotAClient:
        pass

    assert await _decide(_NotAClient()) == JEV_DECISION_KEEP


async def test_a_client_whose_decide_attribute_raises_keeps() -> None:
    class _ExplodingAttribute:
        @property
        def decide(self) -> Any:
            raise RuntimeError("no client configured")

    assert await _decide(_ExplodingAttribute()) == JEV_DECISION_KEEP


async def test_a_client_rejecting_the_keyword_contract_keeps() -> None:
    class _WrongSignature:
        async def decide(self, payload: Any) -> Any:  # no keyword params
            return _Answer(decisions={})

    assert await _decide(_WrongSignature()) == JEV_DECISION_KEEP


# --------------------------------------------------------------------------
# The timeout bounds BOTH client shapes.
# --------------------------------------------------------------------------


async def test_slow_client_times_out_to_keep() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Slow:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            await asyncio.sleep(1.0)
            return _Answer(decisions={candidate.candidate_id: "drop"})

    assert (
        await decide_single_candidate(
            _Slow(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=0.05
        )
        == JEV_DECISION_KEEP
    )


async def test_a_slow_SYNCHRONOUS_client_also_times_out_and_never_blocks_the_loop() -> None:
    # A blocking `decide` is inside the stated interface ("both a coroutine and
    # a plain return are accepted"), and this runs on the WebSocket relay's
    # event loop: calling it inline would freeze every other session for its
    # whole duration and no timeout could fire. It must be offloaded.
    candidate, boundary = _candidate_and_boundary()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    class _SlowSync:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            loop.call_soon_threadsafe(started.set)
            time.sleep(1.0)
            return _Answer(decisions={candidate.candidate_id: "drop"})

    async def _heartbeat() -> int:
        beats = 0
        while not decided.done():
            await asyncio.sleep(0.01)
            beats += 1
        return beats

    decided = asyncio.ensure_future(
        decide_single_candidate(
            _SlowSync(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=0.05
        )
    )
    beats = await _heartbeat()
    assert await decided == JEV_DECISION_KEEP
    # The loop kept running while the blocking call sat in its worker thread.
    assert started.is_set()
    assert beats >= 2
    # Let the orphaned worker thread finish before the loop closes.
    await asyncio.sleep(1.1)


async def test_a_fast_synchronous_client_is_still_honored() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Sync:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            return _Answer(decisions={candidate.candidate_id: "drop"})

    assert (
        await decide_single_candidate(
            _Sync(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )


async def test_a_synchronous_client_that_hands_back_an_awaitable_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _SyncReturningCoroutine:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            async def _later() -> Any:
                return _Answer(decisions={candidate.candidate_id: "drop"})

            return _later()

    assert (
        await decide_single_candidate(
            _SyncReturningCoroutine(),
            candidate,
            boundary,
            session_id="ws1",
            model=None,
            timeout_seconds=5.0,
        )
        == JEV_DECISION_DROP
    )


async def test_a_synchronous_client_whose_awaitable_hangs_keeps() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _SyncReturningSlowCoroutine:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            async def _later() -> Any:
                await asyncio.sleep(1.0)
                return _Answer(decisions={candidate.candidate_id: "drop"})

            return _later()

    assert (
        await decide_single_candidate(
            _SyncReturningSlowCoroutine(),
            candidate,
            boundary,
            session_id="ws1",
            model=None,
            timeout_seconds=0.05,
        )
        == JEV_DECISION_KEEP
    )


async def test_a_zero_timeout_keeps_without_calling_a_slow_client() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Slow:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            await asyncio.sleep(1.0)
            return _Answer(decisions={candidate.candidate_id: "drop"})

    assert (
        await decide_single_candidate(
            _Slow(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=0.0
        )
        == JEV_DECISION_KEEP
    )


# --------------------------------------------------------------------------
# Cancellation is not a degradation: it must propagate.
# --------------------------------------------------------------------------


async def test_cancellation_propagates_and_is_never_swallowed() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Cancelled:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await decide_single_candidate(
            _Cancelled(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )


async def test_an_outer_cancel_is_not_turned_into_keep() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Hangs:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            await asyncio.sleep(10.0)

    task = asyncio.ensure_future(
        decide_single_candidate(
            _Hangs(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=30.0
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------
# Nothing credential-bearing reaches a log line.
# --------------------------------------------------------------------------


async def test_a_failing_client_logs_nothing_credential_bearing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "sk-jev-SUPERSECRET-0123456789"
    secret_endpoint = "https://jev.internal.example/v1/decide"
    config = JevConfig(mode="active", api_key=secret_key, endpoint=secret_endpoint)

    candidate, boundary = _candidate_and_boundary()
    # httpx routinely echoes the request URL into its exception messages, and a
    # gateway can echo the Authorization header back in an error body.
    boom = RuntimeError(f"POST {secret_endpoint} failed, Authorization: Bearer {secret_key}")
    client = _Client(boom)
    client._config = config  # type: ignore[attr-defined]

    with caplog.at_level(logging.DEBUG, logger="headroom.proxy.jev.compaction_decision"):
        assert (
            await decide_single_candidate(
                client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
            )
            == JEV_DECISION_KEEP
        )
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered  # the failure is observable at all
    assert secret_key not in rendered
    assert secret_endpoint not in rendered
    assert "RuntimeError" in rendered


async def test_a_failing_client_with_no_reachable_config_logs_no_message_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # With no config there is nothing to scrub against, so the exception's own
    # text must not be rendered at all -- only its type.
    secret = "sk-jev-UNSCRUBBABLE-9876543210"
    client = _Client(RuntimeError(f"leaking {secret}"))

    with caplog.at_level(logging.DEBUG, logger="headroom.proxy.jev.compaction_decision"):
        assert await _decide(client) == JEV_DECISION_KEEP
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in rendered
    assert "RuntimeError" in rendered
