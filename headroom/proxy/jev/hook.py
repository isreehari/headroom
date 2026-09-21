"""The single call the provider handlers make into Jev shadow mode.

The handlers get exactly one ``await`` and no error handling of their own. This
adapter owns the whole failure surface: an unknown model, a missing tokenizer
method, a runner bug, anything at all. Every one of those returns ``None`` and
records ``shadow_fail_open``, so a Track A regression shows up as a counter
rather than as a 500 on somebody's coding session.

Two failure shapes are deliberately kept apart:

* An **unusable context limit** (no limit source, an unrecognised model, a
  ``None`` from ``ProxyConfig.get_context_limit``) is a *skip*, not a failure:
  there is nothing to measure a threshold against. It is forwarded to the
  runner as ``context_limit=0``, which is the runner's own
  ``shadow_no_context_limit`` gate, so the skip stays visible under the metric
  name that already exists instead of being miscounted as a fail-open.
* A **raising** limit source or tokenizer is a real failure and fails open.

Double counting is avoided by scope: :meth:`JevShadowRunner.maybe_run` already
guards its own body and records ``shadow_fail_open`` itself, returning a
``fail_open`` result rather than raising. That result is passed straight
through; the ``except`` below only ever fires for work this adapter did
*around* the runner (resolving the tokenizer and the context limit) or for a
runner that raised in spite of its guard. So exactly one counter moves per
failure.

``asyncio.CancelledError`` is a ``BaseException`` and is deliberately not
caught: a cancelled turn is the caller's cancellation, not a Jev failure.

The call IS on the request path and adds at most ``HEADROOM_JEV_TIMEOUT_MS``
(default 500ms) of latency, and only on the turns that pass the threshold and
cooldown gates. Nothing it returns is applied to the forwarded request.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, cast

from headroom.proxy.jev.client import scrub_secrets
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.shadow import JevShadowResult

logger = logging.getLogger(__name__)

#: Matches ``shadow.py``: a scrubbed error still gets a length bound before it
#: reaches a log line.
_MAX_ERROR_CHARS = 400


def _record(proxy: Any, event: str) -> None:
    """Best-effort counter. A broken metrics backend is not a request failure.

    The lookup is inside the suppression too: this runs from an ``except``
    block, where raising again would defeat the whole point of failing open.
    """
    with contextlib.suppress(Exception):
        metrics = getattr(proxy, "metrics", None)
        if metrics is not None:
            metrics.record_jev_event(event)


def _config_for(proxy: Any, runner: Any) -> JevConfig:
    """The config to scrub error text against.

    Prefers the runner's own (it is the one holding the live key and endpoint),
    then a config parked on the proxy, then an empty default so scrubbing still
    happens -- against nothing -- rather than being skipped.
    """
    for owner, attr in ((runner, "config"), (proxy, "jev_config")):
        candidate: Any = None
        with contextlib.suppress(Exception):
            candidate = getattr(owner, attr, None)
        if isinstance(candidate, JevConfig):
            return candidate
    return JevConfig()


def _resolve_context_limit(context_limit_source: Any, model: str) -> int:
    """The model's context window, or ``0`` when there isn't a usable one.

    ``context_limit_source`` is whatever the handler already holds --
    ``proxy.anthropic_provider`` / ``proxy.openai_provider``, whose
    ``get_context_limit`` returns ``int``, or a ``ProxyConfig``, whose
    ``get_context_limit`` returns ``int | None``. A raising getter is left to
    propagate to the caller's fail-open guard; everything else that cannot be
    read as a positive whole number becomes ``0`` (the runner's skip gate).
    ``bool`` is excluded explicitly: it is an ``int`` subclass and ``True``
    would otherwise pass as a one-token context window.
    """
    getter = getattr(context_limit_source, "get_context_limit", None)
    if not callable(getter):
        return 0
    limit = getter(model)
    if isinstance(limit, bool) or not isinstance(limit, int):
        return 0
    return limit if limit > 0 else 0


async def run_jev_shadow_hook(
    proxy: Any,
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]] | None,
    frozen_prefix: int,
    optimized_tokens: int,
    original_tokens: int = 0,
    session_id: str,
    tokenizer: Any,
    message_shape: str,
    request_id: str,
    context_limit_source: Any,
) -> JevShadowResult | None:
    """Run one shadow attempt. Returns ``None`` when off or on any failure.

    ``messages`` is forwarded by identity -- Track A measures the very list the
    handler is about to send, and never mutates or copies it on the way in.

    The off path (no runner, or a runner whose mode is not ``shadow``) returns
    immediately and records nothing: it fires on every request of a feature
    that is off by default, so a counter there would be pure noise.
    """
    runner: Any = None
    try:
        # Inside the guard on purpose: ``jev_shadow`` and ``enabled`` are
        # attributes on objects this module does not own, and a descriptor
        # that raises must fail open like everything else rather than take
        # the request down before the guard starts.
        runner = getattr(proxy, "jev_shadow", None)
        if runner is None or not getattr(runner, "enabled", False):
            return None

        count_text = getattr(tokenizer, "count_text", None)
        count_messages = getattr(tokenizer, "count_messages", None)
        if not callable(count_text) or not callable(count_messages):
            raise TypeError(
                f"tokenizer {type(tokenizer).__name__} has no callable count_text/count_messages"
            )
        # ``proxy`` is untyped at the call site (the handlers hold a
        # ``HeadroomProxy`` this package must not import), so the runner's
        # declared return type is restated here rather than leaking ``Any``
        # into every handler.
        result = await runner.maybe_run(
            provider=provider,
            model=model,
            # ``messages or []`` keeps the caller's object when there is one and
            # hands the runner its own ``shadow_no_messages`` gate when there
            # is not, rather than silently dropping the turn here.
            messages=messages or [],
            frozen_prefix=max(0, int(frozen_prefix or 0)),
            optimized_tokens=max(0, int(optimized_tokens or 0)),
            original_tokens=max(0, int(original_tokens or 0)),
            context_limit=_resolve_context_limit(context_limit_source, model),
            session_id=session_id,
            count_text=count_text,
            count_messages=count_messages,
            message_shape=message_shape,
        )
        return cast("JevShadowResult | None", result)
    except Exception as exc:  # noqa: BLE001 - fail open: Track A must never be
        # the reason a proxied request fails. CancelledError is a
        # BaseException and is deliberately not caught.
        #
        # The exception is arbitrary (a caller-supplied tokenizer, a provider
        # object echoing a URL), so it goes through the same scrubber the
        # client and the runner use before it reaches a log line. Scrub first,
        # then truncate -- a key straddling the cut would otherwise survive as
        # a prefix. ``exc_info`` is withheld on purpose: a traceback would put
        # unscrubbed chained-exception text back into the log.
        detail = "<unprintable exception>"
        with contextlib.suppress(Exception):
            detail = scrub_secrets(f"{type(exc).__name__}: {exc}", _config_for(proxy, runner))[
                :_MAX_ERROR_CHARS
            ]
        logger.warning("[%s] jev shadow hook failed open: %s", request_id, detail)
        _record(proxy, "shadow_fail_open")
        return None
