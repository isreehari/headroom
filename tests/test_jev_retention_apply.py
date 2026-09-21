"""Applying keep/truncate/drop to the forwarded conversation.

Unlike the shadow projection (which deletes messages on a private copy), active
mode REPLACES content with a CCR retrieval marker and never removes a message:
removing a tool result orphans its tool_call and the provider rejects the turn.
"""

from __future__ import annotations

import json
from typing import Any

from headroom.proxy.jev.candidates import JevCandidate, text_of
from headroom.proxy.jev.retention_apply import JEV_TRUNCATE_CHARS, apply_retention
from headroom.proxy.jev.retention_ccr import RetentionLease
from headroom.proxy.jev.shadow import TRUNCATE_CHARS


def _lease(candidate_id: str, hash_key: str) -> RetentionLease:
    return RetentionLease(
        candidate_id=candidate_id,
        hash_key=hash_key,
        marker=f"Retrieve original: hash={hash_key}",
        original_tokens=100,
        lease_seconds=86_400,
    )


def _candidate(
    candidate_id: str,
    message_index: int,
    candidate_type: str,
    content: str,
    block_index: int | None = None,
    role: str = "tool",
) -> JevCandidate:
    return JevCandidate(
        candidate_id=candidate_id,
        message_index=message_index,
        block_index=block_index,
        candidate_type=candidate_type,
        role=role,
        tool_call_id="call_1",
        content=content,
        est_tokens=100,
    )


def test_drop_replaces_openai_chat_tool_content_with_the_marker() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"},
    ]
    cand = _candidate("c0", 1, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "a" * 24)})
    assert applied == ["c0"]
    assert out[1]["content"] == f"Retrieve original: hash={'a' * 24}"
    assert out[1]["tool_call_id"] == "call_1"  # the message itself survives
    assert len(out) == len(messages)
    assert messages[1]["content"] == "BIG OUTPUT"  # input untouched


def test_truncate_keeps_a_head_and_appends_the_marker() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "X" * 5000}
    ]
    cand = _candidate("c0", 0, "tool_result", "X" * 5000)
    out, applied = apply_retention(
        messages,
        [cand],
        {"c0": "truncate"},
        {"c0": _lease("c0", "b" * 24)},
        truncate_chars=10,
    )
    assert applied == ["c0"]
    assert out[0]["content"].startswith("X" * 10)
    assert out[0]["content"].endswith(f"Retrieve original: hash={'b' * 24}")
    assert "X" * 11 not in out[0]["content"]


def test_keep_changes_nothing() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}
    ]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {"c0": "keep"}, {"c0": _lease("c0", "c" * 24)})
    assert applied == []
    assert out == messages


def test_candidate_without_a_lease_is_never_applied() -> None:
    """A failed CCR step keeps the original — this is the last line of that rule."""
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}
    ]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {})
    assert applied == []
    assert out == messages


def test_responses_output_slots_are_supported() -> None:
    messages: list[dict[str, Any]] = [
        {"type": "function_call_output", "call_id": "call_1", "output": "FN OUT"},
        {"type": "custom_tool_call_output", "call_id": "call_2", "output": "CUSTOM OUT"},
    ]
    cands = [
        _candidate("c0", 0, "function_call_output", "FN OUT"),
        _candidate("c1", 1, "custom_tool_call_output", "CUSTOM OUT"),
    ]
    out, applied = apply_retention(
        messages,
        cands,
        {"c0": "drop", "c1": "drop"},
        {"c0": _lease("c0", "d" * 24), "c1": _lease("c1", "e" * 24)},
    )
    assert applied == ["c0", "c1"]
    assert out[0]["output"] == f"Retrieve original: hash={'d' * 24}"
    assert out[1]["output"] == f"Retrieve original: hash={'e' * 24}"


def test_anthropic_tool_result_block_is_supported() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here you go"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "BLOCK OUTPUT"},
            ],
        }
    ]
    cand = _candidate("c0", 0, "tool_result", "BLOCK OUTPUT", block_index=1)
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "f" * 24)})
    assert applied == ["c0"]
    assert out[0]["content"][0] == {"type": "text", "text": "here you go"}
    assert out[0]["content"][1]["content"] == f"Retrieve original: hash={'f' * 24}"
    assert out[0]["content"][1]["tool_use_id"] == "tu_1"


def test_stale_candidate_index_is_skipped() -> None:
    """A candidate whose slot no longer holds its content is left alone."""
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "SOMETHING ELSE"}
    ]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "0" * 24)})
    assert applied == []
    assert out == messages


# --- beyond the brief -------------------------------------------------------


def test_default_truncate_width_matches_the_shadow_projection() -> None:
    """TP (Track A) and TF (Track B) must truncate at the same width."""
    assert JEV_TRUNCATE_CHARS == TRUNCATE_CHARS == 400


def test_anthropic_list_content_block_flattens_to_a_string() -> None:
    """``cand.content`` is the JSON of a LIST payload; the slot must re-derive it.

    Track B replaces the block payload with a plain string, which is a valid
    ``tool_result.content`` wire shape, and keeps every other envelope key.
    """
    payload = [{"type": "text", "text": "LIST OUTPUT"}]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "is_error": False,
                    "cache_control": {"type": "ephemeral"},
                    "content": payload,
                }
            ],
        }
    ]
    cand = _candidate("c0", 0, "tool_result", text_of(payload), block_index=0, role="user")
    assert cand.content == json.dumps(payload)
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "1" * 24)})
    assert applied == ["c0"]
    block = out[0]["content"][0]
    assert block == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "is_error": False,
        "cache_control": {"type": "ephemeral"},
        "content": f"Retrieve original: hash={'1' * 24}",
    }
    assert messages[0]["content"][0]["content"] == payload  # input untouched


