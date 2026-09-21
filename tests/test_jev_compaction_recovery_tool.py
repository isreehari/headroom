"""Track C fail-open rule: never drop a candidate the model cannot get back.

Retention rewrites a tool output into a retrieval marker. If the frame does not
advertise ``headroom_retrieve``, the model has no way to redeem that marker and
the drop is permanent data loss -- so the gate is a hard precondition, and its
only safe failure direction is ``False``.

Codex >= 0.149.0 nests tool definitions in ``input`` items of type
``additional_tools`` (see ``_lift_codex_additional_tools`` in
``headroom/proxy/handlers/openai.py``), so the gate has to look in both places.
Missing the nested form would silently disable Track C for current Codex
clients -- a false negative that costs savings with no visible symptom.

Every case asserts an exact ``True``/``False``. "Did not raise" is not an
assertion: that looseness is what let a ``TypeError`` on an unhashable ``type``
ship twice from this module.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.compaction import ADDITIONAL_TOOLS_ITEM_TYPE, has_recovery_tool

# --------------------------------------------------------------------------
# The two declaration sites.
# --------------------------------------------------------------------------


def test_top_level_responses_tool_list_is_recognized() -> None:
    inner = {"tools": [{"type": "function", "name": CCR_TOOL_NAME}]}
    assert has_recovery_tool(inner) is True


def test_chat_shaped_nested_function_tool_is_recognized() -> None:
    inner = {"tools": [{"type": "function", "function": {"name": CCR_TOOL_NAME}}]}
    assert has_recovery_tool(inner) is True


def test_mcp_prefixed_tool_name_is_recognized() -> None:
    """Mirrors the namespaced match the Responses handler already applies.

    An MCP-served retrieve tool reaches the model as
    ``mcp__Headroom__headroom_retrieve``; it is the same recovery path, so the
    gate must not read the prefix as a different tool.
    """
    inner = {"tools": [{"type": "function", "name": f"mcp__Headroom__{CCR_TOOL_NAME}"}]}
    assert has_recovery_tool(inner) is True


def test_additional_tools_carrier_item_is_recognized() -> None:
    inner = {
        "input": [
            {
                "type": ADDITIONAL_TOOLS_ITEM_TYPE,
                "tools": [
                    {"type": "function", "name": "shell"},
                    {"type": "function", "name": CCR_TOOL_NAME},
                ],
            },
            {"type": "compaction_trigger"},
        ]
    }
    assert has_recovery_tool(inner) is True


def test_additional_tools_carrier_accepts_the_chat_shaped_nesting_too() -> None:
    inner = {
        "input": [
            {
                "type": ADDITIONAL_TOOLS_ITEM_TYPE,
                "tools": [{"type": "function", "function": {"name": CCR_TOOL_NAME}}],
            }
        ]
    }
    assert has_recovery_tool(inner) is True


def test_either_site_alone_is_enough() -> None:
    """The carrier encoding and the classic array are alternatives, not a pair."""
    carrier_only = {
        "tools": [],
        "input": [
            {"type": ADDITIONAL_TOOLS_ITEM_TYPE, "tools": [{"name": CCR_TOOL_NAME}]},
        ],
    }
    array_only = {
        "tools": [{"name": CCR_TOOL_NAME}],
        "input": [{"type": "compaction_trigger"}],
    }
    assert has_recovery_tool(carrier_only) is True
    assert has_recovery_tool(array_only) is True


# --------------------------------------------------------------------------
# Absence: the gate's whole purpose.
# --------------------------------------------------------------------------


def test_missing_recovery_tool_fails_the_gate() -> None:
    assert has_recovery_tool({"tools": [{"type": "function", "name": "shell"}]}) is False
    assert has_recovery_tool({"input": [{"type": "compaction_trigger"}]}) is False
    assert has_recovery_tool({}) is False
    assert has_recovery_tool(None) is False


def test_a_confusable_tool_name_does_not_satisfy_the_gate() -> None:
    """Only the recovery tool itself, or a namespaced form of it, counts."""
    for name in (
        f"{CCR_TOOL_NAME}_extra",
        f"{CCR_TOOL_NAME}2",
        f"not_{CCR_TOOL_NAME[1:]}",
        CCR_TOOL_NAME.upper(),
        "headroom_compress",
    ):
        assert has_recovery_tool({"tools": [{"name": name}]}) is False, name


def test_tools_nested_in_a_non_carrier_input_item_do_not_count() -> None:
    """Only the ``additional_tools`` carrier advertises tools to the model.

    A ``tools`` key on some other item type is not a declaration Codex makes,
    and treating it as one would let attacker-shaped transcript content unlock
    a drop the model cannot redeem.
    """
    inner = {
        "input": [
            {"type": "message", "tools": [{"name": CCR_TOOL_NAME}]},
            {"type": "custom_tool_call_output", "tools": [{"name": CCR_TOOL_NAME}]},
        ]
    }
    assert has_recovery_tool(inner) is False


def test_an_empty_tool_name_never_satisfies_the_gate() -> None:
    assert has_recovery_tool({"tools": [{"name": ""}, {"function": {"name": ""}}]}) is False


# --------------------------------------------------------------------------
# The gate reads the shared constant, not a copied literal.
# --------------------------------------------------------------------------


def test_the_gate_follows_a_rename_of_the_shared_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicated string constant is how this gate silently stops matching.

    If the recovery tool is ever renamed, a gate holding its own copy of the
    old name keeps returning False and Track C quietly turns itself off --
    which is the failure the gate exists to prevent, inverted. Binding to
    ``headroom.ccr``'s constant is what makes that impossible, so it is
    asserted behaviourally rather than left to the import test alone.
    """
    from headroom.proxy.jev import compaction as compaction_module

    monkeypatch.setattr(compaction_module, "CCR_TOOL_NAME", "headroom_recover_v2")
    assert has_recovery_tool({"tools": [{"name": "headroom_recover_v2"}]}) is True
    assert has_recovery_tool({"tools": [{"name": CCR_TOOL_NAME}]}) is False


