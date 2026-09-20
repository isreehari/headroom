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

The API key lives on this object but never leaves it. :meth:`JevConfig.redacted`
is the only serialization path and reports presence, never the value.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

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


@dataclass(frozen=True)
class JevConfig:
    """Resolved Jev settings. Constructed once at the configuration boundary."""

    mode: str = "off"
    api_key: str = ""
    endpoint: str = DEFAULT_JEV_ENDPOINT
    model: str = DEFAULT_JEV_MODEL
    timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS
    threshold_percent: int = DEFAULT_JEV_THRESHOLD_PERCENT
    cooldown_turns: int = DEFAULT_JEV_COOLDOWN_TURNS
    max_candidate_tokens: int = DEFAULT_JEV_MAX_CANDIDATE_TOKENS
    max_candidates: int = DEFAULT_JEV_MAX_CANDIDATES
    max_state_tokens: int = DEFAULT_JEV_MAX_STATE_TOKENS

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
