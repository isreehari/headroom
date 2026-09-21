"""Candidate eligibility: allowlisted tool-result shapes, outside the 6-message
recent tail, outside the frozen prefix, bounded in number."""

from __future__ import annotations

from typing import Any

from headroom.proxy.jev.candidates import (
    RECENT_TAIL_EXCLUSION,
    JevCandidate,
    count_messages_corrected,
    select_candidates,
    text_of,
)


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _padding(n: int) -> list[dict[str, object]]:
    return [{"role": "assistant", "content": f"filler {i}"} for i in range(n)]


def test_selects_openai_chat_tool_messages() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "call_1", "content": "RESULT BODY"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=10)
    assert [c.candidate_id for c in found] == ["cand_0000"]
    assert found[0].candidate_type == "tool_result"
    assert found[0].message_index == 1
    assert found[0].block_index is None
    assert found[0].tool_call_id == "call_1"
    assert found[0].content == "RESULT BODY"


def test_selects_responses_function_call_output_items() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"type": "function_call_output", "call_id": "fc_1", "output": "PAYLOAD"},
        {"type": "custom_tool_call_output", "call_id": "ct_1", "output": "PAYLOAD2"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=10)
    assert [c.candidate_type for c in found] == [
        "function_call_output",
        "custom_tool_call_output",
    ]
    assert [c.content for c in found] == ["PAYLOAD", "PAYLOAD2"]


def test_selects_anthropic_tool_result_blocks_with_block_index() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "ignore me"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "BLOCK BODY"},
            ],
        },
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=10)
    assert len(found) == 1
    assert found[0].block_index == 1
    assert found[0].tool_call_id == "tu_1"
    assert found[0].content == "BLOCK BODY"


def test_recent_tail_is_never_eligible() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(10)]
    found = select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
    # 10 messages, last 6 excluded -> indices 0..3 remain.
    assert [c.message_index for c in found] == [0, 1, 2, 3]


def test_frozen_prefix_is_never_eligible() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(10)]
    found = select_candidates(messages, frozen_prefix=2, count_text=_count_text, max_candidates=10)
    assert [c.message_index for c in found] == [2, 3]


def test_max_candidates_keeps_the_oldest() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(20)]
    found = select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=3)
    assert [c.message_index for c in found] == [0, 1, 2]
    assert [c.candidate_id for c in found] == ["cand_0000", "cand_0001", "cand_0002"]


def test_no_candidates_when_nothing_is_eligible() -> None:
    messages = [{"role": "user", "content": "hello"}, *_padding(RECENT_TAIL_EXCLUSION)]
    assert (
        select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
        == []
    )


def test_fingerprint_tracks_content() -> None:
    a = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "body", 1)
    b = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "body", 1)
    c = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "other", 1)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_count_messages_corrected_prices_responses_output_payloads() -> None:
    # A Responses function_call_output carries its payload in `output`, which a
    # `content`-only counter prices at ~0 -- the Phase 0a token-accounting bug.
    messages = [{"type": "function_call_output", "call_id": "fc_1", "output": "x" * 400}]

    def naive_count_messages(msgs: list[dict[str, object]]) -> int:
        return 0

    assert (
        count_messages_corrected(
            messages, count_messages=naive_count_messages, count_text=_count_text
        )
        == 100
    )


# --- fail-open hardening on request-shaped data -----------------------------


def test_text_of_never_raises_on_odd_values() -> None:
    circular: list[Any] = []
    circular.append(circular)
    unhashable_keys = {(1, 2): "tuple key is not JSON-serialisable"}

    for value in (circular, unhashable_keys, {1: object()}, b"bytes", object()):
        assert isinstance(text_of(value), str)

    assert text_of("plain") == "plain"
    # A missing payload is empty text, not the literal string "null": the
    # content is what later phases project and price, and "null" is noise.
    assert text_of(None) == ""