# --------------------------------------------------------------------------
# Adversarial input. The relay reaches this on client-controlled JSON, so the
# contract is "returns a bool", never "raises".
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [None, [], "tools", 7, 0.5, True, set(), ("tools",), object()],
)
def test_a_non_dict_frame_fails_the_gate(frame: Any) -> None:
    assert has_recovery_tool(frame) is False


@pytest.mark.parametrize(
    "tools",
    [None, "headroom_retrieve", 3, {"name": CCR_TOOL_NAME}, set(), ()],
)
def test_a_non_list_tools_value_fails_the_gate(tools: Any) -> None:
    assert has_recovery_tool({"tools": tools}) is False


@pytest.mark.parametrize(
    "items",
    [None, "input", 3, {"type": ADDITIONAL_TOOLS_ITEM_TYPE}, set(), ()],
)
def test_a_non_list_input_value_fails_the_gate(items: Any) -> None:
    assert has_recovery_tool({"input": items}) is False


def test_non_dict_entries_are_skipped_not_fatal() -> None:
    """Junk beside a real declaration must not cost the frame its gate."""
    inner = {
        "tools": [None, "shell", 7, [], (), {"name": CCR_TOOL_NAME}],
        "input": [None, "trigger", 7, []],
    }
    assert has_recovery_tool(inner) is True
    assert has_recovery_tool({"tools": [None, "shell", 7, [], ()]}) is False


@pytest.mark.parametrize("name", [[], {}, set(), None, 7, 0.5, ("a",)])
def test_a_non_string_tool_name_fails_the_gate(name: Any) -> None:
    """A ``name`` decoded as a JSON array or object must not reach ``==``/``endswith``.

    CPython special-cases ``set``, so a passing ``set`` case proves nothing
    about ``list`` and ``dict`` -- both are parametrized here for that reason.
    """
    assert has_recovery_tool({"tools": [{"name": name}]}) is False
    assert has_recovery_tool({"tools": [{"function": {"name": name}}]}) is False
    assert (
        has_recovery_tool(
            {"input": [{"type": ADDITIONAL_TOOLS_ITEM_TYPE, "tools": [{"name": name}]}]}
        )
        is False
    )


@pytest.mark.parametrize("item_type", [[], {}, set(), None, 7, True])
def test_a_non_string_input_item_type_fails_the_gate(item_type: Any) -> None:
    """The same unhashable-``type`` family that crashed detection twice."""
    inner = {"input": [{"type": item_type, "tools": [{"name": CCR_TOOL_NAME}]}]}
    assert has_recovery_tool(inner) is False


@pytest.mark.parametrize("nested", [[], "fn", 7, None, set()])
def test_a_non_dict_function_value_is_skipped(nested: Any) -> None:
    assert has_recovery_tool({"tools": [{"function": nested}]}) is False
    assert has_recovery_tool({"tools": [{"function": nested, "name": CCR_TOOL_NAME}]}) is True


@pytest.mark.parametrize("carrier_tools", [None, "shell", 7, {}, set(), ()])
def test_a_carrier_with_a_non_list_tools_value_fails_the_gate(carrier_tools: Any) -> None:
    inner = {"input": [{"type": ADDITIONAL_TOOLS_ITEM_TYPE, "tools": carrier_tools}]}
    assert has_recovery_tool(inner) is False


def test_deeply_nested_junk_does_not_recurse_or_raise() -> None:
    """The gate scans two fixed levels; depth must cost nothing.

    Built iteratively because a recursive scan -- or a recursive ``repr``/
    comparison reached by accident -- would blow the stack on client JSON.
    """
    deep: Any = "leaf"
    for _ in range(5000):
        deep = [deep]
    inner = {
        "tools": [{"name": deep, "function": {"name": deep}}, {"name": CCR_TOOL_NAME}],
        "input": [{"type": ADDITIONAL_TOOLS_ITEM_TYPE, "tools": [{"name": deep}]}],
    }
    assert has_recovery_tool(inner) is True
    assert has_recovery_tool({"tools": [{"name": deep}]}) is False


def test_a_self_referential_frame_does_not_hang_the_gate() -> None:
    tools: list[Any] = [{"name": "shell"}]
    tools.append({"name": CCR_TOOL_NAME, "self": tools})
    assert has_recovery_tool({"tools": tools}) is True


# --------------------------------------------------------------------------
# The frame is relayed onward; the gate is read-only.
# --------------------------------------------------------------------------


def test_the_gate_does_not_mutate_the_frame() -> None:
    inner = {
        "tools": [{"type": "function", "name": "shell"}],
        "input": [
            {"type": ADDITIONAL_TOOLS_ITEM_TYPE, "tools": [{"name": CCR_TOOL_NAME}]},
            {"type": "compaction_trigger"},
        ],
    }
    before = copy.deepcopy(inner)
    assert has_recovery_tool(inner) is True
    assert inner == before


# The import discipline this gate's one new dependency lives under -- stdlib
# plus `CCR_TOOL_NAME` and nothing else -- is asserted structurally next to the
# rest of the module's leaf rules, in
# tests/test_jev_compaction_boundary.py::test_the_module_imports_only_stdlib_and_the_ccr_tool_name.
# It is checked in one place on purpose: two copies of an allowlist drift.
