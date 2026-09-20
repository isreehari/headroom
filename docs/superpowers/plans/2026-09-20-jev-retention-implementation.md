# Jev Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Jev retention decisions inside Headroom on this machine as three independently-shippable tracks — shadow-mode measurement (A), active mode at the explicit `/v1/compress` CCR boundary (B), and active mode at Codex's native WebSocket compaction boundary (C) — all default-off and fail-open.

**Architecture:** A new `headroom/proxy/jev/` package owns the whole feature: typed config resolved from `HEADROOM_JEV_*` and carried on `ProxyConfig.jev`, an in-process session/branch/revision identity store, a bounded fail-open System One HTTP client, candidate selection over the three real tool-result wire shapes, and a measured request budget. Track A calls that stack after Headroom's own deterministic compression and records a projection (`TP`) without ever touching the forwarded request; Track B reuses it on a caller-declared `/v1/compress` compaction boundary and really rewrites content, but only after each original is written to the CCR store, read back, bound to `(session_id, branch_id, content)` and given a retention lease; Track C does the same for the single candidate Codex's native compaction carries over the `/v1/responses` WebSocket relay. Every gate at every layer falls back to forwarding Headroom's ordinary compressed output unchanged.

**Tech Stack:** Python 3.11+ (Headroom proxy), FastAPI + Starlette WebSockets, `httpx.AsyncClient` (bounded Jev calls, `httpx.MockTransport` in tests), the existing `CompressionStore` CCR layer with its SQLite/in-memory backends, `headroom.tokenizers` (`count_text` / `count_messages`), Prometheus text-format metrics, and `pytest` + `pytest-asyncio`. No new runtime dependency, no Node subprocess, no vendored fork.

## Global Constraints

Copied verbatim from `docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`.

**Non-Goals** (that document's "Non-Goals" section, verbatim):

- No multi-machine pilot, no cross-Mac state sharing, no SSH tunnels.
- No new session-engine or coordinator service.
- No cross-worker/multi-process admission ledger (this machine runs Headroom
  single-worker).
- No PII anonymization claim; Jev is a retention-decision service only.
- No native-summarization replacement; Jev is additive to Headroom's existing
  deterministic compression, never a substitute for it.
- No changes to Rust `headroom-core`; Python proxy only, Rust parity is a later
  follow-up exactly as the original plan stated.

**Project-wide gate** (from "Architecture: Four Independent Tracks", verbatim):

> All Headroom-side tracks stay behind `HEADROOM_JEV_MODE=off` (default) and fail
> open at every gate: timeout, malformed response, stale revision, rejected
> candidate, or CCR write failure all fall back to forwarding Headroom's normal
> compressed output unchanged.

**Track A constraints** (from "Track A: Shadow Mode", verbatim):

> - Config: `HEADROOM_JEV_MODE=off|shadow|active`, `HEADROOM_JEV_API_KEY`,
>   `HEADROOM_JEV_ENDPOINT`, `HEADROOM_JEV_MODEL`, `HEADROOM_JEV_TIMEOUT_MS`,
>   `HEADROOM_JEV_THRESHOLD_PERCENT` (default 80), `HEADROOM_JEV_COOLDOWN_TURNS`
>   (default 5), `HEADROOM_JEV_MAX_CANDIDATE_TOKENS`.
> - Identity: `session_id`/`branch_id`/`revision`/`event_id` exactly as defined
>   in the original plan §1 (PR1 identity and revision contract), **minus** the
>   shared cross-worker admission-sequence machinery — single worker means the
>   in-process latest-revision store is sufficient.
> - Trigger: soft-threshold check after existing Headroom compression, cooldown
>   enforced, one bounded call, no-candidate skip recorded.
> - Never mutates the forwarded request. Records a projection (`TP`) only.
> - Wired into the Anthropic Messages and OpenAI Chat/Responses handler paths
>   (the two provider adapters the original plan scoped for PR1). Unsupported
>   routes fail open with an explicit metric.

**Track B constraints** (from "Track B: Active Mode via `/v1/compress` CCR", verbatim):

> - Before applying `drop`/`truncate`: write original to CCR → require
>   acknowledged success → bind to session/branch/candidate hash + retention
>   lease → commit atomically. Any failed step keeps the original.
> - Single-worker SQLite CCR backend (`headroom/cache/backends/sqlite.py`); no
>   cross-worker lease renewal or shared ledger.
> - Applies to both Anthropic- and OpenAI-shaped `/v1/compress` callers per the
>   earlier "Anthropic + OpenAI both active" decision.
> - Retrieval via the existing `POST /v1/retrieve` path.

with the request shape, verbatim:

```json
{
  "config": {
    "mode": "ccr",
    "session_id": "caller-owned-session-id",
    "jev_compaction_boundary": true
  }
}
```

**Track C constraints** (from "Track C: Active Mode via Native Codex Boundary", verbatim):

> - **Where it lives:** the WS relay/interception between Codex and OpenAI in
>   Headroom's proxy (`headroom/providers/codex/`). Only Headroom can see this
>   boundary — Codex's plugin/hook system (`SessionStart`, `UserPromptSubmit`,
>   `PreToolUse`, `PermissionRequest`, `PostToolUse`, `SubagentStart`,
>   `SubagentStop`, `Stop`) has no compaction-lifecycle hook, so there is no
>   plugin-level alternative to reaching this boundary.
> - **Decision logic stays native Python, not a forked library call.** […]
>   Reuse the already-reviewed, already-fixed Jev-calling pattern from
>   `benchmarks/jev_savings_spike.py` (correct `state`/`questions`/`answers` API
>   shape, credential redaction, bounded timeout, fail-open handling), trimmed to
>   a single-candidate call […]. No new runtime dependency (Node subprocess,
>   vendored fork) in Headroom's request path.
> - **Item-type vocabulary must include** `additional_tools`, `custom_tool_call`,
>   and `custom_tool_call_output` — the real wire types Phase 0b observed, wider
>   than the original plan's and the abandoned probe's assumed set.
> - Same fail-open rules as tracks A/B: missing recovery tool, missing turn
>   identity, stale revision, or CCR failure all preserve the original.
> - Scoped to this machine, single worker, no durable cross-restart replay beyond
>   what the existing CCR backend already provides.
> - Known gap from the probe: only the client→proxy direction was observed:
>   a compaction signal carried solely in the provider's *response* would not be
>   visible to this design either. Acceptable for PR1 of this track; revisit if
>   it turns out to matter.

**Dashboard / metrics constraint** (from "Dashboard and Metrics", verbatim):

> - `T0` (pre-Headroom), `TH` (post-Headroom), `TF` (post-active-retention,
>   measured), `TP` (shadow projection, reported separately, never added to `TF`
>   savings).
> - Calls attempted/completed/timed out/rejected; candidate count/tokens;
>   keep/truncate/drop counts; CCR staged/acknowledged/failed; fallback
>   count/reason; configured model/endpoint label (no secrets).
> - Single-worker event accounting: existing local persistence path is
>   sufficient (no shared SQLite ledger requirement, since that requirement in
>   the original plan only exists for multi-worker deployments).

**Ordering:** Track A ships first (safe measurement backbone), then B, then C — the
design doc's rollout order. Tasks below are numbered continuously in that order;
Track D (the `fast-jev-compaction` Claude Code plugin) is not Headroom code and is
not part of this plan.

---

## Track A: Shadow Mode

Every module lands under a new `headroom/proxy/jev/` package. Track A never mutates a forwarded request: it records a projection (`TP`) and a metric, nothing else. Default mode is `off`.

---

### Task 1: Jev configuration module

**Files:**
- Create: `headroom/proxy/jev/__init__.py`
- Create: `headroom/proxy/jev/config.py`
- Test: `tests/test_jev_config.py`

**Interfaces:**
- Consumes: nothing (leaf module; imports only stdlib, so `headroom/proxy/models.py` can import it without a cycle)
- Produces:
  - `headroom.proxy.jev.config.JEV_MODES: tuple[str, ...]` = `("off", "shadow", "active")`
  - `headroom.proxy.jev.config.DEFAULT_JEV_ENDPOINT: str`, `DEFAULT_JEV_MODEL: str`, `DEFAULT_JEV_TIMEOUT_MS: int`, `DEFAULT_JEV_THRESHOLD_PERCENT: int`, `DEFAULT_JEV_COOLDOWN_TURNS: int`, `DEFAULT_JEV_MAX_CANDIDATE_TOKENS: int`, `DEFAULT_JEV_MAX_CANDIDATES: int`, `DEFAULT_JEV_MAX_STATE_TOKENS: int`
  - `headroom.proxy.jev.config.redact_endpoint(url: str) -> str`
  - `@dataclass(frozen=True) headroom.proxy.jev.config.JevConfig` with fields `mode: str = "off"`, `api_key: str = ""`, `endpoint: str = DEFAULT_JEV_ENDPOINT`, `model: str = DEFAULT_JEV_MODEL`, `timeout_ms: int = 500`, `threshold_percent: int = 80`, `cooldown_turns: int = 5`, `max_candidate_tokens: int = 20000`, `max_candidates: int = 12`, `max_state_tokens: int = 8000`; properties `enabled: bool`, `is_shadow: bool`; methods `validate(self) -> None` (raises `ValueError`), `redacted(self) -> dict[str, object]`, classmethod `from_env(cls, env: Mapping[str, str] | None = None) -> JevConfig` (raises `ValueError`)

- [ ] **Step 1: Write the failing test**

```python
"""Jev configuration: default-off, strict once enabled, never leaks the key."""

from __future__ import annotations

import pytest

from headroom.proxy.jev.config import (
    DEFAULT_JEV_ENDPOINT,
    JevConfig,
    redact_endpoint,
)


def test_default_config_is_off_and_valid() -> None:
    config = JevConfig()
    assert config.mode == "off"
    assert config.enabled is False
    assert config.is_shadow is False
    config.validate()  # must not raise


def test_shadow_without_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        JevConfig(mode="shadow").validate()


def test_shadow_with_api_key_is_accepted() -> None:
    config = JevConfig(mode="shadow", api_key="sk-test")
    config.validate()
    assert config.enabled is True
    assert config.is_shadow is True
    assert config.endpoint == DEFAULT_JEV_ENDPOINT


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="jev mode must be one of"):
        JevConfig(mode="on").validate()


def test_from_env_off_ignores_every_other_var() -> None:
    # A typo in an unused HEADROOM_JEV_* var must never stop the proxy booting.
    config = JevConfig.from_env(
        {"HEADROOM_JEV_MODE": "off", "HEADROOM_JEV_TIMEOUT_MS": "not-a-number"}
    )
    assert config == JevConfig()


def test_from_env_shadow_reads_every_knob() -> None:
    config = JevConfig.from_env(
        {
            "HEADROOM_JEV_MODE": "shadow",
            "HEADROOM_JEV_API_KEY": "sk-test",
            "HEADROOM_JEV_ENDPOINT": "https://example.invalid/v1/systemone",
            "HEADROOM_JEV_MODEL": "jev-1.2.3",
            "HEADROOM_JEV_TIMEOUT_MS": "1500",
            "HEADROOM_JEV_THRESHOLD_PERCENT": "70",
            "HEADROOM_JEV_COOLDOWN_TURNS": "3",
            "HEADROOM_JEV_MAX_CANDIDATE_TOKENS": "4096",
            "HEADROOM_JEV_MAX_CANDIDATES": "8",
            "HEADROOM_JEV_MAX_STATE_TOKENS": "6000",
        }
    )
    assert config.mode == "shadow"
    assert config.api_key == "sk-test"
    assert config.model == "jev-1.2.3"
    assert config.timeout_ms == 1500
    assert config.threshold_percent == 70
    assert config.cooldown_turns == 3
    assert config.max_candidate_tokens == 4096
    assert config.max_candidates == 8
    assert config.max_state_tokens == 6000


def test_from_env_shadow_without_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        JevConfig.from_env({"HEADROOM_JEV_MODE": "shadow"})


def test_from_env_rejects_non_integer_knob_when_enabled() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_TIMEOUT_MS must be an integer"):
        JevConfig.from_env(
            {
                "HEADROOM_JEV_MODE": "shadow",
                "HEADROOM_JEV_API_KEY": "sk-test",
                "HEADROOM_JEV_TIMEOUT_MS": "half a second",
            }
        )


def test_redacted_never_contains_the_api_key() -> None:
    config = JevConfig(mode="shadow", api_key="sk-super-secret")
    payload = config.redacted()
    assert "sk-super-secret" not in repr(payload)
    assert payload["api_key_configured"] is True
    assert "api_key" not in payload


def test_redact_endpoint_strips_userinfo_and_query() -> None:
    assert (
        redact_endpoint("https://user:pw@api.example.invalid:8443/v1/systemone?token=abc")
        == "https://<redacted>@api.example.invalid:8443/v1/systemone?<redacted>"
    )
    assert redact_endpoint("") == "<unset>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_config.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/__init__.py`:

```python
"""Jev retention-decision integration (TypeSafe System One).

Track A (shadow mode) of
``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md``. Default
off; additive to Headroom's own deterministic compression, never a substitute
for it.
"""

from __future__ import annotations
```

`headroom/proxy/jev/config.py`:

```python
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
    except ValueError:
        return "<unparseable endpoint>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
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
            raise ValueError(
                f"HEADROOM_JEV_API_KEY is required when HEADROOM_JEV_MODE={self.mode}"
            )
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
            raise ValueError(
                f"HEADROOM_JEV_COOLDOWN_TURNS must be >= 0; got {self.cooldown_turns}"
            )
        if self.max_candidate_tokens < 1:
            raise ValueError(
                "HEADROOM_JEV_MAX_CANDIDATE_TOKENS must be >= 1; got "
                f"{self.max_candidate_tokens}"
            )
        if self.max_candidates < 1:
            raise ValueError(
                f"HEADROOM_JEV_MAX_CANDIDATES must be >= 1; got {self.max_candidates}"
            )
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
            timeout_ms=_env_int(
                src, "HEADROOM_JEV_TIMEOUT_MS", DEFAULT_JEV_TIMEOUT_MS, minimum=1
            ),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/__init__.py headroom/proxy/jev/config.py tests/test_jev_config.py
git commit -m "feat(jev): typed JevConfig with default-off env resolution and strict validation"
```

---

### Task 2: Wire `JevConfig` into `ProxyConfig` and proxy startup

**Files:**
- Modify: `headroom/proxy/models.py:18` (import), `headroom/proxy/models.py:546` (new field after `worker_processes`), `headroom/proxy/models.py:548-568` (`__post_init__`)
- Modify: `headroom/proxy/server.py:155` (import), `headroom/proxy/server.py:5623-5636` (`_proxy_config_payload`), `headroom/proxy/server.py:5639-5722` (`_proxy_config_from_env`), `headroom/proxy/server.py:6470` (CLI `ProxyConfig(...)` construction)
- Test: `tests/test_jev_proxy_config.py`

**Interfaces:**
- Consumes: `headroom.proxy.jev.config.JevConfig` (Task 1)
- Produces:
  - `headroom.proxy.models.ProxyConfig.jev: JevConfig` — new field, defaults to `JevConfig()` (mode `"off"`), declared **after** `worker_processes` so no existing positional constructor argument shifts
  - `ProxyConfig.__post_init__` now calls `self.jev.validate()`, so an enabled-but-keyless Jev config raises `ValueError` at proxy construction
  - `headroom.proxy.server._proxy_config_payload` never emits a `jev` key (the API key must not ride in the multi-worker env var); workers rebuild it from `HEADROOM_JEV_*`

- [ ] **Step 1: Write the failing test**

```python
"""ProxyConfig carries the Jev config, validates it at startup, and never
serializes the API key into the multi-worker env payload."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.jev.config import JevConfig
from headroom.proxy.models import ProxyConfig
from headroom.proxy.server import _proxy_config_payload


def test_default_proxy_config_has_jev_off() -> None:
    config = ProxyConfig()
    assert config.jev.mode == "off"
    assert config.jev.enabled is False


def test_shadow_mode_without_api_key_fails_proxy_config_construction() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        ProxyConfig(jev=JevConfig(mode="shadow"))


def test_shadow_mode_with_api_key_constructs() -> None:
    config = ProxyConfig(jev=JevConfig(mode="shadow", api_key="sk-test"))
    assert config.jev.is_shadow is True


def test_multi_worker_payload_omits_the_jev_block_entirely() -> None:
    config = ProxyConfig(jev=JevConfig(mode="shadow", api_key="sk-super-secret"))
    payload = _proxy_config_payload(config)
    assert "jev" not in payload
    assert "sk-super-secret" not in json.dumps(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_proxy_config.py -q`
Expected: FAIL with "AttributeError: 'ProxyConfig' object has no attribute 'jev'"

- [ ] **Step 3: Write minimal implementation**

In `headroom/proxy/models.py`, after line 18 (`from headroom.proxy.model_router import ModelRouterConfig`) add:

```python
from headroom.proxy.jev.config import JevConfig
```

In `headroom/proxy/models.py`, immediately after `worker_processes: int = 1` (line 546) and before `def __post_init__`, add:

```python
    # Jev retention-decision integration (Track A shadow mode). Default off;
    # resolved from HEADROOM_JEV_* at the composition root. Declared last, like
    # worker_processes above, so no existing positional constructor field
    # shifts. Validated in __post_init__ — an enabled mode with no API key is a
    # startup error, not a silent no-op.
    jev: JevConfig = field(default_factory=JevConfig)
```

In `headroom/proxy/models.py.__post_init__`, after the existing `rate_limit_requests_per_minute` check (which ends at line 568) add:

```python
        # Jev: default-off, but strict once switched on (missing key, bad
        # endpoint, out-of-range knobs all fail startup here).
        self.jev.validate()
```

In `headroom/proxy/server.py`, after line 155 (`from headroom.proxy.model_router import ModelRouter, ModelRouterConfig`) add:

```python
from headroom.proxy.jev.config import JevConfig
```

In `headroom/proxy/server.py._proxy_config_payload`, inside the `for field in fields(config):` loop, immediately after the `rollout` branch (line 5629's `continue`) add:

```python
        if field.name == "jev":
            # The Jev config carries HEADROOM_JEV_API_KEY, and this payload is
            # handed to worker processes through an environment variable, which
            # is readable from the process table on most platforms. Workers
            # inherit HEADROOM_JEV_* directly, so _proxy_config_from_env rebuilds
            # the block from env instead of shipping the secret here.
            continue
```

In `headroom/proxy/server.py._proxy_config_from_env`, replace line 5651 (`            return ProxyConfig(**values)`) with:

```python
            # Rebuilt from env, never from the payload — see _proxy_config_payload.
            values["jev"] = JevConfig.from_env()
            return ProxyConfig(**values)
```

In the same function's fallback `return ProxyConfig(` call, add a `jev=` argument immediately before the closing `)` at line 5722 (i.e. after the `model_router=ModelRouterConfig.from_env(...)` entry):

```python
        jev=JevConfig.from_env(),
```

In `headroom/proxy/server.py`'s CLI `ProxyConfig(...)` construction, add the same argument next to `compress_passthrough=compress_passthrough,` (line 6470):

```python
        jev=JevConfig.from_env(),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_proxy_config.py tests/test_proxy_config_rate_limit.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/models.py headroom/proxy/server.py tests/test_jev_proxy_config.py
git commit -m "feat(jev): carry JevConfig on ProxyConfig, validate at startup, keep the key out of the worker payload"
```

---

### Task 3: Session / branch / revision / event identity

**Files:**
- Create: `headroom/proxy/jev/identity.py`
- Test: `tests/test_jev_identity.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. `session_id` is supplied by the caller and comes from the existing Headroom identity machinery — `headroom.cache.prefix_tracker.SessionTrackerStore.compute_session_id(request, model, messages) -> str` (`headroom/proxy/handlers/anthropic.py:1511`, `headroom/proxy/handlers/openai.py:3683` and `:5591`). No new session-id derivation is invented.
- Produces:
  - `headroom.proxy.jev.identity.branch_id_for(session_id: str, branch_root: list[dict[str, Any]] | None) -> str` (24 hex chars)
  - `headroom.proxy.jev.identity.revision_for(candidate_fingerprints: Sequence[str]) -> str` (32 hex chars)
  - `@dataclass(frozen=True) headroom.proxy.jev.identity.JevTurnIdentity` with fields `session_id: str`, `branch_id: str`, `revision: str`, `event_id: str` and property `branch_key: tuple[str, str]`
  - `headroom.proxy.jev.identity.JevIdentityStore` with `__init__(self, *, max_branches: int = 512)`, `identify(self, *, session_id: str, branch_root: list[dict[str, Any]] | None, candidate_fingerprints: Sequence[str]) -> JevTurnIdentity`, `record(self, identity: JevTurnIdentity) -> None`, `latest_revision(self, session_id: str, branch_id: str) -> str | None`, `is_current(self, identity: JevTurnIdentity) -> bool`, property `tracked_branches: int`

- [ ] **Step 1: Write the failing test**

```python
"""Single-worker Jev identity: stable branch ids, rolling revisions, bounded store."""

from __future__ import annotations

import pytest

from headroom.proxy.jev.identity import (
    JevIdentityStore,
    JevTurnIdentity,
    branch_id_for,
    revision_for,
)

ROOT = [{"role": "system", "content": "you are a helpful agent"}]


def test_branch_id_is_stable_for_the_same_root_and_session() -> None:
    assert branch_id_for("sess-1", ROOT) == branch_id_for("sess-1", ROOT)


def test_branch_id_changes_with_session_or_root() -> None:
    assert branch_id_for("sess-1", ROOT) != branch_id_for("sess-2", ROOT)
    assert branch_id_for("sess-1", ROOT) != branch_id_for(
        "sess-1", [{"role": "system", "content": "different"}]
    )


def test_revision_tracks_the_candidate_set() -> None:
    assert revision_for(["a", "b"]) == revision_for(["a", "b"])
    assert revision_for(["a", "b"]) != revision_for(["a", "c"])
    assert revision_for(["a", "b"]) != revision_for(["b", "a"])


def test_identify_is_stable_except_for_the_event_id() -> None:
    store = JevIdentityStore()
    first = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    second = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert first.session_id == second.session_id == "s"
    assert first.branch_id == second.branch_id
    assert first.revision == second.revision
    assert first.event_id != second.event_id


def test_a_newer_revision_makes_the_older_identity_stale() -> None:
    store = JevIdentityStore()
    old = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert store.is_current(old) is True
    store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a", "b"])
    assert store.is_current(old) is False


def test_latest_revision_reads_back_per_branch() -> None:
    store = JevIdentityStore()
    identity = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert store.latest_revision("s", identity.branch_id) == identity.revision
    assert store.latest_revision("s", "nope") is None


def test_store_is_bounded_and_evicts_oldest_first() -> None:
    store = JevIdentityStore(max_branches=2)
    a = store.identify(session_id="a", branch_root=ROOT, candidate_fingerprints=["x"])
    store.identify(session_id="b", branch_root=ROOT, candidate_fingerprints=["x"])
    store.identify(session_id="c", branch_root=ROOT, candidate_fingerprints=["x"])
    assert store.tracked_branches == 2
    assert store.latest_revision("a", a.branch_id) is None


def test_max_branches_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_branches must be >= 1"):
        JevIdentityStore(max_branches=0)


def test_branch_key_pairs_session_and_branch() -> None:
    identity = JevTurnIdentity(session_id="s", branch_id="b", revision="r", event_id="e")
    assert identity.branch_key == ("s", "b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_identity.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.identity'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/identity.py`:

```python
"""Session / branch / revision / event identity for Jev retention (Track A).

Single-machine, single-worker scope. The fresh design
(``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md``, "Track A")
keeps the original plan's four-part identity but drops the shared cross-worker
admission-sequence machinery: one worker means an in-process latest-revision
store is sufficient, so there is no SQLite ledger here.

Nothing in this module derives a session id. ``session_id`` is the one the
proxy already computes with
``headroom.cache.prefix_tracker.SessionTrackerStore.compute_session_id`` —
reusing the existing identity machinery rather than inventing a parallel one.

* ``branch_id`` — the conversation lineage root within a session id. Derived
  from the session id plus the frozen/protected prefix, so a new system prompt
  or a re-rooted conversation forks a branch while ordinary turn growth does
  not. That is the scope for "one bounded call per session/branch".
* ``revision`` — the candidate set as it stands this turn. Rolls whenever the
  eligible candidates change, which is exactly the staleness test a shadow call
  needs when it returns after the conversation has moved on.
* ``event_id`` — unique per shadow attempt, so a duplicate or late response can
  be told apart from a fresh one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_BRANCHES = 512


def branch_id_for(session_id: str, branch_root: list[dict[str, Any]] | None) -> str:
    """Stable id for a conversation lineage root within ``session_id``."""
    canonical = json.dumps(
        branch_root or [],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{session_id}\x00{canonical}".encode()).hexdigest()[:24]


def revision_for(candidate_fingerprints: Sequence[str]) -> str:
    """Revision of the current candidate set. Order-sensitive on purpose."""
    canonical = json.dumps(list(candidate_fingerprints), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class JevTurnIdentity:
    """Identity of one shadow attempt."""

    session_id: str
    branch_id: str
    revision: str
    event_id: str

    @property
    def branch_key(self) -> tuple[str, str]:
        return (self.session_id, self.branch_id)


class JevIdentityStore:
    """In-process latest-revision store, one entry per (session, branch).

    Bounded: the id space is caller-controlled (a client can rotate
    ``x-headroom-session-id`` freely), so the map evicts least-recently-stamped
    branches instead of growing without limit.
    """

    def __init__(self, *, max_branches: int = DEFAULT_MAX_BRANCHES) -> None:
        if max_branches < 1:
            raise ValueError("max_branches must be >= 1")
        self._max_branches = max_branches
        self._latest: OrderedDict[tuple[str, str], str] = OrderedDict()

    def identify(
        self,
        *,
        session_id: str,
        branch_root: list[dict[str, Any]] | None,
        candidate_fingerprints: Sequence[str],
    ) -> JevTurnIdentity:
        """Mint and stamp the identity for this turn's candidate set."""
        identity = JevTurnIdentity(
            session_id=session_id,
            branch_id=branch_id_for(session_id, branch_root),
            revision=revision_for(candidate_fingerprints),
            event_id=uuid.uuid4().hex,
        )
        self.record(identity)
        return identity

    def record(self, identity: JevTurnIdentity) -> None:
        """Make ``identity.revision`` the branch's latest."""
        key = identity.branch_key
        self._latest[key] = identity.revision
        self._latest.move_to_end(key)
        while len(self._latest) > self._max_branches:
            self._latest.popitem(last=False)

    def latest_revision(self, session_id: str, branch_id: str) -> str | None:
        return self._latest.get((session_id, branch_id))

    def is_current(self, identity: JevTurnIdentity) -> bool:
        """False once a newer revision has been stamped for the same branch."""
        return self._latest.get(identity.branch_key) == identity.revision

    @property
    def tracked_branches(self) -> int:
        return len(self._latest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_identity.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/identity.py tests/test_jev_identity.py
git commit -m "feat(jev): in-process session/branch/revision/event identity store"
```

---

### Task 4: Bounded, fail-open Jev HTTP client

**Files:**
- Create: `headroom/proxy/jev/client.py`
- Test: `tests/test_jev_client.py`

**Interfaces:**
- Consumes: `headroom.proxy.jev.config.JevConfig`, `headroom.proxy.jev.config.redact_endpoint` (Task 1)
- Produces:
  - `headroom.proxy.jev.client.JEV_DECISIONS: tuple[str, ...]` = `("keep", "truncate", "drop")`
  - `headroom.proxy.jev.client.STATE_FIELD`/`QUESTIONS_FIELD`/`ANSWERS_FIELD`/`DECISION_FIELD`: `str`
  - `headroom.proxy.jev.client.build_request_payload(config: JevConfig, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]`
  - `@dataclass headroom.proxy.jev.client.JevAnswer` with fields `decisions: dict[str, str]`, `latency_ms: float`, `error: str | None`, `jev_model: str | None`, `usage: dict[str, Any] | None`, `unparsed: int`; property `ok: bool`
  - `headroom.proxy.jev.client.JevClient` with `__init__(self, config: JevConfig, *, http_client: httpx.AsyncClient | None = None)`, `async decide(self, *, state: dict[str, Any], questions: dict[str, Any], candidate_ids: Sequence[str]) -> JevAnswer`, `async aclose(self) -> None`

- [ ] **Step 1: Write the failing test**

```python
"""JevClient speaks the real System One state/questions->answers contract,
is bounded by timeout_ms, fails open to keep, and never leaks the API key."""

from __future__ import annotations

import httpx

from headroom.proxy.jev.client import JevClient, build_request_payload
from headroom.proxy.jev.config import JevConfig

CONFIG = JevConfig(
    mode="shadow",
    api_key="sk-super-secret",
    endpoint="https://api.example.invalid/v1/systemone?token=leaky",
    model="jev-latest",
    timeout_ms=1000,
)


def _client(handler) -> JevClient:
    return JevClient(CONFIG, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_build_request_payload_uses_the_state_questions_shape() -> None:
    payload = build_request_payload(CONFIG, {"candidates": []}, {"cand_0000": {}})
    assert set(payload) == {"state", "model", "questions"}
    assert payload["model"] == "jev-latest"


async def test_happy_path_parses_choices() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = httpx.Response(200).json if False else request.content
        return httpx.Response(
            200,
            json={
                "model": "jev-1.2.3",
                "answers": {
                    "cand_0000": {"type": "choice", "choice": "drop", "confidence": 0.8},
                    "cand_0001": {"type": "choice", "choice": "truncate"},
                },
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    jev = _client(handler)
    answer = await jev.decide(
        state={"candidates": []},
        questions={"cand_0000": {}, "cand_0001": {}},
        candidate_ids=["cand_0000", "cand_0001"],
    )
    await jev.aclose()

    assert answer.ok is True
    assert answer.decisions == {"cand_0000": "drop", "cand_0001": "truncate"}
    assert answer.jev_model == "jev-1.2.3"
    assert answer.usage == {"input_tokens": 10, "output_tokens": 2}
    assert answer.unparsed == 0
    assert seen["auth"] == "Bearer sk-super-secret"


async def test_unparseable_answer_falls_back_to_keep() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"cand_0000": {"choice": "obliterate"}}})

    jev = _client(handler)
    answer = await jev.decide(
        state={}, questions={}, candidate_ids=["cand_0000", "cand_0001"]
    )
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep", "cand_0001": "keep"}
    assert answer.unparsed == 2


async def test_http_error_fails_open_to_keep_and_redacts_the_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"detail": {"error_type": "max_tokens_exceeded"}}
        )

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error is not None
    assert "max_tokens_exceeded" in answer.error
    assert "sk-super-secret" not in answer.error
    assert "token=leaky" not in answer.error


async def test_transport_failure_fails_open_and_scrubs_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out reading https://api.example.invalid/v1/systemone?token=leaky")

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error is not None
    assert answer.error.startswith("ReadTimeout:")
    assert "token=leaky" not in answer.error


async def test_missing_answers_block_fails_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "jev-1.2.3"})

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert "no dict at 'answers'" in (answer.error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_client.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.client'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/client.py`:

```python
"""Bounded, fail-open HTTP client for the Jev (TypeSafe System One) API.

The wire contract is the one ``benchmarks/jev_savings_spike.py`` proved against
the real API in Phase 0a: System One is a **structured question-answering**
service, not a bespoke keep/truncate/drop retention API. The retention view is
the ``state``; each candidate is one ``choice`` question::

    POST <endpoint>
    Authorization: Bearer <key>
    {"state": {...retention view...}, "model": "jev-latest",
     "questions": {"cand_0000": {"type": "choice", "instructions": "...",
                                 "criteria": {"keep": "...", "truncate": "...",
                                              "drop": "..."}}}}

    -> {"model": "jev-1.x.y",
        "answers": {"cand_0000": {"type": "choice", "choice": "drop", ...}},
        "usage": {"input_tokens": 392, "output_tokens": 65}}

Fail-open rules carried over from the spike, because this now runs in the live
request path:

* One bounded call. ``timeout_ms`` (default 500) caps the whole round trip.
* :meth:`JevClient.decide` never raises. Every failure — timeout, TLS, 4xx/5xx,
  non-JSON, missing ``answers``, an unrecognized choice — resolves to ``keep``
  for every candidate, so an ambiguous answer can never move a token.
* The API key travels in a header and is never logged. The endpoint may carry
  credentials in its userinfo or a token in its query string, so every error
  string is scrubbed through ``redact_endpoint`` before it leaves this module.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from headroom.proxy.jev.config import JevConfig, redact_endpoint

logger = logging.getLogger(__name__)

JEV_DECISIONS: tuple[str, ...] = ("keep", "truncate", "drop")

STATE_FIELD = "state"
QUESTIONS_FIELD = "questions"
ANSWERS_FIELD = "answers"
DECISION_FIELD = "choice"


def build_request_payload(
    config: JevConfig,
    state: dict[str, Any],
    questions: dict[str, Any],
) -> dict[str, Any]:
    """The exact request body :meth:`JevClient.decide` sends.

    Exposed so the request-budget check can measure the real serialized payload
    rather than an estimate of it (the Phase 0a lesson: Jev rejects an oversized
    request outright with ``max_tokens_exceeded`` and the whole call is lost).
    """
    return {STATE_FIELD: state, "model": config.model, QUESTIONS_FIELD: questions}


def _scrub(text: str, endpoint: str) -> str:
    """Replace any verbatim endpoint echoed back by httpx or the server."""
    return text.replace(endpoint, redact_endpoint(endpoint)) if endpoint else text


@dataclass
class JevAnswer:
    """One Jev round trip. ``decisions`` is always fully populated."""

    decisions: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str | None = None
    jev_model: str | None = None
    usage: dict[str, Any] | None = None
    unparsed: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class JevClient:
    """Owns the Jev connection. Separate from the proxy's upstream pool: a
    500ms retention call must not share timeouts or keepalive economics with a
    300s model call."""

    def __init__(self, config: JevConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http_client = http_client
        self._owns_client = http_client is None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_ms / 1000.0)
            )
            self._owns_client = True
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and self._owns_client:
            await self._http_client.aclose()
        self._http_client = None

    async def decide(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        candidate_ids: Sequence[str],
    ) -> JevAnswer:
        """One bounded call. Never raises; anything ambiguous becomes ``keep``."""
        answer = JevAnswer(decisions={cid: "keep" for cid in candidate_ids})
        endpoint = self._config.endpoint
        payload = build_request_payload(self._config, state, questions)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        try:
            response = await self._client().post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self._config.timeout_ms / 1000.0,
            )
        except Exception as exc:  # timeout, DNS, TLS, connection reset
            answer.latency_ms = (time.perf_counter() - started) * 1000.0
            # httpx error strings routinely embed the request URL.
            answer.error = _scrub(f"{type(exc).__name__}: {exc}", endpoint)
            return answer
        answer.latency_ms = (time.perf_counter() - started) * 1000.0

        if response.status_code >= 400:
            answer.error = _scrub(
                f"HTTP {response.status_code}: {response.text[:400]}", endpoint
            )
            return answer

        try:
            body = response.json()
        except Exception as exc:
            answer.error = f"non-JSON response: {type(exc).__name__}"
            return answer

        if not isinstance(body, dict):
            answer.error = f"unexpected response type: {type(body).__name__}"
            return answer

        if isinstance(body.get("model"), str):
            answer.jev_model = body["model"]
        if isinstance(body.get("usage"), dict):
            answer.usage = dict(body["usage"])

        answers = body.get(ANSWERS_FIELD)
        if not isinstance(answers, dict):
            answer.error = (
                f"no dict at '{ANSWERS_FIELD}' (keys were {sorted(body)[:12]}); "
                "falling back to keep for every candidate"
            )
            return answer

        for cid in candidate_ids:
            raw = answers.get(cid)
            decision: str | None = None
            if isinstance(raw, dict):
                choice = raw.get(DECISION_FIELD)
                if isinstance(choice, str) and choice.strip().lower() in JEV_DECISIONS:
                    decision = choice.strip().lower()
            elif isinstance(raw, str) and raw.strip().lower() in JEV_DECISIONS:
                decision = raw.strip().lower()
            if decision is None:
                answer.unparsed += 1
                decision = "keep"  # never guess-mutate on ambiguous output
            answer.decisions[cid] = decision

        if answer.unparsed:
            logger.debug(
                "jev: %d/%d answers unparseable -> forced keep",
                answer.unparsed,
                len(answer.decisions),
            )
        return answer
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_client.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/client.py tests/test_jev_client.py
git commit -m "feat(jev): bounded fail-open System One client with credential redaction"
```

---

### Task 5: Candidate selection

**Files:**
- Create: `headroom/proxy/jev/candidates.py`
- Test: `tests/test_jev_candidates.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `headroom.proxy.jev.candidates.RECENT_TAIL_EXCLUSION: int` = `6`
  - `headroom.proxy.jev.candidates.ELIGIBLE_OUTPUT_ITEM_TYPES: frozenset[str]` = `{"function_call_output", "custom_tool_call_output"}`
  - `headroom.proxy.jev.candidates.text_of(value: Any) -> str`
  - `@dataclass(frozen=True) headroom.proxy.jev.candidates.JevCandidate` with fields `candidate_id: str`, `message_index: int`, `block_index: int | None`, `candidate_type: str`, `role: str`, `tool_call_id: str | None`, `content: str`, `est_tokens: int`; properties `content_sha256: str`, `fingerprint: str`
  - `headroom.proxy.jev.candidates.select_candidates(messages: list[dict[str, Any]], *, frozen_prefix: int, count_text: Callable[[str], int], max_candidates: int, recent_tail: int = RECENT_TAIL_EXCLUSION) -> list[JevCandidate]`
  - `headroom.proxy.jev.candidates.count_messages_corrected(messages: list[dict[str, Any]], *, count_messages: Callable[[list[dict[str, Any]]], int], count_text: Callable[[str], int]) -> int`

- [ ] **Step 1: Write the failing test**

```python
"""Candidate eligibility: allowlisted tool-result shapes, outside the 6-message
recent tail, outside the frozen prefix, bounded in number."""

from __future__ import annotations

from headroom.proxy.jev.candidates import (
    RECENT_TAIL_EXCLUSION,
    JevCandidate,
    count_messages_corrected,
    select_candidates,
)


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _padding(n: int) -> list[dict[str, object]]:
    return [{"role": "assistant", "content": f"filler {i}"} for i in range(n)]


def test_selects_openai_chat_tool_messages() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "call_1", "content": "RESULT BODY"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(
        messages, frozen_prefix=1, count_text=_count_text, max_candidates=10
    )
    assert [c.candidate_id for c in found] == ["cand_0000"]
    assert found[0].candidate_type == "tool_result"
    assert found[0].message_index == 1
    assert found[0].block_index is None
    assert found[0].tool_call_id == "call_1"
    assert found[0].content == "RESULT BODY"


def test_selects_responses_function_call_output_items() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"type": "function_call_output", "call_id": "fc_1", "output": "PAYLOAD"},
        {"type": "custom_tool_call_output", "call_id": "ct_1", "output": "PAYLOAD2"},
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(
        messages, frozen_prefix=1, count_text=_count_text, max_candidates=10
    )
    assert [c.candidate_type for c in found] == [
        "function_call_output",
        "custom_tool_call_output",
    ]
    assert [c.content for c in found] == ["PAYLOAD", "PAYLOAD2"]


def test_selects_anthropic_tool_result_blocks_with_block_index() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "ignore me"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "BLOCK BODY"},
            ],
        },
        *_padding(RECENT_TAIL_EXCLUSION),
    ]
    found = select_candidates(
        messages, frozen_prefix=1, count_text=_count_text, max_candidates=10
    )
    assert len(found) == 1
    assert found[0].block_index == 1
    assert found[0].tool_call_id == "tu_1"
    assert found[0].content == "BLOCK BODY"