def test_non_string_item_type_never_raises() -> None:
    # `msg.get("type") in frozenset(...)` raises TypeError on an unhashable
    # value, and a JSON body can carry `{"type": []}`.
    messages: list[dict[str, Any]] = [
        {"type": [], "output": "ignored"},
        {"type": {"nested": True}, "output": "ignored"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    assert (
        select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
        == []
    )
    assert (
        count_messages_corrected(messages, count_messages=lambda _msgs: 7, count_text=_count_text)
        == 7
    )


def test_malformed_messages_are_skipped_not_fatal() -> None:
    messages: list[Any] = [
        None,
        "a bare string",
        42,
        {},  # no role, no type, no content
        {"role": "tool"},  # role but no content
        {"role": "user", "content": [None, "text", {"type": "tool_result"}]},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
    # Only the tool_result block survives, with an empty (missing) body.
    assert [(c.message_index, c.block_index, c.content) for c in found] == [(5, 2, "")]
    assert found[0].tool_call_id is None
    assert found[0].role == "user"


def test_messages_that_are_not_a_list_yield_no_candidates() -> None:
    assert (
        select_candidates(
            None,  # type: ignore[arg-type]
            frozen_prefix=0,
            count_text=_count_text,
            max_candidates=10,
        )
        == []
    )
    assert (
        count_messages_corrected(
            None,  # type: ignore[arg-type]
            count_messages=lambda _msgs: 0,
            count_text=_count_text,
        )
        == 0
    )


def test_short_conversation_is_entirely_inside_the_recent_tail() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(3)]
    assert (
        select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
        == []
    )


def test_out_of_range_bounds_are_clamped() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(10)]
    # A negative tail must not wrap around and exclude the whole list.
    assert (
        len(
            select_candidates(
                messages,
                frozen_prefix=-5,
                count_text=_count_text,
                max_candidates=10,
                recent_tail=-1,
            )
        )
        == 10
    )
    assert (
        select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=0) == []
    )
    assert (
        select_candidates(messages, frozen_prefix=99, count_text=_count_text, max_candidates=10)
        == []
    )


def test_est_tokens_uses_the_caller_supplied_counter() -> None:
    calls: list[str] = []

    def counter(text: str) -> int:
        calls.append(text)
        return 4242

    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": "body"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=0, count_text=counter, max_candidates=10)
    assert [c.est_tokens for c in found] == [4242]
    assert calls == ["body"]


def test_a_raising_counter_degrades_instead_of_failing_the_request() -> None:
    def boom(text: str) -> int:
        raise RuntimeError("tokenizer exploded")

    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 40},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=0, count_text=boom, max_candidates=10)
    assert [c.est_tokens for c in found] == [10]

    def boom_messages(msgs: list[dict[str, Any]]) -> int:
        raise RuntimeError("counter exploded")

    assert (
        count_messages_corrected(
            [{"type": "function_call_output", "output": "y" * 40}],
            count_messages=boom_messages,
            count_text=boom,
        )
        == 10
    )


def test_non_string_ids_are_coerced_to_text() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": 17, "content": "a"},
        {"type": "function_call_output", "id": 99, "output": "b"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(messages, frozen_prefix=0, count_text=_count_text, max_candidates=10)
    assert [c.tool_call_id for c in found] == ["17", "99"]


def test_count_messages_corrected_does_not_double_count_content() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hello"},
        {"type": "function_call_output", "output": "z" * 40},
    ]
    # Happy path: the caller's counter covers `content`, we only add `output`.
    assert (
        count_messages_corrected(messages, count_messages=lambda _msgs: 50, count_text=_count_text)
        == 60
    )

    # Fallback path: the caller's counter blew up, so `content` is priced here
    # and `output` still exactly once.
    def boom_messages(msgs: list[dict[str, Any]]) -> int:
        raise RuntimeError("nope")

    assert (
        count_messages_corrected(messages, count_messages=boom_messages, count_text=_count_text)
        == 11
    )
