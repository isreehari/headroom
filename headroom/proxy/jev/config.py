"""Typed configuration for the Jev retention-decision integration.

Default off: an unconfigured proxy behaves exactly as it does today, so this
module reads nothing but ``HEADROOM_JEV_MODE`` until the feature is switched
on. That keeps a typo in an unused ``HEADROOM_JEV_*`` var from stopping the
proxy booting for an off-by-default subsystem — the same hazard
``_qdrant_env_port_or_default`` in ``headroom/proxy/models.py`` exists to avoid.

Once the mode IS shadow/active, validation is strict and fails startup rather
than failing open: a shadow run that silently no-ops because
``HEADROOM_JEV_API_KEY`` was never exported would report "no savings" for a
reason that has nothing to do with Jev.

Two fields on this object are credential-bearing, and neither is ever rendered
raw. The API key never leaves it at all: :meth:`JevConfig.redacted` is the only
serialization path and reports presence, never the value. The endpoint may
carry credentials in its userinfo or a token in its query string, so both
:meth:`JevConfig.redacted` and :meth:`JevConfig.__repr__` put it through
:func:`redact_endpoint` first -- the repr because it is what a stray
``logger.debug("%r", config)`` and every traceback frame holding a
``ProxyConfig`` will print.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field

JEV_MODES: tuple[str, ...] = ("off", "shadow", "active")

DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_TIMEOUT_MS = 500
DEFAULT_JEV_THRESHOLD_PERCENT = 80
DEFAULT_JEV_COOLDOWN_TURNS = 5
DEFAULT_JEV_MAX_CANDIDATE_TOKENS = 20000
DEFAULT_JEV_MAX_CANDIDATES = 12
DEFAULT_JEV_MAX_STATE_TOKENS = 8000


def redact_endpoint(url: str) -> str:
    """Scheme + host + path only.

    The endpoint is logged and can appear verbatim inside httpx error strings.
    A custom ``HEADROOM_JEV_ENDPOINT`` may carry credentials in its userinfo or
    a token in its query string, so neither is ever shown. Lifted from the
    already-reviewed ``benchmarks/jev_savings_spike.py:redact_url``.
    """
    if not url:
        return "<unset>"
    try:
        parts = urllib.parse.urlsplit(url)
        # ``hostname``/``port``/``username`` parse lazily, so a malformed
        # authority (e.g. a non-numeric port) raises here, not at urlsplit.
        # This is a redaction helper on logging and error paths: it must never
        # be the thing that raises.
        host = parts.hostname or ""
        port = parts.port
        has_userinfo = bool(parts.username or parts.password)
    except ValueError:
        return "<unparseable endpoint>"
    if port:
        host = f"{host}:{port}"
    if has_userinfo:
        host = f"<redacted>@{host}"
    shown = urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    if parts.query:
        shown += "?<redacted>"
    return shown or "<redacted endpoint>"


def _env_int(src: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}; got {value}")
    return value


@dataclass(frozen=True, repr=False)
class JevConfig:
    """Resolved Jev settings. Constructed once at the configuration boundary."""

    mode: str = "off"
    # ``repr=False`` is belt-and-braces under the hand-written ``__repr__``
    # below (which never reads this field at all). It is kept so that deleting
    # that method degrades to the key being omitted rather than printed.
    # Equality still compares it; only display drops it.
    api_key: str = field(default="", repr=False)
    endpoint: str = DEFAULT_JEV_ENDPOINT
    model: str = DEFAULT_JEV_MODEL
    timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS
    threshold_percent: int = DEFAULT_JEV_THRESHOLD_PERCENT
    cooldown_turns: int = DEFAULT_JEV_COOLDOWN_TURNS
    max_candidate_tokens: int = DEFAULT_JEV_MAX_CANDIDATE_TOKENS
    max_candidates: int = DEFAULT_JEV_MAX_CANDIDATES
    max_state_tokens: int = DEFAULT_JEV_MAX_STATE_TOKENS

    def __repr__(self) -> str:
        """Display form. Renders neither the API key nor a raw endpoint.

        ``repr=False`` on ``api_key`` alone stopped one field short. The
        endpoint is the other credential-bearing field on this object -- a
        custom ``HEADROOM_JEV_ENDPOINT`` may carry userinfo or a token in its
        query string, which is precisely why :func:`redact_endpoint` exists --
        and the generated repr printed it verbatim. That repr is what a stray
        ``logger.debug("%r", config)`` emits, and what every traceback frame
        holding a ``ProxyConfig`` renders, so it is the single most likely way
        for the endpoint to reach a log line.

        The host is deliberately kept: this is redaction so the object stays
        debuggable, not suppression. Only display changes -- equality and
        hashing are still the dataclass's own, over every field including the
        key.

        A ``__repr__`` must never raise: it runs inside exception rendering,
        where an exception of its own would replace the diagnostic it was
        called to produce. ``JevConfig`` is built in-process by callers and by
        tests rather than parsed from a wire payload, so a field of the wrong
        type is reachable without anything adversarial, and three guards keep
        the invariant honest:

        * The endpoint is handed to :func:`redact_endpoint` **only** when it is
          an actual ``str``. ``urlsplit`` accepts ``bytes`` and the failure
          mode then depends on CPython's internals -- today ``urlunsplit``
          raises ``TypeError`` mixing ``str`` and ``bytes``, which the guard
          would catch, but that is an accident of the current implementation,
          not a promise. A non-``str`` is refused outright rather than passed
          through, so no version of urllib can ever make the raw value the
          thing that gets printed.
        * ``bool(self.api_key)`` is guarded, because ``__bool__`` on a
          caller-supplied object can raise -- and this is the last line of the
          repr, so an exception there would discard the whole rendering.
        * The assembly is wrapped as a whole, because ``!r`` on any other field
          invokes *its* ``__repr__``. The fallback names the type and nothing
          else: a repr that cannot be proven clean is not worth the leak.

        Every fallback is a placeholder. None of them is the raw value.
        """
        try:
            if isinstance(self.endpoint, str):
                try:
                    endpoint = redact_endpoint(self.endpoint)
                except Exception:  # noqa: BLE001 - never raise, never leak
                    endpoint = "<unrenderable endpoint>"
            else:
                endpoint = "<non-str endpoint>"

            key_configured: object
            try:
                key_configured = bool(self.api_key)
            except Exception:  # noqa: BLE001 - a hostile __bool__ is not fatal
                key_configured = "<unreadable>"

            return (
                f"{type(self).__name__}(mode={self.mode!r}, endpoint={endpoint!r}, "
                f"model={self.model!r}, timeout_ms={self.timeout_ms!r}, "
                f"threshold_percent={self.threshold_percent!r}, "
                f"cooldown_turns={self.cooldown_turns!r}, "
                f"max_candidate_tokens={self.max_candidate_tokens!r}, "
                f"max_candidates={self.max_candidates!r}, "
                f"max_state_tokens={self.max_state_tokens!r}, "
                f"api_key_configured={key_configured!r})"
            )
        except Exception:  # noqa: BLE001 - a repr must never raise
            return f"<{type(self).__name__} (unrenderable)>"

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "active")

    @property
    def is_shadow(self) -> bool:
        return self.mode == "shadow"

    def validate(self) -> None:
        """Raise ``ValueError`` on an unusable configuration.

        Called from ``ProxyConfig.__post_init__`` so a bad Jev setup fails at
        startup, alongside the existing worker/retry/rate-limit checks.
        """
        if self.mode not in JEV_MODES:
            raise ValueError(f"jev mode must be one of {', '.join(JEV_MODES)}; got {self.mode!r}")
        if not self.enabled:
            return
        if not self.api_key:
            raise ValueError(f"HEADROOM_JEV_API_KEY is required when HEADROOM_JEV_MODE={self.mode}")
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError(
                "HEADROOM_JEV_ENDPOINT must be an http(s) URL; got "
                f"{redact_endpoint(self.endpoint)}"
            )
        if self.timeout_ms < 1:
            raise ValueError(f"HEADROOM_JEV_TIMEOUT_MS must be >= 1; got {self.timeout_ms}")
        if not 1 <= self.threshold_percent <= 100:
            raise ValueError(
                f"HEADROOM_JEV_THRESHOLD_PERCENT must be 1..100; got {self.threshold_percent}"
            )
        if self.cooldown_turns < 0:
            raise ValueError(f"HEADROOM_JEV_COOLDOWN_TURNS must be >= 0; got {self.cooldown_turns}")
        if self.max_candidate_tokens < 1:
            raise ValueError(
                f"HEADROOM_JEV_MAX_CANDIDATE_TOKENS must be >= 1; got {self.max_candidate_tokens}"
            )
        if self.max_candidates < 1:
            raise ValueError(f"HEADROOM_JEV_MAX_CANDIDATES must be >= 1; got {self.max_candidates}")
        if self.max_state_tokens < 1:
            raise ValueError(
                f"HEADROOM_JEV_MAX_STATE_TOKENS must be >= 1; got {self.max_state_tokens}"
            )

    def redacted(self) -> dict[str, object]:
        """Loggable / dashboard-safe view. Never includes the API key."""
        return {
            "mode": self.mode,
            "endpoint": redact_endpoint(self.endpoint),
            "model": self.model,
            "timeout_ms": self.timeout_ms,
            "threshold_percent": self.threshold_percent,
            "cooldown_turns": self.cooldown_turns,
            "max_candidate_tokens": self.max_candidate_tokens,
            "max_candidates": self.max_candidates,
            "max_state_tokens": self.max_state_tokens,
            "api_key_configured": bool(self.api_key),
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JevConfig:
        """Build from ``HEADROOM_JEV_*``. Raises ``ValueError`` on bad input."""
        src: Mapping[str, str] = os.environ if env is None else env
        mode = (src.get("HEADROOM_JEV_MODE") or "off").strip().lower()
        if mode not in JEV_MODES:
            raise ValueError(
                f"HEADROOM_JEV_MODE must be one of {', '.join(JEV_MODES)}; got {mode!r}"
            )
        if mode == "off":
            return cls()
        config = cls(
            mode=mode,
            api_key=(src.get("HEADROOM_JEV_API_KEY") or "").strip(),
            endpoint=(src.get("HEADROOM_JEV_ENDPOINT") or DEFAULT_JEV_ENDPOINT).strip(),
            model=(src.get("HEADROOM_JEV_MODEL") or DEFAULT_JEV_MODEL).strip(),
            timeout_ms=_env_int(src, "HEADROOM_JEV_TIMEOUT_MS", DEFAULT_JEV_TIMEOUT_MS, minimum=1),
            threshold_percent=_env_int(
                src,
                "HEADROOM_JEV_THRESHOLD_PERCENT",
                DEFAULT_JEV_THRESHOLD_PERCENT,
                minimum=1,
            ),
            cooldown_turns=_env_int(
                src, "HEADROOM_JEV_COOLDOWN_TURNS", DEFAULT_JEV_COOLDOWN_TURNS, minimum=0
            ),
            max_candidate_tokens=_env_int(
                src,
                "HEADROOM_JEV_MAX_CANDIDATE_TOKENS",
                DEFAULT_JEV_MAX_CANDIDATE_TOKENS,
                minimum=1,
            ),
            max_candidates=_env_int(
                src, "HEADROOM_JEV_MAX_CANDIDATES", DEFAULT_JEV_MAX_CANDIDATES, minimum=1
            ),
            max_state_tokens=_env_int(
                src,
                "HEADROOM_JEV_MAX_STATE_TOKENS",
                DEFAULT_JEV_MAX_STATE_TOKENS,
                minimum=1,
            ),
        )
        config.validate()
        return config
