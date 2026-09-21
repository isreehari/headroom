"""One call site for Track B active retention. Never raises.

Sequence, in order, with a hard stop at any failure::

    decide (Task 15) -> stage CCR per candidate (Task 13) -> apply (Task 14)

The plan's safety ordering is the contract: *write the original to CCR ->
require an acknowledged success -> bind to session/branch/candidate hash +
retention lease -> commit atomically; any failed step keeps the original.*
Staging therefore completes for a candidate BEFORE anything is rewritten, and
``apply_retention`` is only ever shown leases that were actually taken. A
candidate that does not get an acknowledged, leased CCR entry is not applied,
so a store failure costs a saving and never a tool result.

This module is the SINGLE fail-open guard for the active path. Everything below
it is deliberately allowed to raise:

* :func:`decide_active_retention` propagates a broken tokenizer, a failed
  tokenizer lookup and a selection failure rather than fabricating a token
  count (see ``active.py``'s module docstring);
* :func:`stage_retention` never raises but returns ``None``, and
  :func:`apply_retention` skips whatever it cannot safely rewrite.

So any exception that reaches here is a bug in retention, and a bug in
retention must not fail a proxied turn: it is reported as ``fail_open`` and the
caller forwards Headroom's ordinary compressed output unchanged.

Metric vocabulary -- exactly one terminal event per attempted turn, so
``active_attempted`` always equals the sum of the outcomes:

* ``active_attempted``     -- mode is active; the work below started.
* ``active_no_candidates`` -- nothing to act on: no eligible candidate
  (``no_candidates``), the measured request budget admitted none
  (``not_asked``), or Jev kept everything (``all_keep``).
* ``active_call_failed``   -- the Jev call reported an error; every candidate
  keeps (``call_failed``).
* ``active_no_lease``      -- something should have been retained and nothing
  was: the CCR sequence refused every candidate (``no_lease``), or the leases
  were taken but no slot could be rewritten (``not_applied``).
* ``active_applied``       -- at least one candidate was retained and rewritten
  (``applied``). Partial refusals stay inside this outcome; ``candidates`` and
  ``applied`` on the result report the shortfall, and ``stage_retention`` has
  already logged each refusal.
* ``active_fail_open``     -- an unexpected exception (``fail_open``).
"""

from __future__ import annotations

import contextlib
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any

from headroom.cache.compression_store import get_compression_store
from headroom.proxy.jev.accounting import classify_call_error, record_jev_accounting
from headroom.proxy.jev.active import decide_active_retention
from headroom.proxy.jev.candidates import count_messages_corrected
from headroom.proxy.jev.client import JevClient, scrub_secrets
from headroom.proxy.jev.compress_gate import JEV_COMPRESS_BRANCH_ID
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.retention_apply import apply_retention
from headroom.proxy.jev.retention_ccr import RetentionLease, stage_retention
from headroom.tokenizers import get_tokenizer

logger = logging.getLogger(__name__)

#: Same bound the client, the shadow runner and the shadow hook put on an error
#: string before it reaches a log line.
_MAX_ERROR_CHARS = 400

#: Bound on the scrubbed traceback that replaces ``exc_info=True`` on the
#: fail-open path. Wider than an error string because it has frames in it.
_MAX_TRACEBACK_CHARS = 4000


@dataclass(frozen=True)
class JevActiveResult:
    """What the turn did.

    ``messages`` is the caller's own list object on every path that changed
    nothing, and a deep copy carrying the rewrites on the applied path -- the
    input is never mutated, so the caller can always fall back to it.

    ``tokens_after`` is measured on the RETURNED messages and is meaningful
    only when ``applied > 0``; it is ``0`` on every other path, where the
    caller already has its own count for the messages it is forwarding
    unchanged.

    ``hashes`` holds the CCR keys of the APPLIED candidates only. A lease that
    was staged but not applied is harmless -- its entry simply expires -- and
    advertising its hash would claim a saving the conversation did not make.
    """

    messages: list[dict[str, Any]]
    tokens_after: int
    applied: int
    candidates: int
    called: bool
    reason: str
    hashes: list[str] = field(default_factory=list)