def test_recent_tail_is_never_eligible() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(10)]
    found = select_candidates(
        messages, frozen_prefix=0, count_text=_count_text, max_candidates=10
    )
    # 10 messages, last 6 excluded -> indices 0..3 remain.
    assert [c.message_index for c in found] == [0, 1, 2, 3]


def test_frozen_prefix_is_never_eligible() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(10)]
    found = select_candidates(
        messages, frozen_prefix=2, count_text=_count_text, max_candidates=10
    )
    assert [c.message_index for c in found] == [2, 3]


def test_max_candidates_keeps_the_oldest() -> None:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x"} for i in range(20)]
    found = select_candidates(
        messages, frozen_prefix=0, count_text=_count_text, max_candidates=3
    )
    assert [c.message_index for c in found] == [0, 1, 2]
    assert [c.candidate_id for c in found] == ["cand_0000", "cand_0001", "cand_0002"]


def test_no_candidates_when_nothing_is_eligible() -> None:
    messages = [{"role": "user", "content": "hello"}, *_padding(RECENT_TAIL_EXCLUSION)]
    assert (
        select_candidates(
            messages, frozen_prefix=0, count_text=_count_text, max_candidates=10
        )
        == []
    )


def test_fingerprint_tracks_content() -> None:
    a = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "body", 1)
    b = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "body", 1)
    c = JevCandidate("cand_0000", 1, None, "tool_result", "tool", "c", "other", 1)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_count_messages_corrected_prices_responses_output_payloads() -> None:
    # A Responses function_call_output carries its payload in `output`, which a
    # `content`-only counter prices at ~0 -- the Phase 0a token-accounting bug.
    messages = [{"type": "function_call_output", "call_id": "fc_1", "output": "x" * 400}]

    def naive_count_messages(msgs: list[dict[str, object]]) -> int:
        return 0

    assert (
        count_messages_corrected(
            messages, count_messages=naive_count_messages, count_text=_count_text
        )
        == 100
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_candidates.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.candidates'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/candidates.py`:

```python
"""Eligible retention candidates in a post-Headroom message list.

Eligibility follows the fresh design's Track A rules, implemented and validated
in ``benchmarks/jev_savings_spike.py`` during Phase 0a:

* An allowlisted tool-result shape — an OpenAI Chat ``role: "tool"`` message, a
  Responses ``function_call_output`` / ``custom_tool_call_output`` item, or an
  Anthropic ``tool_result`` block inside a user message's content list.
  ``custom_tool_call_output`` is in the allowlist because Phase 0b observed it
  as a real wire item type, wider than the original plan's assumed vocabulary.
* Outside the last ``RECENT_TAIL_EXCLUSION`` (6) messages. This is a retention
  *eligibility* rule and is implemented here directly: Headroom's own
  ``protect_recent`` router guard is a compressor knob with a different default
  (4), so it is not a substitute.
* Outside the caller's frozen/protected prefix.
* Bounded in number, oldest first — the oldest candidates are the ones most
  likely to be stale.

Token counting takes a ``count_text`` callable so the caller can pass the
tokenizer the request already resolved (``OpenAICompatibleTokenCounter`` and
every other ``headroom.tokenizers.TokenCounter`` expose ``count_text`` /
``count_messages``), rather than this module resolving a second tokenizer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

#: Nothing in the last N messages is ever a candidate.
RECENT_TAIL_EXCLUSION = 6

#: Bare item types (no ``role``) whose payload lives in ``output``.
ELIGIBLE_OUTPUT_ITEM_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


def text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


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


def select_candidates(
    messages: list[dict[str, Any]],
    *,
    frozen_prefix: int,
    count_text: Callable[[str], int],
    max_candidates: int,
    recent_tail: int = RECENT_TAIL_EXCLUSION,
) -> list[JevCandidate]:
    """Eligible candidates from a post-Headroom message list, oldest first."""
    total = len(messages)
    tail_start = total - max(0, recent_tail)
    floor = max(0, frozen_prefix)
    found: list[JevCandidate] = []

    for idx, msg in enumerate(messages):
        if idx < floor or idx >= tail_start:
            continue
        if not isinstance(msg, dict):
            continue

        role = str(msg.get("role") or "")

        # OpenAI Chat Completions: a whole message with role="tool".
        if role == "tool" and msg.get("content") is not None:
            body = text_of(msg["content"])
            found.append(
                JevCandidate(
                    candidate_id="",
                    message_index=idx,
                    block_index=None,
                    candidate_type="tool_result",
                    role=role,
                    tool_call_id=msg.get("tool_call_id"),
                    content=body,
                    est_tokens=count_text(body),
                )
            )
            continue

        # OpenAI Responses: a bare {"type": "...output", "output": ...} item.
        item_type = msg.get("type")
        if item_type in ELIGIBLE_OUTPUT_ITEM_TYPES:
            body = text_of(msg.get("output", ""))
            found.append(
                JevCandidate(
                    candidate_id="",
                    message_index=idx,
                    block_index=None,
                    candidate_type=str(item_type),
                    role=role or "tool",
                    tool_call_id=msg.get("call_id") or msg.get("id"),
                    content=body,
                    est_tokens=count_text(body),
                )
            )
            continue

        # Anthropic: tool_result blocks inside a user message's content list.
        content = msg.get("content")
        if isinstance(content, list):
            for bidx, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                body = text_of(block.get("content", ""))
                found.append(
                    JevCandidate(
                        candidate_id="",
                        message_index=idx,
                        block_index=bidx,
                        candidate_type="tool_result",
                        role=role or "user",
                        tool_call_id=block.get("tool_use_id"),
                        content=body,
                        est_tokens=count_text(body),
                    )
                )

    found = found[: max(0, max_candidates)]
    return [replace(cand, candidate_id=f"cand_{i:04d}") for i, cand in enumerate(found)]


def count_messages_corrected(
    messages: list[dict[str, Any]],
    *,
    count_messages: Callable[[list[dict[str, Any]]], int],
    count_text: Callable[[str], int],
) -> int:
    """Token count that also prices Responses ``output`` payloads.

    Phase 0a bug: a message counter only ever reads a message's ``content``, so
    an OpenAI Responses ``function_call_output`` item — whose payload lives in
    ``output`` — prices at ~0. That is the exact field candidate selection and
    the projection operate on, so without this correction a drop/truncate of
    such a candidate moves real tokens while TH and TP both stay put, and the
    savings are silently reported as zero.
    """
    try:
        total = int(count_messages(messages))
    except Exception:
        total = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                total += count_text(content)
            elif content is not None:
                total += count_text(text_of(content))

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") not in ELIGIBLE_OUTPUT_ITEM_TYPES:
            continue
        output = msg.get("output")
        if output is not None:
            total += count_text(text_of(output))
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_candidates.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/candidates.py tests/test_jev_candidates.py
git commit -m "feat(jev): candidate selection with recent-tail and frozen-prefix exclusion"
```

---

### Task 6: Retention view, questions, and the measured request budget

**Files:**
- Create: `headroom/proxy/jev/request.py`
- Test: `tests/test_jev_request.py`

**Interfaces:**
- Consumes: `headroom.proxy.jev.candidates.JevCandidate` (Task 5), `headroom.proxy.jev.client.build_request_payload` and `JevConfig` (Tasks 1, 4)
- Produces:
  - `headroom.proxy.jev.request.DECISION_CRITERIA: dict[str, str]`
  - `headroom.proxy.jev.request.QUESTION_INSTRUCTIONS: str`
  - `headroom.proxy.jev.request.MIN_VIEW_TOKENS: int` = `256`
  - `headroom.proxy.jev.request.build_retention_state(*, provider: str, model: str, jev_model: str, session_id: str, branch_id: str, revision: str, message_shape: str, total_messages: int, frozen_prefix: int, recent_tail: int, candidates: list[JevCandidate], max_candidate_tokens: int) -> dict[str, Any]`
  - `headroom.proxy.jev.request.build_questions(candidates: list[JevCandidate], total_messages: int) -> dict[str, dict[str, Any]]`
  - `headroom.proxy.jev.request.enforce_state_budget(candidates: list[JevCandidate], *, count_text: Callable[[str], int], make_payload: Callable[[list[JevCandidate], int], dict[str, Any]], max_candidate_tokens: int, max_state_tokens: int) -> tuple[list[JevCandidate], int, int]` returning `(kept, view_tokens_per_candidate, serialized_tokens)`

- [ ] **Step 1: Write the failing test**

```python
"""Retention view / questions shape, and the measured (not estimated) request budget."""

from __future__ import annotations

import json

from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.client import build_request_payload
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.request import (
    MIN_VIEW_TOKENS,
    build_questions,
    build_retention_state,
    enforce_state_budget,
)

CONFIG = JevConfig(mode="shadow", api_key="sk-test", model="jev-latest")


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _candidate(i: int, body: str) -> JevCandidate:
    return JevCandidate(
        candidate_id=f"cand_{i:04d}",
        message_index=i + 1,
        block_index=None,
        candidate_type="tool_result",
        role="tool",
        tool_call_id=f"call_{i}",
        content=body,
        est_tokens=_count_text(body),
    )


def test_retention_state_carries_identity_and_bounded_candidate_views() -> None:
    cands = [_candidate(0, "y" * 4000)]
    state = build_retention_state(
        provider="openai",
        model="gpt-5.6",
        jev_model="jev-latest",
        session_id="sess",
        branch_id="branch",
        revision="rev",
        message_shape="openai",
        total_messages=30,
        frozen_prefix=1,
        recent_tail=6,
        candidates=cands,
        max_candidate_tokens=100,  # -> 400 chars of view
    )
    assert state["session_id"] == "sess"
    assert state["branch_id"] == "branch"
    assert state["revision"] == "rev"
    assert state["protected_prefix_messages"] == 1
    assert state["recent_tail_excluded_messages"] == 6
    entry = state["candidates"][0]
    assert entry["candidate_id"] == "cand_0000"
    assert len(entry["content"]) == 400
    assert entry["content_truncated_for_view"] is True
    # The true size is still reported, so nobody is misled about what was shown.
    assert entry["estimated_tokens"] == 1000
    assert entry["content_sha256"] == cands[0].content_sha256


def test_questions_are_one_choice_per_candidate() -> None:
    questions = build_questions([_candidate(0, "body"), _candidate(1, "body")], 30)
    assert set(questions) == {"cand_0000", "cand_0001"}
    assert questions["cand_0000"]["type"] == "choice"
    assert set(questions["cand_0000"]["criteria"]) == {"keep", "truncate", "drop"}
    assert "cand_0000" in questions["cand_0000"]["instructions"]


def test_budget_trims_trailing_candidates_until_the_real_payload_fits() -> None:
    cands = [_candidate(i, "z" * 8000) for i in range(6)]

    def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, object]:
        state = build_retention_state(
            provider="openai",
            model="gpt-5.6",
            jev_model="jev-latest",
            session_id="s",
            branch_id="b",
            revision="r",
            message_shape="openai",
            total_messages=30,
            frozen_prefix=1,
            recent_tail=6,
            candidates=sel,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(CONFIG, state, build_questions(sel, 30))

    kept, view_tokens, serialized = enforce_state_budget(
        cands,
        count_text=_count_text,
        make_payload=make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=2000,
    )
    assert len(kept) < len(cands)
    assert view_tokens >= MIN_VIEW_TOKENS
    assert serialized <= 2000
    # The measured size is the size of the payload actually sent.
    assert _count_text(json.dumps(make_payload(kept, view_tokens), default=str)) == serialized


def test_budget_returns_nothing_when_even_one_candidate_will_not_fit() -> None:
    def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, object]:
        state = build_retention_state(
            provider="openai",
            model="gpt-5.6",
            jev_model="jev-latest",
            session_id="s",
            branch_id="b",
            revision="r",
            message_shape="openai",
            total_messages=30,
            frozen_prefix=1,
            recent_tail=6,
            candidates=sel,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(CONFIG, state, build_questions(sel, 30))

    kept, _view, _serialized = enforce_state_budget(
        [_candidate(0, "z" * 8000)],
        count_text=_count_text,
        make_payload=make_payload,
        max_candidate_tokens=20000,
        max_state_tokens=10,
    )
    assert kept == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_request.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.request'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/request.py`:

```python
"""The bounded retention view and the measured request budget.

Both halves are ported from ``benchmarks/jev_savings_spike.py`` after Phase 0a,
including the two bugs Codex forced fixes for:

* Jev rejects an oversized request outright with
  ``{"detail": {"error_type": "max_tokens_exceeded"}}`` — the *whole* call is
  lost, not just the overflow. ``max_candidate_tokens`` bounds each candidate
  individually and says nothing about the total, so :func:`enforce_state_budget`
  is the second, total bound.
* The bound is measured on the **actual serialized request**, not on candidate
  content alone. Candidate metadata, per-question instructions and the three
  criteria descriptions are a large fraction of the payload; a fixed
  per-candidate overhead constant underestimated them by ~50%. The first
  candidate is not exempt either — one oversized candidate on its own is exactly
  the request Jev would reject.

Trimming is honest: a trimmed candidate is still reported as a candidate and is
left untouched in the projection (effectively ``keep``), so TP never claims
savings on a candidate Jev was never asked about.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from headroom.proxy.jev.candidates import JevCandidate

#: Floor for the per-candidate content bound. Below this a candidate's view is
#: too thin for a retention decision to mean anything.
MIN_VIEW_TOKENS = 256

DECISION_CRITERIA: dict[str, str] = {
    "keep": (
        "This tool result still carries information the assistant is likely to "
        "need again later in the conversation; removing or shortening it would "
        "lose facts that are not recoverable from the surrounding messages."
    ),
    "truncate": (
        "Only the beginning / shape of this tool result matters from here on "
        "(a header, a count, the first few rows). The bulk of its body is "
        "redundant detail that can be cut without losing the thread."
    ),
    "drop": (
        "This tool result has been fully superseded, summarized by a later "
        "assistant message, or is simply no longer referenced. Removing it "
        "entirely would not change what the assistant can answer."
    ),
}

QUESTION_INSTRUCTIONS = (
    "Decide what to do with the historical tool result identified by this "
    "question's key ({cid}) in the `candidates` array of the state. It is "
    "message #{idx} of {total}, {from_end} messages from the end of the "
    "conversation, and costs about {tokens} tokens. Choose `keep` only if the "
    "content is genuinely still needed."
)

TASK_DESCRIPTION = (
    "These are historical tool results from an agent conversation that has "
    "already been deterministically compressed. Decide, per candidate, whether "
    "its content must still be kept verbatim, can be truncated to its first "
    "lines, or can be dropped entirely."
)


def build_retention_state(
    *,
    provider: str,
    model: str,
    jev_model: str,
    session_id: str,
    branch_id: str,
    revision: str,
    message_shape: str,
    total_messages: int,
    frozen_prefix: int,
    recent_tail: int,
    candidates: list[JevCandidate],
    max_candidate_tokens: int,
) -> dict[str, Any]:
    """The bounded retention view sent as the Jev ``state``."""
    # ~4 chars per token, the same approximation the Phase 0a corpus used.
    max_chars = max(1, max_candidate_tokens) * 4
    return {
        "provider": provider,
        "model": model,
        "jev_model": jev_model,
        "session_id": session_id,
        "branch_id": branch_id,
        "revision": revision,
        "message_shape": message_shape,
        "total_messages": total_messages,
        "protected_prefix_messages": frozen_prefix,
        "recent_tail_excluded_messages": recent_tail,
        "task": TASK_DESCRIPTION,
        "candidates": [
            {
                "candidate_id": cand.candidate_id,
                "candidate_type": cand.candidate_type,
                "role": cand.role,
                "tool_call_id": cand.tool_call_id,
                "message_index": cand.message_index,
                "block_index": cand.block_index,
                "order_from_end": total_messages - cand.message_index,
                "estimated_tokens": cand.est_tokens,
                "content_bytes": len(cand.content.encode("utf-8", "replace")),
                "content_sha256": cand.content_sha256,
                "content_truncated_for_view": len(cand.content) > max_chars,
                "content": cand.content[:max_chars],
            }
            for cand in candidates
        ],
    }


def build_questions(
    candidates: list[JevCandidate], total_messages: int
) -> dict[str, dict[str, Any]]:
    """One ``choice`` question per candidate, keyed by candidate id."""
    return {
        cand.candidate_id: {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS.format(
                cid=cand.candidate_id,
                idx=cand.message_index,
                total=total_messages,
                from_end=total_messages - cand.message_index,
                tokens=cand.est_tokens,
            ),
            "criteria": dict(DECISION_CRITERIA),
        }
        for cand in candidates
    }


def enforce_state_budget(
    candidates: list[JevCandidate],
    *,
    count_text: Callable[[str], int],
    make_payload: Callable[[list[JevCandidate], int], dict[str, Any]],
    max_candidate_tokens: int,
    max_state_tokens: int,
) -> tuple[list[JevCandidate], int, int]:
    """Fit the request inside Jev's input limit, measured not estimated.

    Returns ``(kept, view_tokens_per_candidate, serialized_tokens)``.
    """
    if not candidates:
        return [], max(MIN_VIEW_TOKENS, min(max_candidate_tokens, max_state_tokens)), 0

    share = max_state_tokens // max(1, len(candidates))
    view_bound = max(MIN_VIEW_TOKENS, min(max_candidate_tokens, share))

    def _size(sel: list[JevCandidate]) -> int:
        return count_text(json.dumps(make_payload(sel, view_bound), default=str))

    # Price each candidate by what it actually adds to the serialized payload.
    base = _size([])
    kept: list[JevCandidate] = []
    running = base
    for cand in candidates:
        delta = _size([cand]) - base
        if running + delta > max_state_tokens:
            break
        kept.append(cand)
        running += delta

    # Deltas miss a handful of separator tokens, so settle on the exact size of
    # the payload actually being sent: a measurement, not an estimate.
    while kept:
        exact = _size(kept)
        if exact <= max_state_tokens:
            return kept, view_bound, exact
        kept.pop()
    return [], view_bound, base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_request.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/request.py tests/test_jev_request.py
git commit -m "feat(jev): retention view, choice questions, and measured request budget"
```

---

### Task 7: Jev lifecycle counter on `PrometheusMetrics`

**Files:**
- Modify: `headroom/proxy/prometheus_metrics.py:172` (new counter beside `compression_quarantine_by_event`), `headroom/proxy/prometheus_metrics.py:364-368` (`reset_runtime`), `headroom/proxy/prometheus_metrics.py:577` (new method beside `record_compression_failed`), `headroom/proxy/prometheus_metrics.py:1434-1464` (`export`)
- Test: `tests/test_jev_metrics.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `headroom.proxy.prometheus_metrics.PrometheusMetrics.jev_events_by_event: dict[str, int]`
  - `headroom.proxy.prometheus_metrics.PrometheusMetrics.record_jev_event(self, event: str) -> None` — buckets by event name, empty/None becomes `"unknown"`, guarded by the existing `_obs_counter_lock`
  - Prometheus series `headroom_jev_events_total{event="..."}`

- [ ] **Step 1: Write the failing test**

```python
"""Jev lifecycle counters: every path out of the shadow hook lands in a bucket."""

from __future__ import annotations

from headroom.proxy.prometheus_metrics import PrometheusMetrics


def test_record_jev_event_buckets_by_event() -> None:
    metrics = PrometheusMetrics()

    metrics.record_jev_event("shadow_projected")
    metrics.record_jev_event("shadow_call_error")
    metrics.record_jev_event("shadow_call_error")

    assert metrics.jev_events_by_event["shadow_projected"] == 1
    assert metrics.jev_events_by_event["shadow_call_error"] == 2


def test_record_jev_event_empty_defaults_to_unknown() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("")
    assert metrics.jev_events_by_event["unknown"] == 1


async def test_jev_events_are_exported() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_fail_open")

    text = await metrics.export()

    assert "# TYPE headroom_jev_events_total counter" in text
    assert 'headroom_jev_events_total{event="shadow_fail_open"} 1' in text


async def test_reset_runtime_clears_jev_events() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_projected")

    await metrics.reset_runtime()

    assert dict(metrics.jev_events_by_event) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_metrics.py -q`
Expected: FAIL with "AttributeError: 'PrometheusMetrics' object has no attribute 'record_jev_event'"

- [ ] **Step 3: Write minimal implementation**

In `headroom/proxy/prometheus_metrics.py.__init__`, immediately after the `compression_quarantine_by_event` block (line 172) add:

```python
        # Jev retention (Track A shadow mode) lifecycle events, keyed by event
        # name. Every exit from the shadow hook lands in exactly one bucket —
        # including each fail-open — so a shadow deployment that silently never
        # calls Jev is visible as a counter, not only as an absent log line.
        self.jev_events_by_event: dict[str, int] = defaultdict(int)
```

In `reset_runtime`, inside the existing `with self._obs_counter_lock:` block (after `self.compression_quarantine_by_event.clear()` at line 368) add:

```python
                self.jev_events_by_event.clear()
```

Immediately after `record_compression_failed` (which ends at line 578) add:

```python
    def record_jev_event(self, event: str) -> None:
        """Record one Jev retention lifecycle event, bucketed by ``event``.

        Called from ``headroom/proxy/jev/shadow.py`` and
        ``headroom/proxy/jev/hook.py`` with names like ``shadow_below_threshold``,
        ``shadow_cooldown``, ``shadow_no_candidates``, ``shadow_call_attempted``,
        ``shadow_call_error``, ``shadow_stale_revision``, ``shadow_projected``
        and ``shadow_fail_open``. Guarded by ``_obs_counter_lock`` for the same
        reason as ``record_compression_failed``.
        """
        with self._obs_counter_lock:
            self.jev_events_by_event[event or "unknown"] += 1
```

In `export`, extend the existing snapshot inside `with self._obs_counter_lock:` (line 1434-1438) with:

```python
                jev_events = dict(self.jev_events_by_event)
```

and, after the `compression_failed` emission block (which ends with `lines.append("")` at line 1464), add:

```python
            if jev_events:
                lines.extend(
                    [
                        "# HELP headroom_jev_events_total Jev retention lifecycle events by event name; every shadow-hook exit, including fail-opens, is counted here",
                        "# TYPE headroom_jev_events_total counter",
                    ]
                )
                for event, count in jev_events.items():
                    lines.append(
                        f'headroom_jev_events_total{{event="{_escape_label_value(event)}"}} {count}'
                    )
                lines.append("")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_metrics.py tests/test_prometheus_obs_counters.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/prometheus_metrics.py tests/test_jev_metrics.py
git commit -m "feat(jev): headroom_jev_events_total lifecycle counter"
```

---

### Task 8: Shadow lifecycle runner

**Files:**
- Create: `headroom/proxy/jev/shadow.py`
- Modify: `headroom/proxy/server.py:890` (construct the runner beside `self.model_router`), `headroom/proxy/server.py:2224-2232` (close it in `shutdown`)
- Test: `tests/test_jev_shadow.py`

**Interfaces:**
- Consumes: `JevConfig` (Task 1), `JevIdentityStore` / `JevTurnIdentity` (Task 3), `JevClient` / `JevAnswer` (Task 4), `select_candidates` / `count_messages_corrected` / `RECENT_TAIL_EXCLUSION` (Task 5), `build_retention_state` / `build_questions` / `enforce_state_budget` (Task 6), `PrometheusMetrics.record_jev_event` (Task 7)
- Produces:
  - `headroom.proxy.jev.shadow.TRUNCATE_CHARS: int` = `400`
  - `headroom.proxy.jev.shadow.apply_decisions_to_copy(messages: list[dict[str, Any]], candidates: list[JevCandidate], decisions: dict[str, str]) -> list[dict[str, Any]]`
  - `@dataclass(frozen=True) headroom.proxy.jev.shadow.JevShadowResult` with fields `ran: bool`, `reason: str`, `identity: JevTurnIdentity | None = None`, `candidates: int = 0`, `candidates_sent: int = 0`, `keep: int = 0`, `truncate: int = 0`, `drop: int = 0`, `tokens_baseline: int = 0`, `tokens_headroom: int = 0`, `tokens_projected: int = 0`, `latency_ms: float = 0.0`, `error: str | None = None`; property `projected_savings: int`
  - `headroom.proxy.jev.shadow.JevShadowRunner` with `__init__(self, config: JevConfig, *, client: JevClient | None = None, identity_store: JevIdentityStore | None = None, metrics: Any | None = None)`, property `enabled: bool`, `async maybe_run(self, *, provider: str, model: str, messages: list[dict[str, Any]], frozen_prefix: int, optimized_tokens: int, original_tokens: int = 0, context_limit: int, session_id: str, count_text: Callable[[str], int], count_messages: Callable[[list[dict[str, Any]]], int], message_shape: str) -> JevShadowResult`, `async aclose(self) -> None`
  - `headroom.proxy.server.HeadroomProxy.jev_shadow: JevShadowRunner`

- [ ] **Step 1: Write the failing test**

```python
"""Shadow lifecycle: threshold, cooldown, one call per session/branch, stale
revision, fail-open, and above all: the forwarded messages are never mutated."""

from __future__ import annotations

import copy

from headroom.proxy.jev.client import JevAnswer
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.shadow import JevShadowRunner, apply_decisions_to_copy

CONFIG = JevConfig(
    mode="shadow",
    api_key="sk-test",
    threshold_percent=50,
    cooldown_turns=2,
    max_candidates=12,
    max_state_tokens=100000,
    max_candidate_tokens=20000,
)


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _count_messages(messages: list[dict[str, object]]) -> int:
    return sum(_count_text(str(m.get("content") or "")) for m in messages)


class FakeClient:
    """Stands in for JevClient with the same `decide` signature."""

    def __init__(self, decision: str = "drop", error: str | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls = 0

    async def decide(self, *, state, questions, candidate_ids) -> JevAnswer:
        self.calls += 1
        if self.error is not None:
            return JevAnswer(
                decisions={cid: "keep" for cid in candidate_ids}, error=self.error
            )
        return JevAnswer(decisions={cid: self.decision for cid in candidate_ids})

    async def aclose(self) -> None:
        return None


class FakeMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


def _messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c0", "content": "A" * 2000},
        {"role": "tool", "tool_call_id": "c1", "content": "B" * 2000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]


async def _run(runner: JevShadowRunner, messages=None, optimized_tokens=900, original_tokens=0):
    return await runner.maybe_run(
        provider="openai",
        model="gpt-5.6",
        messages=_messages() if messages is None else messages,
        frozen_prefix=1,
        optimized_tokens=optimized_tokens,
        original_tokens=original_tokens,
        context_limit=1000,
        session_id="sess-1",
        count_text=_count_text,
        count_messages=_count_messages,
        message_shape="openai",
    )


async def test_disabled_runner_does_nothing() -> None:
    client = FakeClient()
    runner = JevShadowRunner(JevConfig(), client=client)
    result = await _run(runner)
    assert runner.enabled is False
    assert result.ran is False
    assert result.reason == "disabled"
    assert client.calls == 0


async def test_below_threshold_is_recorded_and_skipped() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner, optimized_tokens=100)
    assert result.reason == "below_threshold"
    assert client.calls == 0
    assert "shadow_below_threshold" in metrics.events


async def test_projection_is_recorded_and_messages_are_never_mutated() -> None:
    client, metrics = FakeClient(decision="drop"), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    messages = _messages()
    before = copy.deepcopy(messages)

    result = await _run(runner, messages=messages)

    assert result.ran is True
    assert result.reason == "projected"
    assert result.candidates == 2
    assert result.candidates_sent == 2
    assert result.drop == 2
    assert result.projected_savings > 0
    assert result.tokens_projected < result.tokens_headroom
    assert messages == before  # the forwarded list is untouched
    assert "shadow_call_attempted" in metrics.events
    assert "shadow_projected" in metrics.events


async def test_the_pre_headroom_baseline_is_carried_through_for_the_dashboard() -> None:
    # T0 is measured by the handler, not here; the runner's job is to report it
    # beside its own TH/TP so /stats can show all three against one turn.
    runner = JevShadowRunner(CONFIG, client=FakeClient(decision="drop"))
    result = await _run(runner, original_tokens=4321)
    assert result.ran is True
    assert result.tokens_baseline == 4321
    # It is a passthrough, never a substitute for the runner's own measurement.
    assert result.tokens_headroom != 4321


async def test_cooldown_blocks_the_next_turns_on_the_same_branch() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)

    assert (await _run(runner)).ran is True
    assert (await _run(runner)).reason == "cooldown"
    assert (await _run(runner)).reason == "cooldown"
    assert (await _run(runner)).ran is True
    assert client.calls == 2
    assert metrics.events.count("shadow_cooldown") == 2


