"""Track C: detect Codex's native compaction boundary on the WS Responses path.

Shapes here are the ones benchmarks/jev_codex_boundary_probe.py actually
observed: a `response.create` frame whose `input` carries exactly one tool
output item plus a `compaction_trigger`, anchored by `previous_response_id`.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
from typing import Any

import pytest

from headroom.proxy.jev.compaction import (
    ADDITIONAL_TOOLS_ITEM_TYPE,
    COMPACTION_TRIGGER_ITEM_TYPE,
    CUSTOM_TOOL_CALL_ITEM_TYPE,
    JEV_COMPACTION_WIRE_ITEM_TYPES,
    JEV_TOOL_OUTPUT_ITEM_TYPES,
    JevCompactionBoundary,
    detect_compaction_boundary,
    unwrap_response_create,
)


def _observed_frame() -> dict[str, Any]:
    return {
        "type": "response.create",
        "response": {
            "model": "gpt-5.6-sol",
            "previous_response_id": "resp_abc123",
            "input": [
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_9",
                    "output": "total 48\ndrwxr-xr-x  12 user  staff   384 Sep 20 13:15 .",
                },
                {"type": "compaction_trigger"},
            ],
        },
    }


def test_unwrap_response_create_handles_both_wire_shapes() -> None:
    inner, wrapped = unwrap_response_create(_observed_frame())
    assert wrapped is True
    assert inner is not None and inner["previous_response_id"] == "resp_abc123"

    bare = {"input": [], "previous_response_id": "resp_1"}
    inner, wrapped = unwrap_response_create(bare)
    assert wrapped is False
    assert inner is bare

    assert unwrap_response_create({"type": "response.cancel"}) == (None, False)
    assert unwrap_response_create("not a dict") == (None, False)


def test_detect_boundary_on_the_observed_shape() -> None:
    inner, _ = unwrap_response_create(_observed_frame())
    assert detect_compaction_boundary(inner) == JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=1,
        candidate_index=0,
        item_count=2,
    )


def test_detect_boundary_accepts_previous_response_id_on_the_trigger_item() -> None:
    inner, _ = unwrap_response_create(_observed_frame())
    assert inner is not None
    del inner["previous_response_id"]
    inner["input"][1]["previous_response_id"] = "resp_nested"
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert boundary.previous_response_id == "resp_nested"


def test_ordinary_turn_is_not_a_boundary() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "message", "role": "user", "content": "go"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"},
        ],
    }
    assert detect_compaction_boundary(inner) is None


def test_trigger_without_previous_response_id_is_not_a_boundary() -> None:
    inner = {
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"},
            {"type": "compaction_trigger"},
        ]
    }
    assert detect_compaction_boundary(inner) is None


def test_more_than_one_candidate_is_out_of_track_c_scope() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "function_call_output", "call_id": "call_2", "output": "b"},
            {"type": "compaction_trigger"},
        ],
    }
    assert detect_compaction_boundary(inner) is None


def test_carrier_and_call_items_do_not_block_detection() -> None:
    """`additional_tools`, `custom_tool_call` and `custom_tool_call_output` are
    all real wire item types Phase 0b observed, wider than the original plan's
    assumed vocabulary. Only the OUTPUT item is a candidate; the other two must
    be ignored rather than counted as a second candidate."""
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "additional_tools", "tools": [{"type": "function", "name": "shell"}]},
            {"type": "custom_tool_call", "call_id": "call_1", "name": "shell", "input": "ls"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert (boundary.candidate_index, boundary.trigger_index) == (2, 3)


def test_the_wire_vocabulary_is_complete_and_wider_than_the_allowlist() -> None:
    """The design doc names three types the original plan's vocabulary missed.

    They are named here, in the vocabulary set -- and pointedly NOT in the
    candidate allowlist, which is the set that licenses a drop.
    """
    for item_type in ("additional_tools", "custom_tool_call", "custom_tool_call_output"):
        assert item_type in JEV_COMPACTION_WIRE_ITEM_TYPES
    assert COMPACTION_TRIGGER_ITEM_TYPE in JEV_COMPACTION_WIRE_ITEM_TYPES
    assert JEV_TOOL_OUTPUT_ITEM_TYPES < JEV_COMPACTION_WIRE_ITEM_TYPES
    # A carrier item and a call item are never retention candidates.
    assert ADDITIONAL_TOOLS_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES
    assert CUSTOM_TOOL_CALL_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES
    assert COMPACTION_TRIGGER_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES


# --------------------------------------------------------------------------
# The exact public surface five later tasks import.
# --------------------------------------------------------------------------


def test_the_public_constants_hold_their_contracted_values() -> None:
    """Tasks 21-25 import these by name; the values are the wire vocabulary."""
    assert COMPACTION_TRIGGER_ITEM_TYPE == "compaction_trigger"
    assert ADDITIONAL_TOOLS_ITEM_TYPE == "additional_tools"
    assert CUSTOM_TOOL_CALL_ITEM_TYPE == "custom_tool_call"
    assert JEV_TOOL_OUTPUT_ITEM_TYPES == frozenset(
        {
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
            "tool_search_output",
        }
    )
    assert JEV_COMPACTION_WIRE_ITEM_TYPES == JEV_TOOL_OUTPUT_ITEM_TYPES | frozenset(
        {"compaction_trigger", "additional_tools", "custom_tool_call"}
    )
    # Both are immutable: a consumer cannot widen the allowlist in place and so
    # cannot grow the set of item types that may be dropped.
    assert isinstance(JEV_TOOL_OUTPUT_ITEM_TYPES, frozenset)
    assert isinstance(JEV_COMPACTION_WIRE_ITEM_TYPES, frozenset)


def test_the_boundary_record_is_frozen_and_comparable() -> None:
    boundary = JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=1,
        candidate_index=0,
        item_count=2,
    )
    assert boundary == JevCompactionBoundary("resp_abc123", 1, 0, 2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        boundary.item_count = 3  # type: ignore[misc]


# --------------------------------------------------------------------------
# Hot-path discipline: never raise on arbitrary, attacker-reachable JSON.
# --------------------------------------------------------------------------


_MALFORMED: list[Any] = [
    None,
    "",
    "not a dict",
    b"bytes",
    0,
    1.5,
    True,
    [],
    ["compaction_trigger"],
    (),
    {"type": None},
    {"type": 7},
    {"type": "response.create"},
    {"type": "response.create", "response": None},
    {"type": "response.create", "response": "nope"},
    {"type": "response.create", "response": []},
    {"type": "response.create", "response": {"input": None}},
    {"type": "response.create", "response": {"input": "not a list"}},
    {"type": "response.create", "response": {"input": {}}},
    {"input": None},
    {"input": {}},
    {"input": []},
    {"input": [None, 1, "x", []]},
    {"input": [{"type": None}]},
    # An UNHASHABLE item `type`. A set-membership test on decoded JSON raises
    # `TypeError: unhashable type` on these unless the value is type-guarded
    # first -- the same defect class Task 5's `select_candidates` hit.
    {"input": [{"type": []}], "previous_response_id": "resp_1"},
    {"input": [{"type": {}}], "previous_response_id": "resp_1"},
    {"input": [{"type": [["nested"]]}], "previous_response_id": "resp_1"},
    {"input": [{"type": {"k": "v"}}], "previous_response_id": "resp_1"},
    {"previous_response_id": None, "input": [{"type": "compaction_trigger"}]},
    {"previous_response_id": 42, "input": [{"type": "compaction_trigger"}]},
    {"previous_response_id": "", "input": [{"type": "compaction_trigger"}]},
    {
        "previous_response_id": "resp_1",
        "input": [{"type": "compaction_trigger"}],
    },
    {
        "input": [
            {"type": "custom_tool_call_output", "output": "a"},
            {"type": "compaction_trigger", "previous_response_id": None},
        ]
    },
    {
        "input": [
            {"type": "custom_tool_call_output", "output": "a"},
            {"type": "compaction_trigger", "previous_response_id": 9},
        ]
    },
    {
        "input": [
            {"type": "custom_tool_call_output", "output": "a"},
            {"type": "compaction_trigger", "previous_response_id": ""},
        ]
    },
]


#: Frames that are not a usable Responses create payload. The contract is the
#: exact sentinel ``(None, False)`` -- not merely "did not raise". Asserting the
#: result is what catches an envelope handed onward as though it were a payload.
_NOT_A_RESPONSES_PAYLOAD: list[Any] = [
    None,
    "",
    "not a dict",
    b"bytes",
    0,
    1.5,
    True,
    [],
    (),
    {},
    {"type": "response.cancel"},
    {"type": "response.cancel", "input": []},
    # An explicit JSON null `type` is not a missing `type`: a frame that
    # declares a null type is malformed, not a bare payload.
    {"type": None},
    {"type": None, "input": []},
    {"type": None, "input": [{"type": "compaction_trigger"}]},
    # A non-string `type` is never a wire frame type.
    {"type": 7, "input": []},
    {"type": [], "input": []},
    {"type": {}, "input": []},
    {"type": True, "input": []},
    # A create frame whose envelope is malformed: `response` is present but is
    # not a dict, so there is no inner payload to return. Handing the OUTER
    # frame back here would present an envelope as a payload.
    {"type": "response.create", "response": None},
    {"type": "response.create", "response": "nope"},
    {"type": "response.create", "response": []},
    {"type": "response.create", "response": 0},
    {"type": "response.create", "response": None, "input": []},
    # A create frame with neither a `response` envelope nor a payload `input`.
    {"type": "response.create"},
    {"type": "response.create", "input": None},
    {"type": "response.create", "input": "not a list"},
    # A bare payload must carry a list `input`.
    {"input": None},
    {"input": {}},
    {"input": "not a list"},
    {"previous_response_id": "resp_1"},
]


@pytest.mark.parametrize("frame", _NOT_A_RESPONSES_PAYLOAD)
def test_unwrap_returns_the_fail_closed_sentinel_for_a_non_payload(frame: Any) -> None:
    """Fail closed means the exact sentinel, not just the absence of a raise."""
    assert unwrap_response_create(frame) == (None, False)


def test_unwrap_accepts_a_flattened_create_frame() -> None:
    """``response`` ABSENT on a create frame is the flattened shape, not a fault.

    ``_shape_openai_response_create_frame`` in the WS handler falls back to the
    outer frame exactly for this shape, so declining it would be stricter than
    the path this module mirrors. ``response`` *present* but non-dict is a
    different thing -- a malformed envelope -- and is declined above.
    """
    flat = {"type": "response.create", "previous_response_id": "resp_1", "input": []}
    inner, wrapped = unwrap_response_create(flat)
    assert wrapped is False
    assert inner is flat


@pytest.mark.parametrize("frame", _MALFORMED)
def test_unwrap_never_raises_on_a_malformed_frame(frame: Any) -> None:
    """The relay hands this arbitrary decoded JSON; declining is the only option.

    There is no caller to tell, so unlike Track B's ``/v1/compress`` gate this
    module raises nothing at all -- it returns ``(None, False)`` and the frame
    is relayed untouched.
    """
    inner, wrapped = unwrap_response_create(frame)
    assert isinstance(wrapped, bool)
    assert inner is None or isinstance(inner, dict)


@pytest.mark.parametrize("frame", _MALFORMED)
def test_detect_never_raises_on_a_malformed_payload(frame: Any) -> None:
    assert detect_compaction_boundary(frame) is None
    inner, _ = unwrap_response_create(frame)
    assert detect_compaction_boundary(inner) is None


@pytest.mark.parametrize("bad_type", [[], {}, [["nested"]], {"k": "v"}, set()])
def test_an_unhashable_item_type_does_not_crash_the_relay(bad_type: Any) -> None:
    """A set-membership test on decoded JSON must type-guard its operand.

    ``item_type in JEV_TOOL_OUTPUT_ITEM_TYPES`` raises ``TypeError: unhashable
    type`` when an item's ``type`` arrives as a JSON array or object. This is
    attacker-reachable -- the relay decodes whatever the client sends -- and it
    is precisely the input class the never-raise guarantee exists for.
    """
    inner = {"previous_response_id": "resp_1", "input": [{"type": bad_type}]}
    assert detect_compaction_boundary(inner) is None


def test_an_unhashable_item_type_beside_a_real_boundary_is_skipped() -> None:
    """The junk item must be ignored, not crash and not veto the boundary."""
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": []},
            {"type": {"nested": "object"}},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
        ],
    }
    assert detect_compaction_boundary(inner) == JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=3,
        candidate_index=2,
        item_count=4,
    )


def test_non_dict_items_beside_a_real_boundary_are_skipped_not_fatal() -> None:
    """A single junk element must not cost a real boundary its detection."""
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            None,
            "junk",
            42,
            ["nested"],
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
        ],
    }
    assert detect_compaction_boundary(inner) == JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=5,
        candidate_index=4,
        item_count=6,
    )


def test_two_triggers_are_out_of_scope() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
            {"type": "compaction_trigger"},
        ],
    }
    assert detect_compaction_boundary(inner) is None


def test_a_candidate_without_a_trigger_is_not_a_boundary() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [{"type": "function_call_output", "call_id": "call_1", "output": "a"}],
    }
    assert detect_compaction_boundary(inner) is None


@pytest.mark.parametrize("candidate_type", sorted(JEV_TOOL_OUTPUT_ITEM_TYPES))
def test_every_allowlisted_output_type_is_a_candidate(candidate_type: str) -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": candidate_type, "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert boundary.candidate_index == 0


def test_a_top_level_previous_response_id_wins_over_the_nested_one() -> None:
    inner = {
        "previous_response_id": "resp_top",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger", "previous_response_id": "resp_nested"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert boundary.previous_response_id == "resp_top"


def test_detection_does_not_mutate_the_frame_it_inspects() -> None:
    """The relay forwards this very object; detection is read-only."""
    frame = _observed_frame()
    before = copy.deepcopy(frame)
    inner, _ = unwrap_response_create(frame)
    assert detect_compaction_boundary(inner) is not None
    assert frame == before


# --------------------------------------------------------------------------
# Leaf discipline.
# --------------------------------------------------------------------------


def test_the_module_is_a_stdlib_only_leaf() -> None:
    """Import isolation, exactly as Track B's ``compress_gate`` keeps it.

    The relay must be able to import this module on every WS connection without
    dragging in the ``jev`` package, the proxy, or any third-party dependency --
    ``HEADROOM_JEV_MODE`` is unset by default and an unconfigured proxy must pay
    nothing. Checked structurally on the source so the prose above, which names
    ``headroom`` and ``jev``, cannot satisfy or trip it.
    """
    import headroom.proxy.jev.compaction as compaction_module

    tree = ast.parse(inspect.getsource(compaction_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import is by definition a package import
                imported.add(".")
            elif node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "typing"}, imported
