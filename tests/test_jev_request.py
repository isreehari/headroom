"""Retention view / questions shape, and the measured (not estimated) request budget."""

from __future__ import annotations

import json
from typing import Any

import pytest

from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.client import build_request_payload
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.request import (
    MIN_VIEW_TOKENS,
    build_questions,
    build_retention_state,
    enforce_state_budget,
)

CONFIG = JevConfig(mode="shadow", api_key="sk-test", model="jev-latest")


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _candidate(i: int, body: str) -> JevCandidate:
    return JevCandidate(
        candidate_id=f"cand_{i:04d}",
        message_index=i + 1,
        block_index=None,
        candidate_type="tool_result",
        role="tool",
        tool_call_id=f"call_{i}",
        content=body,
        est_tokens=_count_text(body),
    )


def _make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, object]:
    state = build_retention_state(
        provider="openai",
        model="gpt-5.6",
        jev_model="jev-latest",
        session_id="s",
        branch_id="b",
        revision="r",
        message_shape="openai",
        total_messages=30,
        frozen_prefix=1,
        recent_tail=6,
        candidates=sel,
        max_candidate_tokens=view_tokens,
    )
    return build_request_payload(CONFIG, state, build_questions(sel, 30))


def _measure(sel: list[JevCandidate], view_tokens: int) -> int:
    return _count_text(json.dumps(_make_payload(sel, view_tokens), default=str))


def test_retention_state_carries_identity_and_bounded_candidate_views() -> None:
    cands = [_candidate(0, "y" * 4000)]
    state = build_retention_state(
        provider="openai",
        model="gpt-5.6",
        jev_model="jev-latest",
        session_id="sess",
        branch_id="branch",
        revision="rev",
        message_shape="openai",
        total_messages=30,
        frozen_prefix=1,
        recent_tail=6,
        candidates=cands,
        max_candidate_tokens=100,  # -> 400 chars of view
    )
    assert state["session_id"] == "sess"
    assert state["branch_id"] == "branch"
    assert state["revision"] == "rev"
    assert state["protected_prefix_messages"] == 1
    assert state["recent_tail_excluded_messages"] == 6
    entry = state["candidates"][0]
    assert entry["candidate_id"] == "cand_0000"
    assert len(entry["content"]) == 400
    assert entry["content_truncated_for_view"] is True
    # The true size is still reported, so nobody is misled about what was shown.
    assert entry["estimated_tokens"] == 1000
    assert entry["content_sha256"] == cands[0].content_sha256


def test_questions_are_one_choice_per_candidate() -> None:
    questions = build_questions([_candidate(0, "body"), _candidate(1, "body")], 30)
    assert set(questions) == {"cand_0000", "cand_0001"}
    assert questions["cand_0000"]["type"] == "choice"
    assert set(questions["cand_0000"]["criteria"]) == {"keep", "truncate", "drop"}
    assert "cand_0000" in questions["cand_0000"]["instructions"]


