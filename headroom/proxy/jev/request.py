"""The bounded retention view and the measured request budget.

Both halves are ported from ``benchmarks/jev_savings_spike.py`` after Phase 0a,
including the two bugs Codex forced fixes for:

* Jev rejects an oversized request outright with
  ``{"detail": {"error_type": "max_tokens_exceeded"}}`` -- the *whole* call is
  lost, not just the overflow. ``max_candidate_tokens`` bounds each candidate
  individually and says nothing about the total, so :func:`enforce_state_budget`
  is the second, total bound.
* The bound is measured on the **actual serialized request**, not on candidate
  content alone. Candidate metadata, per-question instructions and the three
  criteria descriptions are a large fraction of the payload; a fixed
  per-candidate overhead constant underestimated them by ~50%. The first
  candidate is not exempt either -- one oversized candidate on its own is
  exactly the request Jev would reject.

Trimming is honest: a trimmed candidate is still reported as a candidate and is
left untouched in the projection (effectively ``keep``), so TP never claims
savings on a candidate Jev was never asked about. For the same reason the state
reports each candidate's *true* size and digest next to the truncated view, so
neither Jev nor a later reader is misled about what was actually shown.

Nothing credential-bearing reaches the state: every field is a caller-supplied
identity string, and the Jev model is passed in as ``jev_model``. The API key
and the endpoint live on :class:`~headroom.proxy.jev.config.JevConfig` and are
only ever used by the client's transport.

A raising ``count_text`` is deliberately *not* absorbed here, matching
``candidates.py``: the caller's hook wraps the whole attempt in its own
fail-open guard and records the abort, so a broken tokenizer stays observable
instead of silently producing a fabricated budget.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.encoding import encode_identity_text

#: Floor for the per-candidate content bound. Below this a candidate's view is
#: too thin for a retention decision to mean anything.
MIN_VIEW_TOKENS = 256

#: Characters per token in the view bound. The same approximation the Phase 0a
#: corpus used; the view is a *bound*, and the request as a whole is measured
#: with the caller's real tokenizer, so this only has to be roughly right.
_CHARS_PER_TOKEN = 4

DECISION_CRITERIA: dict[str, str] = {
    "keep": (
        "This tool result still carries information the assistant is likely to "
        "need again later in the conversation; removing or shortening it would "
        "lose facts that are not recoverable from the surrounding messages."
    ),
    "truncate": (
        "Only the beginning / shape of this tool result matters from here on "
        "(a header, a count, the first few rows). The bulk of its body is "
        "redundant detail that can be cut without losing the thread."
    ),
    "drop": (
        "This tool result has been fully superseded, summarized by a later "
        "assistant message, or is simply no longer referenced. Removing it "
        "entirely would not change what the assistant can answer."
    ),
}

QUESTION_INSTRUCTIONS = (
    "Decide what to do with the historical tool result identified by this "
    "question's key ({cid}) in the `candidates` array of the state. It is "
    "message #{idx} of {total}, {from_end} messages from the end of the "
    "conversation, and costs about {tokens} tokens. Choose `keep` only if the "
    "content is genuinely still needed."
)

TASK_DESCRIPTION = (
    "These are historical tool results from an agent conversation that has "
    "already been deterministically compressed. Decide, per candidate, whether "
    "its content must still be kept verbatim, can be truncated to its first "
    "lines, or can be dropped entirely."
)


def build_retention_state(
    *,
    provider: str,
    model: str,
    jev_model: str,
    session_id: str,
    branch_id: str,
    revision: str,
    message_shape: str,
    total_messages: int,
    frozen_prefix: int,
    recent_tail: int,
    candidates: list[JevCandidate],
    max_candidate_tokens: int,
) -> dict[str, Any]:
    """The bounded retention view sent as the Jev ``state``.

    Each candidate's ``content`` is a deterministic leading slice of the real
    content -- the same input always produces the same view -- bounded by
    ``max_candidate_tokens``. ``estimated_tokens``, ``content_bytes`` and
    ``content_sha256`` always describe the *whole* candidate, and
    ``content_truncated_for_view`` says whether the two differ.

    ``content_bytes`` is a measurement rather than an identity, but it goes
    through the same :func:`~headroom.proxy.jev.encoding.encode_identity_text`
    the hash beside it uses: one string encoded two different ways one line
    apart is how the two drift.
    """
    max_chars = max(1, max_candidate_tokens) * _CHARS_PER_TOKEN
    return {
        "provider": provider,
        "model": model,
        "jev_model": jev_model,
        "session_id": session_id,
        "branch_id": branch_id,
        "revision": revision,
        "message_shape": message_shape,
        "total_messages": total_messages,
        "protected_prefix_messages": frozen_prefix,
        "recent_tail_excluded_messages": recent_tail,
        "task": TASK_DESCRIPTION,
        "candidates": [
            {
                "candidate_id": cand.candidate_id,
                "candidate_type": cand.candidate_type,
                "role": cand.role,
                "tool_call_id": cand.tool_call_id,
                "message_index": cand.message_index,
                "block_index": cand.block_index,
                "order_from_end": total_messages - cand.message_index,
                "estimated_tokens": cand.est_tokens,
                "content_bytes": len(encode_identity_text(cand.content)),
                "content_sha256": cand.content_sha256,
                "content_truncated_for_view": len(cand.content) > max_chars,
                "content": cand.content[:max_chars],
            }
            for cand in candidates
        ],
    }


def build_questions(
    candidates: list[JevCandidate], total_messages: int
) -> dict[str, dict[str, Any]]:
    """One ``choice`` question per candidate, keyed by candidate id."""
    return {
        cand.candidate_id: {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS.format(
                cid=cand.candidate_id,
                idx=cand.message_index,
                total=total_messages,
                from_end=total_messages - cand.message_index,
                tokens=cand.est_tokens,
            ),
            "criteria": dict(DECISION_CRITERIA),
        }
        for cand in candidates
    }


def _view_ladder(start: int, floor: int) -> Iterator[int]:
    """``start`` then repeated halving, ending exactly on ``floor``.

    Finite by construction (``O(log2(start / floor))`` rungs), which is what
    keeps :func:`enforce_state_budget` terminating on any ``count_text``.
    """
    view = max(start, floor)
    while view > floor:
        yield view
        view = max(floor, view // 2)
    yield floor


def enforce_state_budget(
    candidates: list[JevCandidate],
    *,
    count_text: Callable[[str], int],
    make_payload: Callable[[list[JevCandidate], int], dict[str, Any]],
    max_candidate_tokens: int,
    max_state_tokens: int,
) -> tuple[list[JevCandidate], int, int]:
    """Fit the request inside Jev's input limit, measured not estimated.

    Returns ``(kept, view_tokens_per_candidate, serialized_tokens)``, where
    ``serialized_tokens`` is the measured size of ``make_payload(kept,
    view_tokens_per_candidate)`` -- the payload the caller is expected to send.
    When ``kept`` is empty no request should be made at all, and the third
    value is the measured fixed overhead (0 when there was nothing to measure).

    Two levers, in order of preference:

    1. Drop trailing (newest) candidates. The oldest are the likeliest to be
       stale, and a dropped candidate is simply never asked about.
    2. Only when *nothing* fits, thin the per-candidate view and try again,
       down to :data:`MIN_VIEW_TOKENS`. Thinning degrades every remaining
       decision, so it is the last thing tried before giving up on the call.

    ``count_text`` is called on the real serialized payload; if it raises, the
    exception propagates (see the module docstring).
    """
    count = len(candidates)
    ceiling = max(1, max_candidate_tokens)
    floor = min(ceiling, MIN_VIEW_TOKENS)
    share = max_state_tokens // max(1, count)
    start = min(ceiling, max(share, floor))

    if not candidates:
        return [], start, 0

    def _size(sel: list[JevCandidate], view: int) -> int:
        return int(count_text(json.dumps(make_payload(sel, view), default=str)))

    # The empty payload is the fixed cost of asking at all: identity, the task
    # description and the envelope. No view is thin enough to get under it, so
    # a budget below it is hopeless and is rejected once, not once per rung.
    base = _size([], start)
    if base > max_state_tokens:
        return [], floor, base

    for view in _view_ladder(start, floor):
        # Price each candidate by what it actually adds to the serialized
        # payload, then settle on the exact size of the payload being sent:
        # deltas miss a handful of separator tokens, so the last word is a
        # measurement, not an estimate.
        kept: list[JevCandidate] = []
        running = base
        for cand in candidates:
            delta = _size([cand], view) - base
            if running + delta > max_state_tokens:
                break
            kept.append(cand)
            running += delta

        while kept:
            exact = _size(kept, view)
            if exact <= max_state_tokens:
                return kept, view, exact
            kept.pop()

    return [], floor, base
