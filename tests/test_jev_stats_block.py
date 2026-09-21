"""The /stats `jev` block reports Jev accounting and never a secret.

Three tracks report through ONE recorder and ONE error classifier
(:mod:`headroom.proxy.jev.accounting`), so "a failed call" cannot come to mean
three slightly different things. The tests below pin that sharing by identity,
not by convention, and pin the one semantic the design doc makes binding: the
shadow projection ``TP`` is reported on its own and is never folded into the
realized savings measured by Tracks B and C.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.accounting import classify_call_error, record_jev_accounting
from headroom.proxy.jev.client import JevAnswer
from headroom.proxy.jev.compaction_hook import apply_jev_compaction_boundary
from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.shadow import JevShadowRunner
from headroom.proxy.prometheus_metrics import JEV_ACCOUNTING_FIELDS, PrometheusMetrics

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402

ENDPOINT = "https://user:pw@jev.example.test/v1/decide?token=abc"
API_KEY = "sk-jev-super-secret-key"


# ---------------------------------------------------------------------------
# The counter itself
# ---------------------------------------------------------------------------


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
        "applied",
        "ccr_staged",
        "ccr_acknowledged",
        "ccr_failed",
        "fallbacks",
        "projected_savings",
        "realized_savings",
        "realized_savings_estimated",
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


def test_a_shadow_projection_alone_never_produces_a_realized_saving() -> None:
    """The binding semantic, stated as its own test.

    Track A can record an enormous projection; until Track B or C measures a
    rewrite, ``realized_savings`` must stay exactly zero.
    """
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(
        tokens_baseline=100_000, tokens_headroom=80_000, tokens_projected=10_000
    )

    snapshot = metrics.jev_snapshot()
    assert snapshot["projected_savings"] == 70_000
    assert snapshot["realized_savings"] == 0
    assert snapshot["tokens_final"] == 0


def test_savings_never_go_negative() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(tokens_headroom=100, tokens_projected=400)
    metrics.record_jev_accounting(tokens_active_baseline=100, tokens_final=400)
    snapshot = metrics.jev_snapshot()
    assert snapshot["projected_savings"] == 0
    assert snapshot["realized_savings"] == 0


def test_record_jev_accounting_drops_unusable_values_without_raising() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(calls_attempted="3")  # type: ignore[arg-type]
    metrics.record_jev_accounting(calls_attempted=None)  # type: ignore[arg-type]
    metrics.record_jev_accounting(drop=object())  # type: ignore[arg-type]
    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 3
    assert snapshot["drop"] == 0


def test_call_errors_are_classified_once_for_every_track() -> None:
    assert classify_call_error("ReadTimeout: timed out") == "calls_timed_out"
    assert classify_call_error("TimeoutError: ") == "calls_timed_out"
    assert classify_call_error("HTTP 401: invalid api key") == "calls_rejected"
    assert classify_call_error("max_tokens_exceeded") == "calls_rejected"
    assert classify_call_error(None) is None
    assert classify_call_error("") is None


def test_the_classifier_is_literally_shared_by_every_track() -> None:
    """One definition of "a failed call", not three lookalikes."""
    from headroom.proxy.jev import accounting, active_hook, compaction_decision, shadow

    assert shadow.classify_call_error is accounting.classify_call_error
    assert active_hook.classify_call_error is accounting.classify_call_error
    assert compaction_decision.classify_call_error is accounting.classify_call_error
    assert shadow.record_jev_accounting is accounting.record_jev_accounting
    assert active_hook.record_jev_accounting is accounting.record_jev_accounting
    assert compaction_decision.record_jev_accounting is accounting.record_jev_accounting


def test_record_jev_accounting_helper_tolerates_a_metricsless_proxy() -> None:
    class _Exploding:
        def record_jev_accounting(self, **fields: int) -> None:
            raise RuntimeError("counter blew up")

    record_jev_accounting(None, calls_attempted=1)  # must not raise
    record_jev_accounting(object(), calls_attempted=1)  # no recorder at all
    record_jev_accounting(_Exploding(), calls_attempted=1)


def test_record_jev_accounting_helper_tolerates_a_raising_attribute() -> None:
    class _Hostile:
        @property
        def record_jev_accounting(self) -> Any:
            raise RuntimeError("even the lookup explodes")

    record_jev_accounting(_Hostile(), calls_attempted=1)


async def test_metrics_export_carries_the_accounting_totals() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(drop=3, calls_timed_out=1)
    export = await metrics.export()
    assert 'headroom_jev_accounting_total{field="drop"} 3' in export
    assert 'headroom_jev_accounting_total{field="calls_timed_out"} 1' in export
    assert 'headroom_jev_accounting_total{field="keep"} 0' in export


async def test_every_accounting_field_is_exported_even_at_zero() -> None:
    export = await PrometheusMetrics().export()
    for field in JEV_ACCOUNTING_FIELDS:
        assert f'headroom_jev_accounting_total{{field="{field}"}} 0' in export


def test_jev_snapshot_carries_the_event_buckets() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_projected")
    assert metrics.jev_snapshot()["events"] == {"shadow_projected": 1}


async def test_reset_runtime_clears_jev_totals() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_accounting(calls_attempted=1)
    await metrics.reset_runtime()
    assert metrics.jev_snapshot()["calls_attempted"] == 0


# ---------------------------------------------------------------------------
# Track A (shadow): the projection, and the two signals earlier tasks deferred
# ---------------------------------------------------------------------------

_SHADOW_CONFIG = JevConfig(
    mode="shadow",
    api_key=API_KEY,
    endpoint=ENDPOINT,
    threshold_percent=50,
    cooldown_turns=2,
    max_candidates=12,
    max_state_tokens=100_000,
    max_candidate_tokens=20_000,
)


def _count_text(text: str) -> int:
    return max(1, len(text) // 4)


def _count_messages(messages: list[dict[str, Any]]) -> int:
    return sum(_count_text(str(m.get("content") or "")) for m in messages)


class _ShadowClient:
    def __init__(self, decision: str = "drop", error: str | None = None) -> None:
        self.decision = decision
        self.error = error

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> JevAnswer:
        if self.error is not None:
            return JevAnswer(decisions=dict.fromkeys(candidate_ids, "keep"), error=self.error)
        return JevAnswer(decisions=dict.fromkeys(candidate_ids, self.decision))

    async def aclose(self) -> None:
        return None


def _shadow_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c0", "content": "A" * 2000},
        {"role": "tool", "tool_call_id": "c1", "content": "B" * 2000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]


async def _run_shadow(
    metrics: PrometheusMetrics,
    *,
    client: Any,
    optimized_tokens: int = 900,
    count_text: Any = _count_text,
) -> Any:
    runner = JevShadowRunner(_SHADOW_CONFIG, client=client, metrics=metrics)
    return await runner.maybe_run(
        provider="openai",
        model="gpt-5.6",
        messages=_shadow_messages(),
        frozen_prefix=1,
        optimized_tokens=optimized_tokens,
        original_tokens=4000,
        context_limit=1000,
        session_id="sess-1",
        count_text=count_text,
        count_messages=_count_messages,
        message_shape="openai",
    )


async def test_shadow_projection_records_t0_th_and_tp_but_no_realized_saving() -> None:
    metrics = PrometheusMetrics()
    await _run_shadow(metrics, client=_ShadowClient("drop"))

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_completed"] == 1
    assert snapshot["candidates"] == 2
    assert snapshot["candidates_sent"] == 2
    assert snapshot["drop"] == 2
    assert snapshot["tokens_baseline"] == 4000
    assert snapshot["tokens_headroom"] > 0
    assert snapshot["projected_savings"] > 0
    # Track A measures nothing; TF and the realized series stay untouched.
    assert snapshot["tokens_active_baseline"] == 0
    assert snapshot["tokens_final"] == 0
    assert snapshot["realized_savings"] == 0
    assert snapshot["applied"] == 0


async def test_shadow_call_timeout_is_counted_as_a_timeout_not_a_rejection() -> None:
    metrics = PrometheusMetrics()
    await _run_shadow(metrics, client=_ShadowClient(error="ReadTimeout: bound exceeded"))

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_failed"] == 1
    assert snapshot["calls_timed_out"] == 1
    assert snapshot["calls_rejected"] == 0
    assert snapshot["fallbacks"] == 1
    assert snapshot["calls_completed"] == 0


async def test_shadow_call_rejection_is_counted_as_a_rejection() -> None:
    metrics = PrometheusMetrics()
    await _run_shadow(metrics, client=_ShadowClient(error="HTTPStatusError: 401 unauthorized"))

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_rejected"] == 1
    assert snapshot["calls_timed_out"] == 0
    assert snapshot["calls_failed"] == 1


async def test_shadow_fail_open_is_visible_as_its_own_event_bucket() -> None:
    """Deferred from Task 8/10: the fail-open guard also swallows programming
    errors, so a rising ``shadow_fail_open`` is the "something is broken"
    signal rather than merely a quiet one. It has to be on /stats."""

    def _explodes(text: str) -> int:
        raise RuntimeError(f"tokenizer exploded with {API_KEY} at {ENDPOINT}")

    metrics = PrometheusMetrics()
    await _run_shadow(metrics, client=_ShadowClient(), count_text=_explodes)

    events = metrics.jev_snapshot()["events"]
    assert events.get("shadow_fail_open") == 1


async def test_below_threshold_is_visible_as_its_own_event_bucket() -> None:
    """Deferred from the Responses recount work: a too-tight recount timeout
    manifests as a silent rise in ``shadow_below_threshold`` and nothing
    else, so the count has to be readable off /stats."""
    metrics = PrometheusMetrics()
    await _run_shadow(metrics, client=_ShadowClient(), optimized_tokens=10)

    events = metrics.jev_snapshot()["events"]
    assert events.get("shadow_below_threshold") == 1
    assert metrics.jev_snapshot()["calls_attempted"] == 0


# ---------------------------------------------------------------------------
# Track B (active): applied vs candidates, and the CCR staging gap
# ---------------------------------------------------------------------------


class _ActiveConfigHolder:
    def __init__(self, jev: JevConfig) -> None:
        self.jev = jev


class _ActiveProxy:
    def __init__(self, jev: JevConfig, metrics: PrometheusMetrics) -> None:
        self.config = _ActiveConfigHolder(jev)
        self.metrics = metrics


def _active_config() -> JevConfig:
    return JevConfig(
        mode="active",
        api_key=API_KEY,
        endpoint=ENDPOINT,
        model="jev-test",
        timeout_ms=500,
        threshold_percent=80,
        cooldown_turns=5,
        max_candidate_tokens=4000,
        max_candidates=12,
        max_state_tokens=200_000,
    )


def _blob(seed: str, rows: int = 60) -> str:
    return json.dumps([{"id": i, "seed": seed, "blob": "z" * 200} for i in range(rows)])


def _active_messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_0", "content": _blob("a")},
        {"role": "tool", "tool_call_id": "call_1", "content": _blob("b")},
        *[{"role": "assistant", "content": f"step {i}"} for i in range(6)],
    ]


def _install_active_client(
    monkeypatch: pytest.MonkeyPatch, *, decision: str = "drop", error: str | None = None
) -> None:
    monkeypatch.setattr(
        "headroom.proxy.jev.active_hook.JevClient",
        lambda config, **kwargs: _ShadowClient(decision, error),
    )


class _HalfRefusingStore(CompressionStore):
    """Read-back refuses the first candidate: one lease of two is taken."""

    def __init__(self, *args: Any, refuse: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._refuse = refuse
        self.peeks = 0

    def peek(self, hash_key: str) -> Any:
        self.peeks += 1
        if self.peeks <= self._refuse:
            return None
        return super().peek(hash_key)


async def test_active_applied_records_its_own_baseline_and_measured_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from headroom.proxy.jev.active_hook import run_jev_active_retention

    _install_active_client(monkeypatch)
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: store)

    metrics = PrometheusMetrics()
    proxy = _ActiveProxy(_active_config(), metrics)
    result = await run_jev_active_retention(
        proxy=proxy, messages=_active_messages(), model="gpt-4o", session_id="s1"
    )
    assert result.reason == "applied"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_completed"] == 1
    assert snapshot["candidates"] == 2
    assert snapshot["drop"] == 2
    assert snapshot["applied"] == result.applied == 2
    assert snapshot["ccr_staged"] == 2
    assert snapshot["ccr_acknowledged"] == 2
    assert snapshot["ccr_failed"] == 0
    assert snapshot["tokens_active_baseline"] > snapshot["tokens_final"] > 0
    assert snapshot["realized_savings"] == (
        snapshot["tokens_active_baseline"] - snapshot["tokens_final"]
    )
    # Track B measures with the tokenizer, so none of this is an estimate.
    assert snapshot["realized_savings_estimated"] == 0
    # Track B never touches the projection pair.
    assert snapshot["tokens_projected"] == 0
    assert snapshot["projected_savings"] == 0


async def test_active_partial_staging_shows_the_candidates_applied_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred from Task 16: a partial staging failure is reported as
    ``active_applied`` and is otherwise visible only as the gap between
    candidates and applied. Both are on /stats, and ``ccr_failed`` names it."""
    from headroom.proxy.jev.active_hook import run_jev_active_retention

    _install_active_client(monkeypatch)
    store = _HalfRefusingStore(
        default_ttl=60, enable_feedback=False, backend=InMemoryBackend(), refuse=1
    )
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: store)

    metrics = PrometheusMetrics()
    proxy = _ActiveProxy(_active_config(), metrics)
    result = await run_jev_active_retention(
        proxy=proxy, messages=_active_messages(), model="gpt-4o", session_id="s1"
    )
    assert result.reason == "applied"

    snapshot = metrics.jev_snapshot()
    assert snapshot["candidates"] == 2
    assert snapshot["applied"] == 1
    assert snapshot["ccr_staged"] == 2
    assert snapshot["ccr_acknowledged"] == 1
    assert snapshot["ccr_failed"] == 1
    assert snapshot["events"].get("active_applied") == 1