def test_budget_trims_trailing_candidates_until_the_real_payload_fits() -> None:
    cands = [_candidate(i, "z" * 8000) for i in range(6)]

    def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, object]:
        state = build_retention_state(
            provider="openai",
            model="gpt-5.6",
            jev_model="jev-latest",
            session_id="s",
            branch_id="b",
            revision="r",
            message_shape="openai",
            total_messages=30,
            frozen_prefix=1,
            recent_tail=6,
            candidates=sel,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(CONFIG, state, build_questions(sel, 30))

    kept, view_tokens, serialized = enforce_state_budget(
        cands,
        count_text=_count_text,
        make_payload=make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=2000,
    )
    assert len(kept) < len(cands)
    assert view_tokens >= MIN_VIEW_TOKENS
    assert serialized <= 2000
    # The measured size is the size of the payload actually sent.
    assert _count_text(json.dumps(make_payload(kept, view_tokens), default=str)) == serialized


def test_budget_returns_nothing_when_even_one_candidate_will_not_fit() -> None:
    def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, object]:
        state = build_retention_state(
            provider="openai",
            model="gpt-5.6",
            jev_model="jev-latest",
            session_id="s",
            branch_id="b",
            revision="r",
            message_shape="openai",
            total_messages=30,
            frozen_prefix=1,
            recent_tail=6,
            candidates=sel,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(CONFIG, state, build_questions(sel, 30))

    kept, _view, _serialized = enforce_state_budget(
        [_candidate(0, "z" * 8000)],
        count_text=_count_text,
        make_payload=make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=10,
    )
    assert kept == []


# --- the retention view -------------------------------------------------


def test_retention_view_is_deterministic_and_is_a_prefix_of_the_content() -> None:
    cand = _candidate(0, "".join(chr(0x41 + (i % 26)) for i in range(5000)))
    kwargs: dict[str, Any] = {
        "provider": "openai",
        "model": "gpt-5.6",
        "jev_model": "jev-latest",
        "session_id": "s",
        "branch_id": "b",
        "revision": "r",
        "message_shape": "openai",
        "total_messages": 30,
        "frozen_prefix": 1,
        "recent_tail": 6,
        "candidates": [cand],
        "max_candidate_tokens": 64,
    }
    first = build_retention_state(**kwargs)  # type: ignore[arg-type]
    second = build_retention_state(**kwargs)  # type: ignore[arg-type]
    assert first == second
    entry = first["candidates"][0]
    assert entry["content"] == cand.content[:256]
    # The honest metadata still describes the whole candidate, not the view.
    assert entry["estimated_tokens"] == cand.est_tokens
    assert entry["content_sha256"] == cand.content_sha256
    assert entry["order_from_end"] == 30 - cand.message_index


def test_short_candidates_are_not_marked_truncated() -> None:
    cand = _candidate(0, "small body")
    state = build_retention_state(
        provider="anthropic",
        model="claude",
        jev_model="jev-latest",
        session_id="s",
        branch_id="b",
        revision="r",
        message_shape="anthropic",
        total_messages=30,
        frozen_prefix=0,
        recent_tail=6,
        candidates=[cand],
        max_candidate_tokens=20000,
    )
    entry = state["candidates"][0]
    assert entry["content"] == "small body"
    assert entry["content_truncated_for_view"] is False


def test_state_and_payload_never_carry_the_api_key_or_the_endpoint() -> None:
    config = JevConfig(
        mode="shadow",
        api_key="sk-super-secret",
        endpoint="https://user:pw@jev.example.internal/v1/systemone?token=abc",
        model="jev-latest",
    )
    cands = [_candidate(i, "payload") for i in range(2)]
    state = build_retention_state(
        provider="openai",
        model="gpt-5.6",
        jev_model=config.model,
        session_id="s",
        branch_id="b",
        revision="r",
        message_shape="openai",
        total_messages=30,
        frozen_prefix=1,
        recent_tail=6,
        candidates=cands,
        max_candidate_tokens=200,
    )
    blob = json.dumps(build_request_payload(config, state, build_questions(cands, 30)))
    assert "sk-super-secret" not in blob
    assert "jev.example.internal" not in blob
    assert "token=abc" not in blob


# --- the measured budget ------------------------------------------------


def test_budget_keeps_the_oldest_candidates_and_drops_the_newest_tail() -> None:
    cands = [_candidate(i, "z" * 8000) for i in range(6)]
    kept, _view, _serialized = enforce_state_budget(
        cands,
        count_text=_count_text,
        make_payload=_make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=2000,
    )
    assert kept, "expected at least one candidate to survive a 2000-token budget"
    assert kept == cands[: len(kept)]


def test_budget_shrinks_the_view_rather_than_giving_up_on_a_candidate() -> None:
    """Deviation from the plan's reference: the view is a second lever.

    The plan's code derived the view once from ``max_state_tokens // n`` and
    then only dropped candidates, so a budget that fits a floor-width view but
    not the derived one returned nothing at all -- an avoidable lost call.
    """
    cand = _candidate(0, "z" * 8000)
    # Just enough room for a floor-width view of this candidate...
    budget = _measure([cand], MIN_VIEW_TOKENS) + 5
    # ...and provably not enough for the share-derived (= whole budget) view.
    assert _measure([cand], budget) > budget

    kept, view_tokens, serialized = enforce_state_budget(
        [cand],
        count_text=_count_text,
        make_payload=_make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=budget,
    )
    assert kept == [cand]
    assert view_tokens == MIN_VIEW_TOKENS
    assert serialized <= budget
    assert serialized == _measure(kept, view_tokens)


def test_budget_never_shrinks_the_view_below_the_floor() -> None:
    kept, view_tokens, _serialized = enforce_state_budget(
        [_candidate(0, "z" * 8000)],
        count_text=_count_text,
        make_payload=_make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=10,
    )
    assert kept == []
    assert view_tokens == MIN_VIEW_TOKENS


def test_budget_respects_max_candidate_tokens_as_a_ceiling() -> None:
    cands = [_candidate(i, "z" * 200) for i in range(3)]
    kept, view_tokens, serialized = enforce_state_budget(
        cands,
        count_text=_count_text,
        make_payload=_make_payload,
        max_candidate_tokens=300,
        max_state_tokens=1_000_000,
    )
    assert kept == cands
    assert view_tokens == 300  # not the (enormous) per-candidate share
    assert serialized == _measure(kept, view_tokens)


def test_budget_with_no_candidates_measures_nothing() -> None:
    kept, view_tokens, serialized = enforce_state_budget(
        [],
        count_text=_count_text,
        make_payload=_make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=8000,
    )
    assert kept == []
    assert view_tokens >= MIN_VIEW_TOKENS
    assert serialized == 0


def test_budget_propagates_a_failing_tokenizer() -> None:
    """Per Task 5's ruling: a broken tokenizer is the outer hook's to absorb."""

    def boom(text: str) -> int:
        raise RuntimeError("tokenizer exploded")

    with pytest.raises(RuntimeError, match="tokenizer exploded"):
        enforce_state_budget(
            [_candidate(0, "z" * 100)],
            count_text=boom,
            make_payload=_make_payload,
            max_candidate_tokens=20000,
            max_state_tokens=8000,
        )


def test_budget_is_bounded_in_work_and_terminates_on_a_hostile_measurer() -> None:
    """A measurer that always overflows must still terminate, and cheaply."""
    calls = 0

    def always_huge(text: str) -> int:
        nonlocal calls
        calls += 1
        return 10**9

    kept, view_tokens, _serialized = enforce_state_budget(
        [_candidate(i, "z" * 500) for i in range(8)],
        count_text=always_huge,
        make_payload=_make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=8000,
    )
    assert kept == []
    assert view_tokens == MIN_VIEW_TOKENS
    assert calls < 200
