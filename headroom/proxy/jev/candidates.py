"""Eligible retention candidates in a post-Headroom message list.

Eligibility follows the fresh design's Track A rules, implemented and validated
in ``benchmarks/jev_savings_spike.py`` during Phase 0a:

* An allowlisted tool-result shape -- an OpenAI Chat ``role: "tool"`` message, a
  Responses ``function_call_output`` / ``custom_tool_call_output`` item, or an
  Anthropic ``tool_result`` block inside a user message's content list.
  ``custom_tool_call_output`` is in the allowlist because Phase 0b observed it
  as a real wire item type, wider than the original plan's assumed vocabulary.
* Outside the last ``RECENT_TAIL_EXCLUSION`` (6) messages. This is a retention
  *eligibility* rule and is implemented here directly: Headroom's own
  ``protect_recent`` router guard is a compressor knob with a different default
  (4), so it is not a substitute.
* Outside the caller's frozen/protected prefix.
* Bounded in number, oldest first -- the oldest candidates are the ones most
  likely to be stale.

Token counting takes a ``count_text`` callable so the caller can pass the
tokenizer the request already resolved (``OpenAICompatibleTokenCounter`` and
every other ``headroom.tokenizers.TokenCounter`` expose ``count_text`` /
``count_messages``), rather than this module resolving a second tokenizer.

Everything here is fail-open. The input is request-shaped data straight off the
wire: keys may be missing, list entries may not be dicts, and a ``type`` may be
any JSON value. Selection degrades to "no candidate" rather than raising, since
a Jev bookkeeping error must never take a proxied request down.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Nothing in the last N messages is ever a candidate.
RECENT_TAIL_EXCLUSION = 6

#: Bare item types (no ``role``) whose payload lives in ``output``.
ELIGIBLE_OUTPUT_ITEM_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


def text_of(value: Any) -> str:
    """Deterministic text for a wire payload, without ever raising.

    ``default=str`` covers exotic *values*, but not exotic *keys* (a tuple key
    is a ``TypeError``) nor a self-referential structure (a ``ValueError``), and
    a deeply nested one is a ``RecursionError``. Those are all reachable from a
    caller that hands us something other than a freshly JSON-decoded body, so
    the serialiser is backstopped rather than trusted.

    ``None`` is empty text on purpose: a missing payload has no content to
    project or price, and the literal ``"null"`` would be four characters of
    noise in both the fingerprint and the token estimate.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return ""


def _optional_id(value: Any) -> str | None:
    """A tool-call id as text, or ``None`` when absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def _estimate(count_text: Callable[[str], int], text: str) -> int:
    """``count_text`` with a crude fallback if the tokenizer misbehaves."""
    try:
        return int(count_text(text))
    except Exception:
        return max(1, len(text) // 4) if text else 0


@dataclass(frozen=True)
class JevCandidate:
    """One eligible historical tool result."""

    candidate_id: str
    message_index: int
    block_index: int | None
    candidate_type: str
    role: str
    tool_call_id: str | None
    content: str
    est_tokens: int

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8", "replace")).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Identity of this candidate for revision hashing."""
        return (
            f"{self.candidate_id}:{self.message_index}:{self.block_index}:"
            f"{self.candidate_type}:{self.content_sha256}"
        )


def _item_type(msg: dict[str, Any]) -> str | None:
    """The message's ``type``, but only when it is a string.

    ``msg.get("type") in ELIGIBLE_OUTPUT_ITEM_TYPES`` raises ``TypeError`` for
    an unhashable value, and ``{"type": []}`` is a body a client can send.
    """
    value = msg.get("type")
    return value if isinstance(value, str) else None


def select_candidates(
    messages: list[dict[str, Any]],
    *,
    frozen_prefix: int,
    count_text: Callable[[str], int],
    max_candidates: int,
    recent_tail: int = RECENT_TAIL_EXCLUSION,
) -> list[JevCandidate]:
    """Eligible candidates from a post-Headroom message list, oldest first."""
    if not isinstance(messages, list):
        return []

    limit = max(0, max_candidates)
    if limit == 0:
        return []

    total = len(messages)
    tail_start = total - max(0, recent_tail)
    floor = max(0, frozen_prefix)
    found: list[JevCandidate] = []

    def _add(
        *,
        message_index: int,
        block_index: int | None,
        candidate_type: str,
        role: str,
        tool_call_id: Any,
        body: str,
    ) -> None:
        found.append(
            JevCandidate(
                candidate_id=f"cand_{len(found):04d}",
                message_index=message_index,
                block_index=block_index,
                candidate_type=candidate_type,
                role=role,
                tool_call_id=_optional_id(tool_call_id),
                content=body,
                est_tokens=_estimate(count_text, body),
            )
        )

    for idx in range(floor, min(tail_start, total)):
        if len(found) >= limit:
            break
        msg = messages[idx]
        if not isinstance(msg, dict):
            continue

        role = str(msg.get("role") or "")

        # OpenAI Chat Completions: a whole message with role="tool".
        if role == "tool" and msg.get("content") is not None:
            _add(
                message_index=idx,
                block_index=None,
                candidate_type="tool_result",
                role=role,
                tool_call_id=msg.get("tool_call_id"),
                body=text_of(msg["content"]),
            )
            continue

        # OpenAI Responses: a bare {"type": "...output", "output": ...} item.
        item_type = _item_type(msg)
        if item_type in ELIGIBLE_OUTPUT_ITEM_TYPES:
            _add(
                message_index=idx,
                block_index=None,
                candidate_type=str(item_type),
                role=role or "tool",
                tool_call_id=msg.get("call_id") or msg.get("id"),
                body=text_of(msg.get("output")),
            )
            continue

        # Anthropic: tool_result blocks inside a user message's content list.
        content = msg.get("content")
        if isinstance(content, list):
            for bidx, block in enumerate(content):
                if len(found) >= limit:
                    break
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                _add(
                    message_index=idx,
                    block_index=bidx,
                    candidate_type="tool_result",
                    role=role or "user",
                    tool_call_id=block.get("tool_use_id"),
                    body=text_of(block.get("content")),
                )

    return found


def count_messages_corrected(
    messages: list[dict[str, Any]],
    *,
    count_messages: Callable[[list[dict[str, Any]]], int],
    count_text: Callable[[str], int],
) -> int:
    """Token count that also prices Responses ``output`` payloads.

    Phase 0a bug: a message counter only ever reads a message's ``content``, so
    an OpenAI Responses ``function_call_output`` item -- whose payload lives in
    ``output`` -- prices at ~0. That is the exact field candidate selection and
    the projection operate on, so without this correction a drop/truncate of
    such a candidate moves real tokens while TH and TP both stay put, and the
    savings are silently reported as zero.
    """
    if not isinstance(messages, list):
        return 0

    try:
        total = int(count_messages(messages))
    except Exception:
        total = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if content is not None:
                total += _estimate(count_text, text_of(content))

    for msg in messages:
        if not isinstance(msg, dict) or _item_type(msg) not in ELIGIBLE_OUTPUT_ITEM_TYPES:
            continue
        output = msg.get("output")
        if output is not None:
            total += _estimate(count_text, text_of(output))
    return total