async def test_no_candidates_is_recorded_without_spending_a_call() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    messages = [{"role": "user", "content": "x" * 4000}] + [
        {"role": "assistant", "content": f"tail {i}"} for i in range(6)
    ]
    result = await _run(runner, messages=messages)
    assert result.reason == "no_candidates"
    assert client.calls == 0
    assert "shadow_no_candidates" in metrics.events


async def test_call_error_fails_open_with_a_metric() -> None:
    client, metrics = FakeClient(error="HTTP 500: boom"), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner)
    assert result.ran is False
    assert result.reason == "call_error"
    assert result.error == "HTTP 500: boom"
    assert result.tokens_projected == 0
    assert "shadow_call_error" in metrics.events


async def test_stale_revision_discards_the_projection() -> None:
    metrics = FakeMetrics()

    class RevisionRollingClient(FakeClient):
        def __init__(self, runner_box: dict) -> None:
            super().__init__(decision="drop")
            self.runner_box = runner_box

        async def decide(self, *, state, questions, candidate_ids):
            # The conversation moves on while the call is in flight.
            self.runner_box["runner"].identity_store.identify(
                session_id="sess-1",
                branch_root=[{"role": "system", "content": "sys"}],
                candidate_fingerprints=["moved-on"],
            )
            return await super().decide(
                state=state, questions=questions, candidate_ids=candidate_ids
            )

    box: dict = {}
    client = RevisionRollingClient(box)
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    box["runner"] = runner

    result = await _run(runner)
    assert result.ran is False
    assert result.reason == "stale_revision"
    assert "shadow_stale_revision" in metrics.events


def test_apply_decisions_to_copy_handles_drop_and_truncate() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _messages()
    cands = select_candidates(
        messages, frozen_prefix=1, count_text=_count_text, max_candidates=12
    )
    projected = apply_decisions_to_copy(
        messages, cands, {"cand_0000": "drop", "cand_0001": "truncate"}
    )
    assert len(projected) == len(messages) - 1
    truncated = [m for m in projected if m.get("tool_call_id") == "c1"][0]
    assert len(str(truncated["content"])) < 2000
    assert str(truncated["content"]).startswith("B" * 400)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_shadow.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.shadow'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/shadow.py`:

```python
"""Track A: shadow-mode retention measurement.

Runs AFTER Headroom's own deterministic compression and BEFORE anything is
forwarded. It answers one question — "how many more tokens would Jev's
retention decisions have saved on top of what Headroom already did" — and
answers it by measuring a projection (``TP``) on a private deep copy.

Hard invariant: this module never mutates the message list it is given, and its
return value is never applied to a forwarded request. Track B and Track C own
mutation; Track A does not.

Gates, in order (each cheaper than the next):

1. mode is ``shadow``
2. soft threshold — post-Headroom tokens vs ``threshold_percent`` of the model's
   context limit
3. per-(session, branch) cooldown, in turns
4. per-(session, branch) in-flight guard: one bounded call at a time
5. eligible candidates exist (a zero-candidate skip is recorded, not silent)
6. the request fits the measured state budget

Every exit records exactly one metric event.
"""

from __future__ import annotations

import contextlib
import json
import logging
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
from headroom.proxy.jev.identity import JevIdentityStore, JevTurnIdentity, branch_id_for
from headroom.proxy.jev.request import build_questions, build_retention_state, enforce_state_budget

logger = logging.getLogger(__name__)

#: What a ``truncate`` decision does to candidate content, in characters.
TRUNCATE_CHARS = 400

_TRUNCATION_NOTE = "\n…[truncated by Jev retention decision]"


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


def apply_decisions_to_copy(
    messages: list[dict[str, Any]],
    candidates: list[JevCandidate],
    decisions: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply keep/truncate/drop to a DEEP COPY. Nothing here is ever forwarded."""
    projected: list[dict[str, Any]] = json.loads(json.dumps(messages, default=str))

    drop_messages: set[int] = set()
    drop_blocks: set[tuple[int, int]] = set()

    for cand in candidates:
        decision = decisions.get(cand.candidate_id, "keep")
        if decision == "keep":
            continue

        if cand.block_index is None:
            msg = projected[cand.message_index]
            if decision == "drop":
                drop_messages.add(cand.message_index)
            elif decision == "truncate":
                key = "content" if msg.get("content") is not None else "output"
                original = text_of(msg.get(key, ""))
                msg[key] = original[:TRUNCATE_CHARS] + (
                    _TRUNCATION_NOTE if len(original) > TRUNCATE_CHARS else ""
                )
        else:
            block = projected[cand.message_index]["content"][cand.block_index]
            if decision == "drop":
                drop_blocks.add((cand.message_index, cand.block_index))
            elif decision == "truncate":
                original = text_of(block.get("content", ""))
                block["content"] = original[:TRUNCATE_CHARS] + (
                    _TRUNCATION_NOTE if len(original) > TRUNCATE_CHARS else ""
                )

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
    ) -> None:
        self._config = config
        self._client = client if client is not None else JevClient(config)
        self.identity_store = identity_store or JevIdentityStore()
        self._metrics = metrics
        self._turns_since_call: dict[tuple[str, str], int] = {}
        self._inflight: set[tuple[str, str]] = set()

    @property
    def enabled(self) -> bool:
        return self._config.is_shadow

    def _record(self, event: str) -> None:
        if self._metrics is None:
            return
        with contextlib.suppress(Exception):
            self._metrics.record_jev_event(event)

    def _skip(self, reason: str, *, event: str | None = None, **fields: Any) -> JevShadowResult:
        if event is not None:
            self._record(event)
        return JevShadowResult(ran=False, reason=reason, **fields)

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
        Jev-side failure (the client fails open)."""
        if not self.enabled:
            return JevShadowResult(ran=False, reason="disabled")
        if not messages:
            return self._skip("no_messages", event="shadow_no_messages")

        # 1. Soft threshold against the model's context window.
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

        # 4. Cooldown in turns. The first eligible turn on a branch always runs.
        since = self._turns_since_call.get(key)
        if since is not None and since < self._config.cooldown_turns:
            self._turns_since_call[key] = since + 1
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
            return build_request_payload(
                self._config, state, build_questions(sel, len(messages))
            )

        # 6. Measured request budget.
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
            self._turns_since_call[key] = 0

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
```

In `headroom/proxy/server.py`, immediately after `self.model_router = ModelRouter(config.model_router)` (line 890) add:

```python
        # Jev retention, Track A (shadow). Constructed unconditionally so the
        # handlers have one object to call; `enabled` is False unless
        # HEADROOM_JEV_MODE=shadow, and a disabled runner returns immediately
        # without touching the network.
        from headroom.proxy.jev.shadow import JevShadowRunner

        self.jev_shadow = JevShadowRunner(config.jev, metrics=self.metrics)
```

In `headroom/proxy/server.py.shutdown`, after the `self.http_client` close block (line 2232) add:

```python
        with contextlib.suppress(Exception):
            await self.jev_shadow.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_shadow.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/shadow.py headroom/proxy/server.py tests/test_jev_shadow.py
git commit -m "feat(jev): shadow lifecycle runner with threshold, cooldown and stale-revision gates"
```

---

### Task 9: Fail-open handler adapter

**Files:**
- Create: `headroom/proxy/jev/hook.py`
- Test: `tests/test_jev_hook.py`

**Interfaces:**
- Consumes: `JevShadowRunner` / `JevShadowResult` (Task 8), `PrometheusMetrics.record_jev_event` (Task 7)
- Produces:
  - `headroom.proxy.jev.hook.run_jev_shadow_hook(proxy: Any, *, provider: str, model: str, messages: list[dict[str, Any]] | None, frozen_prefix: int, optimized_tokens: int, original_tokens: int = 0, session_id: str, tokenizer: Any, message_shape: str, request_id: str, context_limit_source: Any) -> JevShadowResult | None` — a coroutine that **never raises**; returns `None` when Jev is off or anything at all goes wrong, recording `shadow_fail_open` in that case

- [ ] **Step 1: Write the failing test**

```python
"""The handler-facing adapter: one await, never raises, always leaves a metric."""

from __future__ import annotations

from typing import Any

from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.hook import run_jev_shadow_hook
from headroom.proxy.jev.shadow import JevShadowResult, JevShadowRunner


class FakeMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


class FakeTokenizer:
    def count_text(self, text: str) -> int:
        return max(1, len(text) // 4)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count_text(str(m.get("content") or "")) for m in messages)


class FakeLimitSource:
    def __init__(self, limit: int = 1000, raises: bool = False) -> None:
        self.limit = limit
        self.raises = raises

    def get_context_limit(self, model: str) -> int:
        if self.raises:
            raise RuntimeError("unknown model")
        return self.limit


class FakeProxy:
    def __init__(self, runner: Any, metrics: FakeMetrics) -> None:
        self.jev_shadow = runner
        self.metrics = metrics


async def _call(proxy: FakeProxy, limit_source: Any) -> JevShadowResult | None:
    return await run_jev_shadow_hook(
        proxy,
        provider="openai",
        model="gpt-5.6",
        messages=[{"role": "user", "content": "hi"}],
        frozen_prefix=0,
        optimized_tokens=10,
        original_tokens=40,
        session_id="sess",
        tokenizer=FakeTokenizer(),
        message_shape="openai",
        request_id="req-1",
        context_limit_source=limit_source,
    )


async def test_returns_none_when_jev_is_off_and_records_nothing() -> None:
    metrics = FakeMetrics()
    proxy = FakeProxy(JevShadowRunner(JevConfig(), metrics=metrics), metrics)
    assert await _call(proxy, FakeLimitSource()) is None
    assert metrics.events == []


async def test_returns_none_when_the_proxy_has_no_runner() -> None:
    metrics = FakeMetrics()
    proxy = FakeProxy(None, metrics)
    assert await _call(proxy, FakeLimitSource()) is None
    assert metrics.events == []


async def test_a_raising_context_limit_source_fails_open_with_a_metric() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(
        JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics
    )
    proxy = FakeProxy(runner, metrics)

    assert await _call(proxy, FakeLimitSource(raises=True)) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_the_baseline_reaches_the_runner() -> None:
    metrics = FakeMetrics()

    class CapturingRunner:
        enabled = True

        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def maybe_run(self, **kwargs: Any) -> JevShadowResult:
            self.kwargs = kwargs
            return JevShadowResult(ran=False, reason="captured")

    runner = CapturingRunner()
    proxy = FakeProxy(runner, metrics)
    assert (await _call(proxy, FakeLimitSource())).reason == "captured"
    # T0 is only measurable at the handler, so the hook has to carry it.
    assert runner.kwargs["original_tokens"] == 40
    assert runner.kwargs["optimized_tokens"] == 10


async def test_a_raising_runner_fails_open_with_a_metric() -> None:
    metrics = FakeMetrics()

    class ExplodingRunner:
        enabled = True

        async def maybe_run(self, **kwargs: Any) -> JevShadowResult:
            raise RuntimeError("boom")

    proxy = FakeProxy(ExplodingRunner(), metrics)
    assert await _call(proxy, FakeLimitSource()) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_a_successful_skip_is_passed_through() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(
        JevConfig(mode="shadow", api_key="sk-test", threshold_percent=99), metrics=metrics
    )
    proxy = FakeProxy(runner, metrics)

    result = await _call(proxy, FakeLimitSource())
    assert isinstance(result, JevShadowResult)
    assert result.reason == "below_threshold"
    assert metrics.events == ["shadow_below_threshold"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_hook.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.hook'"

- [ ] **Step 3: Write minimal implementation**

`headroom/proxy/jev/hook.py`:

```python
"""The single call the provider handlers make into Jev shadow mode.

The handlers get exactly one `await` and no error handling of their own. This
adapter owns the whole failure surface: an unknown model, a missing tokenizer
method, a runner bug, anything at all. Every one of those returns ``None`` and
records ``shadow_fail_open``, so a Track A regression shows up as a counter
rather than as a 500 on somebody's coding session.

The call IS on the request path and adds at most ``HEADROOM_JEV_TIMEOUT_MS``
(default 500ms) of latency, and only on the turns that pass the threshold and
cooldown gates. Nothing it returns is applied to the forwarded request.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from headroom.proxy.jev.shadow import JevShadowResult

logger = logging.getLogger(__name__)


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

    ``context_limit_source`` is the provider object the handler already holds
    (``proxy.anthropic_provider`` / ``proxy.openai_provider``); its
    ``get_context_limit(model)`` is called inside this function's try block so
    an unknown model can never raise at the call site.
    """
    runner = getattr(proxy, "jev_shadow", None)
    if runner is None or not getattr(runner, "enabled", False):
        return None
    if not messages:
        return None

    metrics = getattr(proxy, "metrics", None)
    try:
        context_limit = int(context_limit_source.get_context_limit(model))
        return await runner.maybe_run(
            provider=provider,
            model=model,
            messages=messages,
            frozen_prefix=max(0, int(frozen_prefix or 0)),
            optimized_tokens=max(0, int(optimized_tokens or 0)),
            original_tokens=max(0, int(original_tokens or 0)),
            context_limit=context_limit,
            session_id=session_id,
            count_text=tokenizer.count_text,
            count_messages=tokenizer.count_messages,
            message_shape=message_shape,
        )
    except Exception as exc:
        logger.warning(
            "[%s] jev shadow hook failed open: %s: %s", request_id, type(exc).__name__, exc
        )
        if metrics is not None:
            with contextlib.suppress(Exception):
                metrics.record_jev_event("shadow_fail_open")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/hook.py tests/test_jev_hook.py
git commit -m "feat(jev): fail-open shadow hook adapter for the provider handlers"
```

---

### Task 10: Call the shadow hook from the Anthropic and OpenAI handler paths

**Files:**
- Modify: `headroom/proxy/handlers/anthropic.py:2355-2356` (after the `post_compress` hook, before CCR tool injection)
- Modify: `headroom/proxy/handlers/openai.py:4050-4051` (`handle_openai_chat`, same position)
- Modify: `headroom/proxy/handlers/openai.py:6077-6078` (`handle_openai_responses`, after the waste-signal block, before the CCR section)
- Test: `tests/test_jev_shadow_wiring.py`

**Interfaces:**
- Consumes: `headroom.proxy.jev.hook.run_jev_shadow_hook` (Task 9)
- Produces: no new symbols. Three call sites, each passing the post-Headroom message list:
  - Anthropic Messages: `messages=optimized_messages`, `frozen_prefix=frozen_message_count` (`handlers/anthropic.py:1551`), `session_id=session_id` (`:1511`), `message_shape="anthropic"`
  - OpenAI Chat Completions: `messages=optimized_messages`, `frozen_prefix=openai_frozen_count` (`handlers/openai.py:3731`), `session_id=openai_session_id` (`:3683`), `message_shape="openai"`
  - OpenAI Responses: `messages=body["input"]`, `frozen_prefix=0`, `session_id=_responses_session_id` (`handlers/openai.py:5591`), `message_shape="openai_responses"`

- [ ] **Step 1: Write the failing test**

```python
"""The shadow hook is actually called from all three compressed-request paths.

These assert against the handler source rather than driving a full request:
booting a real Anthropic/OpenAI turn pulls in the whole compression pipeline,
while what can silently regress here is the *call site* -- someone refactoring
the post_compress region and dropping the hook. Behaviour is covered by
tests/test_jev_hook.py and tests/test_jev_shadow.py.
"""

from __future__ import annotations

import inspect

from headroom.proxy.handlers.anthropic import AnthropicHandlerMixin
from headroom.proxy.handlers.openai import OpenAIHandlerMixin


def test_anthropic_messages_calls_the_shadow_hook() -> None:
    source = inspect.getsource(AnthropicHandlerMixin.handle_anthropic_messages)
    assert "run_jev_shadow_hook(" in source
    assert 'message_shape="anthropic"' in source
    assert "messages=optimized_messages" in source
    # T0 for the /stats `jev` block: the baseline is in scope here and the
    # hook is the only place it can be joined to Jev's own numbers.
    assert "original_tokens=original_tokens" in source


def test_openai_chat_calls_the_shadow_hook() -> None:
    source = inspect.getsource(OpenAIHandlerMixin.handle_openai_chat)
    assert "run_jev_shadow_hook(" in source
    assert 'message_shape="openai"' in source
    assert "messages=optimized_messages" in source
    assert "original_tokens=original_tokens" in source


def test_openai_responses_calls_the_shadow_hook_on_the_input_items() -> None:
    source = inspect.getsource(OpenAIHandlerMixin.handle_openai_responses)
    assert "run_jev_shadow_hook(" in source
    assert 'message_shape="openai_responses"' in source
    assert 'body.get("input")' in source
    assert "original_tokens=original_tokens" in source


def test_no_call_site_assigns_from_the_hook() -> None:
    # Shadow mode must never feed anything back into the forwarded request.
    for fn in (
        AnthropicHandlerMixin.handle_anthropic_messages,
        OpenAIHandlerMixin.handle_openai_chat,
        OpenAIHandlerMixin.handle_openai_responses,
    ):
        source = inspect.getsource(fn)
        assert "= await run_jev_shadow_hook(" not in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_shadow_wiring.py -q`
Expected: FAIL with "assert 'run_jev_shadow_hook(' in source"

- [ ] **Step 3: Write minimal implementation**

In `headroom/proxy/handlers/anthropic.py`, replace the `except` tail of the `post_compress` hook (line 2354-2355):

```python
                except Exception as e:
                    logger.debug(f"[{request_id}] post_compress hook error: {e}")
```

with:

```python
                except Exception as e:
                    logger.debug(f"[{request_id}] post_compress hook error: {e}")

            # Jev retention, Track A (shadow). Runs AFTER Headroom's own
            # compression and BEFORE anything is forwarded: it records a
            # projection (TP) and a metric, and never mutates
            # `optimized_messages`. Default off; every failure path — including
            # an unknown model or a Jev timeout — fails open with a metric.
            from headroom.proxy.jev.hook import run_jev_shadow_hook

            await run_jev_shadow_hook(
                self,
                provider="anthropic",
                model=model,
                messages=optimized_messages,
                frozen_prefix=frozen_message_count,
                optimized_tokens=optimized_tokens,
                original_tokens=original_tokens,
                session_id=session_id,
                tokenizer=tokenizer,
                message_shape="anthropic",
                request_id=request_id,
                context_limit_source=self.anthropic_provider,
            )
```

In `headroom/proxy/handlers/openai.py` (`handle_openai_chat`), replace line 4049-4050:

```python
            except Exception as e:
                logger.debug(f"[{request_id}] post_compress hook error: {e}")
```

with:

```python
            except Exception as e:
                logger.debug(f"[{request_id}] post_compress hook error: {e}")

        # Jev retention, Track A (shadow) — see handlers/anthropic.py for the
        # contract. Observational only; `optimized_messages` is never mutated.
        from headroom.proxy.jev.hook import run_jev_shadow_hook

        await run_jev_shadow_hook(
            self,
            provider="openai",
            model=model,
            messages=optimized_messages,
            frozen_prefix=int(openai_frozen_count or 0),
            optimized_tokens=optimized_tokens,
            original_tokens=original_tokens,
            session_id=openai_session_id,
            tokenizer=tokenizer,
            message_shape="openai",
            request_id=request_id,
            context_limit_source=self.openai_provider,
        )
```

In `headroom/proxy/handlers/openai.py` (`handle_openai_responses`), replace the waste-signal block's tail (lines 6075-6078):

```python
                if _waste.total() > 0:
                    waste_signals_dict = _waste.to_dict()
            except Exception:
                pass
```

with:

```python
                if _waste.total() > 0:
                    waste_signals_dict = _waste.to_dict()
            except Exception:
                pass

        # Jev retention, Track A (shadow) on the Responses path. The
        # post-compression item list lives in body["input"] here (compression
        # goes through CompressionUnits and rewrites the body in place), not in
        # an `optimized_messages` variable, and this path keeps no frozen-prefix
        # bookkeeping — so the protected prefix is 0. Observational only.
        from headroom.proxy.jev.hook import run_jev_shadow_hook

        _jev_input = body.get("input")
        await run_jev_shadow_hook(
            self,
            provider="openai",
            model=str(model or ""),
            messages=_jev_input if isinstance(_jev_input, list) else None,
            frozen_prefix=0,
            optimized_tokens=optimized_tokens,
            original_tokens=original_tokens,
            session_id=_responses_session_id,
            tokenizer=tokenizer,
            message_shape="openai_responses",
            request_id=request_id,
            context_limit_source=self.openai_provider,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_shadow_wiring.py tests/test_jev_hook.py tests/test_jev_shadow.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/handlers/anthropic.py headroom/proxy/handlers/openai.py tests/test_jev_shadow_wiring.py
git commit -m "feat(jev): call the shadow hook from the Anthropic, Chat and Responses paths"
```

---

---

## Track B: Active Mode via the `/v1/compress` CCR Boundary

Track B is the first track that really mutates a forwarded conversation, and it only
does so on a turn the caller explicitly declared a compaction boundary. It builds on
Track A's package with no forked config, identity, client, candidate-selection or
request-budget logic.

**Consumed from Track A** — these are the signatures Tasks 1–10 actually ship, and
every Track B task below is written against them:

- `headroom.proxy.jev.config.JevConfig` (Task 1) — frozen dataclass with `mode: str`
  (`"off" | "shadow" | "active"`), `api_key: str`, `endpoint: str`, `model: str`,
  `timeout_ms: int`, `threshold_percent: int`, `cooldown_turns: int`,
  `max_candidate_tokens: int`, `max_candidates: int`, `max_state_tokens: int`;
  properties `enabled` / `is_shadow`; `validate() -> None`,
  `redacted() -> dict[str, object]`, classmethod `from_env(env=None) -> JevConfig`.
  Reachable as `ProxyConfig.jev` (Task 2).
- `headroom.proxy.jev.identity.revision_for(candidate_fingerprints: Sequence[str]) -> str`
  (Task 3).
- `headroom.proxy.jev.client.build_request_payload(config, state, questions) -> dict[str, Any]`,
  `JevClient(config, *, http_client=None)` with
  `async decide(*, state, questions, candidate_ids) -> JevAnswer` and
  `async aclose() -> None`; `JevAnswer` carries `decisions: dict[str, str]`
  (always fully populated; anything ambiguous is `"keep"`), `latency_ms: float`,
  `error: str | None`, `jev_model: str | None`, `usage`, `unparsed` (Task 4).
- `headroom.proxy.jev.candidates.JevCandidate` (Task 5) — frozen dataclass with
  `candidate_id`, `message_index`, `block_index`, `candidate_type`, `role`,
  `tool_call_id`, `content`, `est_tokens`, plus `content_sha256` and `fingerprint`
  as **properties**, not methods.
- `headroom.proxy.jev.candidates.select_candidates(messages, *, frozen_prefix: int, count_text: Callable[[str], int], max_candidates: int, recent_tail: int = RECENT_TAIL_EXCLUSION) -> list[JevCandidate]`
  and `RECENT_TAIL_EXCLUSION: int = 6` (Task 5). It takes the tokenizer's
  `count_text` callable, not a tokenizer object.
- `headroom.proxy.jev.candidates.count_messages_corrected(messages, *, count_messages: Callable[[list[dict[str, Any]]], int], count_text: Callable[[str], int]) -> int`
  (Task 5) — mandatory for any token accounting on Responses-shaped traffic.
- `headroom.proxy.jev.request.build_retention_state(*, provider: str, model: str, jev_model: str, session_id: str, branch_id: str, revision: str, message_shape: str, total_messages: int, frozen_prefix: int, recent_tail: int, candidates: list[JevCandidate], max_candidate_tokens: int) -> dict[str, Any]`,
  `build_questions(candidates, total_messages) -> dict[str, dict[str, Any]]`, and
  `enforce_state_budget(candidates, *, count_text, make_payload: Callable[[list[JevCandidate], int], dict[str, Any]], max_candidate_tokens, max_state_tokens) -> tuple[list[JevCandidate], int, int]`
  returning `(kept, view_tokens_per_candidate, serialized_tokens)` (Task 6). The
  `make_payload` callable must return the whole POST body, not just the state.
- `headroom.proxy.jev.shadow.TRUNCATE_CHARS: int = 400` (Task 8) — the truncation
  width Track A's projection uses; Track B matches it so `TP` and `TF` stay
  comparable.
- `PrometheusMetrics.record_jev_event(event: str) -> None` (Task 7) — backs
  `headroom_jev_events_total{event}`. Track B adds `active_*` event names to that
  same counter rather than introducing a second one.

### Task 11: `/v1/compress` compaction-boundary gate

**Files:**
- Create: `headroom/proxy/jev/compress_gate.py`
- Test: `tests/test_jev_compress_gate.py`

**Interfaces:**
- Consumes: nothing from Track A (pure validation of the request body; deliberately
  importable without the rest of the `jev` package so the handler can raise a 400 even
  when Jev is off).
- Produces:
  - `JEV_COMPRESS_BRANCH_ID: str = "compress"`
  - `class JevGateError(ValueError)` with attribute `message: str`
  - `parse_compaction_boundary(compress_config: dict[str, Any], mode: str | None) -> bool`
    — returns `True` only for `config.jev_compaction_boundary is True` together with
    `config.mode == "ccr"` and a non-empty `config.session_id`; returns `False` when the
    flag is absent/`None`/`False`; raises `JevGateError` otherwise.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compress_gate.py
"""Request-level gate for Jev active retention on POST /v1/compress.

The proven request shape (docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md,
"Track B") is: config.mode="ccr" + config.session_id + config.jev_compaction_boundary=true.
This gate is the only thing that opens the active path, so its rejections are part of
the route's public contract.
"""

from __future__ import annotations

import pytest

from headroom.proxy.jev.compress_gate import (
    JEV_COMPRESS_BRANCH_ID,
    JevGateError,
    parse_compaction_boundary,
)


def test_absent_flag_is_not_a_boundary() -> None:
    assert parse_compaction_boundary({}, "ccr") is False
    assert parse_compaction_boundary({"jev_compaction_boundary": None}, "ccr") is False
    assert parse_compaction_boundary({"jev_compaction_boundary": False}, None) is False


def test_proven_request_shape_is_a_boundary() -> None:
    config = {
        "mode": "ccr",
        "session_id": "caller-owned-session-id",
        "jev_compaction_boundary": True,
    }
    assert parse_compaction_boundary(config, "ccr") is True


def test_non_boolean_flag_is_rejected() -> None:
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary({"jev_compaction_boundary": "true"}, "ccr")
    assert "jev_compaction_boundary" in excinfo.value.message
    # An int 1 is not a JSON boolean either: accepting it would make a typo look
    # like consent to rewrite history.
    with pytest.raises(JevGateError):
        parse_compaction_boundary({"jev_compaction_boundary": 1}, "ccr")


@pytest.mark.parametrize("mode", [None, "lossy_inline", "lossless_then_lossy"])
def test_boundary_requires_ccr_mode(mode: str | None) -> None:
    config = {"jev_compaction_boundary": True, "session_id": "s1", "mode": mode}
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary(config, mode)
    assert 'config.mode="ccr"' in excinfo.value.message


@pytest.mark.parametrize("session_id", [None, "", "   ", 17])
def test_boundary_requires_session_id(session_id: object) -> None:
    config = {"jev_compaction_boundary": True, "mode": "ccr", "session_id": session_id}
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary(config, "ccr")
    assert "config.session_id" in excinfo.value.message


def test_branch_id_constant_is_stable() -> None:
    # Track A's identity store keys on (session_id, branch_id); compress turns
    # must not share a lane with proxy-path branches for the same session id.
    assert JEV_COMPRESS_BRANCH_ID == "compress"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_compress_gate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.proxy.jev.compress_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compress_gate.py
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
``HEADROOM_JEV_MODE=off``, which is the default.
"""

from __future__ import annotations

from typing import Any

#: Branch id recorded for every ``/v1/compress`` turn. The sidecar route has no
#: branch concept of its own, and Track A's identity store keys on
#: ``(session_id, branch_id)`` — a constant keeps compress turns in their own
#: lane instead of colliding with proxy-path branches for the same session id.
JEV_COMPRESS_BRANCH_ID = "compress"


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
            marker-free pipeline).

    Returns:
        True when active retention may run on this turn, False when the caller
        did not declare a boundary.

    Raises:
        JevGateError: the flag is present but the request cannot support
            retention. Every message names the field and says why.
    """
    raw = compress_config.get("jev_compaction_boundary", False)
    if raw is False or raw is None:
        return False
    if raw is not True:
        # `is not True` rather than a truthiness check: JSON `1`, `"true"` and
        # `[]` all arrive here, and silently reading them as consent to delete
        # tool output is exactly the failure this gate exists to prevent.
        raise JevGateError(
            f"Invalid config.jev_compaction_boundary: {raw!r}. Expected true or false."
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_compress_gate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compress_gate.py tests/test_jev_compress_gate.py
git commit -m "feat(jev): add the /v1/compress jev_compaction_boundary request gate"
```

---

### Task 12: retention lease primitive on the CCR store

**Files:**
- Modify: `headroom/cache/compression_store.py:598-632` (insert `extend_ttl` after
  `get_entry_status`, before `get_stats` at line 634)
- Test: `tests/test_ccr_retention_lease.py`

**Interfaces:**
- Consumes: the existing store API — `CompressionStore.store(original, compressed, *, ttl=None, explicit_hash=None, ...) -> str`,
  `CompressionStore.get_entry_status(hash_key, *, clean_expired=False) -> dict[str, Any]`,
  `CompressionStoreBackend.get/set`.
- Produces:
  - `CompressionStore.extend_ttl(hash_key: str, ttl: int) -> bool` — one-way TTL
    extension (never shortens); `True` when the entry exists, is unexpired and now holds
    a TTL of at least `ttl`; `False` when missing or expired; raises `ValueError` for a
    negative `ttl`.

This is the *smallest* addition that supports a retention lease: it goes on
`CompressionStore` and uses only `backend.get`/`backend.set`, so the
`CompressionStoreBackend` protocol (`headroom/cache/backends/base.py`) is unchanged and
`SQLiteBackend.set` (`headroom/cache/backends/sqlite.py:199-211`) rewrites the `ttl`
column for free. Single-worker scope: no renewal loop, no cross-worker lease table.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ccr_retention_lease.py
"""Retention lease: one-way TTL extension on a CCR entry.

Jev active retention (Track B) removes a tool result from the forwarded
conversation and leaves a `Retrieve original: hash=` marker behind. That entry is
then the ONLY copy of the content, so it must outlive the 30-minute default TTL
an ordinary compression entry gets.
"""

from __future__ import annotations

import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore


def _store() -> CompressionStore:
    return CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())