async def test_active_call_failure_is_classified_like_every_other_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from headroom.proxy.jev.active_hook import run_jev_active_retention

    _install_active_client(monkeypatch, error="ConnectTimeout: no route")
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: store)

    metrics = PrometheusMetrics()
    proxy = _ActiveProxy(_active_config(), metrics)
    result = await run_jev_active_retention(
        proxy=proxy, messages=_active_messages(), model="gpt-4o", session_id="s1"
    )
    assert result.reason == "call_failed"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_timed_out"] == 1
    assert snapshot["calls_failed"] == 1
    assert snapshot["fallbacks"] == 1
    assert snapshot["applied"] == 0


async def test_active_no_lease_is_counted_as_ccr_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from headroom.proxy.jev.active_hook import run_jev_active_retention

    _install_active_client(monkeypatch)
    store = _HalfRefusingStore(
        default_ttl=60, enable_feedback=False, backend=InMemoryBackend(), refuse=99
    )
    monkeypatch.setattr("headroom.proxy.jev.active_hook.get_compression_store", lambda: store)

    metrics = PrometheusMetrics()
    proxy = _ActiveProxy(_active_config(), metrics)
    result = await run_jev_active_retention(
        proxy=proxy, messages=_active_messages(), model="gpt-4o", session_id="s1"
    )
    assert result.reason == "no_lease"

    snapshot = metrics.jev_snapshot()
    assert snapshot["ccr_staged"] == 2
    assert snapshot["ccr_acknowledged"] == 0
    assert snapshot["ccr_failed"] == 2
    assert snapshot["applied"] == 0
    assert snapshot["tokens_final"] == 0


