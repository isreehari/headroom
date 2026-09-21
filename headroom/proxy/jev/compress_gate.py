"""Request-level gate for Track B active retention on ``POST /v1/compress``.

The active path is opened by ONE request-level flag, never by configuration
alone: ``config.jev_compaction_boundary``. A boundary turn is allowed to rewrite
history the caller has already forwarded — that is what a compaction event is —
so it must not be inferable from ordinary traffic. The caller says so on the
exact turn it means it, and on no other turn.

The proven request shape is::

    {"config": {"mode": "ccr",
                "session_id": "caller-owned-session-id",
                "jev_compaction_boundary": true}}

This module imports nothing from the rest of the ``jev`` package on purpose: the
handler must be able to reject a malformed boundary request with a 400 even when
``HEADROOM_JEV_MODE=off``, which is the default. Keep it stdlib-only and keep it
free of ``headroom`` imports.

Every :class:`JevGateError` message is echoed verbatim into the 400 body, so a
message may contain only static prose and values the caller itself sent — never
server configuration, and never an unbounded echo of the caller's own payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Branch id recorded for every ``/v1/compress`` turn. The sidecar route has no
#: branch concept of its own, and Track A's identity store keys on
#: ``(session_id, branch_id)`` — a constant keeps compress turns in their own
#: lane instead of colliding with proxy-path branches for the same session id.
JEV_COMPRESS_BRANCH_ID = "compress"

#: Cap on how much of a rejected value is quoted back. The flag's slot holds a
#: JSON bool, but a caller can put a megabyte of anything there; the 400 body
#: should diagnose, not mirror.
_MAX_ECHOED_VALUE_CHARS = 60


def _echo(value: object) -> str:
    """Bounded ``repr`` of a caller-supplied value, safe for a 400 body."""
    text = repr(value)
    if len(text) > _MAX_ECHOED_VALUE_CHARS:
        return text[:_MAX_ECHOED_VALUE_CHARS] + "... (truncated)"
    return text


class JevGateError(ValueError):
    """A malformed ``jev_compaction_boundary`` request. Maps to HTTP 400."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_compaction_boundary(compress_config: dict[str, Any], mode: str | None) -> bool:
    """Return whether this ``/v1/compress`` turn is a declared Jev compaction boundary.

    Args:
        compress_config: The request body's ``config`` object (already
            normalised to a dict by the handler).
        mode: The validated ``config.mode`` value (``None`` for the default
            marker-free pipeline). This argument is authoritative; the ``mode``
            entry inside ``compress_config`` is deliberately not re-read, so a
            value the handler rejected cannot re-enter through the raw dict.

    Returns:
        True when active retention may run on this turn, False when the caller
        did not declare a boundary.

    Raises:
        JevGateError: the flag is present but the request cannot support
            retention. Every message names the field and says why.
    """
    if not isinstance(compress_config, Mapping):
        # The handler coerces a non-dict ``config`` to ``{}`` before calling
        # here, so this is belt-and-braces — but it fails closed the same way
        # that coercion does, rather than raising AttributeError out of a
        # module whose whole job is turning bad input into a clean 400.
        return False
    raw = compress_config.get("jev_compaction_boundary", False)
    if raw is False or raw is None:
        return False
    if raw is not True:
        # `is not True` rather than a truthiness check: JSON `1`, `"true"` and
        # `[]` all arrive here, and silently reading them as consent to delete
        # tool output is exactly the failure this gate exists to prevent.
        raise JevGateError(
            f"Invalid config.jev_compaction_boundary: {_echo(raw)}. Expected true or false."
        )
    if mode != "ccr":
        raise JevGateError(
            'config.jev_compaction_boundary=true requires config.mode="ccr". '
            "Active retention replaces a tool result with a CCR retrieval marker, "
            "and the other modes emit no markers and write nothing to the CCR "
            "store, so the original would be unrecoverable."
        )
    session_id = compress_config.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise JevGateError(
            "config.jev_compaction_boundary=true requires a non-empty "
            "config.session_id. Every retained original is bound to "
            "(session_id, branch_id, candidate hash), so a boundary turn with no "
            "session id has nothing to bind its retention lease to."
        )
    return True