def test_extend_ttl_lengthens_a_live_entry() -> None:
    store = _store()
    hash_key = store.store("the original tool output", "compressed")
    assert store.extend_ttl(hash_key, 86_400) is True
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 86_400


def test_extend_ttl_never_shortens() -> None:
    store = _store()
    hash_key = store.store("the original tool output", "compressed", ttl=86_400)
    assert store.extend_ttl(hash_key, 60) is True
    # A later ordinary re-store must not be able to shrink a lease that
    # retention already took, or the marker outlives its content.
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 86_400


def test_extend_ttl_reports_missing_entry() -> None:
    store = _store()
    assert store.extend_ttl("deadbeefdeadbeefdeadbeef", 86_400) is False


def test_extend_ttl_reports_expired_entry() -> None:
    store = _store()
    hash_key = store.store("the original tool output", "compressed", ttl=0)
    # ttl=0 means already expired on the next read: the lease must fail so the
    # caller keeps the original content instead of dropping it.
    assert store.extend_ttl(hash_key, 86_400) is False


def test_extend_ttl_rejects_negative() -> None:
    store = _store()
    hash_key = store.store("the original tool output", "compressed")
    with pytest.raises(ValueError):
        store.extend_ttl(hash_key, -1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ccr_retention_lease.py -q`
Expected: FAIL with `AttributeError: 'CompressionStore' object has no attribute 'extend_ttl'`

- [ ] **Step 3: Write minimal implementation**

Insert into `headroom/cache/compression_store.py` immediately after `get_entry_status`
ends (line 632) and before `def get_stats` (line 634):

```python
    def extend_ttl(self, hash_key: str, ttl: int) -> bool:
        """Extend a live entry's TTL to at least ``ttl`` seconds (retention lease).

        Jev active retention drops a tool result out of the forwarded
        conversation and leaves a ``Retrieve original: hash=`` marker in its
        place. That entry then holds the only copy of the content, so it has to
        outlive the session-scale default TTL an ordinary compression entry
        gets (where the original is still sitting in the caller's transcript).

        Extension is ONE-WAY: a ``ttl`` shorter than the entry already holds is
        ignored. Ordinary compression re-stores the same content on every turn a
        marker is re-encountered, and letting one of those shorten a lease that
        retention took would expire the entry while its marker is still in the
        conversation — a guaranteed 404 on ``/v1/retrieve`` with no copy left
        anywhere.

        Args:
            hash_key: Key returned by :meth:`store`.
            ttl: Minimum TTL in seconds the entry must hold afterwards.

        Returns:
            True when the entry exists, is not expired, and now holds a TTL of
            at least ``ttl``. False when the entry is missing or already
            expired — the caller must then keep the original content.

        Raises:
            ValueError: ``ttl`` is negative.
        """
        if ttl < 0:
            raise ValueError(f"ttl must be non-negative, got {ttl!r}")
        with self._lock:
            entry = self._backend.get(hash_key)
            if entry is None or entry.is_expired():
                return False
            if entry.ttl < ttl:
                entry.ttl = ttl
                # Write back through the backend: SQLiteBackend.set refreshes
                # the `ttl` column the purge query reads, so the lease survives
                # a restart as well as an opportunistic purge.
                self._backend.set(hash_key, entry)
            return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ccr_retention_lease.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/cache/compression_store.py tests/test_ccr_retention_lease.py
git commit -m "feat(jev): add CompressionStore.extend_ttl as the retention lease primitive"
```

---

### Task 13: Shared CCR retention sequence — write, acknowledge, bind, lease

**Files:**
- Create: `headroom/proxy/jev/retention_ccr.py`
- Test: `tests/test_jev_retention_ccr.py`

**Interfaces:**
- Consumes: `CompressionStore.store(...) -> str`, `CompressionStore.retrieve(hash_key, query=None) -> CompressionEntry | None`,
  `CompressionStore.extend_ttl(hash_key, ttl) -> bool` (Task 12).
- Produces:
  - `JEV_RETENTION_LEASE_SECONDS: int = 86_400`
  - `candidate_retention_hash(session_id: str, branch_id: str, content: str) -> str`
    — 24 hex chars, bound to session + branch + content.
  - `retention_marker(hash_key: str, *, original_tokens: int = 0) -> str` — the
    replacement text; matches BOTH the handler's `_CCR_HASH_RE`
    (`headroom/proxy/handlers/openai.py:116-118`) and
    `CCRToolInjector.scan_for_markers` (`headroom/ccr/tool_injection.py:237-265`),
    so `/v1/retrieve`, the response's `ccr_hashes` and the injected
    `headroom_retrieve` tool all resolve it.
  - `@dataclass(frozen=True) class RetentionLease` with `candidate_id: str`,
    `hash_key: str`, `marker: str`, `original_tokens: int`, `lease_seconds: int`.
  - `stage_retention(store: CompressionStore, *, candidate_id: str, session_id: str, branch_id: str, content: str, tool_name: str | None, tool_call_id: str | None, original_tokens: int, lease_seconds: int = JEV_RETENTION_LEASE_SECONDS) -> RetentionLease | None`
    — performs write → acknowledged read-back → lease, returning `None` on any failure
    (the caller then keeps that candidate's original content).

This is the single CCR staging sequence for **both** active tracks. Track B stages
every candidate of a `/v1/compress` boundary turn here, and Track C's Codex
WebSocket boundary (Task 25) stages the one candidate it carries through the same
function with `branch_id=boundary.previous_response_id`. There is deliberately no
second single-candidate CCR module: the write → acknowledge → bind → lease ordering
is the safety contract, and two copies of it would drift.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_retention_ccr.py
"""The CCR safety sequence for Jev active retention.

Contract (design doc, "Track B: Active Mode via /v1/compress CCR"): write the
original to CCR -> require an ACKNOWLEDGED success -> bind to
session/branch/candidate hash + retention lease -> commit. Any failed step keeps
the original, so every failure here must return None rather than raise.
"""

from __future__ import annotations

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev.retention_ccr import (
    JEV_RETENTION_LEASE_SECONDS,
    candidate_retention_hash,
    retention_marker,
    stage_retention,
)


def _store() -> CompressionStore:
    return CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())


def _stage(store: CompressionStore, content: str = "ORIGINAL TOOL OUTPUT"):
    return stage_retention(
        store,
        candidate_id="cand_0000",
        session_id="s1",
        branch_id="compress",
        content=content,
        tool_name="get_items",
        tool_call_id="call_1",
        original_tokens=4096,
    )


def test_hash_is_bound_to_session_and_branch() -> None:
    same = candidate_retention_hash("s1", "compress", "payload")
    assert same == candidate_retention_hash("s1", "compress", "payload")
    assert same != candidate_retention_hash("s2", "compress", "payload")
    assert same != candidate_retention_hash("s1", "other", "payload")
    assert same != candidate_retention_hash("s1", "compress", "other payload")
    assert len(same) == 24
    assert all(c in "0123456789abcdef" for c in same)


def test_marker_is_resolvable_by_both_marker_scanners() -> None:
    from headroom.ccr.tool_injection import CCRToolInjector
    from headroom.proxy.handlers.openai import _CCR_HASH_RE

    hash_key = candidate_retention_hash("s1", "compress", "payload")
    marker = retention_marker(hash_key, original_tokens=812)
    assert _CCR_HASH_RE.findall(marker) == [hash_key]
    # The same marker must also make the proxy inject `headroom_retrieve`: a
    # marker the model cannot redeem is silent data loss, not compression.
    injector = CCRToolInjector(inject_tool=False, inject_system_instructions=False)
    injector.scan_for_markers([{"role": "user", "content": marker}])
    assert injector.detected_hashes == [hash_key]


def test_successful_stage_writes_acknowledges_and_leases() -> None:
    store = _store()
    lease = _stage(store)
    assert lease is not None
    assert lease.candidate_id == "cand_0000"
    assert lease.hash_key == candidate_retention_hash("s1", "compress", "ORIGINAL TOOL OUTPUT")
    assert lease.marker == retention_marker(lease.hash_key, original_tokens=4096)
    assert lease.lease_seconds == JEV_RETENTION_LEASE_SECONDS
    # Acknowledged: the bytes are readable back, under the bound hash.
    entry = store.retrieve(lease.hash_key)
    assert entry is not None
    assert entry.original_content == "ORIGINAL TOOL OUTPUT"
    # Leased: the entry outlives the store's 60s default.
    assert store.get_entry_status(lease.hash_key)["ttl_seconds"] == JEV_RETENTION_LEASE_SECONDS


def test_unacknowledged_write_returns_none() -> None:
    """A store that swallows the write must NOT yield a lease.

    CompressionStore.store() returns the hash even when it refuses to persist
    (e.g. a bare CCR marker as `original`), so the return value alone is not an
    acknowledgement — only a read-back is.
    """
    store = _store()
    lease = _stage(store, content="<<ccr:abc123abc123>>")
    assert lease is None


def test_store_failure_returns_none_and_does_not_raise() -> None:
    class Exploding(CompressionStore):
        def store(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("disk on fire")

    lease = _stage(Exploding(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None


def test_lease_failure_returns_none() -> None:
    class NoLease(CompressionStore):
        def extend_ttl(self, hash_key: str, ttl: int) -> bool:  # type: ignore[override]
            return False

    lease = _stage(NoLease(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_retention_ccr.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.proxy.jev.retention_ccr'`

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/retention_ccr.py
"""The CCR safety sequence for Jev active retention (Track B).

The ORDER is the contract, not an implementation detail:

1. write the ORIGINAL content to the CCR store under a hash bound to
   ``(session_id, branch_id, content)``,
2. require an ACKNOWLEDGED success — read the entry back and compare bytes,
3. take a retention lease by extending that entry's TTL,
4. only then may the caller rewrite the conversation.

Any step that fails returns ``None`` and the caller keeps that candidate's
original content untouched. Nothing in this module mutates a conversation, and
nothing here raises: a retention failure is a missed saving, never a dropped
tool result.

Single-worker scope (design doc, "Non-Goals"): no cross-worker lease renewal, no
shared admission ledger. The lease is one TTL extension on the entry the marker
points at.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from headroom.cache.compression_store import CompressionStore

logger = logging.getLogger(__name__)

#: How long a retained original must stay retrievable. The store's default TTL
#: (30 minutes) assumes the caller still holds the content in its own
#: transcript; after active retention it does not, so the entry is the only
#: copy and has to outlive the agent session that produced it.
JEV_RETENTION_LEASE_SECONDS = 86_400

#: Namespaces the hash so a retention key can never collide with an ordinary
#: content-addressed compression key for the same bytes.
_RETENTION_HASH_VERSION = "jev-retention-v1"


def candidate_retention_hash(session_id: str, branch_id: str, content: str) -> str:
    """Storage key for one retained candidate, bound to its session and branch.

    Binding matters: two sessions can legitimately hold byte-identical tool
    output, and a bare content hash would let session B's entry — and its lease,
    and its TTL — stand in for session A's. 24 hex chars is the same width
    ``CompressionStore``'s own default key uses and sits inside the 12–24 range
    the handler's marker scanner accepts.
    """
    digest = hashlib.sha256()
    for part in (_RETENTION_HASH_VERSION, session_id, branch_id, content):
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")  # unambiguous field separator
    return digest.hexdigest()[:24]


def retention_marker(hash_key: str, *, original_tokens: int = 0) -> str:
    """The text that replaces a retained tool result.

    ONE marker shape for both active tracks, and it has to satisfy two
    independent scanners:

    * the handler's ``_CCR_HASH_RE`` (``Retrieve more|original: hash=<hex>``
      followed by a non-hex character or end-of-string), which is what fills the
      response's ``ccr_hashes`` and what ``/v1/retrieve`` resolves;
    * ``CCRToolInjector.scan_for_markers``, which is what decides whether the
      ``headroom_retrieve`` tool gets injected at all. A marker that matches the
      first but not the second hands the model a pointer it has no tool to
      redeem — silent data loss, not compression (issue #1006).

    The bracketed ``[N tokens compressed to 0. … Retrieve more: hash=…]`` shape
    satisfies both, and matches what every other Headroom compressor emits.
    """
    measured = f"{original_tokens} tokens" if original_tokens > 0 else "Tool output"
    return (
        f"[{measured} compressed to 0. The original was withheld to free "
        "context; call headroom_retrieve to read it in full. "
        f"Retrieve more: hash={hash_key}]"
    )


@dataclass(frozen=True)
class RetentionLease:
    """A committed, acknowledged, leased retention of one candidate."""

    candidate_id: str
    hash_key: str
    marker: str
    original_tokens: int
    lease_seconds: int


def stage_retention(
    store: CompressionStore,
    *,
    candidate_id: str,
    session_id: str,
    branch_id: str,
    content: str,
    tool_name: str | None,
    tool_call_id: str | None,
    original_tokens: int,
    lease_seconds: int = JEV_RETENTION_LEASE_SECONDS,
) -> RetentionLease | None:
    """Run the full write → acknowledge → bind → lease sequence for one candidate.

    Returns the lease when every step succeeded, otherwise ``None``.
    """
    hash_key = candidate_retention_hash(session_id, branch_id, content)
    marker = retention_marker(hash_key, original_tokens=original_tokens)

    # 1. Write the original.
    try:
        returned = store.store(
            content,
            marker,
            original_tokens=original_tokens,
            original_item_count=1,
            compressed_item_count=1,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            compression_strategy="jev_retention",
            ttl=lease_seconds,
            explicit_hash=hash_key,
        )
    except Exception as exc:  # noqa: BLE001 - any store failure keeps the original
        logger.warning(
            "jev retention: CCR write failed for candidate %s (%s: %s); keeping original",
            candidate_id,
            type(exc).__name__,
            exc,
        )
        return None
    if returned != hash_key:
        logger.warning(
            "jev retention: CCR write returned %r, expected %r; keeping original",
            returned,
            hash_key,
        )
        return None

    # 2. Require an ACKNOWLEDGED success. The return value above is not one:
    #    CompressionStore.store() also returns the hash on the path where it
    #    refuses to persist (a bare CCR marker as `original`), and a transient
    #    SQLite error is swallowed inside the backend. Only reading the bytes
    #    back proves the content is retrievable.
    try:
        entry = store.retrieve(hash_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jev retention: CCR read-back failed for candidate %s (%s: %s); keeping original",
            candidate_id,
            type(exc).__name__,
            exc,
        )
        return None
    if entry is None or entry.original_content != content:
        logger.warning(
            "jev retention: CCR write for candidate %s was not acknowledged "
            "(hash=%s present=%s); keeping original",
            candidate_id,
            hash_key,
            entry is not None,
        )
        return None

    # 3. Take the lease.
    try:
        leased = store.extend_ttl(hash_key, lease_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jev retention: lease failed for candidate %s (%s: %s); keeping original",
            candidate_id,
            type(exc).__name__,
            exc,
        )
        return None
    if not leased:
        logger.warning(
            "jev retention: lease refused for candidate %s (hash=%s); keeping original",
            candidate_id,
            hash_key,
        )
        return None

    return RetentionLease(
        candidate_id=candidate_id,
        hash_key=hash_key,
        marker=marker,
        original_tokens=original_tokens,
        lease_seconds=lease_seconds,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_retention_ccr.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/retention_ccr.py tests/test_jev_retention_ccr.py
git commit -m "feat(jev): add the CCR write/acknowledge/bind/lease sequence for active retention"
```

---

### Task 14: applying a retention decision to both wire shapes

**Files:**
- Create: `headroom/proxy/jev/retention_apply.py`
- Test: `tests/test_jev_retention_apply.py`

**Interfaces:**
- Consumes: `JevCandidate` (Track A), `RetentionLease` (Task 13).
- Produces:
  - `JEV_TRUNCATE_CHARS: int` — aliased to Track A's `shadow.TRUNCATE_CHARS` (400)
  - `apply_retention(messages: list[dict[str, Any]], candidates: Sequence[JevCandidate], decisions: Mapping[str, str], leases: Mapping[str, RetentionLease], *, truncate_chars: int = JEV_TRUNCATE_CHARS) -> tuple[list[dict[str, Any]], list[str]]`
    — returns `(new_messages, applied_candidate_ids)`; deep-copies, never mutates the
    input; skips any candidate without a lease; supports `role="tool"` messages,
    `function_call_output` / `custom_tool_call_output` items and Anthropic
    `tool_result` blocks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_retention_apply.py
"""Applying keep/truncate/drop to the forwarded conversation.

Unlike the shadow projection (which deletes messages on a private copy), active
mode REPLACES content with a CCR retrieval marker and never removes a message:
removing a tool result orphans its tool_call and the provider rejects the turn.
"""

from __future__ import annotations

from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.retention_apply import apply_retention
from headroom.proxy.jev.retention_ccr import RetentionLease


def _lease(candidate_id: str, hash_key: str) -> RetentionLease:
    return RetentionLease(
        candidate_id=candidate_id,
        hash_key=hash_key,
        marker=f"Retrieve original: hash={hash_key}",
        original_tokens=100,
        lease_seconds=86_400,
    )


def _candidate(candidate_id, message_index, candidate_type, content, block_index=None):
    return JevCandidate(
        candidate_id=candidate_id,
        message_index=message_index,
        block_index=block_index,
        candidate_type=candidate_type,
        role="tool",
        tool_call_id="call_1",
        content=content,
        est_tokens=100,
    )


def test_drop_replaces_openai_chat_tool_content_with_the_marker() -> None:
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"},
    ]
    cand = _candidate("c0", 1, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(
        messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "a" * 24)}
    )
    assert applied == ["c0"]
    assert out[1]["content"] == f"Retrieve original: hash={'a' * 24}"
    assert out[1]["tool_call_id"] == "call_1"  # the message itself survives
    assert len(out) == len(messages)
    assert messages[1]["content"] == "BIG OUTPUT"  # input untouched


def test_truncate_keeps_a_head_and_appends_the_marker() -> None:
    messages = [{"role": "tool", "tool_call_id": "call_1", "content": "X" * 5000}]
    cand = _candidate("c0", 0, "tool_result", "X" * 5000)
    out, applied = apply_retention(
        messages, [cand], {"c0": "truncate"}, {"c0": _lease("c0", "b" * 24)}, truncate_chars=10
    )
    assert applied == ["c0"]
    assert out[0]["content"].startswith("X" * 10)
    assert out[0]["content"].endswith(f"Retrieve original: hash={'b' * 24}")
    assert "X" * 11 not in out[0]["content"]


def test_keep_changes_nothing() -> None:
    messages = [{"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(
        messages, [cand], {"c0": "keep"}, {"c0": _lease("c0", "c" * 24)}
    )
    assert applied == []
    assert out == messages


def test_candidate_without_a_lease_is_never_applied() -> None:
    """A failed CCR step keeps the original — this is the last line of that rule."""
    messages = [{"role": "tool", "tool_call_id": "call_1", "content": "BIG OUTPUT"}]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(messages, [cand], {"c0": "drop"}, {})
    assert applied == []
    assert out == messages


def test_responses_output_slots_are_supported() -> None:
    messages = [
        {"type": "function_call_output", "call_id": "call_1", "output": "FN OUT"},
        {"type": "custom_tool_call_output", "call_id": "call_2", "output": "CUSTOM OUT"},
    ]
    cands = [
        _candidate("c0", 0, "function_call_output", "FN OUT"),
        _candidate("c1", 1, "custom_tool_call_output", "CUSTOM OUT"),
    ]
    out, applied = apply_retention(
        messages,
        cands,
        {"c0": "drop", "c1": "drop"},
        {"c0": _lease("c0", "d" * 24), "c1": _lease("c1", "e" * 24)},
    )
    assert applied == ["c0", "c1"]
    assert out[0]["output"] == f"Retrieve original: hash={'d' * 24}"
    assert out[1]["output"] == f"Retrieve original: hash={'e' * 24}"


def test_anthropic_tool_result_block_is_supported() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here you go"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "BLOCK OUTPUT"},
            ],
        }
    ]
    cand = _candidate("c0", 0, "tool_result", "BLOCK OUTPUT", block_index=1)
    out, applied = apply_retention(
        messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "f" * 24)}
    )
    assert applied == ["c0"]
    assert out[0]["content"][0] == {"type": "text", "text": "here you go"}
    assert out[0]["content"][1]["content"] == f"Retrieve original: hash={'f' * 24}"
    assert out[0]["content"][1]["tool_use_id"] == "tu_1"


def test_stale_candidate_index_is_skipped() -> None:
    """A candidate whose slot no longer holds its content is left alone."""
    messages = [{"role": "tool", "tool_call_id": "call_1", "content": "SOMETHING ELSE"}]
    cand = _candidate("c0", 0, "tool_result", "BIG OUTPUT")
    out, applied = apply_retention(
        messages, [cand], {"c0": "drop"}, {"c0": _lease("c0", "0" * 24)}
    )
    assert applied == []
    assert out == messages
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_retention_apply.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.proxy.jev.retention_apply'`

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/retention_apply.py
"""Apply a Jev retention decision to the messages the caller will forward.

Two rules separate this from the shadow projection:

* **Nothing is ever removed.** Shadow deletes messages on a private copy to
  measure a projection; active mode hands the result back to a caller who
  forwards it to a provider, and a removed ``role="tool"`` message orphans its
  ``tool_call`` (a 400 from both OpenAI and Anthropic). So ``drop`` replaces the
  CONTENT with a CCR retrieval marker and leaves the envelope intact.
* **Only leased candidates are applied.** A candidate without an acknowledged,
  leased CCR entry keeps its original content, no matter what Jev answered.

The three wire shapes handled here are the ones Phase 0 actually observed:
OpenAI Chat ``role="tool"`` messages, Responses ``function_call_output`` and
``custom_tool_call_output`` items, and Anthropic ``tool_result`` blocks.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.retention_ccr import RetentionLease
from headroom.proxy.jev.shadow import TRUNCATE_CHARS

#: How much of a truncated candidate stays inline. Deliberately the SAME width
#: Track A's shadow projection truncates at
#: (``headroom.proxy.jev.shadow.TRUNCATE_CHARS``). The dashboard compares TP
#: (Track A's projection) against TF (what active mode actually realized), so a
#: different truncation width here would make the projection systematically
#: over- or under-report the very savings it exists to predict.
JEV_TRUNCATE_CHARS = TRUNCATE_CHARS

#: The Responses item types whose payload lives in ``output`` rather than
#: ``content``. ``custom_tool_call_output`` is a real wire type Phase 0b
#: observed and the original plan's vocabulary missed.
_OUTPUT_SLOT_TYPES = ("function_call_output", "custom_tool_call_output")


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _slot_key(candidate_type: str) -> str:
    return "output" if candidate_type in _OUTPUT_SLOT_TYPES else "content"


def apply_retention(
    messages: list[dict[str, Any]],
    candidates: Sequence[JevCandidate],
    decisions: Mapping[str, str],
    leases: Mapping[str, RetentionLease],
    *,
    truncate_chars: int = JEV_TRUNCATE_CHARS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(new_messages, applied_candidate_ids)``.

    ``messages`` is never mutated: the caller may still have to fall back to it
    if anything downstream fails.
    """
    out = copy.deepcopy(messages)
    applied: list[str] = []

    for cand in candidates:
        decision = decisions.get(cand.candidate_id, "keep")
        if decision not in ("truncate", "drop"):
            continue
        lease = leases.get(cand.candidate_id)
        if lease is None:
            # A failed CCR step keeps the original. This is the last place that
            # rule can still be enforced, so it is enforced here too.
            continue
        if not (0 <= cand.message_index < len(out)):
            continue
        message = out[cand.message_index]
        if not isinstance(message, dict):
            continue

        key = _slot_key(cand.candidate_type)
        if cand.block_index is None:
            slot_owner: dict[str, Any] = message
        else:
            blocks = message.get("content")
            if not isinstance(blocks, list) or not (0 <= cand.block_index < len(blocks)):
                continue
            block = blocks[cand.block_index]
            if not isinstance(block, dict):
                continue
            slot_owner = block
            key = "content"

        current = _text_of(slot_owner.get(key, ""))
        if current != cand.content:
            # The slot moved or was rewritten between selection and here. The
            # lease is bound to the content we hashed, so applying it to
            # different bytes would point the marker at the wrong original.
            continue

        if decision == "drop":
            slot_owner[key] = lease.marker
        else:
            slot_owner[key] = f"{current[:truncate_chars]}\n…{lease.marker}"
        applied.append(cand.candidate_id)

    return out, applied
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_retention_apply.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/retention_apply.py tests/test_jev_retention_apply.py
git commit -m "feat(jev): apply retention decisions to chat, responses and anthropic shapes"
```

---

### Task 15: active-retention decision (reuses Track A's selection + client)

**Files:**
- Create: `headroom/proxy/jev/active.py`
- Test: `tests/test_jev_active_decision.py`

**Interfaces:**
- Consumes (Track A, exact signatures at the top of this track): `JevConfig`,
  `JevCandidate`, `select_candidates(messages, *, frozen_prefix, count_text, max_candidates)`,
  `RECENT_TAIL_EXCLUSION`, `revision_for(fingerprints)`,
  `build_retention_state(*, provider, model, jev_model, session_id, branch_id, revision, message_shape, total_messages, frozen_prefix, recent_tail, candidates, max_candidate_tokens)`,
  `build_questions(candidates, total_messages)`,
  `enforce_state_budget(candidates, *, count_text, make_payload, max_candidate_tokens, max_state_tokens)`,
  `build_request_payload(config, state, questions)`,
  `JevClient.decide(*, state, questions, candidate_ids) -> JevAnswer`;
  `headroom.tokenizers.get_tokenizer(model) -> TokenCounter` (exposes
  `count_text` / `count_messages`).
- Produces:
  - `@dataclass(frozen=True) class JevActiveDecision` with `candidates: list[JevCandidate]`,
    `decisions: dict[str, str]`, `called: bool`, `error: str | None`, `latency_ms: float`
  - `async decide_active_retention(*, config: JevConfig, client: Any, messages: list[dict[str, Any]], frozen_prefix: int, model: str, session_id: str, branch_id: str, provider: str = "compress", message_shape: str = "openai") -> JevActiveDecision`
    — every candidate is present in `decisions`, defaulting to `"keep"`.

No duplicated selection logic: the 6-message tail exclusion, the frozen-prefix rule, the
`function_call_output` + `custom_tool_call_output` vocabulary and the token-accounting
fix all stay in Track A's `candidates.py`.

No mode-private request bounds either: `config.max_candidates`
(`HEADROOM_JEV_MAX_CANDIDATES`) and `config.max_state_tokens`
(`HEADROOM_JEV_MAX_STATE_TOKENS`) are documented in Task 29 as general operator
controls, so active mode reads the configured values exactly as shadow mode
does. An operator who wants a bigger episodic request at a compaction boundary
raises those two knobs; a hard-coded active-only ceiling would silently discard
what they configured.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_active_decision.py
"""Track B's decision step: Track A's selection + Track A's client, no forks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from headroom.proxy.jev.active import decide_active_retention
from headroom.proxy.jev.config import JevConfig


@dataclass
class _FakeAnswer:
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 12.5
    jev_model: str | None = "jev-test"


class _FakeClient:
    def __init__(self, decision: str = "drop", error: str | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, state, questions, candidate_ids):
        self.calls.append({"state": state, "questions": questions, "ids": list(candidate_ids)})
        if self.error is not None:
            return _FakeAnswer(decisions={}, error=self.error)
        return _FakeAnswer(decisions={cid: self.decision for cid in candidate_ids})


def _config(**overrides: Any) -> JevConfig:
    values: dict[str, Any] = {
        "mode": "active",
        "api_key": "test-key",
        "endpoint": "https://jev.example/v1/decide",
        "model": "jev-test",
        "timeout_ms": 500,
        "threshold_percent": 80,
        "cooldown_turns": 5,
        "max_candidate_tokens": 4000,
        # Active mode reads the SAME operator knobs shadow mode reads; these
        # are set explicitly so the budget never silently trims this fixture.
        "max_candidates": 12,
        "max_state_tokens": 100_000,
    }
    values.update(overrides)
    return JevConfig(**values)


def _conversation() -> list[dict[str, Any]]:
    """Two old tool results plus a long tail, so both sit outside the recent tail."""
    blob = json.dumps([{"id": i, "blob": "z" * 200} for i in range(60)])
    messages: list[dict[str, Any]] = [{"role": "system", "content": "be helpful"}]
    for i in range(2):
        messages.append({"role": "assistant", "content": f"calling tool {i}"})
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": blob})
    messages.extend({"role": "user", "content": f"turn {i}"} for i in range(10))
    return messages


async def test_candidates_are_selected_and_decided() -> None:
    client = _FakeClient(decision="drop")
    decision = await decide_active_retention(
        config=_config(),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert decision.called is True
    assert len(decision.candidates) == 2
    assert set(decision.decisions.values()) == {"drop"}
    assert len(client.calls) == 1
    # The state is the Track A retention state, carrying this turn's identity.
    assert client.calls[0]["state"]["session_id"] == "s1"
    assert client.calls[0]["state"]["branch_id"] == "compress"
    # One question per candidate, keyed by candidate id.
    assert sorted(client.calls[0]["questions"]) == sorted(
        c.candidate_id for c in decision.candidates
    )


async def test_the_operator_configured_bounds_are_honored() -> None:
    # HEADROOM_JEV_MAX_CANDIDATES / HEADROOM_JEV_MAX_STATE_TOKENS are documented
    # as general operator controls, so active mode must not substitute a
    # hard-coded ceiling of its own.
    client = _FakeClient(decision="drop")
    decision = await decide_active_retention(
        config=_config(max_candidates=1),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert len(decision.candidates) == 1
    assert len(client.calls[0]["ids"]) == 1

    # A tiny measured state budget trims the request instead of ignoring it:
    # nothing fits, so no call is made at all and every candidate is kept.
    tiny = _FakeClient(decision="drop")
    trimmed = await decide_active_retention(
        config=_config(max_state_tokens=1),
        client=tiny,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert trimmed.candidates == []
    assert tiny.calls == []


async def test_no_candidates_makes_no_call() -> None:
    client = _FakeClient()
    decision = await decide_active_retention(
        config=_config(),
        client=client,
        messages=[{"role": "user", "content": "hi"}],
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert decision.called is False
    assert decision.candidates == []
    assert client.calls == []


async def test_client_error_falls_open_to_keep() -> None:
    client = _FakeClient(error="HTTP 503")
    decision = await decide_active_retention(
        config=_config(),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert decision.error == "HTTP 503"
    assert set(decision.decisions.values()) == {"keep"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_active_decision.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.proxy.jev.active'`

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/active.py
"""Track B's decision step: one bounded Jev call for a real drop/truncate answer.

Everything about *what* a candidate is, *how* the state is built and *how* the
request is kept inside Jev's input limit lives in Track A
(``candidates.py`` / ``request.py`` / ``client.py``). This module only sequences
those four calls for the active path and guarantees that every candidate comes
back with a decision — ``keep`` when Jev said nothing usable.

Track B does NOT go through ``JevShadowRunner``: its threshold/cooldown gates
exist to decide *when* to speak up on ordinary traffic, and here the caller has
already declared the boundary with ``config.jev_compaction_boundary``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from headroom.proxy.jev.candidates import (
    RECENT_TAIL_EXCLUSION,
    JevCandidate,
    select_candidates,
)
from headroom.proxy.jev.client import build_request_payload
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.identity import revision_for
from headroom.proxy.jev.request import (
    build_questions,
    build_retention_state,
    enforce_state_budget,
)
from headroom.tokenizers import get_tokenizer

# Request bounds are operator configuration, not constants: `max_candidates`
# (HEADROOM_JEV_MAX_CANDIDATES) and `max_state_tokens`
# (HEADROOM_JEV_MAX_STATE_TOKENS) are read off `config` here exactly as the
# shadow runner reads them. A compaction boundary is episodic and can justify a
# larger request than an ordinary shadow turn -- but that is a decision the
# operator makes by raising those knobs, not one this module makes by ignoring
# them. `max_state_tokens` still bounds the MEASURED serialized request (Phase
# 0a found an estimated budget could re-trigger the exact `max_tokens_exceeded`
# error it was meant to prevent, which is why `enforce_state_budget` measures).


@dataclass(frozen=True)
class JevActiveDecision:
    """One turn's retention answer. ``decisions`` covers every kept candidate."""

    candidates: list[JevCandidate]
    decisions: dict[str, str]
    called: bool
    error: str | None
    latency_ms: float


async def decide_active_retention(
    *,
    config: JevConfig,
    client: Any,
    messages: list[dict[str, Any]],
    frozen_prefix: int,
    model: str,
    session_id: str,
    branch_id: str,
    provider: str = "compress",
    message_shape: str = "openai",
) -> JevActiveDecision:
    """Select candidates and ask Jev what may go. Never mutates ``messages``."""
    empty = JevActiveDecision(candidates=[], decisions={}, called=False, error=None, latency_ms=0.0)

    # Track A's selection takes the tokenizer's `count_text` callable, not the
    # tokenizer: the eligibility rules live in one place and this path passes
    # the tokenizer the request already resolved.
    tokenizer = get_tokenizer(model)
    candidates = select_candidates(
        messages,
        frozen_prefix=frozen_prefix,
        count_text=tokenizer.count_text,
        max_candidates=config.max_candidates,
    )
    if not candidates:
        return empty

    # The revision names the candidate set this turn asked about, exactly as
    # Track A's shadow path computes it, and travels in the state so a Jev-side
    # log can be correlated with a Headroom-side one.
    revision = revision_for([cand.fingerprint for cand in candidates])

    def _make_payload(
        cands: list[JevCandidate], view_tokens: int
    ) -> dict[str, Any]:
        # The budget is measured on the payload that is actually POSTed —
        # state AND questions AND model — because that whole body is what Jev
        # rejects with `max_tokens_exceeded` when it is too large.
        state = build_retention_state(
            provider=provider,
            model=model,
            jev_model=config.model,
            session_id=session_id,
            branch_id=branch_id,
            revision=revision,
            message_shape=message_shape,
            total_messages=len(messages),
            frozen_prefix=frozen_prefix,
            recent_tail=RECENT_TAIL_EXCLUSION,
            candidates=cands,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(config, state, build_questions(cands, len(messages)))

    candidates, view_tokens, _serialized_tokens = enforce_state_budget(
        candidates,
        count_text=tokenizer.count_text,
        make_payload=_make_payload,
        max_candidate_tokens=config.max_candidate_tokens,
        max_state_tokens=config.max_state_tokens,
    )
    if not candidates:
        return empty

    payload = _make_payload(candidates, view_tokens)
    candidate_ids = [c.candidate_id for c in candidates]

    answer = await client.decide(
        state=payload["state"],
        questions=payload["questions"],
        candidate_ids=candidate_ids,
    )
    # Fail open to keep: an id Jev did not answer for, or answered
    # unparseably, is never a licence to remove content.
    decisions = {cid: answer.decisions.get(cid, "keep") for cid in candidate_ids}
    return JevActiveDecision(
        candidates=candidates,
        decisions=decisions,
        called=True,
        error=answer.error,
        latency_ms=answer.latency_ms,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_active_decision.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/active.py tests/test_jev_active_decision.py
git commit -m "feat(jev): add the active-retention decision step on top of Track A selection"
```

---

### Task 16: active-retention orchestrator (never raises)

**Files:**
- Create: `headroom/proxy/jev/active_hook.py`
- Test: `tests/test_jev_active_hook.py`

**Interfaces:**
- Consumes: `decide_active_retention(...)` (Task 15), `stage_retention(...)` /
  `RetentionLease` (Task 13), `apply_retention(...)` (Task 14),
  `JEV_COMPRESS_BRANCH_ID` (Task 11); from Track A: `JevClient(config)` plus
  `await JevClient.aclose()`, and
  `count_messages_corrected(messages, *, count_messages, count_text)`;
  `get_compression_store()`, `PrometheusMetrics.record_jev_event(event)`.
- Produces:
  - `@dataclass(frozen=True) class JevActiveResult` with `messages: list[dict[str, Any]]`,
    `tokens_after: int`, `applied: int`, `candidates: int`, `called: bool`,
    `reason: str`, `hashes: list[str]`
  - `async run_jev_active_retention(*, proxy: Any, messages: list[dict[str, Any]], model: str, session_id: str, branch_id: str = JEV_COMPRESS_BRANCH_ID, frozen_prefix: int = 0, message_shape: str = "openai") -> JevActiveResult`
    — never raises; on any failure returns the input messages with `applied == 0` and a
    `reason` string; records `headroom_jev_events_total{event}` for
    `active_attempted`, `active_no_candidates`, `active_call_failed`,
    `active_no_lease`, `active_applied`, `active_fail_open`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_active_hook.py
"""The orchestrator: decide -> stage -> apply, fail-open at every step."""

from __future__ import annotations

import json
from typing import Any

import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev.active import JevActiveDecision
from headroom.proxy.jev.active_hook import run_jev_active_retention
from headroom.proxy.jev.candidates import JevCandidate
from headroom.proxy.jev.config import JevConfig


class _Metrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


class _Config:
    def __init__(self, jev: JevConfig) -> None:
        self.jev = jev


class _Proxy:
    def __init__(self, jev: JevConfig) -> None:
        self.config = _Config(jev)
        self.metrics = _Metrics()


def _jev(mode: str = "active") -> JevConfig:
    return JevConfig(
        mode=mode,
        api_key="test-key",
        endpoint="https://jev.example/v1/decide",
        model="jev-test",
        timeout_ms=500,
        threshold_percent=80,
        cooldown_turns=5,
        max_candidate_tokens=4000,
    )


def _messages() -> list[dict[str, Any]]:
    blob = json.dumps([{"id": i, "blob": "z" * 200} for i in range(60)])
    return [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_0", "content": blob},
    ]


@pytest.fixture
def store(monkeypatch) -> CompressionStore:
    s = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: s)
    return s


def _stub_decision(monkeypatch, decision: str, content: str) -> None:
    cand = JevCandidate(
        candidate_id="c0",
        message_index=1,
        block_index=None,
        candidate_type="tool_result",
        role="tool",
        tool_call_id="call_0",
        content=content,
        est_tokens=4096,
    )

    async def _fake(**kwargs):
        return JevActiveDecision(
            candidates=[cand],
            decisions={"c0": decision},
            called=True,
            error=None,
            latency_ms=10.0,
        )

    monkeypatch.setattr("headroom.proxy.jev.active_hook.decide_active_retention", _fake)


async def test_inactive_config_is_a_no_op(store) -> None:
    proxy = _Proxy(_jev(mode="shadow"))
    messages = _messages()
    result = await run_jev_active_retention(
        proxy=proxy, messages=messages, model="gpt-4o", session_id="s1"
    )
    assert result.applied == 0
    assert result.reason == "jev_inactive"
    assert result.messages is messages


async def test_drop_stages_ccr_then_rewrites(monkeypatch, store) -> None:
    messages = _messages()
    _stub_decision(monkeypatch, "drop", messages[1]["content"])
    proxy = _Proxy(_jev())
    result = await run_jev_active_retention(
        proxy=proxy, messages=messages, model="gpt-4o", session_id="s1"
    )
    assert result.applied == 1
    assert result.reason == "applied"
    assert len(result.hashes) == 1
    assert f"hash={result.hashes[0]}" in result.messages[1]["content"]
    assert result.tokens_after > 0
    # The original is retrievable under the staged hash.
    entry = store.retrieve(result.hashes[0])
    assert entry is not None
    assert entry.original_content == messages[1]["content"]
    assert "active_applied" in proxy.metrics.events
    # The caller's list is untouched, so a later failure can still fall back.
    assert messages[1]["content"] == entry.original_content


async def test_ccr_failure_keeps_the_original(monkeypatch, store) -> None:
    messages = _messages()
    _stub_decision(monkeypatch, "drop", messages[1]["content"])
    monkeypatch.setattr(
        "headroom.proxy.jev.active_hook.stage_retention", lambda *a, **k: None
    )
    proxy = _Proxy(_jev())
    result = await run_jev_active_retention(
        proxy=proxy, messages=messages, model="gpt-4o", session_id="s1"
    )
    assert result.applied == 0
    assert result.reason == "no_lease"
    assert result.messages == messages
    assert "active_no_lease" in proxy.metrics.events


async def test_unexpected_exception_fails_open(monkeypatch, store) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("jev exploded")

    monkeypatch.setattr("headroom.proxy.jev.active_hook.decide_active_retention", _boom)
    proxy = _Proxy(_jev())
    messages = _messages()
    result = await run_jev_active_retention(
        proxy=proxy, messages=messages, model="gpt-4o", session_id="s1"
    )
    assert result.applied == 0
    assert result.reason == "fail_open"
    assert result.messages is messages
    assert "active_fail_open" in proxy.metrics.events
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_active_hook.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.proxy.jev.active_hook'`

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/active_hook.py
"""One call site for Track B active retention. Never raises.

Sequence, in order, with a hard stop at any failure:

    decide (Task 15) -> stage CCR per candidate (Task 13) -> apply (Task 14)

A candidate that does not get an acknowledged, leased CCR entry is not applied,
so a store failure costs a saving and never a tool result. Any unexpected
exception is caught here and reported as ``fail_open``: the caller forwards
Headroom's ordinary compressed output unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from headroom.cache.compression_store import get_compression_store
from headroom.proxy.jev.active import decide_active_retention
from headroom.proxy.jev.candidates import count_messages_corrected
from headroom.proxy.jev.client import JevClient
from headroom.proxy.jev.compress_gate import JEV_COMPRESS_BRANCH_ID
from headroom.proxy.jev.retention_apply import apply_retention
from headroom.proxy.jev.retention_ccr import stage_retention
from headroom.tokenizers import get_tokenizer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JevActiveResult:
    """What the turn did. ``messages`` is the input list when nothing applied."""

    messages: list[dict[str, Any]]
    tokens_after: int
    applied: int
    candidates: int
    called: bool
    reason: str
    hashes: list[str]


def _record(proxy: Any, event: str) -> None:
    metrics = getattr(proxy, "metrics", None)
    recorder = getattr(metrics, "record_jev_event", None)
    if recorder is not None:
        recorder(event)


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
    """Run active retention for one declared compaction boundary."""

    def _noop(reason: str) -> JevActiveResult:
        return JevActiveResult(
            messages=messages,
            tokens_after=0,
            applied=0,
            candidates=0,
            called=False,
            reason=reason,
            hashes=[],
        )

    try:
        config = getattr(getattr(proxy, "config", None), "jev", None)
        if config is None or getattr(config, "mode", "off") != "active":
            # The caller may leave jev_compaction_boundary on permanently; with
            # HEADROOM_JEV_MODE off or shadow this is a no-op, not an error.
            return _noop("jev_inactive")

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
            await client.aclose()
        if not decision.candidates:
            _record(proxy, "active_no_candidates")
            return _noop("no_candidates")
        if decision.error is not None:
            _record(proxy, "active_call_failed")
            logger.info(
                "jev active retention: call failed (%s); keeping all %d candidates",
                decision.error,
                len(decision.candidates),
            )
            return _noop("call_failed")

        store = get_compression_store()
        leases = {}
        for cand in decision.candidates:
            if decision.decisions.get(cand.candidate_id, "keep") == "keep":
                continue
            lease = stage_retention(
                store,
                candidate_id=cand.candidate_id,
                session_id=session_id,
                branch_id=branch_id,
                content=cand.content,
                tool_name=None,
                tool_call_id=cand.tool_call_id,
                original_tokens=cand.est_tokens,
            )
            if lease is not None:
                leases[cand.candidate_id] = lease

        if not leases:
            _record(proxy, "active_no_lease")
            return _noop("no_lease")

        retained, applied_ids = apply_retention(
            messages, decision.candidates, decision.decisions, leases
        )
        if not applied_ids:
            _record(proxy, "active_no_lease")
            return _noop("no_lease")

        # count_messages_corrected takes both callables: a plain
        # count_messages prices a Responses `function_call_output` at ~0
        # because its payload lives in `output`, which silently zeroed savings
        # in Phase 0a.
        tokenizer = get_tokenizer(model)
        tokens_after = count_messages_corrected(
            retained,
            count_messages=tokenizer.count_messages,
            count_text=tokenizer.count_text,
        )
        _record(proxy, "active_applied")
        return JevActiveResult(
            messages=retained,
            tokens_after=tokens_after,
            applied=len(applied_ids),
            candidates=len(decision.candidates),
            called=True,
            reason="applied",
            hashes=[leases[cid].hash_key for cid in applied_ids],
        )
    except Exception as exc:  # noqa: BLE001 - a retention bug must never fail a turn
        logger.warning(
            "jev active retention failed open (%s: %s); forwarding Headroom's "
            "compressed output unchanged",
            type(exc).__name__,
            exc,
        )
        _record(proxy, "active_fail_open")
        return _noop("fail_open")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_active_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/active_hook.py tests/test_jev_active_hook.py
git commit -m "feat(jev): add the fail-open active-retention orchestrator"
```

---

### Task 17: wire the boundary into the `/v1/compress` handler

**Files:**
- Modify: `headroom/proxy/handlers/openai.py:9976-9990` (after the `config.mode`
  validation block), `headroom/proxy/handlers/openai.py:10205` (after
  `ccr_hashes = _response_ccr_hashes(...)`), `headroom/proxy/handlers/openai.py:10276-10277`
  (after the `session` payload field)
- Test: `tests/test_jev_compress_boundary_route.py`

**Interfaces:**
- Consumes: `parse_compaction_boundary(...)` / `JevGateError` (Task 11),
  `run_jev_active_retention(...)` / `JevActiveResult` (Task 16); existing handler
  locals `compress_config`, `mode`, `model_name`, `session_id`, `frozen_message_count`,
  `comp_cache`, `session_tracker`, `final_messages`, `tokens_after`, `ccr_hashes`.
- Produces:
  - `POST /v1/compress` accepts `config.jev_compaction_boundary`; a malformed one is a
    `400 {"error": {"type": "invalid_request", "message": ...}}`.
  - Response gains a `jev` block on a boundary turn:
    `{"boundary": true, "called": bool, "candidates": int, "applied": int, "reason": str, "hashes": list[str]}`.
  - Session replay state is re-recorded from the RETAINED messages, so the next turn
    replays what the caller actually forwarded.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compress_boundary_route.py
"""POST /v1/compress honours config.jev_compaction_boundary.

The real Jev call is stubbed at the orchestrator seam — this test is about the
route: the 400s, the mutation reaching the response, and the session replay
state being re-recorded from what the caller actually gets back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.jev.active_hook import JevActiveResult  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def _client() -> TestClient:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))


def _messages() -> list[dict[str, Any]]:
    blob = json.dumps([{"id": i, "blob": "z" * 200} for i in range(60)])
    return [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": blob},
    ]


def test_boundary_without_ccr_mode_is_400() -> None:
    with _client() as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {"jev_compaction_boundary": True, "session_id": "s1"},
            },
        )
    assert resp.status_code == 400, resp.text
    assert 'config.mode="ccr"' in resp.json()["error"]["message"]


def test_boundary_without_session_id_is_400() -> None:
    with _client() as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {"mode": "ccr", "jev_compaction_boundary": True},
            },
        )
    assert resp.status_code == 400, resp.text
    assert "config.session_id" in resp.json()["error"]["message"]


def test_non_boundary_request_reports_no_jev_block() -> None:
    with _client() as client:
        resp = client.post(
            "/v1/compress",
            json={"model": "gpt-4o", "messages": _messages(), "config": {"mode": "ccr"}},
        )
    assert resp.status_code == 200, resp.text
    assert "jev" not in resp.json()


def test_boundary_applies_retention_and_rerecords_the_session(monkeypatch) -> None:
    hash_key = "1234567890abcdef12345678"
    marker = f"Retrieve original: hash={hash_key}"

    async def _fake_retention(*, proxy, messages, model, session_id, branch_id, frozen_prefix):
        assert session_id == "s1"
        assert branch_id == "compress"
        retained = [dict(m) for m in messages]
        retained[-1]["content"] = marker
        return JevActiveResult(
            messages=retained,
            tokens_after=42,
            applied=1,
            candidates=1,
            called=True,
            reason="applied",
            hashes=[hash_key],
        )

    monkeypatch.setattr(
        "headroom.proxy.jev.active_hook.run_jev_active_retention", _fake_retention
    )

    body = {
        "model": "gpt-4o",
        "messages": _messages(),
        "config": {"mode": "ccr", "session_id": "s1", "jev_compaction_boundary": True},
    }
    with _client() as client:
        resp = client.post("/v1/compress", json=body)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["jev"] == {
            "boundary": True,
            "called": True,
            "candidates": 1,
            "applied": 1,
            "reason": "applied",
            "hashes": [hash_key],
        }
        assert payload["messages"][-1]["content"] == marker
        assert payload["tokens_after"] == 42
        assert hash_key in payload["ccr_hashes"]

        # The session tracker must hold the RETAINED bytes: it is what the
        # caller forwards, so it is what the next turn has to replay.
        proxy = client.app.state.proxy
        tracker = proxy.session_tracker_store.get_or_create("compress\x00s1", "openai")
        assert tracker.get_last_forwarded_messages()[-1]["content"] == marker
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_compress_boundary_route.py -q`
Expected: FAIL — `test_boundary_without_ccr_mode_is_400` gets 200 instead of 400 (the
flag is ignored today) and `test_boundary_applies_retention_and_rerecords_the_session`
fails with `KeyError: 'jev'`.

- [ ] **Step 3: Write minimal implementation**

Edit 1 — `headroom/proxy/handlers/openai.py`, insert immediately after the
`config.mode` validation block (after line 9990, before
`if mode in ("lossy_inline", "lossless_then_lossy"):`):

```python
            # Jev active retention (Track B) is opened by ONE request-level
            # flag, on the turn the caller means it — never by configuration
            # alone. Parsed here, next to the mode it depends on, and before
            # any compression work so a malformed boundary costs nothing.
            from headroom.proxy.jev.compress_gate import (
                JEV_COMPRESS_BRANCH_ID,
                JevGateError,
                parse_compaction_boundary,
            )

            try:
                jev_boundary = parse_compaction_boundary(compress_config, mode)
            except JevGateError as jev_gate_error:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "type": "invalid_request",
                            "message": jev_gate_error.message,
                        }
                    },
                )
```

Edit 2 — insert immediately after line 10205
(`ccr_hashes = _response_ccr_hashes(final_messages, result.markers_inserted)`):

```python
            # Active retention runs AFTER compression, on the messages the
            # caller will actually forward: its savings are incremental on top
            # of Headroom's, exactly as Phase 0a measured them.
            jev_info: dict[str, Any] | None = None
            if jev_boundary:
                from headroom.proxy.jev.active_hook import run_jev_active_retention

                jev_result = await run_jev_active_retention(
                    proxy=self,
                    messages=final_messages,
                    model=model_name,
                    session_id=session_id,
                    branch_id=JEV_COMPRESS_BRANCH_ID,
                    # A compaction boundary rebuilds the prompt cache by
                    # definition, so the session-derived freeze does not pin
                    # history here. An explicit config.frozen_message_count
                    # still does: the caller may know more about the provider
                    # cache than we do.
                    frozen_prefix=(frozen_message_count or 0),
                )
                jev_info = {
                    "boundary": True,
                    "called": jev_result.called,
                    "candidates": jev_result.candidates,
                    "applied": jev_result.applied,
                    "reason": jev_result.reason,
                    "hashes": list(jev_result.hashes),
                }
                if jev_result.applied:
                    final_messages = jev_result.messages
                    tokens_after = jev_result.tokens_after
                    ccr_hashes = list(dict.fromkeys([*ccr_hashes, *jev_result.hashes]))
                    if session_id:
                        _retained_messages = final_messages

                        def _rerecord_retained_session() -> None:
                            # Without this the tracker still holds the
                            # PRE-retention bytes while the caller forwards the
                            # retained ones: the next turn would replay the old
                            # prefix over the new one and bust the provider
                            # cache on the very first turn after a compaction.
                            with comp_cache.session_turn_lock:
                                comp_cache.update_from_result(messages, _retained_messages)
                                session_tracker.record_returned(messages, _retained_messages)

                        await self._run_compression_in_executor(
                            _rerecord_retained_session,
                            timeout=COMPRESSION_TIMEOUT_SECONDS,
                        )
```

Edit 3 — insert immediately after line 10277 (`_payload["session"] = session_info`),
before the `_finished` block:

```python
            if jev_info is not None:
                _payload["jev"] = jev_info
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_compress_boundary_route.py tests/test_compress_session_mode.py -q`
Expected: PASS (the session-mode suite is the regression guard for Edit 2)

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/handlers/openai.py tests/test_jev_compress_boundary_route.py
git commit -m "feat(jev): wire the compaction boundary into POST /v1/compress"
```

---

### Task 18: end-to-end retrieval through the existing `/v1/retrieve` path

**Files:**
- Modify: `wiki/ccr.md:118-140` (add a "Jev active retention" row + configuration block
  after the existing `## Configuration` section)
- Test: `tests/test_jev_compress_retrieve_e2e.py`

**Interfaces:**
- Consumes: the whole Track B chain plus `JevConfig.from_env()` (Track A) and the
  existing `POST /v1/retrieve` route (`headroom/proxy/server.py:5084-5132`).
- Produces: no new code interface. The proven end-to-end property: a marker returned by
  a boundary `/v1/compress` turn resolves on `POST /v1/retrieve` with the byte-exact
  original, with no Jev-specific retrieval path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compress_retrieve_e2e.py
"""Boundary /v1/compress -> marker -> existing /v1/retrieve, byte-exact.

Only the Jev HTTP call is stubbed (no billed API call in CI). Everything else is
the real chain: candidate selection, the CCR write/acknowledge/lease sequence,
the rewrite, and the retrieval route that already exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.cache.compression_store import reset_compression_store  # noqa: E402
from headroom.proxy.jev.config import JevConfig  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


@dataclass
class _Answer:
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 5.0
    jev_model: str | None = "jev-test"


@pytest.fixture
def jev_active(monkeypatch) -> None:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "active")
    monkeypatch.setenv("HEADROOM_JEV_API_KEY", "test-key")
    monkeypatch.setenv("HEADROOM_JEV_ENDPOINT", "https://jev.invalid/v1/decide")
    monkeypatch.setenv("HEADROOM_JEV_MODEL", "jev-test")
    monkeypatch.setenv("HEADROOM_JEV_TIMEOUT_MS", "500")
    monkeypatch.setenv("HEADROOM_JEV_MAX_CANDIDATE_TOKENS", "4000")
    monkeypatch.setenv("HEADROOM_CCR_BACKEND", "memory")

    async def _decide(self, *, state, questions, candidate_ids):
        # Drop everything offered: this test is about the retrieval guarantee
        # that makes dropping safe, not about decision quality.
        return _Answer(decisions={cid: "drop" for cid in candidate_ids})

    monkeypatch.setattr("headroom.proxy.jev.client.JevClient.decide", _decide)
    reset_compression_store()


def _messages() -> list[dict[str, Any]]:
    blob = json.dumps(
        [{"id": i, "status": "ok", "blob": f"payload-{i:04d}-" + "y" * 200} for i in range(120)]
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": blob},
    ]
    # Push the tool result outside Track A's 6-message recent-tail exclusion.
    messages.extend({"role": "user", "content": f"follow-up {i}"} for i in range(8))
    return messages


def test_boundary_marker_resolves_on_v1_retrieve(jev_active) -> None:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig.from_env(),
    )
    original_messages = _messages()
    original_tool_content = original_messages[2]["content"]

    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": original_messages,
                "config": {
                    "mode": "ccr",
                    "session_id": "caller-owned-session-id",
                    "jev_compaction_boundary": True,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["jev"]["applied"] >= 1, payload["jev"]
        hash_key = payload["jev"]["hashes"][0]

        # The forwarded conversation carries the marker, and the tool message
        # itself is still there (its tool_call would otherwise be orphaned).
        tool_messages = [m for m in payload["messages"] if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert f"hash={hash_key}" in tool_messages[0]["content"]
        assert tool_messages[0]["tool_call_id"] == "c1"

        # Retrieval is the EXISTING path, unchanged.
        retrieved = client.post("/v1/retrieve", json={"hash": hash_key})
        assert retrieved.status_code == 200, retrieved.text
        assert retrieved.json()["original_content"] == original_tool_content


def test_jev_off_leaves_the_boundary_turn_untouched(monkeypatch) -> None:
    monkeypatch.setenv("HEADROOM_JEV_MODE", "off")
    monkeypatch.setenv("HEADROOM_CCR_BACKEND", "memory")
    reset_compression_store()
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig.from_env(),
    )
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        resp = client.post(
            "/v1/compress",
            json={
                "model": "gpt-4o",
                "messages": _messages(),
                "config": {
                    "mode": "ccr",
                    "session_id": "caller-owned-session-id",
                    "jev_compaction_boundary": True,
                },
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["jev"]["reason"] == "jev_inactive"
    assert resp.json()["jev"]["applied"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_compress_retrieve_e2e.py -q`
Expected: FAIL with `AssertionError: {'boundary': True, 'called': ..., 'applied': 0, ...}`
on `payload["jev"]["applied"] >= 1` if any link in the chain (selection, staging,
retrieval binding) is wrong.

- [ ] **Step 3: Write minimal implementation**

No new code — this task's deliverable is the proven end-to-end property plus the
operator documentation for it. Append to `wiki/ccr.md`, immediately after the existing
`## Configuration` fenced block (line 138) and before `## Why This Matters` (line 140):

````markdown
### Jev active retention (`/v1/compress`)

A caller that owns its own compaction lifecycle can ask Headroom to go further
than deterministic compression on a single turn: Jev decides, per historical
tool result, whether it must stay verbatim, can be truncated, or can be replaced
by a retrieval marker.

```json
{
  "model": "gpt-4o",
  "messages": [],
  "config": {
    "mode": "ccr",
    "session_id": "caller-owned-session-id",
    "jev_compaction_boundary": true
  }
}
```

All three fields are required together: `mode="ccr"` because the replacement is
a CCR marker, and `session_id` because every retained original is bound to
`(session_id, branch_id, content hash)`. Any other combination is a 400.

Nothing is deleted. A retained original is written to the CCR store, read back
to confirm the write was acknowledged, and given a 24-hour retention lease
BEFORE the conversation is rewritten; if any of those steps fails the original
content is forwarded untouched. Retrieval is the ordinary `POST /v1/retrieve`
path — the marker is an ordinary `Retrieve original: hash=` marker, and the
hashes also come back in the response's `ccr_hashes`.

Requires `HEADROOM_JEV_MODE=active` (default `off`) plus `HEADROOM_JEV_API_KEY`.
With Jev off or in shadow mode the flag is a documented no-op: the response's
`jev` block reports `"reason": "jev_inactive"` and nothing is rewritten.

A boundary turn deliberately rewrites history the caller has already forwarded,
so it busts the provider prompt cache for that prefix. That is what a compaction
event is; do not set the flag on ordinary turns.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_compress_retrieve_e2e.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wiki/ccr.md tests/test_jev_compress_retrieve_e2e.py
git commit -m "feat(jev): prove boundary retention resolves through the existing /v1/retrieve path"
```

---

---

### Task 19: Prove an Anthropic-shaped boundary turn is retained too

**Files:**
- Test: `tests/test_jev_active_anthropic_shape.py`

**Interfaces:**
- Consumes: `run_jev_active_retention(...)` (Task 16) with `message_shape="anthropic"`,
  plus the real `select_candidates` (Track A, Task 5), `stage_retention` (Task 13) and
  `apply_retention` (Task 14). Only the Jev HTTP call is stubbed.
- Produces: no new code interface. The proven property the design doc's Track B
  section requires — "Applies to both Anthropic- and OpenAI-shaped `/v1/compress`
  callers" — for the shape that is structurally different: an Anthropic candidate is a
  `tool_result` BLOCK inside a user message's content list, not a whole message, so
  its envelope (`type`, `tool_use_id`) has to survive the rewrite or the provider
  rejects the turn. Task 18 covers the OpenAI shape end-to-end through the route.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_active_anthropic_shape.py
"""A boundary turn from an Anthropic-shaped caller is retained too.

Task 18 proves the OpenAI shape end-to-end through POST /v1/compress. This
covers the Anthropic shape at the orchestrator seam, which is where the shape
actually matters: the candidate is a `tool_result` block inside a user message's
content list, the marker has to land in that block's `content`, and the block's
`tool_use_id` has to survive — an orphaned tool_use is a 400 from Anthropic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev.active_hook import run_jev_active_retention
from headroom.proxy.jev.config import JevConfig


@dataclass
class _Answer:
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 4.0
    jev_model: str | None = "jev-test"


class _Config:
    def __init__(self, jev: JevConfig) -> None:
        self.jev = jev


class _Metrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


class _Proxy:
    def __init__(self, jev: JevConfig) -> None:
        self.config = _Config(jev)
        self.metrics = _Metrics()


def _anthropic_messages() -> list[dict[str, Any]]:
    blob = json.dumps([{"id": i, "blob": "z" * 200} for i in range(60)])
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "ls", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": blob}],
        },
    ]
    # Push the tool_result outside Track A's 6-message recent-tail exclusion.
    messages.extend({"role": "user", "content": f"follow-up {i}"} for i in range(7))
    return messages