def test_truncate_shorter_than_the_limit_is_a_no_op() -> None:
    """The original is already fully inline: appending a marker only ADDS tokens.

    Track A's ``shadow._truncated`` leaves such content alone as well, so the
    projection and the realized rewrite stay in step.
    """
    messages: list[dict[str, Any]] = [{"role": "tool", "tool_call_id": "call_1", "content": "tiny"}]
    cand = _candidate("c0", 0, "tool_result", "tiny")
    out, applied = apply_retention(
        messages,
        [cand],
        {"c0": "truncate"},
        {"c0": _lease("c0", "2" * 24)},
        truncate_chars=400,
    )
    assert applied == []
    assert out == messages


def test_truncate_exactly_at_the_limit_is_a_no_op() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "Y" * 10}
    ]
    cand = _candidate("c0", 0, "tool_result", "Y" * 10)
    out, applied = apply_retention(
        messages,
        [cand],
        {"c0": "truncate"},
        {"c0": _lease("c0", "3" * 24)},
        truncate_chars=10,
    )
    assert applied == []
    assert out[0]["content"] == "Y" * 10


def test_result_is_a_deep_copy_even_when_nothing_is_applied() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]
    out, applied = apply_retention(messages, [], {}, {})
    assert applied == []
    assert out == messages
    out[0]["content"][0]["text"] = "mutated"
    assert messages[0]["content"][0]["text"] == "hi"


def test_an_explicit_null_output_still_matches_its_candidate() -> None:
    """``text_of(None)`` is ``"null"`` on both sides, so the lease can apply."""
    messages: list[dict[str, Any]] = [
        {"type": "function_call_output", "call_id": "call_1", "output": None}
    ]
    cand = _candidate("c0", 0, "function_call_output", "null")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "4" * 24)})
    assert applied == ["c0"]
    assert out[0]["output"] == f"Retrieve original: hash={'4' * 24}"


def test_an_unknown_decision_is_treated_as_keep() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}
    ]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(
        messages, [cand], {"c0": "summarize"}, {"c0": _lease("c0", "5" * 24)}
    )
    assert applied == []
    assert out == messages


def test_a_candidate_with_no_decision_at_all_is_kept() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}
    ]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {}, {"c0": _lease("c0", "6" * 24)})
    assert applied == []
    assert out == messages


def test_out_of_range_and_malformed_slots_are_skipped() -> None:
    messages: list[dict[str, Any]] = [
        "not a dict",  # type: ignore[list-item]
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"},
        {"role": "user", "content": "not a block list"},
        {"role": "user", "content": [{"type": "text", "text": "BIG OUTPUT"}]},
    ]
    cands = [
        _candidate("past_end", 99, "tool_result", "BIG OUTPUT"),
        _candidate("negative", -1, "tool_result", "BIG OUTPUT"),
        _candidate("not_a_dict", 0, "tool_result", "BIG OUTPUT"),
        _candidate("block_on_a_string", 2, "tool_result", "BIG OUTPUT", block_index=0),
        _candidate("block_past_end", 3, "tool_result", "BIG OUTPUT", block_index=7),
        _candidate("not_a_tool_result", 3, "tool_result", "BIG OUTPUT", block_index=0),
    ]
    decisions = {cand.candidate_id: "drop" for cand in cands}
    leases = {cand.candidate_id: _lease(cand.candidate_id, "7" * 24) for cand in cands}
    out, applied = apply_retention(messages, cands, decisions, leases)
    assert applied == []
    assert out == messages


def test_an_output_item_that_changed_type_is_skipped() -> None:
    """The envelope moved under the index: the lease is not for these bytes."""
    messages: list[dict[str, Any]] = [
        {"type": "function_call", "call_id": "call_1", "output": "FN OUT"}
    ]
    cand = _candidate("c0", 0, "function_call_output", "FN OUT")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "8" * 24)})
    assert applied == []
    assert out == messages


def test_two_candidates_on_one_slot_apply_only_once() -> None:
    """The second candidate's content no longer matches, so it is left alone."""
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}
    ]
    cands = [
        _candidate("c0", 0, "tool_result", "BIG OUTPUT"),
        _candidate("c1", 0, "tool_result", "BIG OUTPUT"),
    ]
    out, applied = apply_retention(
        messages,
        cands,
        {"c0": "drop", "c1": "drop"},
        {"c0": _lease("c0", "9" * 24), "c1": _lease("c1", "9" * 24)},
    )
    assert applied == ["c0"]
    assert out[0]["content"] == f"Retrieve original: hash={'9' * 24}"


def test_a_real_ccr_marker_survives_truncation_intact() -> None:
    """The appended marker must still be scannable by /v1/retrieve."""
    from headroom.proxy.jev.retention_ccr import retention_marker

    hash_key = "abc123def456abc123def456"
    marker = retention_marker(hash_key, original_tokens=1234)
    lease = RetentionLease(
        candidate_id="c0",
        hash_key=hash_key,
        marker=marker,
        original_tokens=1234,
        lease_seconds=86_400,
    )
    messages: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": "call_1", "content": "Z" * 900}
    ]
    cand = _candidate("c0", 0, "tool_result", "Z" * 900)
    out, applied = apply_retention(messages, [cand], {"c0": "truncate"}, {"c0": lease})
    assert applied == ["c0"]
    assert out[0]["content"].startswith("Z" * 400)
    assert "Z" * 401 not in out[0]["content"]
    assert marker in out[0]["content"]
