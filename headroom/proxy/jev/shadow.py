"""Track A: shadow-mode retention measurement.

Runs AFTER Headroom's own deterministic compression and BEFORE anything is
forwarded. It answers one question -- "how many more tokens would Jev's
retention decisions have saved on top of what Headroom already did" -- and
answers it by measuring a projection (``TP``) on a private deep copy.

Hard invariant: this module never mutates the message list it is given, and its
return value is never applied to a forwarded request. Track B and Track C own
mutation; Track A does not. :attr:`JevShadowResult.projected_savings` is a
*projection*, reported on its own; it is never folded into a realized-savings
total.

Gates, in order (each cheaper than the next):

1. mode is ``shadow``
2. soft threshold -- post-Headroom tokens vs ``threshold_percent`` of the
   model's context limit
3. per-(session, branch) in-flight guard: one bounded call at a time
4. per-(session, branch) cooldown, in turns
5. eligible candidates exist (a zero-candidate skip is recorded, not silent)
6. the request fits the measured state budget

Every exit records exactly one outcome metric (``shadow_call_attempted`` and
``shadow_all_keep`` are extra colour on top of one, not outcomes of their own).

Fail-open is this module's job, not its dependencies'. ``candidates.py`` and
``request.py`` deliberately let a raising ``count_text`` propagate so a broken
tokenizer stays observable rather than fabricating a budget; :meth:`
JevShadowRunner.maybe_run` is the guard that catches it, records
``shadow_fail_open`` and returns a no-op result. ``asyncio.CancelledError`` is
*not* caught: swallowing the caller's own cancellation would be a bug, and a
cancelled turn is not a Jev failure.
"""

from __future__ import annotations

import contextlib
import copy
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from headroom.proxy.jev.candidates import (
    RECENT_TAIL_EXCLUSION,
    JevCandidate,
    count_messages_corrected,
    select_candidates,
    text_of,
)
from headroom.proxy.jev.client import JevClient, build_request_payload
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.identity import (
    DEFAULT_MAX_BRANCHES,
    JevIdentityStore,
    JevTurnIdentity,
    branch_id_for,
)
from headroom.proxy.jev.request import (
    build_questions,
    build_retention_state,
    enforce_state_budget,
)

logger = logging.getLogger(__name__)

#: What a ``truncate`` decision does to candidate content, in characters.
TRUNCATE_CHARS = 400

_TRUNCATION_NOTE = "\n…[truncated by Jev retention decision]"

_MAX_ERROR_CHARS = 400


@dataclass(frozen=True)
class JevShadowResult:
    """Outcome of one shadow attempt. Purely observational."""

    ran: bool
    reason: str
    identity: JevTurnIdentity | None = None
    candidates: int = 0
    candidates_sent: int = 0
    keep: int = 0
    truncate: int = 0
    drop: int = 0
    #: T0 -- the caller's pre-Headroom token count for this turn, passed
    #: straight through so the /stats `jev` block can report Jev's numbers
    #: against the same baseline the rest of the dashboard uses. 0 when the
    #: call site could not supply one.
    tokens_baseline: int = 0
    tokens_headroom: int = 0
    tokens_projected: int = 0
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def projected_savings(self) -> int:
        """``TH - TP``. Reported separately; never added to realized savings."""
        return max(0, self.tokens_headroom - self.tokens_projected)


def _truncated(original: str) -> str:
    """The first :data:`TRUNCATE_CHARS` characters, flagged when anything went."""
    if len(original) <= TRUNCATE_CHARS:
        return original
    return original[:TRUNCATE_CHARS] + _TRUNCATION_NOTE


