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

import hashlib
import json
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

    Codex sends ``{"type": "response.create", "response": {...}}``; older and
    flattened shapes carry the payload fields directly. The second element says
    whether the payload was nested, so a caller that rewrites it can re-wrap in
    the same shape. Mirrors the acceptance rule the WS frame shaper already
    applies in ``headroom/proxy/handlers/openai.py``
    (``_shape_openai_response_create_frame``).

    Three shapes are accepted, and nothing else:

    * ``{"type": "response.create", "response": {...}}`` -> ``(response, True)``
    * ``{"type": "response.create", "input": [...]}`` -- the flattened create
      frame, ``response`` key absent -> ``(frame, False)``
    * ``{"input": [...]}`` -- a bare payload, no ``type`` key at all ->
      ``(frame, False)``

    Everything else fails closed to ``(None, False)``, including a create frame
    whose ``response`` key is *present* but not a dict. That is a malformed
    envelope, not a payload, and returning the outer frame for it would hand a
    consumer an envelope dressed as a payload. An explicit ``"type": null`` is
    likewise not a missing ``type``: a frame that declares a null type is
    malformed, so membership of the key is tested rather than its value.
    """
    if not isinstance(frame, dict):
        return None, False
    if "type" not in frame:
        # A bare payload: no frame envelope at all, just the Responses fields.
        return (frame, False) if isinstance(frame.get("input"), list) else (None, False)
    frame_type = frame.get("type")
    if frame_type != "response.create":
        # Covers every other frame type, and also an explicit null or non-string
        # `type`, neither of which is a wire frame type this module acts on.
        return None, False
    if "response" in frame:
        inner = frame.get("response")
        return (inner, True) if isinstance(inner, dict) else (None, False)
    # Flattened create frame: the payload fields sit beside `type`.
    return (frame, False) if isinstance(frame.get("input"), list) else (None, False)


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
        if not isinstance(item_type, str):
            # Guards the set membership below. An item `type` decoded as a JSON
            # array or object is unhashable, and `item_type in <frozenset>`
            # raises `TypeError: unhashable type` on it -- inside the relay's
            # hot path, on input the client controls. A non-string type is
            # never one of ours, so skipping the item is both safe and correct.
            continue
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


# The body fields a tool-output item may carry, in the order they are scanned.
# `output` is the shape Phase 0b observed on `custom_tool_call_output`;
# `content` is the alternative the Responses vocabulary allows for the other
# allowlisted output types. Exactly one of them is staged per candidate, and
# `replace_candidate_output` requires the same one to still be the winner.
_CANDIDATE_TEXT_FIELDS = ("output", "content")


@dataclass(frozen=True)
class JevCompactionCandidate:
    """The single tool output a compaction boundary carries."""

    candidate_id: str
    item_index: int
    item_type: str
    call_id: str
    output_field: str
    output_text: str
    content_sha256: str
    estimated_tokens: int


def _candidate_output_text(item: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(field name, text)`` for the item's body, or None.

    A structured body is canonicalized with sorted keys so the same decoded
    value always hashes the same way between extraction and replacement.
    Serialization itself is attacker-reachable -- a self-referential object
    raises ``ValueError``, mixed-type keys defeat ``sort_keys`` with a
    ``TypeError``, deep nesting raises ``RecursionError`` -- so a body that
    cannot be canonicalized is simply not a candidate.
    """
    for field_name in _CANDIDATE_TEXT_FIELDS:
        value = item.get(field_name)
        if isinstance(value, str) and value:
            return field_name, value
        if isinstance(value, (dict, list)) and value:
            try:
                return field_name, json.dumps(
                    value, ensure_ascii=False, sort_keys=True, default=str
                )
            except Exception:
                return None
    return None


def extract_compaction_candidate(
    inner: Any,
    boundary: JevCompactionBoundary,
    *,
    max_candidate_bytes: int,
) -> JevCompactionCandidate | None:
    """Extract the boundary's single candidate, or None to fail open to keep.

    ``max_candidate_bytes`` is a UTF-8 byte ceiling (0 or less disables it)
    derived from ``HEADROOM_JEV_MAX_CANDIDATE_TOKENS`` by the caller.
    ``estimated_tokens`` is an estimate used only for the CCR entry's
    bookkeeping and for the question text -- Track C's real savings are reported
    by the existing WS usage accounting, never from this number.

    ``boundary.candidate_index`` addresses the ORIGINAL ``input`` list, so the
    item is read by direct subscript; nothing is filtered first. The frame is
    never mutated here. Like the rest of this module the function never raises:
    every subscript, length and membership test is guarded, and an item whose
    ``type`` decoded to an unhashable JSON array or object is declined before it
    can reach the allowlist's ``in``.
    """
    if not isinstance(inner, dict):
        return None
    items = inner.get("input")
    if not isinstance(items, list):
        return None
    if not 0 <= boundary.candidate_index < len(items):
        return None
    item = items[boundary.candidate_index]
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    # Guards the membership test below: an unhashable `type` raises
    # `TypeError: unhashable type` inside the relay's hot path. CPython
    # special-cases `set`, so only `list` and `dict` actually raise -- which is
    # exactly why the guard is `isinstance(..., str)` and not a try/except
    # around the `in`.
    if not isinstance(item_type, str) or item_type not in JEV_TOOL_OUTPUT_ITEM_TYPES:
        return None
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    found = _candidate_output_text(item)
    if found is None:
        return None
    output_field, output_text = found
    encoded = output_text.encode("utf-8", "replace")
    if max_candidate_bytes > 0 and len(encoded) > max_candidate_bytes:
        return None
    digest = hashlib.sha256(encoded).hexdigest()
    return JevCompactionCandidate(
        candidate_id=f"cand_{digest[:16]}",
        item_index=boundary.candidate_index,
        item_type=item_type,
        call_id=call_id,
        output_field=output_field,
        output_text=output_text,
        content_sha256=digest,
        estimated_tokens=max(1, len(encoded) // 4),
    )


def replace_candidate_output(
    inner: Any,
    candidate: JevCompactionCandidate,
    replacement: str,
) -> bool:
    """Swap the candidate's body for ``replacement`` in place.

    Returns False -- changing nothing -- unless the item still sits at the same
    index, still has the same type, the same ``call_id`` and the same body
    field, and still hashes to the content that was staged in CCR. That binding
    is what stops a rewrite between extraction and commit from replacing content
    whose original was never stored. ``call_id`` is part of it because content
    alone is not identity: two calls of the same tool can return byte-identical
    output, and the marker staged under one call's identity must not land on the
    other's slot.

    A mismatch is a normal outcome, not an error: the caller keeps the original
    frame and relays it untouched. ``inner`` is the object
    ``unwrap_response_create`` handed back BY REFERENCE, so the single
    assignment below lands directly in the frame about to be relayed -- and is
    the only write this module ever performs.
    """
    if not isinstance(inner, dict):
        return False
    items = inner.get("input")
    if not isinstance(items, list):
        return False
    if not 0 <= candidate.item_index < len(items):
        return False
    item = items[candidate.item_index]
    if not isinstance(item, dict) or item.get("type") != candidate.item_type:
        return False
    if item.get("call_id") != candidate.call_id:
        return False
    found = _candidate_output_text(item)
    if found is None or found[0] != candidate.output_field:
        return False
    digest = hashlib.sha256(found[1].encode("utf-8", "replace")).hexdigest()
    if digest != candidate.content_sha256:
        return False
    item[candidate.output_field] = replacement
    return True
