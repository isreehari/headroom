"""One keep/drop question at the Codex compaction boundary (Track C).

The boundary carries exactly one candidate, so this asks exactly one question.
Track A's multi-candidate machinery (``select_candidates``,
``build_retention_state``, ``enforce_state_budget``) is deliberately NOT used:
there is no batch to select from, no recent-tail to exclude, and no request
budget to trim against. What is reused is the already-reviewed
``state``/``questions``/``answers`` contract and its fail-open-to-keep rule.

There is no ``truncate`` option here. Truncating a tool result at a compaction
boundary would mean re-summarizing content Codex is already summarizing; Track C
only ever chooses between forwarding the candidate untouched and replacing it
with a retrievable CCR marker.

Unlike :mod:`headroom.proxy.jev.compaction`, this module is not a near-leaf: it
is only reached once a boundary has already been detected, a candidate already
extracted and the recovery-tool gate already passed, so it may import Track A's
client and Track C's dataclasses. What it must not do is *depend on Track A's
types*: :class:`~headroom.proxy.jev.client.JevAnswer` is read only through
``getattr`` (``.error`` -- falsy means usable -- and ``.decisions``), so a Track
A field rename degrades to ``keep`` instead of raising inside the relay.

Three disciplines this module keeps:

* **Never raises, always answers.** :func:`decide_single_candidate` returns
  ``"keep"`` or ``"drop"`` and nothing else. ``keep`` is the safe direction
  because it changes nothing: the original tool output is relayed untouched.
  The one exception is :class:`asyncio.CancelledError`, which is not a
  degradation but the relay shutting this connection down, and must propagate.
* **The timeout bounds both client shapes.** An ``async def decide`` is awaited
  under :func:`asyncio.wait_for`; a blocking ``decide`` is dispatched to a
  worker thread and waited on under the same bound, so it can neither evade the
  timeout nor stall the WebSocket's event loop.
* **No credential ever reaches a log line or the payload.** The state and the
  questions are built only from candidate/boundary/session identity, never from
  :class:`~headroom.proxy.jev.config.JevConfig`. Any exception text that is
  logged goes through
  :func:`~headroom.proxy.jev.client.scrub_secrets` first, and ``exc_info`` is
  never used anywhere in this package: a traceback re-renders the *raw*
  ``str(exc)`` of the chained exceptions and would defeat the scrubbing.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from typing import Any

from headroom.proxy.jev.accounting import classify_call_error, record_jev_accounting
from headroom.proxy.jev.client import scrub_secrets
from headroom.proxy.jev.compaction import JevCompactionBoundary, JevCompactionCandidate
from headroom.proxy.jev.config import redact_endpoint

logger = logging.getLogger(__name__)

JEV_DECISION_KEEP = "keep"
JEV_DECISION_DROP = "drop"

#: Bound on the scrubbed exception detail that reaches a log line. A gateway
#: can echo an arbitrarily long body back in an error, and one failed retention
#: decision must not be able to flood the proxy's log.
_MAX_DETAIL_CHARS = 300

#: Default ceiling on the candidate body placed in the state. This bounds what
#: leaves the machine, so it is applied while the state is being built -- never
#: to a payload that has already been assembled.
DEFAULT_MAX_CONTENT_CHARS = 20000

DECISION_CRITERIA = {
    "keep": (
        "This tool result still carries information the assistant is likely to "
        "need verbatim after compaction; replacing it with a retrieval marker "
        "would cost a round trip the assistant cannot avoid."
    ),
    "drop": (
        "This tool result has been superseded, summarized, or is no longer "
        "referenced. Removing its body would not change what the assistant can "
        "answer -- and the original stays retrievable on demand."
    ),
}

QUESTION_INSTRUCTIONS = (
    "Decide what to do with the single historical tool result identified by "
    "this question's key ({cid}) in the `candidates` array of the state. Codex "
    "is compacting the conversation at this exact point, so this is the last "
    "time the result crosses the wire. It came from a `{item_type}` item and "
    "costs roughly {tokens} tokens (an estimate, not a measurement). Answering "
    "`drop` does not delete it: Headroom stores the original and leaves a "
    "retrieval marker the assistant can redeem with the `headroom_retrieve` "
    "tool. Answer `keep` if the body is still needed verbatim."
)


def build_single_candidate_state(
    candidate: JevCompactionCandidate,
    boundary: JevCompactionBoundary,
    *,
    session_id: str,
    model: str | None,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> dict[str, Any]:
    """Build the bounded retention state for exactly one candidate.

    ``max_content_chars`` exists to bound what leaves the machine, so the body
    is truncated *here*, on the way into the state, and the state is the only
    thing handed to the client. ``content_truncated_for_view`` says so honestly
    rather than letting Jev read a clipped body as the whole result.

    Two fields are easy to misread and are named for what they are:
    ``estimated_tokens`` is ``bytes // 4`` -- an estimate for the question text
    and the CCR bookkeeping, never a measured saving -- and ``content_sha256``
    hashes the module's canonical form of the body (see
    ``compaction._encode_candidate_text``), not the raw wire bytes.

    Nothing credential-bearing is reachable from here: every value comes from
    the candidate, the boundary or the caller's identity strings. The API key
    and endpoint live on :class:`JevConfig` and are only ever touched by the
    client's transport.
    """
    ceiling = max(0, max_content_chars)
    return {
        "boundary": "codex_native_compaction",
        "session_id": session_id,
        "branch_id": boundary.previous_response_id,
        "model": model,
        "boundary_item_count": boundary.item_count,
        "candidates": [
            {
                "id": candidate.candidate_id,
                "candidate_type": candidate.item_type,
                "tool_call_id": candidate.call_id,
                "estimated_tokens": candidate.estimated_tokens,
                "content_sha256": candidate.content_sha256,
                "content_truncated_for_view": len(candidate.output_text) > ceiling,
                "content": candidate.output_text[:ceiling],
            }
        ],
    }


def build_single_candidate_question(
    candidate: JevCompactionCandidate,
) -> dict[str, dict[str, Any]]:
    """Build the one question keyed by the one candidate id."""
    return {
        candidate.candidate_id: {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS.format(
                cid=candidate.candidate_id,
                item_type=candidate.item_type,
                tokens=candidate.estimated_tokens,
            ),
            "criteria": dict(DECISION_CRITERIA),
        }
    }


def _returns_awaitable(decide: Any) -> bool:
    """Whether ``decide`` can be awaited without first blocking the event loop.

    Track A's ``JevClient.decide`` is a coroutine function, but the interface
    also accepts a plain synchronous ``decide``, and the two have to be told
    apart BEFORE the call: a blocking implementation gives nothing back to
    inspect until it has already finished. ``__call__`` is checked too, so a
    callable object wrapping a coroutine function is still recognized.
    """
    if inspect.iscoroutinefunction(decide):
        return True
    try:
        call = getattr(decide, "__call__", None)  # noqa: B004 - not a callable test
    except Exception:
        return False
    return call is not None and inspect.iscoroutinefunction(call)


def _client_config(client: Any) -> Any | None:
    """The client's :class:`JevConfig`, if it exposes one, else ``None``.

    Only used to *scrub* -- never to read a credential. The parameter list of
    :func:`decide_single_candidate` is fixed by the Track C interface and
    carries no config, and the client is deliberately typed ``Any`` so a test
    double or a future client is accepted, so the config is discovered by duck
    typing. When none is found the exception's text is withheld entirely rather
    than logged unscrubbed.
    """
    for name in ("config", "_config"):
        try:
            found = getattr(client, name, None)
        except Exception:
            continue
        if found is None:
            continue
        if hasattr(found, "endpoint") and hasattr(found, "api_key"):
            return found
    return None


def _scrubbed_detail(client: Any, exc: BaseException) -> str:
    """Render an exception for a log line with nothing credential-bearing left.

    ``scrub_secrets`` is the single entry point for this on the branch, and it
    is used here as-is. One thing is taken further: it rewrites the endpoint to
    ``redact_endpoint``'s scheme+host+path form, which for a plain URL is the
    URL itself. Track C's constraint is stricter -- the endpoint must not reach
    a log line at all -- and this message needs no URL, so that form is removed
    too. With no config in reach there is nothing to scrub against, so only the
    exception's *type* is rendered; a message that cannot be proven clean is
    not worth the leak.
    """
    config = _client_config(client)
    if config is None:
        return type(exc).__name__
    try:
        detail = scrub_secrets(f"{type(exc).__name__}: {exc}", config)
        endpoint = getattr(config, "endpoint", "") or ""
        if endpoint:
            detail = detail.replace(redact_endpoint(endpoint), "<jev endpoint>")
        return detail[:_MAX_DETAIL_CHARS]
    except Exception:
        return type(exc).__name__


def _answer_error(answer: Any) -> str | None:
    """The answer's error text, or ``None`` when it reported none.

    ``getattr`` degrades a *missing* attribute but not one that raises, and
    this object came off the network, so the read is guarded. An unreadable
    answer is not a clean answer: it is reported as an error string so the
    call is accounted for as a failure rather than silently as a success.
    """
    try:
        error = getattr(answer, "error", None)
        return str(error) if error else None
    except Exception:  # noqa: BLE001 - an unreadable answer is a failed call
        return "unreadable answer"


def _record_call_outcome(metrics: Any, error: str | None) -> None:
    """Account for one finished call, split timeout vs rejection once."""
    if error is None:
        record_jev_accounting(metrics, calls_completed=1)
        return
    record_jev_accounting(
        metrics,
        calls_failed=1,
        fallbacks=1,
        **{classify_call_error(error) or "calls_rejected": 1},
    )


def _parse_decision(answer: Any, candidate_id: str) -> str:
    """Read one decision off a ``JevAnswer``. Anything ambiguous means keep.

    Every field is reached with ``getattr``/``isinstance`` and nothing is
    imported to type-check structurally, so a Track A rename, a partially
    populated answer or a hand-rolled client all degrade to ``keep``. A verdict
    for an id we did not ask about is not our verdict, and ``truncate`` -- a
    real Track A option -- is not on offer at a compaction boundary, so both
    land on ``keep`` by the same rule: only the exact string ``drop`` drops.

    This function may still RAISE, and its caller guards it for that reason.
    ``getattr`` degrades a *missing* attribute to the default, but it does not
    degrade one that raises -- a property, a ``__getattr__``, a mapping whose
    ``get`` is overridden, a ``str`` subclass with a hostile ``strip``: each of
    those propagates. The answer object is whatever came back from the network
    client, so it is exactly as adversarial as the call was.
    """
    if answer is None:
        return JEV_DECISION_KEEP
    if getattr(answer, "error", None):
        return JEV_DECISION_KEEP
    decisions = getattr(answer, "decisions", None)
    if not isinstance(decisions, dict):
        return JEV_DECISION_KEEP
    raw: Any = decisions.get(candidate_id)
    if isinstance(raw, dict):
        raw = raw.get("choice") or raw.get("decision")
    if not isinstance(raw, str):
        return JEV_DECISION_KEEP
    return JEV_DECISION_DROP if raw.strip().lower() == JEV_DECISION_DROP else JEV_DECISION_KEEP


async def decide_single_candidate(
    client: Any,
    candidate: JevCompactionCandidate,
    boundary: JevCompactionBoundary,
    *,
    session_id: str,
    model: str | None,
    timeout_seconds: float,
    metrics: Any = None,
) -> str:
    """Ask Track A's Jev client one question. Never raises; keeps on doubt.

    Returns exactly ``"keep"`` or ``"drop"``. Every step is guarded, in two
    separate guards. Building the state, reaching the client's ``decide``
    attribute and calling it sit under the timeout guard: a client that does
    not have the method, whose attribute raises, or whose signature does not
    match is an odd client, and an odd client means ``keep``. Reading the
    answer sits under a second, narrow guard, because ``getattr`` does not
    degrade an attribute that *raises* -- see :func:`_parse_decision`.

    :class:`asyncio.CancelledError` is re-raised explicitly. It is a
    ``BaseException``, so the ``except Exception`` below does not catch it, but
    the clause is written out because swallowing cancellation would leak a task
    past the relay's shutdown -- a branch-wide invariant, not a local choice.

    This is also the only place in Track C that can tell a timeout from a
    rejection -- the return value is deliberately a bare ``"keep"``/``"drop"``
    with the reason thrown away -- so the call outcome is accounted for here,
    through the same recorder and the same
    :func:`~headroom.proxy.jev.accounting.classify_call_error` Tracks A and B
    use. ``metrics`` is optional and every recording call is unconditionally
    swallowed, so accounting can never turn a decision into an exception.
    """
    record_jev_accounting(metrics, calls_attempted=1)
    try:
        state = build_single_candidate_state(
            candidate, boundary, session_id=session_id, model=model
        )
        questions = build_single_candidate_question(candidate)
        decide = client.decide
        call = functools.partial(
            decide,
            state=state,
            questions=questions,
            candidate_ids=[candidate.candidate_id],
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        if _returns_awaitable(decide):
            # Async client: the call itself is cheap, the await is what waits.
            result = await asyncio.wait_for(call(), timeout=timeout_seconds)
        else:
            # Synchronous client: `decide` blocks until it is finished, so
            # calling it inline would block this WebSocket's event loop for its
            # full duration and `timeout_seconds` could never fire. Run it on
            # the default thread pool via `asyncio.to_thread` and bound the WAIT
            # instead. Deliberately NOT the proxy's compression executor: a Jev
            # step must never add a new way to arm the compression quarantine.
            # The thread may outlive the timeout -- a blocking call cannot be
            # cancelled -- but the loop is free again the moment the bound
            # expires, the late result is discarded, and this path is already
            # committed to "keep" by then.
            result = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout_seconds)
        # A sync `decide` is still allowed to hand back an awaitable (a future
        # or a coroutine from a wrapper): finish it inside what is LEFT of the
        # same bound, never a second full timeout.
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=max(0.0, deadline - loop.time()))
    except (asyncio.TimeoutError, TimeoutError):
        record_jev_accounting(metrics, calls_timed_out=1, calls_failed=1, fallbacks=1)
        logger.info(
            "jev compaction: decision timed out after %.2fs; keeping candidate",
            timeout_seconds,
        )
        return JEV_DECISION_KEEP
    except asyncio.CancelledError:
        # Not a degradation: the relay is tearing this connection down, so it
        # is deliberately NOT accounted for as a failed call.
        raise
    except Exception as exc:
        record_jev_accounting(metrics, calls_rejected=1, calls_failed=1, fallbacks=1)
        logger.warning(
            "jev compaction: decision call failed (%s); keeping candidate",
            _scrubbed_detail(client, exc),
        )
        return JEV_DECISION_KEEP

    # Reading the answer gets its OWN guard rather than being folded into the
    # one above. Two reasons: the parse is local work that `timeout_seconds` has
    # no business bounding, and a `TimeoutError` raised by a hostile answer
    # object must not be able to masquerade as the call having timed out. It
    # still fails open to `keep`, and its detail is scrubbed on the same path as
    # every other failure here -- an unscrubbed `str(exc)` escaping to the relay
    # would be a leak from precisely the object the network handed back.
    #
    # The call is accounted for BEFORE the parse: an answer carrying an error
    # is a failed call whatever the parse then makes of it, and a parse that
    # blows up must not be able to count the call twice.
    _record_call_outcome(metrics, _answer_error(result))
    try:
        return _parse_decision(result, candidate.candidate_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "jev compaction: decision answer could not be read (%s); keeping candidate",
            _scrubbed_detail(client, exc),
        )
        return JEV_DECISION_KEEP
