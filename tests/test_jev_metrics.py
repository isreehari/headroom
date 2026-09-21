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


def test_record_jev_event_none_defaults_to_unknown() -> None:
    """A caller wiring this up via ``getattr`` can pass ``None``; it must not
    become a ``None`` key that breaks the text format on export."""
    metrics = PrometheusMetrics()
    metrics.record_jev_event(None)  # type: ignore[arg-type]
    assert metrics.jev_events_by_event["unknown"] == 1


async def test_jev_events_are_exported() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_fail_open")

    text = await metrics.export()

    assert "# TYPE headroom_jev_events_total counter" in text
    assert 'headroom_jev_events_total{event="shadow_fail_open"} 1' in text


async def test_jev_event_label_is_escaped() -> None:
    """Event names are internal constants, but a stray quote or newline must
    still not be able to forge a second series line."""
    metrics = PrometheusMetrics()
    metrics.record_jev_event('bad"name\nshadow_projected')

    text = await metrics.export()

    assert 'headroom_jev_events_total{event="bad\\"name\\nshadow_projected"} 1' in text


async def test_jev_events_absent_when_never_recorded() -> None:
    metrics = PrometheusMetrics()

    text = await metrics.export()

    assert "headroom_jev_events_total" not in text


async def test_reset_runtime_clears_jev_events() -> None:
    metrics = PrometheusMetrics()
    metrics.record_jev_event("shadow_projected")

    await metrics.reset_runtime()

    assert dict(metrics.jev_events_by_event) == {}
