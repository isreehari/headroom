"""Track C: extract the one candidate a compaction boundary carries, and put a
retrieval marker back in its place only while the bytes still match."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from headroom.proxy.jev.compaction import (
    JevCompactionBoundary,
    JevCompactionCandidate,
    detect_compaction_boundary,
    extract_compaction_candidate,
    replace_candidate_output,
)


def _inner(output: Any = "stdout body") -> dict[str, Any]:
    return {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_9", "output": output},
            {"type": "compaction_trigger"},
        ],
    }


def _boundary() -> JevCompactionBoundary:
    """The boundary the observed two-item frame yields, built directly.

    ``detect_compaction_boundary`` declines the malformed frames the adversarial
    cases below need, so those cases hand ``extract_compaction_candidate`` a
    boundary of its own. That is the honest shape of the contract: the two
    functions are separate layers and a caller can reach the second with a
    boundary whose frame has since been reshaped.
    """
    return JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=1,
        candidate_index=0,
        item_count=2,
    )


# --------------------------------------------------------------------------
# Extraction, happy paths.
# --------------------------------------------------------------------------


def test_extract_candidate_from_string_output() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert candidate.item_index == 0
    assert candidate.item_type == "custom_tool_call_output"
    assert candidate.call_id == "call_9"
    assert candidate.output_field == "output"
    assert candidate.output_text == "stdout body"
    assert candidate.content_sha256 == hashlib.sha256(b"stdout body").hexdigest()
    assert candidate.candidate_id == f"cand_{candidate.content_sha256[:16]}"
    assert candidate.estimated_tokens >= 1


def test_extract_candidate_serializes_structured_output() -> None:
    inner = _inner({"rows": [1, 2, 3]})
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert json.loads(candidate.output_text) == {"rows": [1, 2, 3]}


def test_extract_candidate_reads_a_content_body_when_output_is_absent() -> None:
    inner = _inner()
    inner["input"][0].pop("output")
    inner["input"][0]["content"] = "content body"
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert candidate.output_field == "content"
    assert candidate.output_text == "content body"


def test_extract_candidate_respects_the_byte_ceiling() -> None:
    inner = _inner("x" * 4096)
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=1024) is None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=8192) is not None


def test_extract_candidate_requires_a_call_id_and_body() -> None:
    inner = _inner()
    inner["input"][0].pop("call_id")
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=0) is None

    empty = _inner("")
    boundary = detect_compaction_boundary(empty)
    assert boundary is not None
    assert extract_compaction_candidate(empty, boundary, max_candidate_bytes=0) is None


# --------------------------------------------------------------------------
# Extraction, fail-closed paths on attacker-reachable JSON.
# --------------------------------------------------------------------------


# `set` is the trap here: CPython special-cases it so `set() in frozenset(...)`
# is False rather than a TypeError. Only `list` and `dict` actually raise, so a
# suite that tests `set` alone would pass over an unguarded membership test.
_UNHASHABLE_AND_NON_STRING = ([], {}, set(), ["custom_tool_call_output"], 7, 1.5, True, None)


def test_extract_candidate_declines_every_non_string_item_type() -> None:
    for bad_type in _UNHASHABLE_AND_NON_STRING:
        inner = _inner()
        inner["input"][0]["type"] = bad_type
        result = extract_compaction_candidate(inner, _boundary(), max_candidate_bytes=0)
        assert result is None, bad_type


def test_extract_candidate_declines_a_recognised_but_non_allowlisted_item_type() -> None:
    # In the wire vocabulary, but never droppable: dropping a call while keeping
    # its output breaks the pairing.
    for item_type in ("custom_tool_call", "additional_tools", "compaction_trigger", "message"):
        inner = _inner()
        inner["input"][0]["type"] = item_type
        result = extract_compaction_candidate(inner, _boundary(), max_candidate_bytes=0)
        assert result is None, item_type


def test_extract_candidate_declines_every_non_string_call_id() -> None:
    for bad_call_id in (*_UNHASHABLE_AND_NON_STRING, ""):
        inner = _inner()
        inner["input"][0]["call_id"] = bad_call_id
        result = extract_compaction_candidate(inner, _boundary(), max_candidate_bytes=0)
        assert result is None, bad_call_id


def test_extract_candidate_declines_a_missing_or_unusable_body() -> None:
    no_body = _inner()
    no_body["input"][0].pop("output")
    assert extract_compaction_candidate(no_body, _boundary(), max_candidate_bytes=0) is None

    for bad_body in (7, 1.5, True, None, "", [], {}):
        inner = _inner(bad_body)
        result = extract_compaction_candidate(inner, _boundary(), max_candidate_bytes=0)
        assert result is None, bad_body


def test_extract_candidate_declines_an_unserializable_body() -> None:
    circular: dict[str, Any] = {"rows": [1]}
    circular["self"] = circular
    assert (
        extract_compaction_candidate(_inner(circular), _boundary(), max_candidate_bytes=0) is None
    )

    # `sort_keys=True` cannot order mixed-type keys and raises TypeError.
    mixed_keys: dict[Any, Any] = {1: "a", "b": 2}
    assert (
        extract_compaction_candidate(_inner(mixed_keys), _boundary(), max_candidate_bytes=0) is None
    )


def test_extract_candidate_declines_an_out_of_range_or_reshaped_frame() -> None:
    inner = _inner()
    for bad_index in (2, 99, -1):
        boundary = JevCompactionBoundary("resp_abc123", 1, bad_index, 2)
        assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=0) is None

    assert extract_compaction_candidate(None, _boundary(), max_candidate_bytes=0) is None
    assert extract_compaction_candidate("nope", _boundary(), max_candidate_bytes=0) is None
    bad_input = {"input": "nope"}
    assert extract_compaction_candidate(bad_input, _boundary(), max_candidate_bytes=0) is None
    assert extract_compaction_candidate({}, _boundary(), max_candidate_bytes=0) is None
    assert (
        extract_compaction_candidate({"input": ["nope", {}]}, _boundary(), max_candidate_bytes=0)
        is None
    )


def test_extraction_never_mutates_the_frame() -> None:
    inner = _inner({"rows": [1, 2, 3]})
    before = json.dumps(inner, sort_keys=True)
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=0) is not None
    assert json.dumps(inner, sort_keys=True) == before


# --------------------------------------------------------------------------
# The content binding must be injective.
# --------------------------------------------------------------------------


# `json.loads` accepts a lone surrogate escape, so a client can put one in a
# tool output. Encoding it with `errors="replace"` collapses it onto the single
# byte b"?" -- the same bytes a literal "?" produces -- which would make two
# different bodies hash-identical and let a marker land on content that was
# never staged. The encoding used for the binding must be injective.
_LONE_SURROGATE = json.loads('"\\ud800"')


def _candidate_for(output: Any) -> tuple[dict[str, Any], JevCompactionCandidate]:
    inner = _inner(output)
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    return inner, candidate


def test_a_lone_surrogate_body_does_not_collide_with_a_question_mark() -> None:
    assert _LONE_SURROGATE == "\ud800"
    _, surrogate = _candidate_for(_LONE_SURROGATE)
    _, question = _candidate_for("?")
    assert surrogate.output_text == _LONE_SURROGATE
    assert question.output_text == "?"
    assert surrogate.content_sha256 != question.content_sha256
    assert surrogate.candidate_id != question.candidate_id


def test_replace_refuses_a_body_swapped_across_the_old_collision_class() -> None:
    inner, candidate = _candidate_for(_LONE_SURROGATE)
    inner["input"][0]["output"] = "?"
    before = json.dumps(inner, sort_keys=True)
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert json.dumps(inner, sort_keys=True) == before
    assert inner["input"][0]["output"] == "?"

    # ...and the mirror direction: staged "?", body swapped to the surrogate.
    inner, candidate = _candidate_for("?")
    inner["input"][0]["output"] = _LONE_SURROGATE
    before = json.dumps(inner, sort_keys=True)
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert json.dumps(inner, sort_keys=True) == before


def test_a_lone_surrogate_body_still_round_trips_when_it_is_unchanged() -> None:
    # Injectivity must not be bought by declining the candidate: an untouched
    # surrogate body is still extractable and still replaceable.
    inner, candidate = _candidate_for(_LONE_SURROGATE)
    assert replace_candidate_output(inner, candidate, "[marker]") is True
    assert inner["input"][0]["output"] == "[marker]"

    nested, nested_candidate = _candidate_for({"k": _LONE_SURROGATE})
    assert replace_candidate_output(nested, nested_candidate, "[marker]") is True
    assert nested["input"][0]["output"] == "[marker]"


def test_distinct_surrogates_hash_distinctly() -> None:
    seen = {_candidate_for(chr(code))[1].content_sha256 for code in range(0xD800, 0xD810)}
    assert len(seen) == 16


def test_the_byte_ceiling_uses_the_same_encoding_as_the_hash() -> None:
    # A lone surrogate is 3 bytes under the binding's encoding, not 1. The
    # ceiling must agree with the hash or the two can disagree about the body.
    body = _LONE_SURROGATE * 8  # 24 bytes encoded, 8 under a lossy encoding
    inner = _inner(body)
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=16) is None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=24) is not None


# --------------------------------------------------------------------------
# Replacement, bound to what was extracted.
# --------------------------------------------------------------------------


def test_replace_candidate_output_is_bound_to_the_extracted_hash() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None

    assert replace_candidate_output(inner, candidate, "[marker]") is True
    assert inner["input"][0]["output"] == "[marker]"

    # The item no longer hashes to what was staged: refuse to touch it again.
    assert replace_candidate_output(inner, candidate, "[marker2]") is False
    assert inner["input"][0]["output"] == "[marker]"


def test_replace_candidate_output_touches_only_the_one_body_field() -> None:
    inner = _inner()
    inner["model"] = "gpt-5.6-sol"
    inner["input"][0]["extra"] = "keep me"
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None

    assert replace_candidate_output(inner, candidate, "[marker]") is True
    assert inner == {
        "previous_response_id": "resp_abc123",
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "custom_tool_call_output",
                "call_id": "call_9",
                "output": "[marker]",
                "extra": "keep me",
            },
            {"type": "compaction_trigger"},
        ],
    }


def test_replace_candidate_output_round_trips_a_structured_body() -> None:
    inner = _inner({"rows": [1, 2, 3]})
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert replace_candidate_output(inner, candidate, "[marker]") is True
    assert inner["input"][0]["output"] == "[marker]"


def test_replace_candidate_output_refuses_a_tampered_body_byte_for_byte() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None

    inner["input"][0]["output"] = "stdout body!"
    before = json.dumps(inner, sort_keys=True)
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert json.dumps(inner, sort_keys=True) == before


def test_replace_candidate_output_refuses_a_different_call_id() -> None:
    # Same slot, same type, byte-identical body -- but a different call: the
    # marker was staged under the first call's identity and must not be written
    # over the second one's output.
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    inner["input"][0]["call_id"] = "call_10"
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"

    inner["input"][0].pop("call_id")
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"


def test_replace_candidate_output_refuses_a_reshaped_frame() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    inner["input"][0]["type"] = "function_call_output"
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"


def test_replace_candidate_output_refuses_a_moved_body_field() -> None:
    inner = _inner()
    inner["input"][0].pop("output")
    inner["input"][0]["content"] = "stdout body"
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert candidate.output_field == "content"

    # The same bytes now sit under `output`, which is scanned first: the staged
    # field is not where it was, so refuse.
    inner["input"][0].pop("content")
    inner["input"][0]["output"] = "stdout body"
    before = json.dumps(inner, sort_keys=True)
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert json.dumps(inner, sort_keys=True) == before


def test_replace_candidate_output_survives_non_string_and_unhashable_values() -> None:
    base = _inner()
    boundary = detect_compaction_boundary(base)
    assert boundary is not None
    candidate = extract_compaction_candidate(base, boundary, max_candidate_bytes=0)
    assert candidate is not None

    for field in ("type", "call_id", "output"):
        for bad in _UNHASHABLE_AND_NON_STRING:
            inner = _inner()
            inner["input"][0][field] = bad
            before = json.dumps(inner, sort_keys=True, default=repr)
            assert replace_candidate_output(inner, candidate, "[marker]") is False, (field, bad)
            assert json.dumps(inner, sort_keys=True, default=repr) == before

    circular: dict[str, Any] = {"rows": [1]}
    circular["self"] = circular
    inner = _inner(circular)
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] is circular


def test_replace_candidate_output_refuses_an_out_of_range_or_unusable_frame() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None

    assert replace_candidate_output(None, candidate, "[marker]") is False
    assert replace_candidate_output("nope", candidate, "[marker]") is False
    assert replace_candidate_output({}, candidate, "[marker]") is False
    assert replace_candidate_output({"input": "nope"}, candidate, "[marker]") is False
    assert replace_candidate_output({"input": []}, candidate, "[marker]") is False
    assert replace_candidate_output({"input": ["nope"]}, candidate, "[marker]") is False

    shifted = {"input": [{"type": "compaction_trigger"}, inner["input"][0]]}
    before = json.dumps(shifted, sort_keys=True)
    assert replace_candidate_output(shifted, candidate, "[marker]") is False
    assert json.dumps(shifted, sort_keys=True) == before
