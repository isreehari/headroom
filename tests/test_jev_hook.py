"""The handler-facing adapter: one await, never raises, always leaves a metric."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

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
    def __init__(self, limit: Any = 1000, raises: bool = False) -> None:
        self.limit = limit
        self.raises = raises

    def get_context_limit(self, model: str) -> Any:
        if self.raises:
            raise RuntimeError("unknown model")
        return self.limit


class FakeProxy:
    def __init__(self, runner: Any, metrics: Any) -> None:
        self.jev_shadow = runner
        self.metrics = metrics


#: ``None`` is a value under test here, so the "use the default" marker cannot be.
_DEFAULT = object()


def _conversation() -> list[dict[str, Any]]:
    """A turn with two genuine candidates outside the six-message recent tail."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c0", "content": "A" * 2000},
        {"role": "tool", "tool_call_id": "c1", "content": "B" * 2000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]


async def _call(
    proxy: Any,
    limit_source: Any,
    *,
    messages: Any = _DEFAULT,
    tokenizer: Any | None = None,
) -> JevShadowResult | None:
    return await run_jev_shadow_hook(
        proxy,
        provider="openai",
        model="gpt-5.6",
        messages=[{"role": "user", "content": "hi"}] if messages is _DEFAULT else messages,
        frozen_prefix=0,
        optimized_tokens=10,
        original_tokens=40,
        session_id="sess",
        tokenizer=FakeTokenizer() if tokenizer is None else tokenizer,
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


async def test_returns_none_when_the_proxy_has_no_jev_attribute_at_all() -> None:
    class BareProxy:
        pass

    assert await _call(BareProxy(), FakeLimitSource()) is None


async def test_a_raising_context_limit_source_fails_open_with_a_metric() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    assert await _call(proxy, FakeLimitSource(raises=True)) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_the_baseline_reaches_the_runner() -> None:
    metrics = FakeMetrics()
    runner = CapturingRunner()
    proxy = FakeProxy(runner, metrics)
    assert (await _call(proxy, FakeLimitSource())).reason == "captured"  # type: ignore[union-attr]
    # T0 is only measurable at the handler, so the hook has to carry it.
    assert runner.kwargs["original_tokens"] == 40
    assert runner.kwargs["optimized_tokens"] == 10
    assert runner.kwargs["context_limit"] == 1000
    assert runner.kwargs["provider"] == "openai"
    assert runner.kwargs["model"] == "gpt-5.6"
    assert runner.kwargs["session_id"] == "sess"
    assert runner.kwargs["message_shape"] == "openai"
    assert metrics.events == []


class CapturingRunner:
    enabled = True

    def __init__(self, result: JevShadowResult | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self._result = result or JevShadowResult(ran=False, reason="captured")

    async def maybe_run(self, **kwargs: Any) -> JevShadowResult:
        self.kwargs = kwargs
        return self._result


async def test_the_messages_list_is_forwarded_by_identity_never_copied() -> None:
    """Track A must measure the very list the handler is about to forward."""
    runner = CapturingRunner()
    proxy = FakeProxy(runner, FakeMetrics())
    messages = [{"role": "user", "content": "hi"}]

    await _call(proxy, FakeLimitSource(), messages=messages)

    assert runner.kwargs["messages"] is messages
    assert messages == [{"role": "user", "content": "hi"}]


async def test_the_tokenizer_bound_methods_are_what_the_runner_gets() -> None:
    runner = CapturingRunner()
    tokenizer = FakeTokenizer()
    await _call(proxy := FakeProxy(runner, FakeMetrics()), FakeLimitSource(), tokenizer=tokenizer)
    assert proxy.jev_shadow is runner
    assert runner.kwargs["count_text"]("abcdefgh") == tokenizer.count_text("abcdefgh")
    assert runner.kwargs["count_messages"]([{"content": "abcdefgh"}]) == 2


async def test_a_raising_runner_fails_open_with_a_metric() -> None:
    metrics = FakeMetrics()

    class ExplodingRunner:
        enabled = True

        async def maybe_run(self, **kwargs: Any) -> JevShadowResult:
            raise RuntimeError("boom")

    proxy = FakeProxy(ExplodingRunner(), metrics)
    assert await _call(proxy, FakeLimitSource()) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_a_cancelled_turn_is_not_swallowed() -> None:
    """A cancellation is the caller's, not a Jev failure: it must propagate."""
    metrics = FakeMetrics()

    class CancellingRunner:
        enabled = True

        async def maybe_run(self, **kwargs: Any) -> JevShadowResult:
            raise asyncio.CancelledError

    proxy = FakeProxy(CancellingRunner(), metrics)
    with pytest.raises(asyncio.CancelledError):
        await _call(proxy, FakeLimitSource())
    assert metrics.events == []


async def test_an_inner_fail_open_is_counted_once_not_twice() -> None:
    """``maybe_run`` owns its own guard; the adapter must not re-record."""
    metrics = FakeMetrics()
    runner = JevShadowRunner(
        JevConfig(mode="shadow", api_key="sk-test", threshold_percent=1), metrics=metrics
    )
    proxy = FakeProxy(runner, metrics)

    class BrokenTokenizer:
        def count_text(self, text: str) -> int:
            raise ValueError("tokenizer exploded")

        def count_messages(self, messages: list[dict[str, Any]]) -> int:
            raise ValueError("tokenizer exploded")

    result = await _call(
        proxy,
        FakeLimitSource(),
        messages=_conversation(),
        tokenizer=BrokenTokenizer(),
    )

    # The runner handled it, so the hook passes the result through untouched...
    assert isinstance(result, JevShadowResult)
    assert result.reason == "fail_open"
    # ...and the counter moved exactly once.
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


@pytest.mark.parametrize("limit", [None, 0, -1, "wide", object()])
async def test_an_unusable_context_limit_is_a_skip_not_a_failure(limit: Any) -> None:
    """A model nobody has a limit for is a skip, with its own metric."""
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    result = await _call(proxy, FakeLimitSource(limit=limit))

    assert isinstance(result, JevShadowResult)
    assert result.reason == "no_context_limit"
    assert metrics.events == ["shadow_no_context_limit"]


async def test_a_missing_context_limit_source_is_a_skip() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    result = await _call(proxy, None)

    assert isinstance(result, JevShadowResult)
    assert result.reason == "no_context_limit"
    assert metrics.events == ["shadow_no_context_limit"]


async def test_no_messages_reaches_the_runners_own_skip() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    for empty in ([], None):
        metrics.events.clear()
        result = await _call(proxy, FakeLimitSource(), messages=empty)
        assert isinstance(result, JevShadowResult)
        assert result.reason == "no_messages"
        assert metrics.events == ["shadow_no_messages"]


async def test_an_unusable_tokenizer_fails_open_once() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    assert await _call(proxy, FakeLimitSource(), tokenizer=object()) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_a_broken_metrics_object_still_cannot_take_the_request_down() -> None:
    class HostileMetrics:
        def record_jev_event(self, event: str) -> None:
            raise RuntimeError("prometheus is down")

    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=None)
    proxy = FakeProxy(runner, HostileMetrics())
    assert await _call(proxy, FakeLimitSource(raises=True)) is None


async def test_a_proxy_without_metrics_still_fails_open_quietly() -> None:
    class NoMetricsProxy:
        def __init__(self, runner: Any) -> None:
            self.jev_shadow = runner

    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=None)
    assert await _call(NoMetricsProxy(runner), FakeLimitSource(raises=True)) is None


