"""Codex native compaction-boundary detection for Jev active retention (Track C).

Phase 0b (``benchmarks/jev_codex_boundary_probe.py``) established the real shape
of Codex's native compaction boundary on Headroom's ``/v1/responses`` WebSocket
route: a ``response.create`` frame whose ``input`` carries exactly one
tool-output item plus a ``compaction_trigger`` item, anchored by a
``previous_response_id``. Exactly ONE candidate crosses the wire per compaction
event, so Track C asks Jev one keep/drop question -- never a multi-candidate
batch. Anything that does not match that shape is not a boundary and is left
untouched.

Two disciplines this module keeps, and the reasons they are not negotiable:

* **Stdlib-only leaf.** Like Track B's ``compress_gate``, this module imports
  nothing from the rest of the ``jev`` package and nothing from the proxy. The
  relay must be able to reach detection cheaply on a connection where
  ``HEADROOM_JEV_MODE`` is unset -- the default -- and an unconfigured proxy
  must pay nothing for Jev's existence.
* **Fail closed, never raise.** These functions run inline in the relay's hot
  path on arbitrary, attacker-reachable JSON. Unlike the ``/v1/compress`` gate,
  which raises a typed error so the handler can answer 400, there is no caller
  here to tell: a frame that is not recognisably a boundary is simply declined
  -- ``(None, False)`` or ``None`` -- and relayed untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

COMPACTION_TRIGGER_ITEM_TYPE = "compaction_trigger"

#: Codex >= 0.149.0 carries tool definitions in an ``input`` item of this type
#: rather than (only) a top-level ``tools`` array. Read by Task 22's
#: ``has_recovery_tool``; never a retention candidate.
ADDITIONAL_TOOLS_ITEM_TYPE = "additional_tools"

#: The *call* half of a custom tool pair. Its output arrives separately as
#: ``custom_tool_call_output``; the call item itself is never a candidate,
#: because dropping a call while keeping its output breaks the pairing.
CUSTOM_TOOL_CALL_ITEM_TYPE = "custom_tool_call"

# Candidate ALLOWLIST: the item types whose body Track C may replace with a
# retrieval marker. ``custom_tool_call_output`` is outside the vocabulary the
# original plan (and its abandoned probe) assumed; Phase 0b observed it as the
# type the boundary actually carries.
JEV_TOOL_OUTPUT_ITEM_TYPES = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "tool_search_output",
    }
)

# The full wire vocabulary Track C recognizes at a boundary -- deliberately
# WIDER than the allowlist above and deliberately a separate name. Membership
# here means "this type is known and accounted for", not "this type may be
# dropped": `additional_tools` is a tool carrier and `custom_tool_call` is a
# call item, both of which must survive untouched. Having the two sets named
# apart is what stops a future consumer from reaching for the allowlist when it
# means the vocabulary.
JEV_COMPACTION_WIRE_ITEM_TYPES = JEV_TOOL_OUTPUT_ITEM_TYPES | frozenset(
    {
        COMPACTION_TRIGGER_ITEM_TYPE,
        ADDITIONAL_TOOLS_ITEM_TYPE,
        CUSTOM_TOOL_CALL_ITEM_TYPE,
    }
)


@dataclass(frozen=True)
class JevCompactionBoundary:
    """One recognized compaction event on the Codex WS Responses path."""

    previous_response_id: str
    trigger_index: int
    candidate_index: int
    item_count: int


def unwrap_response_create(frame: Any) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(inner response payload, wrapped)`` for a Responses create frame.

    Codex sends ``{"type": "response.create", "response": {...}}``; older shapes
    send the payload directly. The second element says whether the payload was
    nested, so a caller that rewrites it can re-wrap in the same shape. Mirrors
    the acceptance rule the WS frame shaper already applies in
    ``headroom/proxy/handlers/openai.py``
    (``_shape_openai_response_create_frame``): a ``response.create`` frame whose
    ``response`` is a dict is wrapped, otherwise the frame itself is the payload.
    """
    if not isinstance(frame, dict):
        return None, False
    frame_type = frame.get("type")
    if frame_type == "response.create":
        inner = frame.get("response")
        if isinstance(inner, dict):
            return inner, True
        return frame, False
    if frame_type is None and isinstance(frame.get("input"), list):
        return frame, False
    return None, False


def detect_compaction_boundary(inner: Any) -> JevCompactionBoundary | None:
    """Recognize the observed compaction shape, or return None.

    Requires all three observed markers: a single ``compaction_trigger`` item, a
    single tool-output candidate item, and a non-empty ``previous_response_id``
    (top level, or carried on the trigger item). More than one trigger or more
    than one candidate is a shape this track did not observe and does not act
    on.
    """
    if not isinstance(inner, dict):
        return None
    items = inner.get("input")
    if not isinstance(items, list) or not items:
        return None

    previous_response_id = inner.get("previous_response_id")
    trigger_index = -1
    candidate_index = -1
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == COMPACTION_TRIGGER_ITEM_TYPE:
            if trigger_index >= 0:
                return None
            trigger_index = index
            if not isinstance(previous_response_id, str) or not previous_response_id:
                nested = item.get("previous_response_id")
                if isinstance(nested, str) and nested:
                    previous_response_id = nested
        elif item_type in JEV_TOOL_OUTPUT_ITEM_TYPES:
            if candidate_index >= 0:
                return None
            candidate_index = index

    if trigger_index < 0 or candidate_index < 0:
        return None
    if not isinstance(previous_response_id, str) or not previous_response_id:
        return None
    return JevCompactionBoundary(
        previous_response_id=previous_response_id,
        trigger_index=trigger_index,
        candidate_index=candidate_index,
        item_count=len(items),
    )