# ---------------------------------------------------------------------------
# Track C (Codex WS compaction boundary)
# ---------------------------------------------------------------------------


class _CompactionConfig:
    mode = "active"
    timeout_ms = 5000
    max_candidate_tokens = 0
    model = "jev-latest"
    endpoint = ENDPOINT
    api_key = API_KEY


class _CompactionClient:
    def __init__(self, decision: str = "drop", error: str | None = None) -> None:
        self.decision = decision
        self.error = error

    async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
        return JevAnswer(decisions={candidate_ids[0]: self.decision}, error=self.error)


def _compaction_frame() -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.6-sol",
                "previous_response_id": "resp_abc123",
                "tools": [{"type": "function", "name": CCR_TOOL_NAME}],
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_9",
                        "output": "stdout body " * 200,
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )


async def _run_compaction(metrics: PrometheusMetrics, **kwargs: Any) -> tuple[str, str]:
    defaults: dict[str, Any] = {
        "jev_config": _CompactionConfig(),
        "client": _CompactionClient(),
        "session_id": "ws1",
        "request_id": "req1",
        "revisions": JevCompactionRevisionStore(),
        "metrics": metrics,
        "store": CompressionStore(backend=InMemoryBackend()),
    }
    defaults.update(kwargs)
    return await apply_jev_compaction_boundary(_compaction_frame(), **defaults)