@pytest.fixture
def store(monkeypatch) -> CompressionStore:
    s = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: s)
    return s


async def test_anthropic_tool_result_block_is_retained_and_retrievable(
    monkeypatch, store
) -> None:
    async def _decide(self, *, state, questions, candidate_ids):
        # Only the network call is stubbed: selection, staging and the rewrite
        # are the real code paths.
        assert state["message_shape"] == "anthropic"
        return _Answer(decisions={cid: "drop" for cid in candidate_ids})

    monkeypatch.setattr("headroom.proxy.jev.client.JevClient.decide", _decide)

    messages = _anthropic_messages()
    original_block = messages[2]["content"][0]["content"]
    proxy = _Proxy(JevConfig(mode="active", api_key="test-key", model="jev-test"))

    result = await run_jev_active_retention(
        proxy=proxy,
        messages=messages,
        model="claude-sonnet-4-5-20250929",
        session_id="caller-owned-session-id",
        message_shape="anthropic",
    )

    assert result.applied == 1
    assert result.reason == "applied"
    block = result.messages[2]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"  # the envelope survives
    assert f"hash={result.hashes[0]}" in block["content"]
    # The caller's list is untouched, so a later failure can still fall back.
    assert messages[2]["content"][0]["content"] == original_block

    entry = store.retrieve(result.hashes[0])
    assert entry is not None
    assert entry.original_content == original_block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jev_active_anthropic_shape.py -q`
Expected: FAIL — before Tasks 13-16 land this is a `ModuleNotFoundError` on
`headroom.proxy.jev.active_hook`; with them landed but `message_shape` not threaded
through, it fails on `assert state["message_shape"] == "anthropic"`.

- [ ] **Step 3: Write minimal implementation**

No new module, and no new production code: this task is the Anthropic-shape
coverage the design doc's Track B section requires, and it is expected to pass on
the code Tasks 13-16 already wrote. The three lines it depends on, each of which
exists as written in this plan:

1. `select_candidates` recognises `tool_result` blocks and records their
   `block_index` (Task 5).
2. `apply_retention` writes the marker into `blocks[block_index]["content"]` and
   leaves `type` / `tool_use_id` alone (Task 14).
3. `run_jev_active_retention` passes `message_shape=message_shape` into
   `decide_active_retention`, which forwards it to `build_retention_state`
   (Task 16, in the `decide_active_retention(...)` call inside its `try:` block).

Verify all three before running the test; a failure on
`assert state["message_shape"] == "anthropic"` means (3) regressed to the
`"openai"` default and belongs in `headroom/proxy/jev/active_hook.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_jev_active_anthropic_shape.py tests/test_jev_active_hook.py tests/test_jev_retention_apply.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_jev_active_anthropic_shape.py
git commit -m "test(jev): prove Anthropic-shaped boundary turns are retained and retrievable"
```

---

## Track C: Active Mode via the Native Codex WebSocket Compaction Boundary

> **Placement note (supersedes the design doc):** the fresh design doc names
> `headroom/proxy/jev_compact.py`. That file does not exist in this worktree
> (verified by grep: only `benchmarks/jev_plugin_compare_export.py` matches
> `jev_compact`). Track A ships the `headroom/proxy/jev/` package, so Track C's
> modules live **inside that package** rather than as a sibling module. Track C
> never edits `headroom/proxy/jev/__init__.py` (Track A owns it); every import is
> from a submodule path.
>
> **Verified wire facts this track is built on** (from
> `benchmarks/jev_codex_boundary_probe.py`, read-only research code — none of its
> shim/relay machinery is reused): the boundary is WebSocket-only on
> `/v1/responses`; it is a `response.create` frame whose `input` is
> `[custom_tool_call_output, compaction_trigger]` with a `previous_response_id`;
> the real item-type vocabulary includes `additional_tools`, `custom_tool_call`
> and `custom_tool_call_output`. Production interception therefore happens in
> `HeadroomProxy.handle_openai_responses_ws`
> (`headroom/proxy/handlers/openai.py:6749`), registered at
> `headroom/providers/proxy_routes.py:166-171`, **not** in
> `headroom/providers/codex/responses.py` (which only forwards HTTP subpaths).
>
> **CCR staging is shared with Track B.** Track C's boundary carries exactly one
> candidate, but it stages that candidate through the same
> `headroom.proxy.jev.retention_ccr.stage_retention(...)` sequence Track B uses
> (Task 13) — write, acknowledged read-back, bind to
> `(session_id, branch_id, content)`, retention lease. There is no separate
> single-candidate CCR module. What IS Track C's own is the stale-revision gate
> (Task 24): Track A's `JevIdentityStore` answers "is this the latest revision on
> this branch", whereas the WS boundary needs "has this `previous_response_id`
> already been decided in this process, ever" — a claim-once rule, because Codex
> replays a boundary wholesale after a reconnect and the replayed bytes need not
> be the bytes already staged in CCR.
>
> **The revision claim is process-wide, deliberately not per-connection.** The WS
> handler mints `session_id = uuid.uuid4().hex` fresh for every accepted socket
> (`headroom/proxy/handlers/openai.py:6773`), so a reconnect is by construction a
> *new* session id. Keying the claim by `(session_id, previous_response_id)` would
> therefore let exactly the case this gate exists for — the reconnect replay —
> through as a second drop. The claim is keyed by `previous_response_id` alone,
> which is a provider-assigned response id and unique to the branch point it
> names, so it is stable across reconnects and cannot collide between unrelated
> conversations. `session_id` is still required as turn identity (and is what CCR
> binds to), but it is not part of the claim key.

### Task 20: Compaction-boundary detection

**Files:**
- Create: `headroom/proxy/jev/compaction.py`
- Test: `tests/test_jev_compaction_boundary.py`

**Interfaces:**
- Consumes: nothing from Track A or B. Pure functions over an already-parsed WS frame dict.
- Produces:
  - `COMPACTION_TRIGGER_ITEM_TYPE: str = "compaction_trigger"`
  - `ADDITIONAL_TOOLS_ITEM_TYPE: str = "additional_tools"`, `CUSTOM_TOOL_CALL_ITEM_TYPE: str = "custom_tool_call"`
  - `JEV_TOOL_OUTPUT_ITEM_TYPES: frozenset[str]` — the candidate **allowlist**: `{"function_call_output", "custom_tool_call_output", "local_shell_call_output", "tool_search_output"}`
  - `JEV_COMPACTION_WIRE_ITEM_TYPES: frozenset[str]` — the full observed wire vocabulary (the allowlist plus `compaction_trigger`, `additional_tools`, `custom_tool_call`). This is the design doc's "item-type vocabulary must include" list, named once and kept **separate** from the allowlist: a type belonging to the vocabulary says Track C recognizes it, not that it may be dropped. `additional_tools` is a tool *carrier* (Task 22 reads the recovery tool out of it) and `custom_tool_call` is the *call* half of a tool pair; neither is ever a retention candidate.
  - `@dataclass(frozen=True) class JevCompactionBoundary` with fields `previous_response_id: str`, `trigger_index: int`, `candidate_index: int`, `item_count: int`
  - `unwrap_response_create(frame: Any) -> tuple[dict[str, Any] | None, bool]` — returns `(inner_response_payload, wrapped)`; `(None, False)` when the frame is not a Responses create frame
  - `detect_compaction_boundary(inner: Any) -> JevCompactionBoundary | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_boundary.py
