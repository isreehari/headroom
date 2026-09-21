"""The single accounting recorder every Jev track reports through.

Tracks A (shadow), B (``/v1/compress``) and C (the Codex WS compaction
boundary) all need to say "a call was attempted", "a call failed" and "this is
what it cost". They say it here, once, so "a failed call" cannot come to mean
three slightly different things on one dashboard.

Two rules hold for everything in this module:

* **Recording never fails the thing it measures.** These functions run on the
  request path and on a live WebSocket relay. A metrics bug must cost a number
  on a dashboard, never a turn, so :func:`record_jev_accounting` swallows
  everything — including a raising attribute lookup on an exotic ``metrics``
  object.
* **``asyncio.CancelledError`` propagates.** It is a ``BaseException``, so the
  ``except Exception`` below already lets it past; nothing here uses a bare
  ``except`` or catches ``BaseException``.

Nothing here touches :class:`~headroom.proxy.jev.config.JevConfig`, so no
credential is reachable from this module at all: the only strings it sees are
error texts the client has already run through ``scrub_secrets``, and it
neither logs nor stores them — :func:`classify_call_error` reduces one to a
single field name and discards it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Error-string fragments Track A's client produces for a call that ran out of
#: time rather than one the service refused. ``JevClient.decide`` formats every
#: failure as ``f"{type(exc).__name__}: {exc}"``, so httpx's ``ReadTimeout`` /
#: ``ConnectTimeout`` / ``PoolTimeout`` and asyncio's ``TimeoutError`` all land
#: here. Underscores are stripped before matching so ``timed_out`` and
#: ``TimedOut`` are the same fragment.
_TIMEOUT_MARKERS = ("timeout", "timedout")


def classify_call_error(error: str | None) -> str | None:
    """Return the accounting field one call error belongs in, or ``None``.

    A timeout and a rejection are operationally different problems -- one says
    the bound is too tight or the service is slow, the other says the request
    or the credentials were refused -- and the design doc asks for both. They
    are told apart here, once, from the error string the client already built.

    ``None`` means "there was no error", not "unclassifiable": every caller
    treats a non-empty error it cannot place as a rejection.
    """
    if not error:
        return None
    try:
        lowered = str(error).lower().replace("_", "")
    except Exception:  # noqa: BLE001 - a hostile __str__ is still a failed call
        return "calls_rejected"
    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return "calls_timed_out"
    return "calls_rejected"


def record_jev_accounting(metrics: Any, **fields: int) -> None:
    """Add totals to ``metrics`` if it can take them. Never raises.

    ``metrics`` may be ``None``, or an object from a Headroom build that
    predates ``record_jev_accounting``; either way a missing counter costs a
    number on a dashboard, never a turn. The attribute lookup is inside the
    guard too, because a property on a caller-supplied object can raise and
    this helper is called from ``except`` handlers where an escaping exception
    would break the never-raises contract at the exact moment it matters most.
    """
    try:
        recorder = getattr(metrics, "record_jev_accounting", None)
        if recorder is None:
            return
        recorder(**fields)
    except Exception:  # noqa: BLE001 - accounting never fails a turn
        logger.debug("jev: accounting not recorded")
