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

* One bounded call. ``timeout_ms`` (default 500) caps the whole round trip: the
  per-request ``httpx.Timeout`` bounds each *phase* (connect/read/write/pool),
  and an outer ``asyncio.wait_for`` bounds the *total*, so neither a slow-drip
  response nor a transport that ignores timeouts (a caller-injected mock, a
  custom ``AsyncBaseTransport``) can hold the request path open.
* :meth:`JevClient.decide` never raises. Every failure — timeout, TLS, 4xx/5xx,
  non-JSON, missing ``answers``, an unrecognized choice — resolves to ``keep``
  for every candidate, so an ambiguous answer can never move a token. The one
  deliberate exception is ``asyncio.CancelledError``: swallowing the caller's
  own cancellation would be a bug, not a fail-open.
* The API key travels in a header and is never logged. The endpoint may carry
  credentials in its userinfo or a token in its query string, so every error
  string is scrubbed through :func:`_scrub` (endpoint via ``redact_endpoint``,
  plus the key itself) before it leaves this module.
"""

from __future__ import annotations

import asyncio
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

_MAX_ERROR_CHARS = 400


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


def _scrub(text: str, config: JevConfig) -> str:
    """Strip anything credential-bearing out of a string bound for an error.

    Two sources leak: httpx echoes the request URL into most of its exception
    messages, and a gateway can echo the ``Authorization`` header or the full
    URL back in an error body. So both the endpoint (in the spelling we sent
    *and* in httpx's normalized spelling, which may differ by an added default
    port or percent-encoding) and the raw key are replaced.
    """
    endpoint = config.endpoint
    if endpoint:
        redacted = redact_endpoint(endpoint)
        text = text.replace(endpoint, redacted)
        try:
            normalized = str(httpx.URL(endpoint))
        except Exception:  # pragma: no cover - a URL httpx itself cannot parse
            normalized = ""
        if normalized and normalized != endpoint:
            text = text.replace(normalized, redacted)
    if config.api_key:
        text = text.replace(config.api_key, "<redacted api key>")
    return text


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
        self._closed = False

    def _client(self) -> httpx.AsyncClient:
        if self._closed:
            # Without this, a decide() after aclose() would silently build a
            # fresh real client and dial the live endpoint during shutdown.
            raise RuntimeError("JevClient is closed")
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout())
            self._owns_client = True
        return self._http_client

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._config.timeout_ms / 1000.0)

    async def aclose(self) -> None:
        client, self._http_client = self._http_client, None
        self._closed = True
        if client is not None and self._owns_client:
            await client.aclose()

    async def decide(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        candidate_ids: Sequence[str],
    ) -> JevAnswer:
        """One bounded call. Never raises; anything ambiguous becomes ``keep``."""
        answer = JevAnswer(decisions=dict.fromkeys(candidate_ids, "keep"))
        payload = build_request_payload(self._config, state, questions)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        budget_s = self._config.timeout_ms / 1000.0

        started = time.perf_counter()
        try:
            # The per-request timeout overrides whatever the client was built
            # with (a caller may inject one with no timeout at all); wait_for
            # is the backstop for the total, including transports that honour
            # no timeout of their own.
            response = await asyncio.wait_for(
                self._client().post(
                    self._config.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout(),
                ),
                timeout=budget_s,
            )
        except Exception as exc:  # timeout, DNS, TLS, connection reset
            answer.latency_ms = (time.perf_counter() - started) * 1000.0
            # httpx error strings routinely embed the request URL.
            answer.error = _scrub(f"{type(exc).__name__}: {exc}", self._config)
            return answer
        answer.latency_ms = (time.perf_counter() - started) * 1000.0

        if response.status_code >= 400:
            # Scrub before truncating: a key straddling the cut would otherwise
            # survive as a prefix.
            detail = _scrub(response.text, self._config)[:_MAX_ERROR_CHARS]
            answer.error = f"HTTP {response.status_code}: {detail}"
            return answer

        try:
            body = response.json()
        except Exception as exc:
            # Only the exception's type name -- the message can quote the body.
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
            # Keys only: a value could be anything the server chose to echo.
            answer.error = (
                f"no dict at '{ANSWERS_FIELD}' (keys were {sorted(map(str, body))[:12]}); "
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