def _record(proxy: Any, event: str) -> None:
    """Count one lifecycle event. A metrics failure never fails a turn."""
    with contextlib.suppress(Exception):
        metrics = getattr(proxy, "metrics", None)
        recorder = getattr(metrics, "record_jev_event", None)
        if recorder is not None:
            recorder(event)


def _record_accounting(proxy: Any, **fields: int) -> None:
    """Add to the shared /stats totals. Never raises (see accounting.py).

    The ``proxy.metrics`` lookup is suppressed here for the same reason
    ``accounting.record_jev_accounting`` guards its own: a property on a
    caller-supplied proxy object can raise, and this is called from the
    fail-open path where an escaping exception would defeat the point.
    """
    with contextlib.suppress(Exception):
        record_jev_accounting(getattr(proxy, "metrics", None), **fields)


def _active_config(proxy: Any) -> JevConfig | None:
    """``proxy.config.jev`` when this proxy is in active mode, else ``None``.

    The caller may leave ``jev_compaction_boundary`` on permanently; with
    ``HEADROOM_JEV_MODE`` off or shadow that is a no-op, not an error. The
    lookup itself is guarded because it runs before the fail-open handler has a
    config to scrub error text against.
    """
    try:
        config = getattr(getattr(proxy, "config", None), "jev", None)
        if config is None or getattr(config, "mode", "off") != "active":
            return None
    except Exception as exc:  # noqa: BLE001 - an exotic config object is a no-op, not a 500
        # No `exc_info`: there is no config to scrub the traceback against
        # here, and a property raising with the endpoint or key in its message
        # would then be logged verbatim. The type name is enough to diagnose a
        # malformed proxy object.
        logger.warning(
            "jev active retention: unreadable proxy config (%s); skipping",
            type(exc).__name__,
        )
        return None
    return config  # type: ignore[no-any-return]


def _detail(exc: BaseException, config: JevConfig) -> str:
    """Log-safe text for an arbitrary exception.

    The exception is arbitrary -- a caller-supplied tokenizer, an injected
    client or the store can put the configured endpoint (userinfo, query token)
    or the API key straight into its message -- so the same scrubber the client
    uses runs here. Scrub first, then truncate: a key straddling the cut would
    otherwise survive as a prefix. If scrubbing itself fails the message is
    dropped entirely rather than logged unscrubbed.
    """
    try:
        return scrub_secrets(f"{type(exc).__name__}: {exc}", config)[:_MAX_ERROR_CHARS]
    except Exception:  # noqa: BLE001 - never trade a leak for a nicer log line
        return type(exc).__name__


def _trace(exc: BaseException, config: JevConfig) -> str:
    """The traceback, scrubbed, for a DEBUG line.

    ``logger.warning(..., exc_info=True)`` cannot be used anywhere on this
    path: the formatter renders the ORIGINAL exception, so the raw ``str(exc)``
    (and each chained ``__cause__``) lands in the log unscrubbed. Rendering the
    traceback ourselves keeps the one invariant this branch holds -- every
    exception text reaching a log goes through ``scrub_secrets`` first. The
    TAIL is kept because that is where the frames closest to the failure, and
    the exception line itself, live.
    """
    try:
        text = "".join(traceback.format_exception(exc))
        return scrub_secrets(text, config)[-_MAX_TRACEBACK_CHARS:]
    except Exception:  # noqa: BLE001 - never trade a leak for a nicer log line
        return type(exc).__name__


