"""Track C orchestration: one keep/drop decision at the Codex compaction boundary.

Entry point for the Codex ``/v1/responses`` WebSocket relay. It never raises and
never changes the forwarded bytes unless a full, acknowledged CCR commit
succeeded for the single candidate the boundary carries. Missing identity,
missing recovery tool, a stale (already decided) revision, no Jev client, an
ambiguous answer, or any CCR failure all forward the original frame string
unchanged -- the same fail-open rules tracks A and B apply.

Four properties of this module are contracts rather than implementation
details, and each is the reason a piece of the code below looks the way it does.

**The forwarded frame is the ORIGINAL STRING, not a re-serialization.** Every
declining path returns ``raw_msg`` itself. Parsing and re-``json.dumps``-ing a
frame that is being forwarded unchanged would reorder nothing semantically and
yet change the bytes the provider sees -- key order, separator whitespace,
non-ASCII escaping -- which is a defect even when it "looks the same". Only the
``jev_compaction_dropped`` path serializes, because only that path changed
something. The parsed object graph this module mutates comes from
``json.loads(raw_msg)`` and is private to the call, so the in-place rewrite
``replace_candidate_output`` performs can never reach ``raw_msg``.

**The reason vocabulary is closed.** :data:`JEV_COMPACTION_REASONS` is the whole
of it. The relay branches on these strings, ``/stats`` reports them and the
operator docs name them, so a new outcome joins an existing bucket or the
vocabulary changes deliberately -- it never grows a silent fourteenth member.
The shape-drift declines are the case that pushed on this: ``unwrap_response_create``
has four distinct ways to say "not a create frame" and they all land on
``jev_compaction_not_response_create``, so the specific cause is carried on a
debug log line (see :func:`_decline_cause`) instead of on the reason.

**Nothing credential-bearing reaches a log line.** This module logs on every
gate, so it is the module with the most chances to leak. Exception text is
rendered only through :func:`_scrubbed_detail`, which runs
:func:`~headroom.proxy.jev.client.scrub_secrets` against the ``JevConfig`` the
caller already handed us and withholds the message entirely when there is no
config to scrub against. ``exc_info`` is never used anywhere in this package: a
rendered traceback re-exposes the raw ``str(exc)`` of the chained exceptions and
defeats the scrubbing.

**Cancellation is not a degradation.** :class:`asyncio.CancelledError` is the
relay tearing this connection down and is re-raised explicitly. It is a
``BaseException``, so ``except Exception`` already lets it past; the clause is
written out because swallowing it would leak a task past shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from headroom.cache.compression_store import get_compression_store
from headroom.proxy.jev.client import scrub_secrets
from headroom.proxy.jev.compaction import (
    detect_compaction_boundary,
    extract_compaction_candidate,
    has_recovery_tool,
    replace_candidate_output,
    unwrap_response_create,
)
from headroom.proxy.jev.compaction_decision import (
    JEV_DECISION_DROP,
    decide_single_candidate,
)
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore
from headroom.proxy.jev.config import redact_endpoint
from headroom.proxy.jev.retention_ccr import stage_retention

logger = logging.getLogger(__name__)

DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS = 5.0

#: Bound on the scrubbed exception detail that reaches a log line. A gateway can
#: echo an arbitrarily long body back in an error, and one failed boundary must
#: not be able to flood the proxy's log. Matches ``compaction_decision``'s bound.
_MAX_DETAIL_CHARS = 300

REASON_DISABLED = "jev_compaction_disabled"
REASON_NOT_JSON = "jev_compaction_not_json"
REASON_NOT_RESPONSE_CREATE = "jev_compaction_not_response_create"
REASON_NO_BOUNDARY = "jev_compaction_no_boundary"
REASON_MISSING_IDENTITY = "jev_compaction_missing_identity"
REASON_STALE_REVISION = "jev_compaction_stale_revision"
REASON_MISSING_RECOVERY_TOOL = "jev_compaction_missing_recovery_tool"
REASON_NO_CANDIDATE = "jev_compaction_no_candidate"
REASON_NO_CLIENT = "jev_compaction_no_client"
REASON_KEEP = "jev_compaction_keep"
REASON_CCR_FAILED = "jev_compaction_ccr_failed"
REASON_DROPPED = "jev_compaction_dropped"
REASON_ERROR = "jev_compaction_error"

#: The complete, closed set of reasons :func:`apply_jev_compaction_boundary`
#: can return. Exported so the relay, ``/stats`` and the tests can all assert
#: against one definition rather than three copies of a string list.
JEV_COMPACTION_REASONS = frozenset(
    {
        REASON_DISABLED,
        REASON_NOT_JSON,
        REASON_NOT_RESPONSE_CREATE,
        REASON_NO_BOUNDARY,
        REASON_MISSING_IDENTITY,
        REASON_STALE_REVISION,
        REASON_MISSING_RECOVERY_TOOL,
        REASON_NO_CANDIDATE,
        REASON_NO_CLIENT,
        REASON_KEEP,
        REASON_CCR_FAILED,
        REASON_DROPPED,
        REASON_ERROR,
    }
)


def resolve_jev_client(proxy: Any) -> Any | None:
    """Find Track A's Jev client on the proxy, or None (which means keep).

    Track A wires ``HeadroomProxy.jev_shadow: JevShadowRunner`` (Task 8) and
    that runner constructs ``JevClient(config.jev)`` unconditionally in its
    ``__init__``, keeps it on ``_client``, and closes it from
    ``JevShadowRunner.aclose()`` during proxy shutdown. Track C reuses that one
    bounded client rather than opening a second httpx pool nobody would ever
    close.

    ``proxy.jev_client`` is probed first as an explicit override. Both lookups
    are ``getattr`` with a default inside a guard, so a Track A rename -- or a
    proxy attribute that raises -- degrades to "no decision, keep the original"
    instead of raising on a live WS frame.
    """
    try:
        direct = getattr(proxy, "jev_client", None)
        if direct is not None and hasattr(direct, "decide"):
            return direct
        shadow_client = getattr(getattr(proxy, "jev_shadow", None), "_client", None)
        if shadow_client is not None and hasattr(shadow_client, "decide"):
            return shadow_client
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("jev compaction: could not resolve a Jev client; keeping originals")
    return None


def _record_jev_event(metrics: Any, event: str) -> None:
    """Increment Track A's ``headroom_jev_events_total{event}`` counter if present.

    Fully guarded, including the attribute lookup: this is called from inside
    the orchestrator's own ``except`` handler, where an escaping exception would
    break the never-raises contract at the exact moment it matters most. An
    absent recorder is the normal case and is not an error.
    """
    try:
        recorder = getattr(metrics, "record_jev_event", None)
        if recorder is None:
            return
        recorder(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("jev compaction: metric %s not recorded", event)


def _scrubbed_detail(jev_config: Any, exc: BaseException) -> str:
    """Render an exception for a log line with nothing credential-bearing left.

    ``scrub_secrets`` is the single entry point for this on the branch and is
    used here as-is, against the ``JevConfig`` the caller already passed us --
    no duck-typed hunt for a config on the client is needed at this layer. One
    thing is taken further: ``scrub_secrets`` rewrites the endpoint to
    ``redact_endpoint``'s scheme+host+path form, which for a plain URL is the
    URL itself, and Track C's constraint is stricter -- the endpoint must not
    reach a log line at all -- so that form is removed too.

    With no config in reach there is nothing to scrub against, so only the
    exception's *type* is rendered. A message that cannot be proven clean is not
    worth the leak, and the type alone still tells an operator what broke.
    """
    fallback = type(exc).__name__
    try:
        endpoint = getattr(jev_config, "endpoint", None)
        api_key = getattr(jev_config, "api_key", None)
        if not isinstance(endpoint, str) and not isinstance(api_key, str):
            return fallback
        detail = scrub_secrets(f"{fallback}: {exc}", jev_config)
        if isinstance(endpoint, str) and endpoint:
            detail = detail.replace(redact_endpoint(endpoint), "<jev endpoint>")
        return detail[:_MAX_DETAIL_CHARS]
    except asyncio.CancelledError:
        raise
    except Exception:
        return fallback


def _decline_cause(frame: Any) -> str:
    """Name *why* ``unwrap_response_create`` declined this frame, for the log.

    Task 20's stricter unwrap made shape drift a SILENT non-detection: a Codex
    release that renames the envelope, or a relay that hands this function a
    partly decoded frame, simply stops producing boundaries and nothing says so.
    The reason vocabulary cannot grow a member per cause without breaking the
    closed contract the relay, ``/stats`` and the docs share, so the cause rides
    on a debug log line instead and an operator can tell the four apart.

    Mirrors ``unwrap_response_create``'s branches exactly; it is read-only and
    reached only on the declining path, so it costs nothing on a live boundary.
    """
    if not isinstance(frame, dict):
        return "frame_is_not_a_json_object"
    if "type" not in frame:
        return "bare_payload_without_input_list"
    if frame.get("type") != "response.create":
        return "frame_type_is_not_response_create"
    if "response" in frame:
        return "response_envelope_is_not_an_object"
    return "flattened_create_without_input_list"


def _positive_int(value: Any) -> int:
    """Coerce a config field to a positive int, or 0 when it is not usable."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _timeout_seconds(jev_config: Any) -> float:
    """Resolve the decision bound, falling back to the module default."""
    try:
        timeout_ms = float(getattr(jev_config, "timeout_ms", 0) or 0)
    except (TypeError, ValueError):
        return DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS
    if timeout_ms <= 0:
        return DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS
    return timeout_ms / 1000.0