async def test_compaction_drop_records_an_estimated_realized_saving() -> None:
    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics)
    assert reason == "jev_compaction_dropped"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_completed"] == 1
    assert snapshot["candidates"] == 1
    assert snapshot["candidates_sent"] == 1
    assert snapshot["candidate_tokens"] > 0
    assert snapshot["drop"] == 1
    assert snapshot["applied"] == 1
    assert snapshot["ccr_staged"] == 1
    assert snapshot["ccr_acknowledged"] == 1
    assert snapshot["realized_savings"] > 0
    # Track C's pair is `bytes // 4` on both sides, so every token of it is
    # declared as an estimate rather than passed off as a measurement.
    assert snapshot["realized_savings_estimated"] == snapshot["realized_savings"]
    assert snapshot["tokens_projected"] == 0


async def test_compaction_keep_records_a_keep_and_no_saving() -> None:
    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics, client=_CompactionClient("keep"))
    assert reason == "jev_compaction_keep"

    snapshot = metrics.jev_snapshot()
    assert snapshot["keep"] == 1
    assert snapshot["candidates"] == 1
    assert snapshot["calls_completed"] == 1
    assert snapshot["applied"] == 0
    assert snapshot["realized_savings"] == 0


async def test_compaction_keep_and_stale_revision_are_both_visible_events() -> None:
    """Deferred from Task 23/25: everything after the revision claim is
    single-shot, so a flaky endpoint shows up as a high ``compaction_keep``
    next to a high ``compaction_stale_revision`` and retention is permanently
    forgone. Both buckets have to be readable off one /stats payload."""
    metrics = PrometheusMetrics()
    revisions = JevCompactionRevisionStore()
    store = CompressionStore(backend=InMemoryBackend())

    _out, first = await _run_compaction(
        metrics, client=_CompactionClient("keep"), revisions=revisions, store=store
    )
    _out, second = await _run_compaction(
        metrics, client=_CompactionClient("drop"), revisions=revisions, store=store
    )
    assert first == "jev_compaction_keep"
    assert second == "jev_compaction_stale_revision"

    events = metrics.jev_snapshot()["events"]
    assert events.get("compaction_keep") == 1
    assert events.get("compaction_stale_revision") == 1