"""Track C: detect Codex's native compaction boundary on the WS Responses path.

Shapes here are the ones benchmarks/jev_codex_boundary_probe.py actually
observed: a `response.create` frame whose `input` carries exactly one tool
output item plus a `compaction_trigger`, anchored by `previous_response_id`.
"""

from __future__ import annotations

from typing import Any

from headroom.proxy.jev.compaction import (
    ADDITIONAL_TOOLS_ITEM_TYPE,
    COMPACTION_TRIGGER_ITEM_TYPE,
    CUSTOM_TOOL_CALL_ITEM_TYPE,
    JEV_COMPACTION_WIRE_ITEM_TYPES,
    JEV_TOOL_OUTPUT_ITEM_TYPES,
    JevCompactionBoundary,
    detect_compaction_boundary,
    unwrap_response_create,
)


def _observed_frame() -> dict[str, Any]:
    return {
        "type": "response.create",
        "response": {
            "model": "gpt-5.6-sol",
            "previous_response_id": "resp_abc123",
            "input": [
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_9",
                    "output": "total 48\ndrwxr-xr-x  12 user  staff   384 Sep 20 13:15 .",
                },
                {"type": "compaction_trigger"},
            ],
        },
    }


def test_unwrap_response_create_handles_both_wire_shapes() -> None:
    inner, wrapped = unwrap_response_create(_observed_frame())
    assert wrapped is True
    assert inner is not None and inner["previous_response_id"] == "resp_abc123"

    bare = {"input": [], "previous_response_id": "resp_1"}
    inner, wrapped = unwrap_response_create(bare)
    assert wrapped is False
    assert inner is bare

    assert unwrap_response_create({"type": "response.cancel"}) == (None, False)
    assert unwrap_response_create("not a dict") == (None, False)


def test_detect_boundary_on_the_observed_shape() -> None:
    inner, _ = unwrap_response_create(_observed_frame())
    assert detect_compaction_boundary(inner) == JevCompactionBoundary(
        previous_response_id="resp_abc123",
        trigger_index=1,
        candidate_index=0,
        item_count=2,
    )


def test_detect_boundary_accepts_previous_response_id_on_the_trigger_item() -> None:
    inner, _ = unwrap_response_create(_observed_frame())
    assert inner is not None
    del inner["previous_response_id"]
    inner["input"][1]["previous_response_id"] = "resp_nested"
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert boundary.previous_response_id == "resp_nested"


def test_ordinary_turn_is_not_a_boundary() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "message", "role": "user", "content": "go"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"},
        ],
    }
    assert detect_compaction_boundary(inner) is None


def test_trigger_without_previous_response_id_is_not_a_boundary() -> None:
    inner = {
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"},
            {"type": "compaction_trigger"},
        ]
    }
    assert detect_compaction_boundary(inner) is None


def test_more_than_one_candidate_is_out_of_track_c_scope() -> None:
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "function_call_output", "call_id": "call_2", "output": "b"},
            {"type": "compaction_trigger"},
        ],
    }
    assert detect_compaction_boundary(inner) is None