async def apply_jev_compaction_boundary(
    raw_msg: str,
    *,
    jev_config: Any,
    client: Any | None,
    session_id: str,
    request_id: str,
    revisions: JevCompactionRevisionStore,
    metrics: Any = None,
    store: Any | None = None,
) -> tuple[str, str]:
    """Return ``(frame to forward, reason)``; the frame is ``raw_msg`` unless dropped."""
    try:
        mode = str(getattr(jev_config, "mode", "off") or "off").strip().lower()
        if mode != "active":
            # The default-off path, and the first thing checked: an unconfigured
            # proxy must not pay for Jev's existence, not even a JSON parse.
            return raw_msg, REASON_DISABLED

        try:
            frame = json.loads(raw_msg)
        except (ValueError, TypeError):
            # Includes json.JSONDecodeError. Not every WS text frame on this
            # route is JSON, so this is an ordinary outcome, not a failure.
            return raw_msg, REASON_NOT_JSON

        inner, wrapped = unwrap_response_create(frame)
        if inner is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[%s] jev compaction: frame declined (%s); forwarding unchanged",
                    request_id,
                    _decline_cause(frame),
                )
            return raw_msg, REASON_NOT_RESPONSE_CREATE

        boundary = detect_compaction_boundary(inner)
        if boundary is None:
            # A create frame that simply is not a compaction event: by far the
            # most common outcome on an active connection, so debug only.
            logger.debug("[%s] jev compaction: no boundary in this frame", request_id)
            return raw_msg, REASON_NO_BOUNDARY

        if not session_id:
            # Checked BEFORE the revision is touched: without a session id there
            # is no identity to bind a CCR entry to, and burning the boundary's
            # one claim on a caller-side omission would make it permanent.
            _record_jev_event(metrics, "compaction_missing_identity")
            logger.info(
                "[%s] jev compaction: boundary has no session identity; keeping original",
                request_id,
            )
            return raw_msg, REASON_MISSING_IDENTITY

        _record_jev_event(metrics, "compaction_boundary_detected")

        # A pure read, taken as soon as the boundary is recognised so a replay
        # this process already decided costs nothing beyond the parse. It is NOT
        # the decision -- `claim` below is -- it is only an early exit.
        if revisions.seen(boundary.previous_response_id):
            _record_jev_event(metrics, "compaction_stale_revision")
            logger.info(
                "[%s] jev compaction: revision %s already decided; keeping original",
                request_id,
                boundary.previous_response_id,
            )
            return raw_msg, REASON_STALE_REVISION

        # The gate the rest of the track depends on, and deliberately the FIRST
        # gate after detection: retention replaces a tool output with a
        # retrieval marker, and a marker the model has no tool to redeem is
        # permanent data loss, not compression. It runs before the candidate is
        # extracted, before the decision is asked and before anything is written
        # to CCR, so a frame without the tool costs one read-only scan.
        if not has_recovery_tool(inner):
            _record_jev_event(metrics, "compaction_missing_recovery_tool")
            logger.info(
                "[%s] jev compaction: frame does not advertise the recovery tool; keeping original",
                request_id,
            )
            return raw_msg, REASON_MISSING_RECOVERY_TOOL

        # `max_candidate_tokens` is a token ceiling; the extractor takes a UTF-8
        # byte ceiling, and `estimated_tokens` is `bytes // 4`, so the same
        # factor converts between them. 0 disables the ceiling.
        max_candidate_tokens = _positive_int(getattr(jev_config, "max_candidate_tokens", 0))
        candidate = extract_compaction_candidate(
            inner,
            boundary,
            max_candidate_bytes=max_candidate_tokens * 4,
        )
        if candidate is None:
            _record_jev_event(metrics, "compaction_no_candidate")
            logger.info(
                "[%s] jev compaction: no usable candidate at the boundary "
                "(oversized, unreadable body, or a shape this track does not drop); "
                "keeping original",
                request_id,
            )
            return raw_msg, REASON_NO_CANDIDATE

        if client is None:
            _record_jev_event(metrics, "compaction_no_client")
            logger.info(
                "[%s] jev compaction: no Jev client attached; keeping original",
                request_id,
            )
            return raw_msg, REASON_NO_CLIENT

        # Claim the revision HERE: after every static gate, immediately before
        # the first irreversible step. The gates above are properties of this
        # frame and this process's current state (is the recovery tool
        # advertised, does the candidate fit the ceiling, is a client attached),
        # and every one of them can differ on a legitimate retry of the same
        # boundary -- burning the claim on them would turn a transient miss into
        # a permanent one. From this point on the claim IS spent whatever
        # happens: a timeout, an ambiguous answer or a failed CCR commit all
        # leave the original content on the wire, so a retry that skips
        # straight to "keep" loses an optimisation, never content. Retrying a
        # boundary whose CCR commit already succeeded is the case that must not
        # happen, and it is on this side of the claim.
        #
        # The return value is the AUTHORITATIVE gate, not a formality. `seen`
        # above is a pure read taken earlier in wall-clock time; treating it as
        # the decision and firing `claim` only for its side effect would be a
        # test-and-set split into a test and a set, which is exactly the race
        # Task 24 took a lock to close. `claim` is also the place an unusable
        # revision (over-length, blank, not a string) is refused, and refusing
        # is always the safe direction: it costs an optimisation, never content.
        if not revisions.claim(boundary.previous_response_id):
            _record_jev_event(metrics, "compaction_stale_revision")
            logger.info(
                "[%s] jev compaction: revision %s could not be claimed; keeping original",
                request_id,
                boundary.previous_response_id,
            )
            return raw_msg, REASON_STALE_REVISION

        # `decide_single_candidate` bounds both async and blocking clients
        # itself and returns exactly "keep" or "drop"; it never raises except
        # for CancelledError. No second timeout is layered on top, because two
        # bounds that can disagree is worse than one.
        decision = await decide_single_candidate(
            client,
            candidate,
            boundary,
            session_id=session_id,
            model=getattr(jev_config, "model", None),
            timeout_seconds=_timeout_seconds(jev_config),
        )
        if decision != JEV_DECISION_DROP:
            _record_jev_event(metrics, "compaction_keep")
            logger.debug(
                "[%s] jev compaction: keep for candidate %s", request_id, candidate.candidate_id
            )
            return raw_msg, REASON_KEEP

        # The shared CCR sequence (Task 13): write the original, require an
        # ACKNOWLEDGED byte-equal read-back, bind it to (session, branch,
        # content) and take the retention lease -- then, and only then, rewrite
        # the frame. The branch here is the boundary's `previous_response_id`,
        # the anchor Codex hangs this compaction off, so two sessions that
        # produce byte-identical tool output still get distinct entries and
        # distinct leases. Every failure inside it returns None.
        lease = stage_retention(
            store if store is not None else get_compression_store(),
            candidate_id=candidate.candidate_id,
            session_id=session_id,
            branch_id=boundary.previous_response_id,
            content=candidate.output_text,
            tool_name=candidate.item_type,
            tool_call_id=candidate.call_id,
            original_tokens=candidate.estimated_tokens,
        )
        # Short-circuit order matters: with no lease the rewrite is never
        # attempted, so nothing is mutated on the failing path. And
        # `replace_candidate_output` itself mutates nothing unless the item
        # still matches on index, type, call_id, body field AND content hash --
        # so a False here also leaves the parsed frame untouched.
        if lease is None or not replace_candidate_output(inner, candidate, lease.marker):
            _record_jev_event(metrics, "compaction_ccr_failed")
            logger.warning(
                "[%s] jev compaction: retention not committed for candidate %s; keeping original",
                request_id,
                candidate.candidate_id,
            )
            return raw_msg, REASON_CCR_FAILED

        # `inner` was handed back BY REFERENCE and the rewrite above already
        # landed in it. Re-wrap in the shape the client sent so the envelope it
        # chose is the envelope the provider receives.
        if wrapped:
            frame["response"] = inner
        else:
            frame = inner
        rewritten = json.dumps(frame)
        _record_jev_event(metrics, "compaction_dropped")
        logger.info(
            "[%s] jev compaction boundary: dropped 1 candidate session_id=%s "
            "revision=%s candidate=%s hash=%s est_tokens~%d",
            request_id,
            session_id,
            boundary.previous_response_id,
            candidate.candidate_id,
            lease.hash_key,
            candidate.estimated_tokens,
        )
        return rewritten, REASON_DROPPED
    except asyncio.CancelledError:
        # Not a degradation: the relay is tearing this connection down.
        raise
    except Exception as exc:
        _record_jev_event(metrics, "compaction_fail_open")
        logger.warning(
            "[%s] jev compaction boundary failed open (%s); forwarding original",
            request_id,
            _scrubbed_detail(jev_config, exc),
        )
        return raw_msg, REASON_ERROR