async def run_jev_active_retention(
    *,
    proxy: Any,
    messages: list[dict[str, Any]],
    model: str,
    session_id: str,
    branch_id: str = JEV_COMPRESS_BRANCH_ID,
    frozen_prefix: int = 0,
    message_shape: str = "openai",
) -> JevActiveResult:
    """Run active retention for one declared compaction boundary. Never raises."""

    def _unchanged(reason: str, *, candidates: int = 0, called: bool = False) -> JevActiveResult:
        return JevActiveResult(
            messages=messages,
            tokens_after=0,
            applied=0,
            candidates=candidates,
            called=called,
            reason=reason,
            hashes=[],
        )

    config = _active_config(proxy)
    if config is None:
        return _unchanged("jev_inactive")

    try:
        _record(proxy, "active_attempted")

        # One bounded client per boundary turn, closed in `finally`. Track A's
        # JevClient opens its own httpx pool on first use (a 500ms retention
        # call must not share timeouts or keepalive economics with a 300s model
        # call), so leaving it open would leak one pool per compaction event.
        client = JevClient(config)
        try:
            decision = await decide_active_retention(
                config=config,
                client=client,
                messages=messages,
                frozen_prefix=frozen_prefix,
                model=model,
                session_id=session_id,
                branch_id=branch_id,
                message_shape=message_shape,
            )
        finally:
            # Best effort: a failing close must not mask the exception that
            # caused it, nor turn a good decision into a fail-open.
            with contextlib.suppress(Exception):
                await client.aclose()

        total = len(decision.candidates)
        if not total:
            _record(proxy, "active_no_candidates")
            return _unchanged("no_candidates", called=decision.called)
        if decision.error is not None:
            # Already scrubbed by the client, and every decision is `keep`.
            _record(proxy, "active_call_failed")
            logger.info(
                "jev active retention: call failed (%s); keeping all %d candidates",
                decision.error,
                total,
            )
            _record_accounting(
                proxy,
                calls_attempted=1,
                calls_failed=1,
                fallbacks=1,
                candidates=len(decision.candidates),
                **{classify_call_error(decision.error) or "calls_rejected": 1},
            )
            return _unchanged("call_failed", candidates=total, called=decision.called)
        if not decision.called:
            # Candidates were selected but the measured request budget admitted
            # none, so Jev was never asked. Nothing to retain.
            _record(proxy, "active_no_candidates")
            return _unchanged("not_asked", candidates=total)

        # The call itself is accounted for here, once, on the single path
        # where it is known to have been made and to have come back clean.
        # `keep`/`truncate`/`drop` are Jev's DECISIONS; what was carried out is
        # `applied`, recorded further down, and the gap between the two is a
        # partial-staging failure.
        tallies = {"keep": 0, "truncate": 0, "drop": 0}
        for cand in decision.candidates:
            decided = decision.decisions.get(cand.candidate_id, "keep")
            if decided in tallies:
                tallies[decided] += 1
        _record_accounting(
            proxy,
            calls_attempted=1,
            calls_completed=1,
            candidates=len(decision.candidates),
            candidates_sent=len(decision.candidates),
            candidate_tokens=sum(c.est_tokens for c in decision.candidates),
            keep=tallies["keep"],
            truncate=tallies["truncate"],
            drop=tallies["drop"],
        )

        removable = [
            cand
            for cand in decision.candidates
            if decision.decisions.get(cand.candidate_id, "keep") != "keep"
        ]
        if not removable:
            _record(proxy, "active_no_candidates")
            return _unchanged("all_keep", candidates=total, called=True)

        # Stage FIRST, for every removable candidate, and only then rewrite:
        # `apply_retention` is shown leases exclusively for candidates whose
        # original is already written, acknowledged and leased in the store.
        store = get_compression_store()
        leases: dict[str, RetentionLease] = {}
        for cand in removable:
            lease = stage_retention(
                store,
                candidate_id=cand.candidate_id,
                session_id=session_id,
                branch_id=branch_id,
                content=cand.content,
                # The wire shapes carry a tool-call id, not a tool NAME
                # (an Anthropic `tool_result` block and a Responses
                # `function_call_output` both name only the call). Inventing
                # one would put a fabricated name on the stored entry, which
                # the CCR statistics group by.
                tool_name=None,
                tool_call_id=cand.tool_call_id,
                original_tokens=cand.est_tokens,
            )
            if lease is not None:
                leases[cand.candidate_id] = lease

        # `ccr_staged` counts staging ATTEMPTS; a lease is Task 13's proof that
        # the write was read back and verified, so it is what `acknowledged`
        # means. The gap between the two is the signal that a CCR backend is
        # quietly losing writes, and it is recorded here — before the
        # short-circuit below — so a total staging failure is counted exactly
        # like a partial one.
        _record_accounting(
            proxy,
            ccr_staged=len(removable),
            ccr_acknowledged=len(leases),
            ccr_failed=max(0, len(removable) - len(leases)),
        )

        if not leases:
            _record(proxy, "active_no_lease")
            logger.warning(
                "jev active retention: no candidate could be staged (%d removable of %d); "
                "keeping every original",
                len(removable),
                total,
            )
            return _unchanged("no_lease", candidates=total, called=True)

        retained, applied_ids = apply_retention(
            messages, decision.candidates, decision.decisions, leases
        )
        if not applied_ids:
            # Leases were taken but no slot was rewritten (a shape that moved
            # since selection, or a truncate whose original is already shorter
            # than the inline width). The entries are harmless; they expire.
            _record(proxy, "active_no_lease")
            logger.info(
                "jev active retention: %d lease(s) applied to nothing; keeping every original",
                len(leases),
            )
            return _unchanged("not_applied", candidates=total, called=True)

        # count_messages_corrected takes both callables: a plain
        # count_messages prices a Responses `function_call_output` at ~0
        # because its payload lives in `output`, which silently zeroed savings
        # in Phase 0a. Measured on the RETAINED messages -- what the caller
        # forwards -- not on the input.
        tokenizer = get_tokenizer(model)
        tokens_after = count_messages_corrected(
            retained,
            count_messages=tokenizer.count_messages,
            count_text=tokenizer.count_text,
        )

        _record(proxy, "active_applied")
        # TF needs its own TH or it says nothing: measure the SAME message list
        # the same way, before retention was applied, and report the pair. TH
        # as Track A measured it on shadow turns is not a baseline for the
        # different turns active retention ran on, which is why
        # `tokens_active_baseline` exists as a field of its own.
        _record_accounting(
            proxy,
            applied=len(applied_ids),
            tokens_active_baseline=count_messages_corrected(
                messages,
                count_messages=tokenizer.count_messages,
                count_text=tokenizer.count_text,
            ),
            tokens_final=tokens_after,
        )
        logger.info(
            "jev active retention: applied %d of %d candidate(s) (%d staged); tokens_after=%d",
            len(applied_ids),
            total,
            len(leases),
            tokens_after,
        )
        return JevActiveResult(
            messages=retained,
            tokens_after=tokens_after,
            applied=len(applied_ids),
            candidates=total,
            called=True,
            reason="applied",
            # `applied_ids` is the authority: only a candidate whose slot was
            # actually rewritten gets its hash advertised.
            hashes=[leases[cid].hash_key for cid in applied_ids],
        )
    except Exception as exc:  # noqa: BLE001 - a retention bug must never fail a turn.
        # CancelledError is a BaseException and is deliberately not caught.
        #
        # Anything already staged is simply not used: the conversation the
        # caller forwards is its own untouched list, and the orphaned CCR
        # entries expire with their lease. Losing a saving is the cheap side.
        # No `exc_info=True`: the logging formatter appends the ORIGINAL
        # traceback, whose last line is the raw `str(exc)` (and every chained
        # `__cause__` message with it), which would defeat `_detail`'s
        # scrubbing for exactly the exceptions that carry the endpoint or the
        # key. The scrubbed traceback goes to DEBUG instead.
        logger.warning(
            "jev active retention failed open (%s); forwarding Headroom's "
            "compressed output unchanged",
            _detail(exc, config),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("jev active retention traceback: %s", _trace(exc, config))
        _record(proxy, "active_fail_open")
        return _unchanged("fail_open")