async def test_compaction_call_timeout_is_classified_as_a_timeout() -> None:
    class _Slow:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            raise TimeoutError("bound exceeded")

    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics, client=_Slow())
    assert reason == "jev_compaction_keep"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_timed_out"] == 1
    assert snapshot["calls_failed"] == 1
    assert snapshot["fallbacks"] == 1


async def test_compaction_call_rejection_is_classified_as_a_rejection() -> None:
    class _Refuses:
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            raise RuntimeError("HTTP 401: invalid api key")

    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics, client=_Refuses())
    assert reason == "jev_compaction_keep"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_rejected"] == 1
    assert snapshot["calls_timed_out"] == 0
    assert snapshot["calls_failed"] == 1


async def test_compaction_answer_error_is_classified_from_the_shared_classifier() -> None:
    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(
        metrics, client=_CompactionClient("drop", error="ReadTimeout: upstream")
    )
    assert reason == "jev_compaction_keep"

    snapshot = metrics.jev_snapshot()
    assert snapshot["calls_attempted"] == 1
    assert snapshot["calls_timed_out"] == 1
    assert snapshot["calls_completed"] == 0


async def test_compaction_ccr_failure_counts_the_drop_but_not_the_saving() -> None:
    class _RefusingStore(CompressionStore):
        def peek(self, hash_key: str) -> Any:
            return None

    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics, store=_RefusingStore(backend=InMemoryBackend()))
    assert reason == "jev_compaction_ccr_failed"

    snapshot = metrics.jev_snapshot()
    assert snapshot["candidates"] == 1
    assert snapshot["drop"] == 1
    assert snapshot["ccr_staged"] == 1
    assert snapshot["ccr_acknowledged"] == 0
    assert snapshot["ccr_failed"] == 1
    assert snapshot["applied"] == 0
    assert snapshot["realized_savings"] == 0