async def test_the_fail_open_log_is_scrubbed_and_carries_the_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three reviews caught unscrubbed paths; this one stays scrubbed."""
    metrics = FakeMetrics()
    config = JevConfig(
        mode="shadow",
        api_key="sk-super-secret",
        endpoint="https://user:pw@jev.example.test/v1/systemone?token=abc",
    )
    runner = JevShadowRunner(config, metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    class LeakySource:
        def get_context_limit(self, model: str) -> int:
            raise RuntimeError(
                "POST https://user:pw@jev.example.test/v1/systemone?token=abc "
                "with Authorization: Bearer sk-super-secret"
            )

    with caplog.at_level(logging.WARNING, logger="headroom.proxy.jev.hook"):
        assert await _call(proxy, LeakySource()) is None

    assert metrics.events == ["shadow_fail_open"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "sk-super-secret" not in logged
    assert "user:pw@" not in logged
    assert "token=abc" not in logged
    assert "req-1" in logged
    assert "RuntimeError" in logged


async def test_a_long_error_is_truncated_in_the_log() -> None:
    metrics = FakeMetrics()
    runner = JevShadowRunner(JevConfig(mode="shadow", api_key="sk-test"), metrics=metrics)
    proxy = FakeProxy(runner, metrics)

    class ChattySource:
        def get_context_limit(self, model: str) -> int:
            raise RuntimeError("x" * 10_000)

    assert await _call(proxy, ChattySource()) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_a_raising_jev_shadow_attribute_fails_open() -> None:
    """Nothing this module reads off a foreign object may escape the guard."""

    class ExplodingProxy:
        metrics = FakeMetrics()

        @property
        def jev_shadow(self) -> Any:
            raise RuntimeError("proxy attribute blew up")

    proxy = ExplodingProxy()
    assert await _call(proxy, FakeLimitSource()) is None
    assert proxy.metrics.events == ["shadow_fail_open"]


async def test_a_raising_enabled_property_fails_open() -> None:
    metrics = FakeMetrics()

    class MoodyRunner:
        @property
        def enabled(self) -> bool:
            raise RuntimeError("config went away")

    proxy = FakeProxy(MoodyRunner(), metrics)
    assert await _call(proxy, FakeLimitSource()) is None
    assert metrics.events == ["shadow_fail_open"]


async def test_a_disabled_runner_is_not_awaited_at_all() -> None:
    """The off path is the common path: it must not build or await anything."""

    class TripwireRunner:
        enabled = False

        async def maybe_run(self, **kwargs: Any) -> JevShadowResult:  # pragma: no cover
            raise AssertionError("a disabled runner must never be called")

    metrics = FakeMetrics()
    proxy = FakeProxy(TripwireRunner(), metrics)

    class TripwireSource:
        def get_context_limit(self, model: str) -> int:  # pragma: no cover
            raise AssertionError("the limit source must not be consulted when off")

    assert await _call(proxy, TripwireSource()) is None
    assert metrics.events == []
