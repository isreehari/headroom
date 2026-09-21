"""Apply a Jev retention decision to the messages the caller will forward.

Two rules separate this from the shadow projection:

* **Nothing is ever removed.** Shadow deletes messages on a private copy to
  measure a projection; active mode hands the result back to a caller who
  forwards it to a provider, and a removed ``role="tool"`` message orphans its
  ``tool_call`` (a 400 from both OpenAI and Anthropic). So ``drop`` replaces the
  CONTENT with a CCR retrieval marker and leaves the envelope intact.
* **Only leased candidates are applied.** A candidate without an acknowledged,
  leased CCR entry keeps its original content, no matter what Jev answered.

The three wire shapes handled here are the ones Phase 0 actually observed:
OpenAI Chat ``role="tool"`` messages, Responses ``function_call_output`` and
``custom_tool_call_output`` items, and Anthropic ``tool_result`` blocks.

A rewrite is **content-bound**: the slot's current text is re-derived exactly
the way ``candidates.select_candidates`` derived ``cand.content`` (through
``candidates.text_of``, so an Anthropic block whose ``content`` is a LIST
flattens to the same JSON on both sides) and must still match before the marker
goes in. The lease's hash is bound to the bytes that were hashed and written to
the CCR store; pointing that marker at different bytes would hand the model a
retrieval key for something it never saw. Every other index and shape guard in
here exists for the same reason: whatever fails a check keeps its original.

For an Anthropic ``tool_result`` block the rewrite makes the block's ``content``
a plain string. That is a valid wire shape for ``tool_result.content``, and the
block's envelope (``type``, ``tool_use_id``, ``is_error``, ``cache_control`` and
anything else the client sent) is preserved byte for byte.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from headroom.proxy.jev.candidates import JevCandidate, text_of
from headroom.proxy.jev.retention_ccr import RetentionLease
from headroom.proxy.jev.shadow import TRUNCATE_CHARS

#: How much of a truncated candidate stays inline. Deliberately the SAME width
#: Track A's shadow projection truncates at
#: (``headroom.proxy.jev.shadow.TRUNCATE_CHARS``). The dashboard compares TP
#: (Track A's projection) against TF (what active mode actually realized), so a
#: different truncation width here would make the projection systematically
#: over- or under-report the very savings it exists to predict.
JEV_TRUNCATE_CHARS = TRUNCATE_CHARS

#: The Responses item types whose payload lives in ``output`` rather than
#: ``content``. ``custom_tool_call_output`` is a real wire type Phase 0b
#: observed and the original plan's vocabulary missed. Kept in step with
#: ``candidates.ELIGIBLE_OUTPUT_ITEM_TYPES``, which is what put the candidate
#: here in the first place.
_OUTPUT_SLOT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

#: Separates the retained head from the marker, so the marker never fuses onto
#: the last surviving character of the original (the handler's CCR scanner
#: anchors on ``Retrieve more|original: hash=``, and a truncated line ending in
#: a word would otherwise run straight into it).
_TRUNCATION_JOIN = "\n…"

_APPLICABLE_DECISIONS = frozenset({"truncate", "drop"})


def apply_retention(
    messages: list[dict[str, Any]],
    candidates: Sequence[JevCandidate],
    decisions: Mapping[str, str],
    leases: Mapping[str, RetentionLease],
    *,
    truncate_chars: int = JEV_TRUNCATE_CHARS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(new_messages, applied_candidate_ids)``.

    ``messages`` is never mutated -- the return value is a deep copy even when
    nothing was applied, because the caller may still have to fall back to the
    original if anything downstream fails.

    ``applied_candidate_ids`` lists ONLY the candidates whose slot was actually
    rewritten, in the order the rewrites happened. It is what the caller may
    count realized savings for and what it must reconcile its staged leases
    against; a candidate that is missing from it still has its original content
    in the forwarded conversation.
    """
    out: list[dict[str, Any]] = copy.deepcopy(messages)
    applied: list[str] = []
    head_chars = max(0, truncate_chars)

    for cand in candidates:
        decision = decisions.get(cand.candidate_id, "keep")
        if decision not in _APPLICABLE_DECISIONS:
            continue
        lease = leases.get(cand.candidate_id)
        if lease is None:
            # A failed CCR step keeps the original. This is the last place that
            # rule can still be enforced, so it is enforced here too.
            continue
        if not 0 <= cand.message_index < len(out):
            continue
        message = out[cand.message_index]
        if not isinstance(message, dict):
            continue

        slot_owner = _slot_owner(message, cand)
        if slot_owner is None:
            continue
        owner, key = slot_owner

        current = text_of(owner.get(key, ""))
        if current != cand.content:
            # The slot moved or was rewritten between selection and here. The
            # lease is bound to the content we hashed, so applying it to
            # different bytes would point the marker at the wrong original.
            continue

        if decision == "drop":
            owner[key] = lease.marker
        else:
            if len(current) <= head_chars:
                # The whole original is already inline: appending a marker
                # would ADD tokens and advertise a retrieval for content the
                # model can already read. Track A's ``shadow._truncated`` makes
                # the same call, so the projection and the realized rewrite do
                # not drift. Not a rewrite, so not an applied candidate.
                continue
            owner[key] = f"{current[:head_chars]}{_TRUNCATION_JOIN}{lease.marker}"
        applied.append(cand.candidate_id)

    return out, applied


def _slot_owner(message: dict[str, Any], cand: JevCandidate) -> tuple[dict[str, Any], str] | None:
    """The dict holding this candidate's payload and the key it lives under.

    ``None`` when the shape under ``cand``'s coordinates is no longer the one
    selection found -- an index past the end, a block list that is not a list,
    a block that is no longer a ``tool_result``, or a Responses item whose
    ``type`` has changed. Every such case keeps the original.
    """
    if cand.block_index is None:
        # The key follows the candidate TYPE, the same distinction
        # ``candidates.py`` made when it read the payload, rather than sniffing
        # whichever key happens to be present.
        if cand.candidate_type in _OUTPUT_SLOT_TYPES:
            if message.get("type") != cand.candidate_type:
                return None
            return message, "output"
        return message, "content"

    blocks = message.get("content")
    if not isinstance(blocks, list) or not 0 <= cand.block_index < len(blocks):
        return None
    block = blocks[cand.block_index]
    if not isinstance(block, dict) or block.get("type") != "tool_result":
        return None
    return block, "content"