def test_carrier_and_call_items_do_not_block_detection() -> None:
    """`additional_tools`, `custom_tool_call` and `custom_tool_call_output` are
    all real wire item types Phase 0b observed, wider than the original plan's
    assumed vocabulary. Only the OUTPUT item is a candidate; the other two must
    be ignored rather than counted as a second candidate."""
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "additional_tools", "tools": [{"type": "function", "name": "shell"}]},
            {"type": "custom_tool_call", "call_id": "call_1", "name": "shell", "input": "ls"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "a"},
            {"type": "compaction_trigger"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert (boundary.candidate_index, boundary.trigger_index) == (2, 3)


def test_the_wire_vocabulary_is_complete_and_wider_than_the_allowlist() -> None:
    """The design doc names three types the original plan's vocabulary missed.

    They are named here, in the vocabulary set -- and pointedly NOT in the
    candidate allowlist, which is the set that licenses a drop.
    """
    for item_type in ("additional_tools", "custom_tool_call", "custom_tool_call_output"):
        assert item_type in JEV_COMPACTION_WIRE_ITEM_TYPES
    assert COMPACTION_TRIGGER_ITEM_TYPE in JEV_COMPACTION_WIRE_ITEM_TYPES
    assert JEV_TOOL_OUTPUT_ITEM_TYPES < JEV_COMPACTION_WIRE_ITEM_TYPES
    # A carrier item and a call item are never retention candidates.
    assert ADDITIONAL_TOOLS_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES
    assert CUSTOM_TOOL_CALL_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES
    assert COMPACTION_TRIGGER_ITEM_TYPE not in JEV_TOOL_OUTPUT_ITEM_TYPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_boundary.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.compaction'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction.py
"""Codex native compaction-boundary detection for Jev active retention (Track C).

Phase 0b (``benchmarks/jev_codex_boundary_probe.py``) established the real shape
of Codex's native compaction boundary on Headroom's ``/v1/responses`` WebSocket
route: a ``response.create`` frame whose ``input`` carries exactly one
tool-output item plus a ``compaction_trigger`` item, anchored by a
``previous_response_id``. Exactly ONE candidate crosses the wire per compaction
event, so Track C asks Jev one keep/drop question -- never a multi-candidate
batch. Anything that does not match that shape is not a boundary and is left
untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

COMPACTION_TRIGGER_ITEM_TYPE = "compaction_trigger"

#: Codex >= 0.149.0 carries tool definitions in an ``input`` item of this type
#: rather than (only) a top-level ``tools`` array. Read by Task 22's
#: ``has_recovery_tool``; never a retention candidate.
ADDITIONAL_TOOLS_ITEM_TYPE = "additional_tools"

#: The *call* half of a custom tool pair. Its output arrives separately as
#: ``custom_tool_call_output``; the call item itself is never a candidate,
#: because dropping a call while keeping its output breaks the pairing.
CUSTOM_TOOL_CALL_ITEM_TYPE = "custom_tool_call"

# Candidate ALLOWLIST: the item types whose body Track C may replace with a
# retrieval marker. ``custom_tool_call_output`` is outside the vocabulary the
# original plan (and its abandoned probe) assumed; Phase 0b observed it as the
# type the boundary actually carries.
JEV_TOOL_OUTPUT_ITEM_TYPES = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
        "tool_search_output",
    }
)

# The full wire vocabulary Track C recognizes at a boundary -- deliberately
# WIDER than the allowlist above and deliberately a separate name. Membership
# here means "this type is known and accounted for", not "this type may be
# dropped": `additional_tools` is a tool carrier and `custom_tool_call` is a
# call item, both of which must survive untouched. Having the two sets named
# apart is what stops a future consumer from reaching for the allowlist when it
# means the vocabulary.
JEV_COMPACTION_WIRE_ITEM_TYPES = JEV_TOOL_OUTPUT_ITEM_TYPES | frozenset(
    {
        COMPACTION_TRIGGER_ITEM_TYPE,
        ADDITIONAL_TOOLS_ITEM_TYPE,
        CUSTOM_TOOL_CALL_ITEM_TYPE,
    }
)


@dataclass(frozen=True)
class JevCompactionBoundary:
    """One recognized compaction event on the Codex WS Responses path."""

    previous_response_id: str
    trigger_index: int
    candidate_index: int
    item_count: int


def unwrap_response_create(frame: Any) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(inner response payload, wrapped)`` for a Responses create frame.

    Codex sends ``{"type": "response.create", "response": {...}}``; older shapes
    send the payload directly. The second element says whether the payload was
    nested, so a caller that rewrites it can re-wrap in the same shape. Matches
    the acceptance rule the WS handler already applies at
    ``headroom/proxy/handlers/openai.py:7675-7677``.
    """
    if not isinstance(frame, dict):
        return None, False
    frame_type = frame.get("type")
    if frame_type == "response.create":
        inner = frame.get("response")
        if isinstance(inner, dict):
            return inner, True
        return frame, False
    if frame_type is None and isinstance(frame.get("input"), list):
        return frame, False
    return None, False


def detect_compaction_boundary(inner: Any) -> JevCompactionBoundary | None:
    """Recognize the observed compaction shape, or return None.

    Requires all three observed markers: a single ``compaction_trigger`` item, a
    single tool-output candidate item, and a non-empty ``previous_response_id``
    (top level, or carried on the trigger item). More than one trigger or more
    than one candidate is a shape this track did not observe and does not act
    on.
    """
    if not isinstance(inner, dict):
        return None
    items = inner.get("input")
    if not isinstance(items, list) or not items:
        return None

    previous_response_id = inner.get("previous_response_id")
    trigger_index = -1
    candidate_index = -1
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == COMPACTION_TRIGGER_ITEM_TYPE:
            if trigger_index >= 0:
                return None
            trigger_index = index
            if not isinstance(previous_response_id, str) or not previous_response_id:
                nested = item.get("previous_response_id")
                if isinstance(nested, str) and nested:
                    previous_response_id = nested
        elif item_type in JEV_TOOL_OUTPUT_ITEM_TYPES:
            if candidate_index >= 0:
                return None
            candidate_index = index

    if trigger_index < 0 or candidate_index < 0:
        return None
    if not isinstance(previous_response_id, str) or not previous_response_id:
        return None
    return JevCompactionBoundary(
        previous_response_id=previous_response_id,
        trigger_index=trigger_index,
        candidate_index=candidate_index,
        item_count=len(items),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_boundary.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction.py tests/test_jev_compaction_boundary.py
git commit -m "feat(jev): detect the Codex native compaction boundary on the WS Responses path"
```

---

### Task 21: Single-candidate extraction and content-bound replacement

**Files:**
- Modify: `headroom/proxy/jev/compaction.py` (append after `detect_compaction_boundary`)
- Test: `tests/test_jev_compaction_candidate.py`

**Interfaces:**
- Consumes: `JevCompactionBoundary`, `JEV_TOOL_OUTPUT_ITEM_TYPES` from Task 20.
- Produces:
  - `@dataclass(frozen=True) class JevCompactionCandidate` with fields `candidate_id: str`, `item_index: int`, `item_type: str`, `call_id: str`, `output_field: str`, `output_text: str`, `content_sha256: str`, `estimated_tokens: int`
  - `extract_compaction_candidate(inner: Any, boundary: JevCompactionBoundary, *, max_candidate_bytes: int) -> JevCompactionCandidate | None`
  - `replace_candidate_output(inner: Any, candidate: JevCompactionCandidate, replacement: str) -> bool` — mutates `inner` in place, returns `False` (no mutation) unless the item at that index still matches the extracted item type, `call_id`, body field and content hash

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_candidate.py
"""Track C: extract the one candidate a compaction boundary carries, and put a
retrieval marker back in its place only while the bytes still match."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from headroom.proxy.jev.compaction import (
    detect_compaction_boundary,
    extract_compaction_candidate,
    replace_candidate_output,
)


def _inner(output: Any = "stdout body") -> dict[str, Any]:
    return {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_9", "output": output},
            {"type": "compaction_trigger"},
        ],
    }


def test_extract_candidate_from_string_output() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert candidate.item_index == 0
    assert candidate.item_type == "custom_tool_call_output"
    assert candidate.call_id == "call_9"
    assert candidate.output_field == "output"
    assert candidate.output_text == "stdout body"
    assert candidate.content_sha256 == hashlib.sha256(b"stdout body").hexdigest()
    assert candidate.candidate_id == f"cand_{candidate.content_sha256[:16]}"
    assert candidate.estimated_tokens >= 1


def test_extract_candidate_serializes_structured_output() -> None:
    inner = _inner({"rows": [1, 2, 3]})
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    assert json.loads(candidate.output_text) == {"rows": [1, 2, 3]}


def test_extract_candidate_respects_the_byte_ceiling() -> None:
    inner = _inner("x" * 4096)
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=1024) is None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=8192) is not None


def test_extract_candidate_requires_a_call_id_and_body() -> None:
    inner = _inner()
    inner["input"][0].pop("call_id")
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    assert extract_compaction_candidate(inner, boundary, max_candidate_bytes=0) is None

    empty = _inner("")
    boundary = detect_compaction_boundary(empty)
    assert boundary is not None
    assert extract_compaction_candidate(empty, boundary, max_candidate_bytes=0) is None


def test_replace_candidate_output_is_bound_to_the_extracted_hash() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None

    assert replace_candidate_output(inner, candidate, "[marker]") is True
    assert inner["input"][0]["output"] == "[marker]"

    # The item no longer hashes to what was staged: refuse to touch it again.
    assert replace_candidate_output(inner, candidate, "[marker2]") is False
    assert inner["input"][0]["output"] == "[marker]"


def test_replace_candidate_output_refuses_a_different_call_id() -> None:
    # Same slot, same type, byte-identical body -- but a different call: the
    # marker was staged under the first call's identity and must not be written
    # over the second one's output.
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    inner["input"][0]["call_id"] = "call_10"
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"

    inner["input"][0].pop("call_id")
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"


def test_replace_candidate_output_refuses_a_reshaped_frame() -> None:
    inner = _inner()
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    inner["input"][0]["type"] = "function_call_output"
    assert replace_candidate_output(inner, candidate, "[marker]") is False
    assert inner["input"][0]["output"] == "stdout body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_candidate.py -q`
Expected: FAIL with "ImportError: cannot import name 'extract_compaction_candidate' from 'headroom.proxy.jev.compaction'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction.py  (append; also add `import hashlib` and
# `import json` to the module's import block)

_CANDIDATE_TEXT_FIELDS = ("output", "content")


@dataclass(frozen=True)
class JevCompactionCandidate:
    """The single tool output a compaction boundary carries."""

    candidate_id: str
    item_index: int
    item_type: str
    call_id: str
    output_field: str
    output_text: str
    content_sha256: str
    estimated_tokens: int


def _candidate_output_text(item: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(field name, text)`` for the item's body, or None."""
    for field_name in _CANDIDATE_TEXT_FIELDS:
        value = item.get(field_name)
        if isinstance(value, str) and value:
            return field_name, value
        if isinstance(value, (dict, list)) and value:
            return field_name, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return None


def extract_compaction_candidate(
    inner: Any,
    boundary: JevCompactionBoundary,
    *,
    max_candidate_bytes: int,
) -> JevCompactionCandidate | None:
    """Extract the boundary's single candidate, or None to fail open to keep.

    ``max_candidate_bytes`` is a UTF-8 byte ceiling (0 disables it) derived from
    ``HEADROOM_JEV_MAX_CANDIDATE_TOKENS`` by the caller. ``estimated_tokens`` is
    an estimate used only for the CCR entry's bookkeeping and for the question
    text -- Track C's real savings are reported by the existing WS usage
    accounting, never from this number.
    """
    if not isinstance(inner, dict):
        return None
    items = inner.get("input")
    if not isinstance(items, list):
        return None
    if not 0 <= boundary.candidate_index < len(items):
        return None
    item = items[boundary.candidate_index]
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in JEV_TOOL_OUTPUT_ITEM_TYPES:
        return None
    found = _candidate_output_text(item)
    if found is None:
        return None
    output_field, output_text = found
    encoded = output_text.encode("utf-8", "replace")
    if max_candidate_bytes > 0 and len(encoded) > max_candidate_bytes:
        return None
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    digest = hashlib.sha256(encoded).hexdigest()
    return JevCompactionCandidate(
        candidate_id=f"cand_{digest[:16]}",
        item_index=boundary.candidate_index,
        item_type=str(item_type),
        call_id=call_id,
        output_field=output_field,
        output_text=output_text,
        content_sha256=digest,
        estimated_tokens=max(1, len(encoded) // 4),
    )


def replace_candidate_output(
    inner: Any,
    candidate: JevCompactionCandidate,
    replacement: str,
) -> bool:
    """Swap the candidate's body for ``replacement`` in place.

    Returns False -- changing nothing -- unless the item still sits at the same
    index, still has the same type, the same ``call_id`` and the same body
    field, and still hashes to the content that was staged in CCR. That binding
    is what stops a rewrite between extraction and commit from replacing content
    whose original was never stored. ``call_id`` is part of it because content
    alone is not identity: two calls of the same tool can return byte-identical
    output, and the marker staged under one call's identity must not land on the
    other's slot.
    """
    if not isinstance(inner, dict):
        return False
    items = inner.get("input")
    if not isinstance(items, list):
        return False
    if not 0 <= candidate.item_index < len(items):
        return False
    item = items[candidate.item_index]
    if not isinstance(item, dict) or item.get("type") != candidate.item_type:
        return False
    if item.get("call_id") != candidate.call_id:
        return False
    found = _candidate_output_text(item)
    if found is None or found[0] != candidate.output_field:
        return False
    if hashlib.sha256(found[1].encode("utf-8", "replace")).hexdigest() != candidate.content_sha256:
        return False
    item[candidate.output_field] = replacement
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_candidate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction.py tests/test_jev_compaction_candidate.py
git commit -m "feat(jev): extract and hash-bind the single compaction-boundary candidate"
```

---

### Task 22: Recovery-tool presence gate

**Files:**
- Modify: `headroom/proxy/jev/compaction.py` (append after `replace_candidate_output`)
- Test: `tests/test_jev_compaction_recovery_tool.py`

**Interfaces:**
- Consumes: `CCR_TOOL_NAME` from `headroom.ccr` (`headroom/ccr/tool_injection.py:22`, re-exported at `headroom/ccr/__init__.py:60`).
- Produces: `has_recovery_tool(inner: Any) -> bool` — True when the frame advertises `headroom_retrieve`, either as a top-level Responses tool def or inside a Codex `additional_tools` carrier item.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_recovery_tool.py
"""Track C fail-open rule: never drop a candidate the model cannot get back.

Codex >= 0.149.0 nests tool definitions in `input` items of type
`additional_tools` (see `_lift_codex_additional_tools` in
headroom/proxy/handlers/openai.py:807), so the gate has to look in both places.
"""

from __future__ import annotations

from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.compaction import has_recovery_tool


def test_top_level_responses_tool_list_is_recognized() -> None:
    inner = {"tools": [{"type": "function", "name": CCR_TOOL_NAME}]}
    assert has_recovery_tool(inner) is True


def test_chat_shaped_nested_function_tool_is_recognized() -> None:
    inner = {"tools": [{"type": "function", "function": {"name": CCR_TOOL_NAME}}]}
    assert has_recovery_tool(inner) is True


def test_mcp_prefixed_tool_name_is_recognized() -> None:
    inner = {"tools": [{"type": "function", "name": f"mcp__Headroom__{CCR_TOOL_NAME}"}]}
    assert has_recovery_tool(inner) is True


def test_additional_tools_carrier_item_is_recognized() -> None:
    inner = {
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {"type": "function", "name": "shell"},
                    {"type": "function", "name": CCR_TOOL_NAME},
                ],
            },
            {"type": "compaction_trigger"},
        ]
    }
    assert has_recovery_tool(inner) is True


def test_missing_recovery_tool_fails_the_gate() -> None:
    assert has_recovery_tool({"tools": [{"type": "function", "name": "shell"}]}) is False
    assert has_recovery_tool({"input": [{"type": "compaction_trigger"}]}) is False
    assert has_recovery_tool({}) is False
    assert has_recovery_tool(None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_recovery_tool.py -q`
Expected: FAIL with "ImportError: cannot import name 'has_recovery_tool' from 'headroom.proxy.jev.compaction'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction.py  (append; also add
# `from headroom.ccr import CCR_TOOL_NAME` to the module's import block)


def _tool_names(tools: Any) -> list[str]:
    """Collect tool names from a Responses (flat) or chat (nested) tool list."""
    names: list[str] = []
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
        function = tool.get("function")
        if isinstance(function, dict):
            nested = function.get("name")
            if isinstance(nested, str) and nested:
                names.append(nested)
    return names


def _is_recovery_tool_name(name: str) -> bool:
    # Mirrors the namespaced-tool match the Responses handler already uses
    # (headroom/proxy/handlers/openai.py:2110).
    return name == CCR_TOOL_NAME or name.endswith(f"__{CCR_TOOL_NAME}")


def has_recovery_tool(inner: Any) -> bool:
    """Whether this frame advertises ``headroom_retrieve`` to the model.

    Dropping a tool result the model cannot redeem is permanent data loss, so
    this gate is a hard precondition for any Track C mutation. Both encodings
    are checked: the classic top-level ``tools`` array, and the Codex >= 0.149.0
    ``additional_tools`` carrier item inside ``input``.
    """
    if not isinstance(inner, dict):
        return False
    if any(_is_recovery_tool_name(name) for name in _tool_names(inner.get("tools"))):
        return True
    items = inner.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type") != ADDITIONAL_TOOLS_ITEM_TYPE:
                continue
            if any(_is_recovery_tool_name(name) for name in _tool_names(item.get("tools"))):
                return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_recovery_tool.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction.py tests/test_jev_compaction_recovery_tool.py
git commit -m "feat(jev): gate compaction drops on an advertised headroom_retrieve tool"
```

---

### Task 23: Single-candidate Jev decision

**Files:**
- Create: `headroom/proxy/jev/compaction_decision.py`
- Test: `tests/test_jev_compaction_decision.py`

**Interfaces:**
- Consumes: Track A's `JevClient.decide(state=, questions=, candidate_ids=) -> JevAnswer`. Track C reads **only** `JevAnswer.error` (falsy means usable) and `JevAnswer.decisions: dict[str, str]` mapping candidate id → `"keep" | "truncate" | "drop"`, both via `getattr`, so a Track A field rename degrades to keep instead of raising. Both a coroutine and a plain return from `decide` are accepted, and `timeout_seconds` bounds **both**: an async `decide` is awaited under `asyncio.wait_for`, and a synchronous one is dispatched to the default executor and waited on under the same bound, so a blocking client can neither evade the timeout nor stall the WebSocket's event loop. (A blocking call cannot be cancelled, so its thread may finish after the bound expires; the result is discarded and the decision is already `keep`.)
- Produces:
  - `JEV_DECISION_KEEP: str = "keep"`, `JEV_DECISION_DROP: str = "drop"`
  - `build_single_candidate_state(candidate: JevCompactionCandidate, boundary: JevCompactionBoundary, *, session_id: str, model: str | None, max_content_chars: int = 20000) -> dict[str, Any]`
  - `build_single_candidate_question(candidate: JevCompactionCandidate) -> dict[str, dict[str, Any]]`
  - `async decide_single_candidate(client: Any, candidate: JevCompactionCandidate, boundary: JevCompactionBoundary, *, session_id: str, model: str | None, timeout_seconds: float) -> str` — always returns `"keep"` or `"drop"`, never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_decision.py
"""Track C: one candidate, one question, fail open to keep on anything odd."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from headroom.proxy.jev.compaction import (
    detect_compaction_boundary,
    extract_compaction_candidate,
)
from headroom.proxy.jev.compaction_decision import (
    JEV_DECISION_DROP,
    JEV_DECISION_KEEP,
    build_single_candidate_question,
    build_single_candidate_state,
    decide_single_candidate,
)


def _candidate_and_boundary():
    inner = {
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_9", "output": "stdout body"},
            {"type": "compaction_trigger"},
        ],
    }
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None
    candidate = extract_compaction_candidate(inner, boundary, max_candidate_bytes=0)
    assert candidate is not None
    return candidate, boundary


@dataclass
class _Answer:
    decisions: dict[str, Any]
    error: str | None = None


class _Client:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
        self.calls.append(
            {"state": state, "questions": questions, "candidate_ids": candidate_ids}
        )
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_state_and_question_carry_exactly_one_candidate() -> None:
    candidate, boundary = _candidate_and_boundary()
    state = build_single_candidate_state(
        candidate, boundary, session_id="ws1", model="jev-latest"
    )
    assert state["branch_id"] == "resp_abc123"
    assert state["session_id"] == "ws1"
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["id"] == candidate.candidate_id
    assert state["candidates"][0]["content"] == "stdout body"

    questions = build_single_candidate_question(candidate)
    assert list(questions) == [candidate.candidate_id]
    assert set(questions[candidate.candidate_id]["criteria"]) == {"keep", "drop"}


async def test_drop_answer_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: "drop"}))
    decision = await decide_single_candidate(
        client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
    )
    assert decision == JEV_DECISION_DROP
    assert client.calls[0]["candidate_ids"] == [candidate.candidate_id]


async def test_structured_choice_answer_is_honored() -> None:
    candidate, boundary = _candidate_and_boundary()
    client = _Client(_Answer(decisions={candidate.candidate_id: {"choice": "drop"}}))
    assert (
        await decide_single_candidate(
            client, candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )


async def test_every_ambiguous_answer_keeps() -> None:
    candidate, boundary = _candidate_and_boundary()
    cases: list[Any] = [
        _Answer(decisions={candidate.candidate_id: "truncate"}),
        _Answer(decisions={candidate.candidate_id: "drop"}, error="http 500"),
        _Answer(decisions={}),
        _Answer(decisions={"other": "drop"}),
        object(),
        None,
        RuntimeError("boom"),
    ]
    for case in cases:
        assert (
            await decide_single_candidate(
                _Client(case),
                candidate,
                boundary,
                session_id="ws1",
                model=None,
                timeout_seconds=5.0,
            )
            == JEV_DECISION_KEEP
        )


async def test_slow_client_times_out_to_keep() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Slow:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            await asyncio.sleep(1.0)
            return _Answer(decisions={candidate.candidate_id: "drop"})

    assert (
        await decide_single_candidate(
            _Slow(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=0.05
        )
        == JEV_DECISION_KEEP
    )


async def test_a_slow_SYNCHRONOUS_client_also_times_out_and_never_blocks_the_loop() -> None:
    # A blocking `decide` is inside the stated interface ("both a coroutine and
    # a plain return are accepted"), and this runs on the WebSocket relay's
    # event loop: calling it inline would freeze every other session for its
    # whole duration and no timeout could fire. It must be offloaded.
    candidate, boundary = _candidate_and_boundary()
    started = asyncio.Event()

    class _SlowSync:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            started.set()
            time.sleep(1.0)
            return _Answer(decisions={candidate.candidate_id: "drop"})

    async def _heartbeat() -> int:
        beats = 0
        while not decided.done():
            await asyncio.sleep(0.01)
            beats += 1
        return beats

    decided = asyncio.ensure_future(
        decide_single_candidate(
            _SlowSync(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=0.05
        )
    )
    beats = await _heartbeat()
    assert await decided == JEV_DECISION_KEEP
    # The loop kept running while the blocking call sat in its worker thread.
    assert started.is_set()
    assert beats >= 2


async def test_a_fast_synchronous_client_is_still_honored() -> None:
    candidate, boundary = _candidate_and_boundary()

    class _Sync:
        def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            return _Answer(decisions={candidate.candidate_id: "drop"})

    assert (
        await decide_single_candidate(
            _Sync(), candidate, boundary, session_id="ws1", model=None, timeout_seconds=5.0
        )
        == JEV_DECISION_DROP
    )
```

The new test needs `import time` beside the existing `import asyncio` at the top of
the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_decision.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.compaction_decision'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction_decision.py
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
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from typing import Any

from headroom.proxy.jev.compaction import JevCompactionBoundary, JevCompactionCandidate

logger = logging.getLogger(__name__)


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
    call = getattr(decide, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)

JEV_DECISION_KEEP = "keep"
JEV_DECISION_DROP = "drop"

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
    "costs about {tokens} tokens. Answering `drop` does not delete it: Headroom "
    "stores the original and leaves a retrieval marker the assistant can redeem "
    "with the `headroom_retrieve` tool. Answer `keep` if the body is still "
    "needed verbatim."
)


def build_single_candidate_state(
    candidate: JevCompactionCandidate,
    boundary: JevCompactionBoundary,
    *,
    session_id: str,
    model: str | None,
    max_content_chars: int = 20000,
) -> dict[str, Any]:
    """Build the bounded retention state for exactly one candidate."""
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
                "content_truncated_for_view": len(candidate.output_text) > max_content_chars,
                "content": candidate.output_text[:max_content_chars],
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


def _parse_decision(answer: Any, candidate_id: str) -> str:
    """Read one decision off a JevAnswer. Anything ambiguous means keep."""
    if answer is None:
        return JEV_DECISION_KEEP
    if getattr(answer, "error", None):
        return JEV_DECISION_KEEP
    decisions = getattr(answer, "decisions", None)
    if not isinstance(decisions, dict):
        return JEV_DECISION_KEEP
    raw = decisions.get(candidate_id)
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
) -> str:
    """Ask Track A's Jev client one question. Never raises; keeps on doubt."""
    state = build_single_candidate_state(
        candidate, boundary, session_id=session_id, model=model
    )
    questions = build_single_candidate_question(candidate)
    call = functools.partial(
        client.decide,
        state=state,
        questions=questions,
        candidate_ids=[candidate.candidate_id],
    )
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        if _returns_awaitable(client.decide):
            # Async client: the call itself is cheap, the await is what waits.
            result = await asyncio.wait_for(call(), timeout=timeout_seconds)
        else:
            # Synchronous client: `decide` blocks until it is finished, so
            # calling it inline would block this WebSocket's event loop for its
            # full duration and `timeout_seconds` could never fire. Run it in a
            # worker thread and bound the WAIT instead. The thread may outlive
            # the timeout -- a blocking call cannot be cancelled -- but the loop
            # is free again the moment the bound expires, and this path is
            # already committed to "keep" by then.
            result = await asyncio.wait_for(
                loop.run_in_executor(None, call), timeout=timeout_seconds
            )
        # A sync `decide` is still allowed to hand back an awaitable (a future
        # or a coroutine from a wrapper): finish it inside what is LEFT of the
        # same bound, never a second full timeout.
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(
                result, timeout=max(0.0, deadline - loop.time())
            )
    except (asyncio.TimeoutError, TimeoutError):
        logger.info(
            "jev compaction: decision timed out after %.2fs; keeping candidate",
            timeout_seconds,
        )
        return JEV_DECISION_KEEP
    except Exception as exc:
        logger.warning(
            "jev compaction: decision call failed (%s: %s); keeping candidate",
            type(exc).__name__,
            exc,
        )
        return JEV_DECISION_KEEP
    return _parse_decision(result, candidate.candidate_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_decision.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction_decision.py tests/test_jev_compaction_decision.py
git commit -m "feat(jev): ask one keep/drop question per Codex compaction boundary"
```

---

### Task 24: Stale-revision store

**Files:**
- Create: `headroom/proxy/jev/compaction_state.py`
- Test: `tests/test_jev_compaction_revisions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `class JevCompactionRevisionStore` with `__init__(self, max_entries: int = 512) -> None`, `claim(self, revision: str) -> bool` (True only the first time a revision is seen in this process), `seen(self, revision: str) -> bool`.

> **Why this is not Track A's `JevIdentityStore`:** the two answer different questions, so they are deliberately separate rather than duplicated. Track A's store answers *"is this still the latest revision on this branch"* — a newer revision supersedes an older one, which is the right rule for a shadow call that returns after the conversation moved on. The WS boundary needs *"has this `previous_response_id` already been decided in this process, ever"*: Codex replays a compaction wholesale after a reconnect, and the replayed frame's bytes need not be the bytes already staged in CCR, so a claim-once rule is the only safe one. The identity differs too — the branch here is the boundary's `previous_response_id`, which Track A's HTTP-path store never sees.

> **Why the key is the revision alone, with no session id in it:** the WS handler assigns `session_id = uuid.uuid4().hex` per accepted socket (`headroom/proxy/handlers/openai.py:6773`), so a reconnect *always* arrives under a new session id. A `(session_id, revision)` key would consequently never recognise a reconnect replay — the single case this store exists to catch. `previous_response_id` is assigned by the provider and names one branch point, so it is both stable across reconnects and unique across unrelated conversations; keying on it alone is what makes the replay stale. The cost of that choice is bounded and safe in one direction only: a false "already claimed" can at worst *skip* a retention opportunity (the original frame is forwarded untouched), never drop content twice.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_revisions.py
"""Track C: a compaction revision may be decided exactly once."""

from __future__ import annotations

import pytest

from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore


def test_first_claim_wins_and_the_replay_is_stale() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True
    assert store.seen("resp_abc") is True
    assert store.claim("resp_abc") is False


def test_a_reconnect_cannot_reclaim_the_same_revision() -> None:
    # The WS handler mints a fresh uuid4 session id per socket, so a reconnect
    # replay carries a NEW session id and the SAME previous_response_id. The
    # store must still call it stale -- that is the whole point of this gate.
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True  # first connection
    assert store.claim("resp_abc") is False  # reconnect replays the boundary


def test_distinct_revisions_are_independent() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True
    assert store.claim("resp_def") is True


def test_empty_identity_never_claims() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("") is False


def test_store_is_bounded_and_evicts_oldest_first() -> None:
    store = JevCompactionRevisionStore(max_entries=2)
    assert store.claim("r1") is True
    assert store.claim("r2") is True
    assert store.claim("r3") is True
    assert store.seen("r1") is False
    assert store.seen("r2") is True
    assert store.seen("r3") is True


def test_max_entries_must_be_positive() -> None:
    with pytest.raises(ValueError):
        JevCompactionRevisionStore(max_entries=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_revisions.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.compaction_state'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction_state.py
"""Track C's stale-revision gate for the Codex compaction boundary."""

from __future__ import annotations

import threading
from collections import OrderedDict


class JevCompactionRevisionStore:
    """Remembers which compaction revisions this process already decided.

    A boundary's revision is its ``previous_response_id``: the provider-assigned
    id of the response the compaction hangs off. Codex retries a turn -- and
    replays it wholesale after a reconnect -- against the same anchor, so a
    second boundary carrying a revision already decided here is stale: the
    candidate bytes it carries need not be the bytes staged in CCR the first
    time. ``claim`` returns False for those and Track C keeps the original.

    The key is the revision ALONE. The WS handler assigns
    ``session_id = uuid.uuid4().hex`` per accepted socket
    (``headroom/proxy/handlers/openai.py:6773``), so a reconnect always carries a
    new session id; including it in the key would make every reconnect replay
    look fresh and defeat this gate. A provider response id already names one
    branch point uniquely, so no session scoping is needed to keep unrelated
    conversations apart.

    In-process and bounded: the fresh design's single-worker scope means no
    cross-worker ledger is required, and the bound is what keeps a long-lived
    proxy from growing one entry per compaction forever. Eviction can only lose
    the memory of an old claim, which costs a skipped-vs-repeated decision on a
    boundary that is hours stale, never a double drop of live content.
    """

    def __init__(self, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._claimed: OrderedDict[str, None] = OrderedDict()

    def claim(self, revision: str) -> bool:
        """Claim ``revision``; True only the first time this process sees it."""
        if not revision:
            return False
        with self._lock:
            if revision in self._claimed:
                self._claimed.move_to_end(revision)
                return False
            self._claimed[revision] = None
            while len(self._claimed) > self._max_entries:
                self._claimed.popitem(last=False)
            return True

    def seen(self, revision: str) -> bool:
        """Whether this revision was already claimed (and not yet evicted)."""
        with self._lock:
            return revision in self._claimed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_revisions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction_state.py tests/test_jev_compaction_revisions.py
git commit -m "feat(jev): add the in-process stale-revision gate for compaction boundaries"
```

---

### Task 25: Boundary orchestrator (the fail-open contract)

**Files:**
- Create: `headroom/proxy/jev/compaction_hook.py`
- Test: `tests/test_jev_compaction_hook.py`

**Interfaces:**
- Consumes: Tasks 20–22 (detection, extraction, recovery-tool gate), Task 23 (single-candidate decision), Task 24 (revision store), and the SHARED CCR sequence `stage_retention(...)` / `RetentionLease` from Task 13; Track A's `JevConfig` via `getattr` only — `mode: str` (`"off" | "shadow" | "active"`), `timeout_ms: int`, `max_candidate_tokens: int`, `model: str | None`; Track A's `HeadroomProxy.jev_shadow: JevShadowRunner`; Track A's `headroom_jev_events_total{event}` counter via a `metrics.record_jev_event(event)` recorder probed with `getattr` (absent recorder = log only, never an error).
- Produces:
  - `DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS: float = 5.0`
  - `resolve_jev_client(proxy: Any) -> Any | None`
  - `async apply_jev_compaction_boundary(raw_msg: str, *, jev_config: Any, client: Any | None, session_id: str, request_id: str, revisions: JevCompactionRevisionStore, metrics: Any = None, store: Any | None = None) -> tuple[str, str]` — returns `(frame_to_forward, reason)`. The frame is the **unchanged input string** for every reason except `"jev_compaction_dropped"`. Reasons: `jev_compaction_disabled`, `jev_compaction_not_json`, `jev_compaction_not_response_create`, `jev_compaction_no_boundary`, `jev_compaction_missing_identity`, `jev_compaction_stale_revision`, `jev_compaction_missing_recovery_tool`, `jev_compaction_no_candidate`, `jev_compaction_no_client`, `jev_compaction_keep`, `jev_compaction_ccr_failed`, `jev_compaction_dropped`, `jev_compaction_error`. Never raises.

**Revision-claim ordering (the retry contract).** The revision is *checked* (`revisions.seen`) as soon as the boundary is recognized, so a known-stale replay costs nothing, but it is only *claimed* (`revisions.claim`) after the recovery-tool, candidate-size and client-presence gates pass — immediately before the Jev call. Those gates are transient: the same boundary retried a moment later may well advertise the tool or find the client attached, and burning the claim on them would turn a recoverable miss into a permanent one. Everything from the claim onwards is deliberately single-shot: a timeout, an ambiguous answer or a failed CCR commit all forward the original bytes, so a retry that is refused as stale loses an optimisation and never content — whereas a retry *after* a successful commit is exactly the double-drop this gate must prevent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_hook.py
"""Track C: every gate fails open to the original frame bytes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.compaction_hook import (
    apply_jev_compaction_boundary,
    resolve_jev_client,
)
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore


@dataclass
class _Config:
    mode: str = "active"
    timeout_ms: int = 5000
    max_candidate_tokens: int = 0
    model: str | None = "jev-latest"


@dataclass
class _Answer:
    decisions: dict[str, Any]
    error: str | None = None


class _Client:
    def __init__(self, decision: str = "drop") -> None:
        self.decision = decision
        self.calls = 0

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
        self.calls += 1
        return _Answer(decisions={candidate_ids[0]: self.decision})


class _Metrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


def _frame(*, with_tool: bool = True) -> str:
    tools = [{"type": "function", "name": CCR_TOOL_NAME}] if with_tool else []
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.6-sol",
                "previous_response_id": "resp_abc123",
                "tools": tools,
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_9",
                        "output": "stdout body",
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )


async def _run(raw: str, **kwargs: Any) -> tuple[str, str]:
    defaults: dict[str, Any] = {
        "jev_config": _Config(),
        "client": _Client(),
        "session_id": "ws1",
        "request_id": "req1",
        "revisions": JevCompactionRevisionStore(),
        "metrics": None,
        "store": CompressionStore(backend=InMemoryBackend()),
    }
    defaults.update(kwargs)
    return await apply_jev_compaction_boundary(raw, **defaults)


async def test_drop_replaces_only_the_candidate_body() -> None:
    raw = _frame()
    store = CompressionStore(backend=InMemoryBackend())
    metrics = _Metrics()
    out, reason = await _run(raw, store=store, metrics=metrics)
    assert reason == "jev_compaction_dropped"

    sent = json.loads(out)["response"]
    items = sent["input"]
    assert items[1] == {"type": "compaction_trigger"}
    assert items[0]["type"] == "custom_tool_call_output"
    assert items[0]["call_id"] == "call_9"
    marker = items[0]["output"]
    assert marker.startswith("[") and "Retrieve more: hash=" in marker
    assert sent["previous_response_id"] == "resp_abc123"

    hash_key = marker.split("hash=")[1].rstrip("]")
    entry = store.retrieve(hash_key)
    assert entry is not None and entry.original_content == "stdout body"
    assert "compaction_dropped" in metrics.events


async def test_keep_forwards_the_original_bytes_unchanged() -> None:
    raw = _frame()
    out, reason = await _run(raw, client=_Client(decision="keep"))
    assert reason == "jev_compaction_keep"
    assert out == raw


async def test_every_fail_open_gate_forwards_the_original_bytes() -> None:
    raw = _frame()
    revisions = JevCompactionRevisionStore()

    assert await _run(raw, jev_config=_Config(mode="shadow")) == (
        raw,
        "jev_compaction_disabled",
    )
    assert await _run("not json") == ("not json", "jev_compaction_not_json")
    cancel = json.dumps({"type": "response.cancel"})
    assert await _run(cancel) == (cancel, "jev_compaction_not_response_create")
    ordinary = json.dumps(
        {"type": "response.create", "response": {"input": [{"type": "message"}]}}
    )
    assert await _run(ordinary) == (ordinary, "jev_compaction_no_boundary")
    assert await _run(raw, session_id="") == (raw, "jev_compaction_missing_identity")
    assert await _run(_frame(with_tool=False))[1] == "jev_compaction_missing_recovery_tool"
    assert await _run(raw, client=None) == (raw, "jev_compaction_no_client")

    # Stale revision: the same previous_response_id twice.
    first_out, first_reason = await _run(raw, revisions=revisions)
    assert first_reason == "jev_compaction_dropped"
    assert first_out != raw
    assert await _run(raw, revisions=revisions) == (raw, "jev_compaction_stale_revision")


async def test_a_reconnect_replay_is_stale_under_a_new_session_id() -> None:
    # The WS handler mints a fresh uuid4 session id per socket, so a reconnect
    # replay of the same boundary arrives with a DIFFERENT session_id and the
    # same previous_response_id. It must not be dropped a second time.
    raw = _frame()
    revisions = JevCompactionRevisionStore()
    _out, reason = await _run(raw, session_id="ws-connection-1", revisions=revisions)
    assert reason == "jev_compaction_dropped"
    assert await _run(raw, session_id="ws-connection-2", revisions=revisions) == (
        raw,
        "jev_compaction_stale_revision",
    )


async def test_a_gate_before_the_decision_does_not_burn_the_revision() -> None:
    # A missing recovery tool, an oversized candidate or a momentarily absent
    # client are all transient: nothing was decided and nothing reached CCR, so
    # the same boundary must still be decidable when it is retried.
    revisions = JevCompactionRevisionStore()
    raw = _frame()

    assert (await _run(_frame(with_tool=False), revisions=revisions))[1] == (
        "jev_compaction_missing_recovery_tool"
    )
    assert (
        await _run(raw, jev_config=_Config(max_candidate_tokens=1), revisions=revisions)
    ) == (raw, "jev_compaction_no_candidate")
    assert await _run(raw, client=None, revisions=revisions) == (
        raw,
        "jev_compaction_no_client",
    )
    assert revisions.seen("resp_abc123") is False

    out, reason = await _run(raw, revisions=revisions)
    assert reason == "jev_compaction_dropped"
    assert out != raw
    assert revisions.seen("resp_abc123") is True


async def test_candidate_over_the_token_ceiling_keeps() -> None:
    big = json.dumps(
        {
            "type": "response.create",
            "response": {
                "previous_response_id": "resp_abc123",
                "tools": [{"type": "function", "name": CCR_TOOL_NAME}],
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_9",
                        "output": "x" * 5000,
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )
    out, reason = await _run(big, jev_config=_Config(max_candidate_tokens=100))
    assert (out, reason) == (big, "jev_compaction_no_candidate")


async def test_ccr_failure_keeps_the_original() -> None:
    class _BrokenStore:
        def store(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("ccr down")

        def exists(self, hash_key: str, clean_expired: bool = False) -> bool:
            return False

    raw = _frame()
    assert await _run(raw, store=_BrokenStore()) == (raw, "jev_compaction_ccr_failed")


async def test_hook_never_raises() -> None:
    class _Exploding:
        @property
        def mode(self) -> str:
            raise RuntimeError("config blew up")

    raw = _frame()
    assert await _run(raw, jev_config=_Exploding()) == (raw, "jev_compaction_error")


def test_resolve_jev_client_probes_both_attachment_points() -> None:
    class _WithDecide:
        def decide(self, **kwargs: Any) -> Any:
            return None

    class _Proxy:
        pass

    proxy = _Proxy()
    assert resolve_jev_client(proxy) is None

    # Track A's JevShadowRunner holds its bounded JevClient on `_client`.
    shadow_client = _WithDecide()
    proxy.jev_shadow = type("S", (), {"_client": shadow_client})()
    assert resolve_jev_client(proxy) is shadow_client

    direct = _WithDecide()
    proxy.jev_client = direct
    assert resolve_jev_client(proxy) is direct
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_hook.py -q`
Expected: FAIL with "ModuleNotFoundError: No module named 'headroom.proxy.jev.compaction_hook'"

- [ ] **Step 3: Write minimal implementation**

```python
# headroom/proxy/jev/compaction_hook.py
"""Track C orchestration: one keep/drop decision at the Codex compaction boundary.

Entry point for the Codex ``/v1/responses`` WebSocket relay. It never raises and
never changes the forwarded bytes unless a full, acknowledged CCR commit
succeeded for the single candidate the boundary carries. Missing identity,
missing recovery tool, a stale (already decided) revision, no Jev client, an
ambiguous answer, or any CCR failure all forward the original frame string
unchanged -- the same fail-open rules tracks A and B apply.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from headroom.proxy.jev.compaction import (
    detect_compaction_boundary,
    extract_compaction_candidate,
    has_recovery_tool,
    replace_candidate_output,
    unwrap_response_create,
)
from headroom.cache.compression_store import get_compression_store
from headroom.proxy.jev.compaction_decision import (
    JEV_DECISION_DROP,
    decide_single_candidate,
)
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore
from headroom.proxy.jev.retention_ccr import stage_retention

logger = logging.getLogger(__name__)

DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS = 5.0


def resolve_jev_client(proxy: Any) -> Any | None:
    """Find Track A's Jev client on the proxy, or None (which means keep).

    Track A wires ``HeadroomProxy.jev_shadow: JevShadowRunner`` (Task 8) and
    that runner constructs ``JevClient(config.jev)`` unconditionally in its
    ``__init__``, keeps it on ``_client``, and closes it from
    ``JevShadowRunner.aclose()`` during proxy shutdown. Track C reuses that one
    bounded client rather than opening a second httpx pool nobody would ever
    close.

    ``proxy.jev_client`` is probed first as an explicit override (that is what
    the tests set). Both lookups are ``getattr`` with a default, so a Track A
    rename degrades to "no decision, keep the original" instead of raising on a
    live WS frame.
    """
    direct = getattr(proxy, "jev_client", None)
    if direct is not None and hasattr(direct, "decide"):
        return direct
    shadow_client = getattr(getattr(proxy, "jev_shadow", None), "_client", None)
    if shadow_client is not None and hasattr(shadow_client, "decide"):
        return shadow_client
    return None


def _record_jev_event(metrics: Any, event: str) -> None:
    """Increment Track A's ``headroom_jev_events_total{event}`` counter if present."""
    recorder = getattr(metrics, "record_jev_event", None)
    if recorder is None:
        return
    try:
        recorder(event)
    except Exception:
        logger.debug("jev compaction: metric %s not recorded", event)


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
            return raw_msg, "jev_compaction_disabled"

        try:
            frame = json.loads(raw_msg)
        except (json.JSONDecodeError, TypeError):
            return raw_msg, "jev_compaction_not_json"

        inner, wrapped = unwrap_response_create(frame)
        if inner is None:
            return raw_msg, "jev_compaction_not_response_create"

        boundary = detect_compaction_boundary(inner)
        if boundary is None:
            return raw_msg, "jev_compaction_no_boundary"

        if not session_id:
            _record_jev_event(metrics, "compaction_missing_identity")
            return raw_msg, "jev_compaction_missing_identity"

        _record_jev_event(metrics, "compaction_boundary_detected")

        if revisions.seen(boundary.previous_response_id):
            _record_jev_event(metrics, "compaction_stale_revision")
            return raw_msg, "jev_compaction_stale_revision"

        if not has_recovery_tool(inner):
            _record_jev_event(metrics, "compaction_missing_recovery_tool")
            return raw_msg, "jev_compaction_missing_recovery_tool"

        max_candidate_tokens = int(getattr(jev_config, "max_candidate_tokens", 0) or 0)
        candidate = extract_compaction_candidate(
            inner,
            boundary,
            max_candidate_bytes=max_candidate_tokens * 4,
        )
        if candidate is None:
            _record_jev_event(metrics, "compaction_no_candidate")
            return raw_msg, "jev_compaction_no_candidate"

        if client is None:
            _record_jev_event(metrics, "compaction_no_client")
            return raw_msg, "jev_compaction_no_client"

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
        if not revisions.claim(boundary.previous_response_id):
            _record_jev_event(metrics, "compaction_stale_revision")
            return raw_msg, "jev_compaction_stale_revision"

        timeout_ms = float(getattr(jev_config, "timeout_ms", 0) or 0)
        decision = await decide_single_candidate(
            client,
            candidate,
            boundary,
            session_id=session_id,
            model=getattr(jev_config, "model", None),
            timeout_seconds=(
                timeout_ms / 1000.0
                if timeout_ms > 0
                else DEFAULT_JEV_COMPACTION_TIMEOUT_SECONDS
            ),
        )
        if decision != JEV_DECISION_DROP:
            _record_jev_event(metrics, "compaction_keep")
            return raw_msg, "jev_compaction_keep"

        # The shared CCR sequence (Task 13): write the original, require an
        # ACKNOWLEDGED read-back, bind it to (session, branch, content) and take
        # the retention lease — then, and only then, rewrite the frame. The
        # branch here is the boundary's `previous_response_id`, the anchor Codex
        # hangs this compaction off, so two sessions that produce byte-identical
        # tool output still get distinct entries and distinct leases.
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
        if lease is None or not replace_candidate_output(inner, candidate, lease.marker):
            _record_jev_event(metrics, "compaction_ccr_failed")
            return raw_msg, "jev_compaction_ccr_failed"

        if wrapped:
            frame["response"] = inner
        else:
            frame = inner
        rewritten = json.dumps(frame)
        _record_jev_event(metrics, "compaction_dropped")
        logger.info(
            "[%s] jev compaction boundary: dropped 1 candidate session_id=%s "
            "revision=%s candidate=%s tokens~%d",
            request_id,
            session_id,
            boundary.previous_response_id,
            candidate.candidate_id,
            candidate.estimated_tokens,
        )
        return rewritten, "jev_compaction_dropped"
    except Exception as exc:
        _record_jev_event(metrics, "compaction_fail_open")
        logger.warning(
            "[%s] jev compaction boundary failed open (%s: %s); forwarding original",
            request_id,
            type(exc).__name__,
            exc,
        )
        return raw_msg, "jev_compaction_error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/compaction_hook.py tests/test_jev_compaction_hook.py
git commit -m "feat(jev): orchestrate the compaction-boundary decision with fail-open-to-keep gates"
```

---

### Task 26: Wire the hook into the live WS relay loop

**Files:**
- Modify: `headroom/proxy/handlers/openai.py:85-86` (import block), `headroom/proxy/handlers/openai.py:110` (module constants), `headroom/proxy/handlers/openai.py:8348-8361` (the `response.create` branch of `_client_to_upstream`)
- Test: `tests/test_jev_compaction_wiring.py`

**Interfaces:**
- Consumes: `apply_jev_compaction_boundary`, `resolve_jev_client` (Task 25), `JevCompactionRevisionStore` (Task 24); the WS handler's in-scope `session_id` (`headroom/proxy/handlers/openai.py:6774`), `request_id` (`:6773`), `self.config.jev` (Track A), `self.metrics`.
- Produces: `_JEV_COMPACTION_REVISIONS: JevCompactionRevisionStore` — module-level and process-wide, keyed by `previous_response_id` alone, so a boundary replayed on a *new* WebSocket connection (which always has a new `session_id`) is still recognized as already decided.

The insertion point is deliberate: it runs **after** `_prepare_memory_frame` and **before** `_maybe_compress_response_create_frame`, so Jev sees and stages the original candidate body rather than an already-compressed marker, and Headroom's normal compression still runs over whatever survives.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_wiring.py
"""Track C is only real if the hook is actually on the WS client->upstream path.

Follows the source-assertion pattern already used for this handler (see
tests/test_codex_ws_savings_deferral.py): the WS relay is a 2000-line closure
with no seam to call directly, so the wiring itself is asserted against the
source, while behavior is covered by tests/test_jev_compaction_hook.py.
"""

from __future__ import annotations

import re
from pathlib import Path

OPENAI_HANDLER = Path(__file__).parent.parent / "headroom" / "proxy" / "handlers" / "openai.py"


def test_relay_loop_calls_the_jev_compaction_hook() -> None:
    source = OPENAI_HANDLER.read_text(encoding="utf-8")
    assert "from headroom.proxy.jev.compaction_hook import (" in source
    assert "apply_jev_compaction_boundary," in source
    assert "resolve_jev_client," in source
    assert re.search(
        r"_JEV_COMPACTION_REVISIONS\s*=\s*JevCompactionRevisionStore\(\)", source
    ), "the revision store must be a process-wide module-level singleton"
    assert source.count("await apply_jev_compaction_boundary(") >= 1


def test_hook_runs_before_compression_so_jev_sees_the_original_candidate() -> None:
    source = OPENAI_HANDLER.read_text(encoding="utf-8")
    relay_start = source.index("async def _client_to_upstream()")
    relay = source[relay_start : source.index("async def _upstream_to_client()")]
    hook_at = relay.index("await apply_jev_compaction_boundary(")
    compress_at = relay.index("await _maybe_compress_response_create_frame(")
    assert hook_at < compress_at, (
        "Jev must stage the ORIGINAL tool output in CCR; running after "
        "compression would stage an already-compressed marker."
    )


def test_hook_receives_the_session_identity_and_config() -> None:
    source = OPENAI_HANDLER.read_text(encoding="utf-8")
    relay_start = source.index("async def _client_to_upstream()")
    relay = source[relay_start : source.index("async def _upstream_to_client()")]
    call_at = relay.index("await apply_jev_compaction_boundary(")
    call = relay[call_at : call_at + 600]
    for argument in (
        'jev_config=getattr(self.config, "jev", None)',
        "client=resolve_jev_client(self)",
        "session_id=session_id",
        "request_id=request_id",
        "revisions=_JEV_COMPACTION_REVISIONS",
        'metrics=getattr(self, "metrics", None)',
    ):
        assert argument in call, f"missing {argument!r} at the WS relay call site"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_wiring.py -q`
Expected: FAIL with "AssertionError: assert 'from headroom.proxy.jev.compaction_hook import (' in source"

- [ ] **Step 3: Write minimal implementation**

Add the import immediately after `from headroom.proxy.image_isolation import run_image_compression_isolated` (line 85):

```python
from headroom.proxy.jev.compaction_hook import (
    apply_jev_compaction_boundary,
    resolve_jev_client,
)
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore
```

Add the singleton immediately after `_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS = 5.0` (line 110):

```python
# Track C: compaction revisions already decided, keyed by
# `previous_response_id`. Module-level so it survives both across frames of one
# session and across reconnects -- the WS `session_id` is a fresh uuid4 per
# socket, so it deliberately plays no part in the key. Bounded, so a long-lived
# proxy cannot grow it without limit.
_JEV_COMPACTION_REVISIONS = JevCompactionRevisionStore()
```

In `_client_to_upstream`, replace the `response.create` branch currently at lines 8348-8361:

```python
                                if (
                                    isinstance(_inbound_frame_body, dict)
                                    and _inbound_frame_body.get("type") == "response.create"
                                ):
                                    ws_response_create_frames += 1
                                    inbound_response = _inbound_frame_body.get(
                                        "response", _inbound_frame_body
                                    )
                                    current_response_input = _responses_input_to_items(
                                        inbound_response.get("input")
                                        if isinstance(inbound_response, dict)
                                        else None
                                    )
                                    msg = await _prepare_memory_frame(_inbound_frame_body, msg)
                                    # Track C: Codex's native compaction boundary
                                    # is WS-only and carries exactly one
                                    # candidate. Runs before compression so Jev
                                    # sees (and CCR stages) the ORIGINAL tool
                                    # output. Returns `msg` byte-identical
                                    # unless an acknowledged CCR commit
                                    # succeeded; never raises.
                                    msg, _jev_reason = await apply_jev_compaction_boundary(
                                        msg,
                                        jev_config=getattr(self.config, "jev", None),
                                        client=resolve_jev_client(self),
                                        session_id=session_id,
                                        request_id=request_id,
                                        revisions=_JEV_COMPACTION_REVISIONS,
                                        metrics=getattr(self, "metrics", None),
                                    )
                                    if _jev_reason == "jev_compaction_dropped":
                                        logger.info(
                                            "[%s] WS /v1/responses jev compaction drop "
                                            "frame=%d session_id=%s",
                                            request_id,
                                            client_frame_index,
                                            session_id,
                                        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_wiring.py tests/test_jev_compaction_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/handlers/openai.py tests/test_jev_compaction_wiring.py
git commit -m "feat(jev): run the compaction-boundary hook on the Codex WS client->upstream relay"
```

---

### Task 27: Cover the WS first frame, and document the track

**Files:**
- Modify: `headroom/proxy/handlers/openai.py:7675-7684` (the first-frame `response.create` branch)
- Modify: `wiki/proxy.md` (append a new section at end of file)
- Test: `tests/test_jev_compaction_wiring.py` (extend)

**Interfaces:**
- Consumes: everything from Task 26, plus the first-frame branch's in-scope `body` and `first_msg_raw`.
- Produces: no new Python symbols. Behavioral guarantee: a compaction boundary that arrives as the **first** frame of a WebSocket (the shape a reconnect replay produces) is handled identically to one mid-session, and because both call sites share the one `_JEV_COMPACTION_REVISIONS` store — which is keyed by `previous_response_id` and not by the per-socket `session_id` — a replay across a reconnect is stale rather than a second drop. The wiring tests below are source assertions (the relay is a 2000-line closure with no seam); the *behavior* of that guarantee is covered at the hook level by `test_a_reconnect_replay_is_stale_under_a_new_session_id` in `tests/test_jev_compaction_hook.py`, which runs the same boundary under two different WS session ids.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_compaction_wiring.py  (append)

WIKI_PROXY = Path(__file__).parent.parent / "wiki" / "proxy.md"


def test_first_frame_path_also_calls_the_jev_compaction_hook() -> None:
    source = OPENAI_HANDLER.read_text(encoding="utf-8")
    assert source.count("await apply_jev_compaction_boundary(") == 2, (
        "both the WS first frame and the relay loop must cross the boundary hook"
    )
    first_frame_at = source.index("first_msg_raw = await _prepare_memory_frame(")
    relay_at = source.index("async def _client_to_upstream()")
    first_frame_block = source[first_frame_at:relay_at]
    assert "first_msg_raw, _jev_first_reason = await apply_jev_compaction_boundary(" in (
        first_frame_block
    )
    assert "revisions=_JEV_COMPACTION_REVISIONS" in first_frame_block


def test_first_frame_hook_runs_before_first_frame_compression() -> None:
    source = OPENAI_HANDLER.read_text(encoding="utf-8")
    hook_at = source.index("first_msg_raw, _jev_first_reason = await apply_jev_compaction_boundary(")
    compress_at = source.index("if self.config.optimize and not _ws_bypass:")
    assert hook_at < compress_at


def test_wiki_documents_the_codex_compaction_boundary() -> None:
    wiki = WIKI_PROXY.read_text(encoding="utf-8")
    assert "## Jev Compaction Boundary (Codex WebSocket)" in wiki
    assert "HEADROOM_JEV_MODE=active" in wiki
    assert "headroom_retrieve" in wiki
    assert "fails open" in wiki
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_jev_compaction_wiring.py -q`
Expected: FAIL with "AssertionError: both the WS first frame and the relay loop must cross the boundary hook" (count is 1)

- [ ] **Step 3: Write minimal implementation**

Replace the first-frame branch at `headroom/proxy/handlers/openai.py:7675-7684`:

```python
            if isinstance(body, dict) and (
                body.get("type") == "response.create" or ("type" not in body and "input" in body)
            ):
                first_msg_raw = await _prepare_memory_frame(body, first_msg_raw)
                # Track C: a compaction boundary can also arrive as the FIRST
                # frame of a connection -- that is the shape a reconnect replay
                # produces. Same hook, same process-wide revision store (keyed
                # by previous_response_id, not by this socket's fresh uuid4
                # session_id), so a replayed boundary is stale rather than
                # dropped twice.
                first_msg_raw, _jev_first_reason = await apply_jev_compaction_boundary(
                    first_msg_raw,
                    jev_config=getattr(self.config, "jev", None),
                    client=resolve_jev_client(self),
                    session_id=session_id,
                    request_id=request_id,
                    revisions=_JEV_COMPACTION_REVISIONS,
                    metrics=getattr(self, "metrics", None),
                )
                if _jev_first_reason == "jev_compaction_dropped":
                    logger.info(
                        "[%s] WS /v1/responses jev compaction drop frame=1 session_id=%s",
                        request_id,
                        session_id,
                    )
                    try:
                        body = json.loads(first_msg_raw)
                    except json.JSONDecodeError:
                        pass
                first_response_body = body.get("response", body)
                current_response_input = _responses_input_to_items(
                    first_response_body.get("input")
                    if isinstance(first_response_body, dict)
                    else None
                )
```

Append to `wiki/proxy.md`:

```markdown
## Jev Compaction Boundary (Codex WebSocket)

Codex's native compaction crosses Headroom on the `/v1/responses` **WebSocket**
route only. At that moment the client sends a `response.create` frame whose
`input` is one tool output plus a `compaction_trigger` item, anchored by
`previous_response_id` — exactly one retention candidate per compaction event.

With `HEADROOM_JEV_MODE=active`, Headroom asks Jev one keep/drop question about
that candidate. On `drop`, the original tool output is written to the CCR store,
the write is acknowledged, and only then is the body replaced on the wire with a
retrieval marker (`[N tokens compressed to 0. Retrieve more: hash=…]`) that the
model redeems with the `headroom_retrieve` tool or `POST /v1/retrieve`. Nothing
is deleted; the item, its type and its `call_id` are preserved so the
tool-call/tool-result pairing stays intact.

The path fails open — forwarding the client's original frame bytes byte for byte
— in every one of these cases:

- `HEADROOM_JEV_MODE` is not `active` (the default is `off`)
- the frame is not a recognizable compaction boundary
- no WebSocket session identity, or a revision (`previous_response_id`) this
  process already decided — a retry, or a replay after a reconnect (the claim is
  process-wide and not scoped to one connection, precisely so that a reconnect,
  which always brings a new internal session id, cannot decide the same
  boundary twice)
- the frame does not advertise `headroom_retrieve`, so the model could not get
  the content back
- the candidate exceeds `HEADROOM_JEV_MAX_CANDIDATE_TOKENS`
- no Jev client configured, the call times out (`HEADROOM_JEV_TIMEOUT_MS`), or
  the answer is anything other than an unambiguous `drop`
- any CCR write or commit is not acknowledged

Outcomes are counted on `headroom_jev_events_total{event}` with the
`compaction_*` event labels. Only the client→proxy direction is inspected: a
compaction signal carried solely in a provider response is not visible here.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_jev_compaction_wiring.py tests/test_jev_compaction_hook.py tests/test_jev_compaction_boundary.py tests/test_jev_compaction_candidate.py tests/test_jev_compaction_recovery_tool.py tests/test_jev_retention_ccr.py tests/test_jev_compaction_decision.py tests/test_jev_compaction_revisions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/handlers/openai.py wiki/proxy.md tests/test_jev_compaction_wiring.py
git commit -m "feat(jev): cover the WS first frame at the compaction boundary and document track C"
```

---

## Cross-Track: Dashboard, Metrics and Documentation

Two requirements in the design doc are not owned by any single track: the `jev` block
the "Dashboard and Metrics" section specifies, and the `wiki/configuration.md`
coverage the "Documentation" section asks for. They land last because they report on
all three tracks at once.

---

### Task 28: `jev` accounting block on `/stats`

**Files:**
- Create: `headroom/proxy/jev/accounting.py` (the one recorder + error classifier
  every track calls; no third ad-hoc copy)
- Modify: `headroom/proxy/prometheus_metrics.py` (new `jev_totals` beside the
  `jev_events_by_event` counter added in Task 7; `reset_runtime`; two new methods
  beside `record_jev_event`)
- Modify: `headroom/proxy/jev/shadow.py` (record accounting on the projected and
  call-error paths — Task 8)
- Modify: `headroom/proxy/jev/active_hook.py` (record accounting on the applied,
  call-failed and no-lease paths — Task 16)
- Modify: `headroom/proxy/jev/compaction_decision.py` (record the call outcome —
  the only place that can tell a Track C timeout from a rejection, Task 23)
- Modify: `headroom/proxy/jev/compaction_hook.py` (record Track C's candidate,
  decision and CCR accounting — Task 25)
- Modify: `headroom/proxy/server.py:4770-4776` (`_build_stats_payload`, immediately
  after the existing `"compression": {...}` block and before `"compression_cache"`)
- Test: `tests/test_jev_stats_block.py`

**Interfaces:**
- Consumes: `PrometheusMetrics.jev_events_by_event` / `record_jev_event` (Task 7),
  `JevShadowRunner` (Task 8), `run_jev_active_retention` (Task 16),
  `JevConfig.redacted()` (Task 1).
- Produces:
  - `headroom.proxy.jev.accounting.record_jev_accounting(metrics: Any, **fields: int) -> None`
    — probes `metrics.record_jev_accounting` with `getattr` and swallows everything;
    accounting never fails a turn
  - `headroom.proxy.jev.accounting.classify_call_error(error: str | None) -> str | None`
    — `"calls_timed_out"`, `"calls_rejected"`, or `None` when there was no error
  - `headroom.proxy.prometheus_metrics.JEV_ACCOUNTING_FIELDS: tuple[str, ...]`
  - `PrometheusMetrics.jev_totals: dict[str, int]`
  - `PrometheusMetrics.record_jev_accounting(self, **fields: int) -> None` — adds
    integer totals, ignores unknown keys, guarded by `_obs_counter_lock`
  - `PrometheusMetrics.jev_snapshot(self) -> dict[str, Any]` — the totals plus the
    derived `projected_savings` (`TH - TP`), `realized_savings` (`TH - TF` on the
    turns active retention actually changed) and the per-event counts
  - `GET /stats` gains a `jev` block: the snapshot plus `"config": config.jev.redacted()`

**The four token letters, and where each comes from.** The design doc's
"Dashboard and Metrics" section names `T0` (pre-Headroom), `TH` (post-Headroom),
`TF` (post-active-retention, measured) and `TP` (shadow projection). All four are
reported, and the two savings numbers are derived from disjoint pairs so a
projection can never be mistaken for a realized saving:

| Field | Letter | Recorded by | Meaning |
|-------|--------|-------------|---------|
| `tokens_baseline` | `T0` | shadow hook (Tasks 8–10) | the caller's tokens before Headroom compressed anything |
| `tokens_headroom` | `TH` | shadow runner | post-Headroom tokens on the turns Jev was asked about |
| `tokens_projected` | `TP` | shadow runner | what those turns WOULD have cost had the answer been applied |
| `tokens_active_baseline` | `TH` | active hook (B), boundary hook (C) | post-Headroom tokens for the content active retention actually decided on |
| `tokens_final` | `TF` | active hook (B), boundary hook (C) | measured tokens for that same content after retention was applied |

`projected_savings = tokens_headroom - tokens_projected` and
`realized_savings = tokens_active_baseline - tokens_final`. `tokens_final` never
appears without its own baseline, which is the whole reason
`tokens_active_baseline` exists as a separate field from `tokens_headroom`: TH as
measured on shadow turns is not a baseline for the different turns active
retention ran on. Track B records both over the whole message list; Track C
records both over the one candidate it decided about (its frame-level totals are
already accounted for by the existing WS usage path), which keeps the subtraction
meaningful in both cases.

**Call outcomes.** `calls_attempted` counts every call started;
`calls_completed`, `calls_timed_out` and `calls_rejected` partition the ones that
finished, and `calls_failed` is the sum of the last two (kept as its own field so
an operator can alert on "any failure" without adding two series).
`classify_call_error` is the single place the distinction is drawn, from the error
string Track A's client already produces (`"ReadTimeout: …"` and friends).

**CCR accounting.** `ccr_staged` counts staging attempts, `ccr_acknowledged`
counts the ones that came back with a lease — which, per Task 13, means the write
was read back and verified — and `ccr_failed` counts the rest. A gap between
staged and acknowledged is exactly the signal that a CCR backend is quietly
dropping writes.

`/stats-history` is deliberately NOT extended. It is the durable realized-savings
series, and the design doc requires `TP` to be "reported separately, never added to
`TF` savings" — folding a shadow projection into the persisted savings ledger would
do exactly that. `TP` is reported here, as `projected_savings`, and nowhere else.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_stats_block.py
"""The /stats `jev` block reports Jev accounting and never a secret."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.prometheus_metrics import PrometheusMetrics

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.jev.config import JevConfig  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def test_record_jev_accounting_sums_known_fields_and_ignores_the_rest() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(calls_attempted=1, drop=2, not_a_field=5)
    metrics.record_jev_accounting(calls_attempted=1, tokens_headroom=1000, tokens_projected=600)

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 2
    assert snapshot["drop"] == 2
    # TP is reported as its own number, never folded into realized savings.
    assert snapshot["projected_savings"] == 400
    assert "not_a_field" not in snapshot


def test_every_design_doc_accounting_field_is_reported() -> None:
    # The design doc's "Dashboard and Metrics" list, field by field: T0/TH/TF/TP,
    # calls attempted/completed/timed out/rejected, candidate count and tokens,
    # keep/truncate/drop, and CCR staged/acknowledged/failed.
    snapshot = PrometheusMetrics().jev_snapshot()
    for field in (
        "tokens_baseline",
        "tokens_headroom",
        "tokens_projected",
        "tokens_active_baseline",
        "tokens_final",
        "calls_attempted",
        "calls_completed",
        "calls_timed_out",
        "calls_rejected",
        "calls_failed",
        "candidates",
        "candidate_tokens",
        "keep",
        "truncate",
        "drop",
        "ccr_staged",
        "ccr_acknowledged",
        "ccr_failed",
        "fallbacks",
        "projected_savings",
        "realized_savings",
    ):
        assert snapshot[field] == 0, f"{field} must be present and zeroed from the start"


def test_realized_and_projected_savings_come_from_disjoint_pairs() -> None:
    metrics = PrometheusMetrics()
    # A shadow turn: TH/TP only.
    metrics.record_jev_accounting(tokens_headroom=1000, tokens_projected=600)
    # An active turn: its own TH baseline and the measured TF.
    metrics.record_jev_accounting(tokens_active_baseline=2000, tokens_final=1200)

    snapshot = metrics.jev_snapshot()
    assert snapshot["projected_savings"] == 400
    assert snapshot["realized_savings"] == 800  # never 400 + 800, never 1400


def test_call_errors_are_classified_once_for_every_track() -> None:
    from headroom.proxy.jev.accounting import classify_call_error

    assert classify_call_error("ReadTimeout: timed out") == "calls_timed_out"
    assert classify_call_error("TimeoutError: ") == "calls_timed_out"
    assert classify_call_error("HTTP 401: invalid api key") == "calls_rejected"
    assert classify_call_error("max_tokens_exceeded") == "calls_rejected"
    assert classify_call_error(None) is None
    assert classify_call_error("") is None


def test_record_jev_accounting_helper_tolerates_a_metricsless_proxy() -> None:
    from headroom.proxy.jev.accounting import record_jev_accounting

    class _Exploding:
        def record_jev_accounting(self, **fields: int) -> None:
            raise RuntimeError("counter blew up")

    record_jev_accounting(None, calls_attempted=1)  # must not raise
    record_jev_accounting(object(), calls_attempted=1)  # no recorder at all
    record_jev_accounting(_Exploding(), calls_attempted=1)


async def test_metrics_export_carries_the_accounting_totals() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(drop=3, calls_timed_out=1)
    export = await metrics.export()
    assert 'headroom_jev_accounting_total{field="drop"} 3' in export
    assert 'headroom_jev_accounting_total{field="calls_timed_out"} 1' in export
    assert 'headroom_jev_accounting_total{field="keep"} 0' in export


def test_jev_snapshot_carries_the_event_buckets() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_projected")
    assert metrics.jev_snapshot()["events"] == {"shadow_projected": 1}


async def test_reset_runtime_clears_jev_totals() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(calls_attempted=1)
    await metrics.reset_runtime()
    assert metrics.jev_snapshot()["calls_attempted"] == 0


def test_stats_exposes_a_redacted_jev_block() -> None:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig(mode="shadow", api_key="sk-super-secret", model="jev-test"),
    )
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        client.app.state.proxy.metrics.record_jev_accounting(
            calls_attempted=1,
            calls_completed=1,
            drop=3,
            tokens_baseline=4000,
            tokens_headroom=1000,
            tokens_projected=750,
            ccr_staged=3,
            ccr_acknowledged=3,
        )
        payload = client.get("/stats").json()

    jev = payload["jev"]
    assert jev["calls_attempted"] == 1
    assert jev["drop"] == 3
    assert jev["tokens_baseline"] == 4000
    assert jev["ccr_acknowledged"] == 3
    assert jev["projected_savings"] == 250
    assert jev["config"]["mode"] == "shadow"
    assert jev["config"]["model"] == "jev-test"
    assert jev["config"]["api_key_configured"] is True
    assert "api_key" not in jev["config"]
    assert "sk-super-secret" not in json.dumps(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_stats_block.py -q`
Expected: FAIL with "AttributeError: 'PrometheusMetrics' object has no attribute 'record_jev_accounting'"

- [ ] **Step 3: Write minimal implementation**

In `headroom/proxy/prometheus_metrics.py`, at module level beside the other counter
constants, add:

```python
#: Every integer total the /stats `jev` block reports. The allowlist is the
#: contract: a caller that mistypes a field name gets it dropped, not silently
#: added as a new key nobody reads, and a dashboard can rely on every field
#: being present (zero-valued) from the first request.
JEV_ACCOUNTING_FIELDS: tuple[str, ...] = (
    # Call outcomes. `calls_completed` + `calls_timed_out` + `calls_rejected`
    # partition the calls that finished; `calls_failed` is the sum of the last
    # two, kept as its own field so "any failure" is one series to alert on.
    "calls_attempted",
    "calls_completed",
    "calls_failed",
    "calls_timed_out",
    "calls_rejected",
    # Candidate volume.
    "candidates",
    "candidates_sent",
    "candidate_tokens",
    # Answers.
    "keep",
    "truncate",
    "drop",
    # Token accounting: T0 / TH / TP for the shadow projection, and the
    # separate TH/TF pair for what active retention really changed. See the
    # table in this task's Interfaces section.
    "tokens_baseline",
    "tokens_headroom",
    "tokens_projected",
    "tokens_active_baseline",
    "tokens_final",
    # CCR: acknowledged means a verified read-back and a lease (Task 13).
    "ccr_staged",
    "ccr_acknowledged",
    "ccr_failed",
    "fallbacks",
)
```

In `PrometheusMetrics.__init__`, immediately after the `jev_events_by_event` block
added in Task 7, add:

```python
        # Jev retention token accounting for the /stats `jev` block. TH/TP are
        # summed here; TF lands in `tokens_final`. TP is NEVER added to the
        # realized-savings series — see jev_snapshot().
        self.jev_totals: dict[str, int] = defaultdict(int)
```

In `reset_runtime`, immediately after `self.jev_events_by_event.clear()`, add:

```python
                self.jev_totals.clear()
```

Immediately after `record_jev_event` (added in Task 7), add:

```python
    def record_jev_accounting(self, **fields: int) -> None:
        """Add Jev retention totals for the ``/stats`` ``jev`` block.

        Accepts only the names in :data:`JEV_ACCOUNTING_FIELDS`; anything else
        is dropped. Nothing here may raise: this runs on the request path, and
        an accounting bug must cost a number on a dashboard, never a turn.
        """
        with self._obs_counter_lock:
            for name, value in fields.items():
                if name not in JEV_ACCOUNTING_FIELDS:
                    continue
                try:
                    self.jev_totals[name] += int(value)
                except (TypeError, ValueError):
                    continue

    def jev_snapshot(self) -> dict[str, Any]:
        """Totals for the ``/stats`` ``jev`` block, plus the two derived savings.

        ``projected_savings`` is ``TH - TP`` — Track A's shadow projection, what
        active mode *would* have saved on the turns it was asked about. It is
        reported here and only here: the design doc requires it to stay out of
        the realized savings the ledger and /stats-history persist.

        ``realized_savings`` is ``TH - TF`` measured on the content active
        retention actually rewrote (``tokens_active_baseline`` against
        ``tokens_final``). The two pairs never mix: a projection and a
        measurement are different claims about different turns, and adding them
        would overstate both.
        """
        with self._obs_counter_lock:
            totals: dict[str, Any] = {
                name: int(self.jev_totals.get(name, 0)) for name in JEV_ACCOUNTING_FIELDS
            }
            events = dict(self.jev_events_by_event)
        totals["projected_savings"] = max(
            0, totals["tokens_headroom"] - totals["tokens_projected"]
        )
        totals["realized_savings"] = max(
            0, totals["tokens_active_baseline"] - totals["tokens_final"]
        )
        totals["events"] = events
        return totals
```

Create `headroom/proxy/jev/accounting.py` — one recorder and one error
classifier, so the three tracks cannot drift into three slightly different
definitions of "a failed call":

```python
"""The single accounting recorder every Jev track reports through."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Error-string fragments Track A's client produces for a call that ran out of
#: time rather than one the service refused. `JevClient.decide` formats every
#: failure as `f"{type(exc).__name__}: {exc}"` (Task 4), so httpx's
#: `ReadTimeout` / `ConnectTimeout` / `PoolTimeout` and asyncio's `TimeoutError`
#: all land here.
_TIMEOUT_MARKERS = ("timeout", "timedout")


def classify_call_error(error: str | None) -> str | None:
    """Return the accounting field one call error belongs in, or None.

    A timeout and a rejection are operationally different problems -- one says
    the bound is too tight or the service is slow, the other says the request
    or the credentials were refused -- and the design doc asks for both. They
    are told apart here, once, from the error string the client already built.
    """
    if not error:
        return None
    lowered = str(error).lower().replace("_", "")
    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return "calls_timed_out"
    return "calls_rejected"


def record_jev_accounting(metrics: Any, **fields: int) -> None:
    """Add totals to ``metrics`` if it can take them. Never raises.

    ``metrics`` may be ``None``, or an object from a Headroom build that
    predates ``record_jev_accounting``; either way a missing counter costs a
    number on a dashboard, never a turn.
    """
    recorder = getattr(metrics, "record_jev_accounting", None)
    if recorder is None:
        return
    try:
        recorder(**fields)
    except Exception:  # noqa: BLE001 - accounting never fails a turn
        logger.debug("jev: accounting not recorded")
```

In `headroom/proxy/jev/shadow.py`, add a recorder beside `_record` on
`JevShadowRunner` (importing `classify_call_error` and `record_jev_accounting`
from `headroom.proxy.jev.accounting`):

```python
    def _record_accounting(self, **fields: int) -> None:
        record_jev_accounting(self._metrics, **fields)
```

In `JevShadowRunner.maybe_run`, in the `answer.error is not None` branch, immediately
before the `return self._skip("call_error", ...)`, add:

```python
            self._record_accounting(
                calls_attempted=1,
                calls_failed=1,
                fallbacks=1,
                candidates=len(eligible),
                candidates_sent=len(sent),
                # A timeout and a refusal are different operational problems;
                # `calls_failed` stays the sum so one series still covers both.
                **{classify_call_error(answer.error) or "calls_rejected": 1},
            )
```

and immediately after `self._record("shadow_projected")`, add:

```python
        self._record_accounting(
            calls_attempted=1,
            calls_completed=1,
            candidates=len(eligible),
            candidates_sent=len(sent),
            candidate_tokens=sum(cand.est_tokens for cand in sent),
            keep=tallies["keep"],
            truncate=tallies["truncate"],
            drop=tallies["drop"],
            # T0 as the caller measured it, TH and TP as this turn measured
            # them. TP is a projection and is kept away from tokens_final.
            tokens_baseline=max(0, int(original_tokens or 0)),
            tokens_headroom=th,
            tokens_projected=tp,
        )
```

In `headroom/proxy/jev/active_hook.py`, add beside `_record`:

```python
def _record_accounting(proxy: Any, **fields: int) -> None:
    record_jev_accounting(getattr(proxy, "metrics", None), **fields)
```

and, in `run_jev_active_retention`, immediately before the `return _noop("call_failed")`:

```python
            _record_accounting(
                proxy,
                calls_attempted=1,
                calls_failed=1,
                fallbacks=1,
                candidates=len(decision.candidates),
                **{classify_call_error(decision.error) or "calls_rejected": 1},
            )
```

and immediately after `_record(proxy, "active_applied")`:

```python
        tallies = {"keep": 0, "truncate": 0, "drop": 0}
        for cid in (c.candidate_id for c in decision.candidates):
            decided = decision.decisions.get(cid, "keep")
            if decided in tallies:
                tallies[decided] += 1
        # TF needs its own TH or it says nothing: measure the SAME message list
        # the same way, before retention was applied, and report the pair.
        tokens_active_baseline = count_messages_corrected(
            messages,
            count_messages=tokenizer.count_messages,
            count_text=tokenizer.count_text,
        )
        staged = len(decision.candidates) - tallies["keep"]
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
            tokens_active_baseline=tokens_active_baseline,
            tokens_final=tokens_after,
            # `ccr_staged` counts attempts; a lease is Task 13's proof that the
            # write was read back and verified, so it is what `acknowledged`
            # means. The gap between the two is the signal that a CCR backend is
            # quietly losing writes.
            ccr_staged=max(0, staged),
            ccr_acknowledged=len(leases),
            ccr_failed=max(0, staged - len(leases)),
        )
```

In `headroom/proxy/jev/compaction_decision.py` (Track C), record the call outcome
inside `decide_single_candidate` — it is the only place that can tell a timeout
from a rejection, because the function deliberately returns a bare
`"keep"`/`"drop"` and throws the reason away. Add a keyword-only
`metrics: Any = None` parameter, then: `record_jev_accounting(metrics,
calls_attempted=1)` before the call; `calls_timed_out=1, calls_failed=1,
fallbacks=1` in the `TimeoutError` handler; `calls_rejected=1, calls_failed=1,
fallbacks=1` in the general `except`; and, once an answer is in hand,
`calls_completed=1` when `getattr(answer, "error", None)` is falsy or
`calls_rejected=1, calls_failed=1` when it is not.

In `headroom/proxy/jev/compaction_hook.py` (Track C), import
`record_jev_accounting` from `headroom.proxy.jev.accounting`, pass
`metrics=metrics` into `decide_single_candidate`, and record the candidate,
decision and CCR numbers around it. The block below goes immediately before the
`return rewritten, "jev_compaction_dropped"` (the only exit where a rewrite
actually happened, so `lease` is in hand):

```python
        # Track C decides about ONE candidate, so its baseline/final pair is
        # candidate-scoped rather than turn-scoped: the frame's own totals are
        # already accounted for by the existing WS usage path, and subtracting
        # a whole-frame TF from a candidate-sized TH would be meaningless.
        marker_tokens = max(1, len(lease.marker) // 4)
        record_jev_accounting(
            metrics,
            candidates=1,
            candidates_sent=1,
            candidate_tokens=candidate.estimated_tokens,
            drop=1,
            ccr_staged=1,
            ccr_acknowledged=1,
            tokens_active_baseline=candidate.estimated_tokens,
            tokens_final=marker_tokens,
        )
```

with the matching one-liners on the other two exits: `keep=1` (plus
`candidates`/`candidates_sent`/`candidate_tokens`) on the keep path, and
`ccr_staged=1, ccr_failed=1` on the `jev_compaction_ccr_failed` path.

In `PrometheusMetrics.export`, beside the `headroom_jev_events_total{event=...}`
series Task 7 added, emit the totals as well so the design doc's `/metrics`
requirement is met by the same numbers `/stats` reports:

```python
            # One series, one label — an operator graphs `drop` or
            # `calls_timed_out` without Headroom minting a metric name per
            # field, and a field added to JEV_ACCOUNTING_FIELDS appears here
            # with no export change. Every field is emitted even at zero, so a
            # dashboard panel exists from the first scrape.
            lines.extend(
                [
                    "# HELP headroom_jev_accounting_total Jev retention accounting totals by field; token letters are T0=tokens_baseline, TH=tokens_headroom/tokens_active_baseline, TP=tokens_projected, TF=tokens_final",
                    "# TYPE headroom_jev_accounting_total counter",
                ]
            )
            for _field in JEV_ACCOUNTING_FIELDS:
                lines.append(
                    f'headroom_jev_accounting_total{{field="{_field}"}} '
                    f"{int(jev_totals.get(_field, 0))}"
                )
            lines.append("")
```

taking `jev_totals` from the same `with self._obs_counter_lock:` block that
already snapshots `jev_events_by_event` (`jev_totals = dict(self.jev_totals)`),
so the export never reads the counters unlocked.

In `headroom/proxy/server.py._build_stats_payload`, immediately after the
`"compression": {...}` block (which ends at line 4776) and before
`"compression_cache": compression_cache_stats,`, add:

```python
            # Jev retention (design doc, "Dashboard and Metrics"). `config` is
            # the redacted view — mode, model and a scheme+host+path endpoint
            # label, never the API key. `projected_savings` is TP and is
            # deliberately absent from `savings` / `savings_history`: a shadow
            # projection is not a realized saving.
            "jev": {
                **proxy.metrics.jev_snapshot(),
                "config": proxy.config.jev.redacted(),
            },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_stats_block.py tests/test_jev_metrics.py tests/test_jev_shadow.py tests/test_jev_active_hook.py tests/test_jev_compaction_decision.py tests/test_jev_compaction_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/jev/accounting.py headroom/proxy/prometheus_metrics.py headroom/proxy/jev/shadow.py headroom/proxy/jev/active_hook.py headroom/proxy/jev/compaction_decision.py headroom/proxy/jev/compaction_hook.py headroom/proxy/server.py tests/test_jev_stats_block.py
git commit -m "feat(jev): report the jev accounting block on /stats without leaking the key"
```

---

### Task 29: Document the Jev configuration, privacy and fail-open contract

**Files:**
- Modify: `wiki/configuration.md` (append a section immediately after the
  `## Environment Variables` table, which ends at line 244, and before
  `## Settings GUI` at line 245)
- Test: `tests/test_jev_docs.py`

**Interfaces:**
- Consumes: the env var names `JevConfig.from_env` reads (Task 1) and the fail-open
  behaviour Tasks 9, 16 and 25 implement.
- Produces: no code interface. The design doc's "Documentation" requirement for
  `wiki/configuration.md` — track A/B configuration, privacy disclosure, fail-open
  behaviour. (`wiki/ccr.md` is covered by Task 18 and `wiki/proxy.md` by Task 27.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jev_docs.py
"""Every HEADROOM_JEV_* knob the code reads is documented, with the privacy and
fail-open disclosure the design doc requires.

A retention feature that sends tool output to a third-party API and can replace
it with a retrieval marker is exactly the kind of thing an operator must be able
to read about before switching it on; an undocumented knob here is a defect.
"""

from __future__ import annotations

from pathlib import Path

WIKI_CONFIG = Path(__file__).parent.parent / "wiki" / "configuration.md"


def test_every_jev_env_var_is_documented() -> None:
    doc = WIKI_CONFIG.read_text(encoding="utf-8")
    for name in (
        "HEADROOM_JEV_MODE",
        "HEADROOM_JEV_API_KEY",
        "HEADROOM_JEV_ENDPOINT",
        "HEADROOM_JEV_MODEL",
        "HEADROOM_JEV_TIMEOUT_MS",
        "HEADROOM_JEV_THRESHOLD_PERCENT",
        "HEADROOM_JEV_COOLDOWN_TURNS",
        "HEADROOM_JEV_MAX_CANDIDATE_TOKENS",
        "HEADROOM_JEV_MAX_CANDIDATES",
        "HEADROOM_JEV_MAX_STATE_TOKENS",
    ):
        assert name in doc, f"{name} is read by JevConfig.from_env but undocumented"


def test_the_documented_knobs_are_the_knobs_the_code_reads() -> None:
    # The reverse direction: a knob added to config.py without a doc entry.
    import re

    from headroom.proxy.jev import config as jev_config

    source = Path(jev_config.__file__).read_text(encoding="utf-8")
    doc = WIKI_CONFIG.read_text(encoding="utf-8")
    for name in sorted(set(re.findall(r"HEADROOM_JEV_[A-Z_]+", source))):
        assert name in doc, f"{name} is read by config.py but undocumented"


def test_privacy_and_fail_open_are_disclosed() -> None:
    doc = WIKI_CONFIG.read_text(encoding="utf-8")
    section = doc[doc.index("## Jev Retention") :]
    assert "off" in section  # default
    assert "shadow" in section and "active" in section
    # What actually leaves the machine, and what never does.
    assert "tool result" in section
    assert "never logged" in section
    assert "fails open" in section
    # Jev is additive to Headroom's own compression, never a replacement.
    assert "additive" in section
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jev_docs.py -q`
Expected: FAIL with "AssertionError: HEADROOM_JEV_MODE is read by JevConfig.from_env but undocumented"

- [ ] **Step 3: Write minimal implementation**

Insert into `wiki/configuration.md` immediately after the `## Environment Variables`
table (line 244) and before `## Settings GUI` (line 245):

```markdown
## Jev Retention (default off)

Jev is a third-party retention-decision service (TypeSafe System One). With it
enabled, Headroom asks it — per historical tool result, after its own
deterministic compression has already run — whether that result must stay
verbatim, can be truncated, or can be replaced by a retrievable CCR marker. It is
**additive** to Headroom's compression and never a substitute for it.

| Variable | Description | Default |
|----------|-------------|---------|
| `HEADROOM_JEV_MODE` | `off`, `shadow` or `active`. `off`: no Jev code runs and no other `HEADROOM_JEV_*` variable is read at all. `shadow`: Jev is called and a projection is recorded, but the forwarded request is never modified. `active`: decisions are applied at a declared compaction boundary only (`POST /v1/compress` with `config.jev_compaction_boundary=true`, or Codex's native WebSocket compaction). | `off` |
| `HEADROOM_JEV_API_KEY` | API key. Required whenever the mode is not `off`; the proxy refuses to start without it rather than silently no-opping. Travels only in an `Authorization` header and is **never logged**, never written to the multi-worker config payload, and never returned by `/stats`. | - |
| `HEADROOM_JEV_ENDPOINT` | Jev API endpoint. Logged and reported only as scheme + host + path — any userinfo or query string is redacted, so a token in the URL cannot leak through an error message. | `https://api.typesafe.ai/v1/systemone` |
| `HEADROOM_JEV_MODEL` | Jev model name. | `jev-latest` |
| `HEADROOM_JEV_TIMEOUT_MS` | Hard bound on the whole Jev round trip. This is added latency on the turns that pass the threshold and cooldown gates, so keep it small. | `500` |
| `HEADROOM_JEV_THRESHOLD_PERCENT` | Shadow mode only: skip the call until post-Headroom tokens reach this percentage of the model's context window. | `80` |
| `HEADROOM_JEV_COOLDOWN_TURNS` | Shadow mode only: turns to wait before another call on the same session/branch. | `5` |
| `HEADROOM_JEV_MAX_CANDIDATE_TOKENS` | Per-candidate ceiling on how much content is shown to Jev. | `20000` |
| `HEADROOM_JEV_MAX_CANDIDATES` | Maximum candidates per call (oldest first). Applies in both `shadow` and `active` mode. | `12` |
| `HEADROOM_JEV_MAX_STATE_TOKENS` | Ceiling on the MEASURED serialized request, in both `shadow` and `active` mode. Jev rejects an oversized request outright, losing the whole call, so the request is trimmed until it really fits. Raise this (with `HEADROOM_JEV_MAX_CANDIDATES`) if you want a bigger request at a compaction boundary. | `8000` |

**What leaves this machine.** When the mode is not `off`, the content of eligible
historical **tool results** (bounded by `HEADROOM_JEV_MAX_CANDIDATE_TOKENS`) plus
their metadata — role, tool call id, position, size, SHA-256 — is sent to the Jev
endpoint, along with an opaque session id, branch id and revision. Prompts, user
messages, assistant messages and system prompts are not sent. Jev is a
retention-decision service, not an anonymizer: nothing is redacted on the way out,
so do not enable it on traffic whose tool output you would not send to a
third-party API. The default is `off`, and with it off no `HEADROOM_JEV_*`
variable other than the mode is even read.

**It fails open, everywhere.** A timeout, a malformed or unparseable answer, an
unknown model, a stale revision, a candidate that does not fit the request budget,
a missing `headroom_retrieve` tool, or any failed CCR write, acknowledgement or
lease all result in Headroom forwarding its ordinary compressed output unchanged.
An ambiguous answer is always read as `keep`. Every one of those exits is counted
on `headroom_jev_events_total{event}`, and the aggregate is on `/stats` under
`jev` (with the endpoint and key redacted), so a deployment that silently never
calls Jev is visible as a counter rather than as an absent log line.

**Nothing is deleted in active mode.** Before a tool result is truncated or
replaced, the original is written to the CCR store, read back to confirm the write
was acknowledged, and given a 24-hour retention lease; the replacement is a
`Retrieve more: hash=…` marker the model redeems with the `headroom_retrieve` tool
or `POST /v1/retrieve`. If any of those steps fails, the original content is
forwarded untouched. See [CCR](ccr.md) for the `/v1/compress` boundary request
shape and [Proxy](proxy.md) for the Codex WebSocket compaction boundary.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jev_docs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wiki/configuration.md tests/test_jev_docs.py
git commit -m "docs(jev): document the HEADROOM_JEV_* knobs, privacy disclosure and fail-open contract"
```