def apply_decisions_to_copy(
    messages: list[dict[str, Any]],
    candidates: list[JevCandidate],
    decisions: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply keep/truncate/drop to a DEEP COPY. Nothing here is ever forwarded.

    A candidate with no decision (one the state budget trimmed away, so Jev was
    never asked about it) defaults to ``keep``: the projection must never claim
    savings on a candidate nobody decided.
    """
    projected: list[dict[str, Any]] = copy.deepcopy(messages)

    drop_messages: set[int] = set()
    drop_blocks: set[tuple[int, int]] = set()

    for cand in candidates:
        decision = decisions.get(cand.candidate_id, "keep")
        if decision not in ("truncate", "drop"):
            continue
        if not 0 <= cand.message_index < len(projected):
            continue
        msg = projected[cand.message_index]
        if not isinstance(msg, dict):
            continue

        if cand.block_index is None:
            if decision == "drop":
                drop_messages.add(cand.message_index)
                continue
            # A Responses ``function_call_output`` item carries its payload in
            # ``output``; everything else in ``content``. ``candidates.py``
            # makes the same distinction, so the two stay in step.
            key = "content" if msg.get("content") is not None else "output"
            msg[key] = _truncated(text_of(msg.get(key, "")))
            continue

        content = msg.get("content")
        if not isinstance(content, list) or not 0 <= cand.block_index < len(content):
            continue
        block = content[cand.block_index]
        if not isinstance(block, dict):
            continue
        if decision == "drop":
            drop_blocks.add((cand.message_index, cand.block_index))
        else:
            # ``cand.content`` is the flattened block text, so that is what the
            # projection truncates -- a list payload collapses to its leading
            # slice, exactly as the state showed it to Jev.
            block["content"] = _truncated(text_of(block.get("content", "")))

    # Remove dropped blocks first; message indices shift once the list changes.
    for midx in sorted({m for m, _ in drop_blocks}):
        surviving = [
            block
            for bidx, block in enumerate(projected[midx]["content"])
            if (midx, bidx) not in drop_blocks
        ]
        if surviving:
            projected[midx]["content"] = surviving
        else:
            drop_messages.add(midx)

    return [msg for i, msg in enumerate(projected) if i not in drop_messages]


class JevShadowRunner:
    """Owns the shadow trigger policy, cooldown and in-flight bookkeeping."""

    def __init__(
        self,
        config: JevConfig,
        *,
        client: Any | None = None,
        identity_store: JevIdentityStore | None = None,
        metrics: Any | None = None,
        max_branches: int = DEFAULT_MAX_BRANCHES,
    ) -> None:
        self._config = config
        self._client = client if client is not None else JevClient(config)
        self.identity_store = identity_store or JevIdentityStore(max_branches=max_branches)
        self._metrics = metrics
        self._max_branches = max(1, max_branches)
        # Bounded for the same reason ``JevIdentityStore`` is: session ids come
        # from a client-controlled header, so an unbounded per-branch map is a
        # memory leak a caller can drive.
        self._turns_since_call: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()

    @property
    def enabled(self) -> bool:
        return self._config.is_shadow

    @property
    def tracked_cooldowns(self) -> int:
        return len(self._turns_since_call)

    def _record(self, event: str) -> None:
        if self._metrics is None:
            return
        with contextlib.suppress(Exception):
            self._metrics.record_jev_event(event)

    def _skip(self, reason: str, *, event: str | None = None, **fields: Any) -> JevShadowResult:
        if event is not None:
            self._record(event)
        return JevShadowResult(ran=False, reason=reason, **fields)

    def _note_cooldown(self, key: tuple[str, str], turns: int) -> None:
        self._turns_since_call[key] = turns
        self._turns_since_call.move_to_end(key)
        while len(self._turns_since_call) > self._max_branches:
            self._turns_since_call.popitem(last=False)

    async def maybe_run(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        frozen_prefix: int,
        optimized_tokens: int,
        # T0: the caller's pre-Headroom count, recorded as-is. Keyword-only with
        # a default so a call site that has no baseline to offer simply omits it.
        original_tokens: int = 0,
        context_limit: int,
        session_id: str,
        count_text: Callable[[str], int],
        count_messages: Callable[[list[dict[str, Any]]], int],
        message_shape: str,
    ) -> JevShadowResult:
        """One shadow attempt. Never mutates ``messages``; never raises for a
        Jev-side failure (the client fails open, and this method is the
        fail-open guard for everything around it)."""
        try:
            return await self._attempt(
                provider=provider,
                model=model,
                messages=messages,
                frozen_prefix=frozen_prefix,
                optimized_tokens=optimized_tokens,
                original_tokens=original_tokens,
                context_limit=context_limit,
                session_id=session_id,
                count_text=count_text,
                count_messages=count_messages,
                message_shape=message_shape,
            )
        except Exception as exc:  # noqa: BLE001 - a bookkeeping bug must not
            # take a proxied request down. CancelledError is a BaseException
            # and is deliberately not caught here.
            detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            logger.warning("jev shadow failed open: %s", detail, exc_info=True)
            return self._skip("fail_open", event="shadow_fail_open", error=detail)

    async def _attempt(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        frozen_prefix: int,
        optimized_tokens: int,
        original_tokens: int,
        context_limit: int,
        session_id: str,
        count_text: Callable[[str], int],
        count_messages: Callable[[list[dict[str, Any]]], int],
        message_shape: str,
    ) -> JevShadowResult:
        if not self.enabled:
            return JevShadowResult(ran=False, reason="disabled")
        if not messages:
            return self._skip("no_messages", event="shadow_no_messages")

        # 1. Soft threshold against the model's context window. Integer
        #    arithmetic on both sides: no float rounding decides a gate.
        if context_limit <= 0:
            return self._skip("no_context_limit", event="shadow_no_context_limit")
        if optimized_tokens * 100 < context_limit * self._config.threshold_percent:
            return self._skip("below_threshold", event="shadow_below_threshold")

        # 2. Branch scope. The root is the frozen/protected prefix, so ordinary
        #    turn growth stays on one branch while a re-rooted conversation forks.
        branch_root = messages[: max(1, frozen_prefix)]
        key = (session_id, branch_id_for(session_id, branch_root))

        # 3. One bounded call at a time per branch.
        if key in self._inflight:
            return self._skip("inflight", event="shadow_inflight")

        # 4. Cooldown in turns. The first eligible turn on a branch always runs,
        #    and only turns that reach this gate count against it -- a turn
        #    skipped as below-threshold was never a candidate for a call.
        since = self._turns_since_call.get(key)
        if since is not None and since < self._config.cooldown_turns:
            self._note_cooldown(key, since + 1)
            return self._skip("cooldown", event="shadow_cooldown")

        # 5. Eligible candidates.
        eligible = select_candidates(
            messages,
            frozen_prefix=frozen_prefix,
            count_text=count_text,
            max_candidates=self._config.max_candidates,
        )
        if not eligible:
            return self._skip("no_candidates", event="shadow_no_candidates")

        identity = self.identity_store.identify(
            session_id=session_id,
            branch_root=branch_root,
            candidate_fingerprints=[cand.fingerprint for cand in eligible],
        )

        def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, Any]:
            state = build_retention_state(
                provider=provider,
                model=model,
                jev_model=self._config.model,
                session_id=identity.session_id,
                branch_id=identity.branch_id,
                revision=identity.revision,
                message_shape=message_shape,
                total_messages=len(messages),
                frozen_prefix=frozen_prefix,
                recent_tail=RECENT_TAIL_EXCLUSION,
                candidates=sel,
                max_candidate_tokens=view_tokens,
            )
            return build_request_payload(self._config, state, build_questions(sel, len(messages)))

        # 6. Measured request budget. An empty ``sent`` means the third value is
        #    the fixed overhead, not a sendable payload: there is no call to make.
        sent, view_tokens, _serialized = enforce_state_budget(
            eligible,
            count_text=count_text,
            make_payload=make_payload,
            max_candidate_tokens=self._config.max_candidate_tokens,
            max_state_tokens=self._config.max_state_tokens,
        )
        if not sent:
            return self._skip(
                "state_budget_exhausted",
                event="shadow_budget_exhausted",
                identity=identity,
                candidates=len(eligible),
            )

        payload = make_payload(sent, view_tokens)
        self._record("shadow_call_attempted")
        self._inflight.add(key)
        try:
            answer = await self._client.decide(
                state=payload["state"],
                questions=payload["questions"],
                candidate_ids=[cand.candidate_id for cand in sent],
            )
        finally:
            self._inflight.discard(key)
            # The call was spent whatever came back (including a cancellation),
            # so the cooldown starts here rather than on success only.
            self._note_cooldown(key, 0)

        if answer.error is not None:
            logger.info("jev shadow call failed open: %s", answer.error)
            return self._skip(
                "call_error",
                event="shadow_call_error",
                identity=identity,
                candidates=len(eligible),
                candidates_sent=len(sent),
                latency_ms=answer.latency_ms,
                error=answer.error,
            )

        # 7. The conversation may have moved on while the call was in flight.
        if not self.identity_store.is_current(identity):
            return self._skip(
                "stale_revision",
                event="shadow_stale_revision",
                identity=identity,
                candidates=len(eligible),
                candidates_sent=len(sent),
                latency_ms=answer.latency_ms,
            )

        # 8. Projection, on a private copy. TH and TP are counted the same way
        #    so the pair stays coherent (Phase 0a token-accounting fix).
        projected_messages = apply_decisions_to_copy(messages, sent, answer.decisions)
        th = count_messages_corrected(
            messages, count_messages=count_messages, count_text=count_text
        )
        tp = count_messages_corrected(
            projected_messages, count_messages=count_messages, count_text=count_text
        )

        tallies = {"keep": 0, "truncate": 0, "drop": 0}
        for decision in answer.decisions.values():
            if decision in tallies:
                tallies[decision] += 1

        self._record("shadow_projected")
        if tallies["keep"] == len(sent):
            # The old metadata-keep-v2 bias: unseen results always come back
            # "keep". Worth a counter, not a failure.
            self._record("shadow_all_keep")

        return JevShadowResult(
            ran=True,
            reason="projected",
            identity=identity,
            candidates=len(eligible),
            candidates_sent=len(sent),
            keep=tallies["keep"],
            truncate=tallies["truncate"],
            drop=tallies["drop"],
            tokens_baseline=max(0, int(original_tokens or 0)),
            tokens_headroom=th,
            tokens_projected=tp,
            latency_ms=answer.latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