async def test_compaction_pays_nothing_when_jev_is_off() -> None:
    class _Off(_CompactionConfig):
        mode = "off"

    metrics = PrometheusMetrics()
    _out, reason = await _run_compaction(metrics, jev_config=_Off())
    assert reason == "jev_compaction_disabled"
    assert metrics.jev_snapshot()["calls_attempted"] == 0
    assert metrics.jev_snapshot()["events"] == {}


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------


def _proxy_config(**jev_overrides: Any) -> ProxyConfig:
    values: dict[str, Any] = {
        "mode": "shadow",
        "api_key": API_KEY,
        "endpoint": ENDPOINT,
        "model": "jev-test",
    }
    values.update(jev_overrides)
    return ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig(**values),
    )


def test_stats_exposes_a_redacted_jev_block() -> None:
    config = _proxy_config()
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


def test_stats_never_carries_the_jev_key_or_the_raw_endpoint() -> None:
    """The one thing this block must never do.

    The endpoint is deliberately given userinfo and a query token, because
    ``redact_endpoint`` keeps scheme+host+path and those are exactly the parts
    that must not survive.
    """
    config = _proxy_config()
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        client.app.state.proxy.metrics.record_jev_event("shadow_projected")
        serialized = json.dumps(client.get("/stats").json())

    assert API_KEY not in serialized
    assert ENDPOINT not in serialized
    assert "user:pw" not in serialized
    assert "token=abc" not in serialized
    # The redacted label is still there, so an operator can tell WHICH
    # endpoint is configured without being handed a credential.
    assert "jev.example.test" in serialized


def test_stats_jev_block_is_zeroed_and_off_on_an_unconfigured_proxy() -> None:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
        jev=JevConfig(),
    )
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        payload = client.get("/stats").json()

    jev = payload["jev"]
    assert jev["config"]["mode"] == "off"
    assert jev["config"]["api_key_configured"] is False
    assert jev["events"] == {}
    assert all(jev[field] == 0 for field in JEV_ACCOUNTING_FIELDS)
    assert jev["projected_savings"] == 0
    assert jev["realized_savings"] == 0


def test_stats_reports_the_event_buckets_the_deferred_reviews_asked_for() -> None:
    config = _proxy_config()
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        metrics = client.app.state.proxy.metrics
        for event in (
            "shadow_fail_open",
            "shadow_below_threshold",
            "compaction_keep",
            "compaction_stale_revision",
        ):
            metrics.record_jev_event(event)
        payload = client.get("/stats").json()

    events = payload["jev"]["events"]
    assert events["shadow_fail_open"] == 1
    assert events["shadow_below_threshold"] == 1
    assert events["compaction_keep"] == 1
    assert events["compaction_stale_revision"] == 1


def test_stats_history_is_not_extended_with_the_projection() -> None:
    """``TP`` is reported once, on /stats, and never enters the durable
    realized-savings series the design doc protects."""
    config = _proxy_config()
    with TestClient(
        create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)
    ) as client:
        client.app.state.proxy.metrics.record_jev_accounting(
            tokens_headroom=10_000, tokens_projected=1_000
        )
        payload = client.get("/stats").json()
        history = client.get("/stats-history").json()

    assert payload["jev"]["projected_savings"] == 9_000
    assert payload["savings_history"] == []
    assert "jev" not in json.dumps(history)
