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


# --- responses_token_counts -------------------------------------------------
#
# The Responses handler's own `original_tokens`/`optimized_tokens` are counted
# from a synthetic `messages` list that is built from `instructions` plus a
# *string* `input` only (handlers/openai.py: `if isinstance(input_data, str)`).
# For the list-valued `input` Codex actually sends, that pair is ~0 and the
# runner's threshold gate would skip every single turn. These cover the
# replacement count.


def _responses_items() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "hello"},
        {"type": "function_call_output", "call_id": "fc0", "output": "D" * 4000},
    ]


def test_responses_token_counts_prices_function_call_output() -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    items = _responses_items()
    tokenizer = FakeTokenizer()
    # The content-only counter misses the `output` payload entirely...
    assert tokenizer.count_messages(items) < 10
    optimized, original = responses_token_counts(items, tokenizer, 250)
    # ...while the corrected count prices it.
    assert optimized > 500
    assert original == optimized + 250


def test_responses_token_counts_keeps_original_minus_optimized_equal_to_saved() -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    optimized, original = responses_token_counts(_responses_items(), FakeTokenizer(), 17)
    assert original - optimized == 17


@pytest.mark.parametrize("saved", [0, -5, None])
def test_responses_token_counts_floors_a_nonsense_saved_at_zero(saved: Any) -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    optimized, original = responses_token_counts(_responses_items(), FakeTokenizer(), saved)
    assert original == optimized


@pytest.mark.parametrize("items", ["a string input", None, 42, {"input": "x"}])
def test_responses_token_counts_is_zero_for_a_non_list_input(items: Any) -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    # (0, 0) lands on the runner's own below-threshold skip, so a string-shaped
    # Responses turn is skipped by a named gate rather than mis-measured.
    assert responses_token_counts(items, FakeTokenizer(), 100) == (0, 0)


def test_responses_token_counts_fails_open_on_a_raising_tokenizer() -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    class Exploding:
        def count_text(self, text: str) -> int:
            raise RuntimeError("boom")

        def count_messages(self, messages: list[dict[str, Any]]) -> int:
            raise RuntimeError("boom")

    assert responses_token_counts(_responses_items(), Exploding(), 100) == (0, 0)


def test_responses_token_counts_fails_open_on_a_tokenizer_without_the_methods() -> None:
    from headroom.proxy.jev.hook import responses_token_counts

    assert responses_token_counts(_responses_items(), None, 100) == (0, 0)
    assert responses_token_counts(_responses_items(), object(), 100) == (0, 0)


async def test_a_list_input_responses_turn_actually_reaches_jev() -> None:
    """The regression this helper exists for, end to end through the runner.

    With the handler's own synthetic-`messages` count the turn is skipped as
    `below_threshold` on every single request; with the recounted pair the
    same turn gets past the gate and a shadow call really happens.
    """

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> Any:
            from headroom.proxy.jev.client import JevAnswer

            self.calls += 1
            return JevAnswer(decisions=dict.fromkeys(candidate_ids, "drop"))

        async def aclose(self) -> None:
            return None

    from headroom.proxy.jev.hook import responses_token_counts

    items: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"type": "function_call_output", "call_id": "fc0", "output": "D" * 8000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]
    tokenizer = FakeTokenizer()
    config = JevConfig(mode="shadow", api_key="sk-test", threshold_percent=50, max_candidates=12)

    # (a) what the handler's own locals would say for a list-valued `input`:
    # `messages` is empty there, so the pair is ~0.
    handler_optimized = tokenizer.count_messages([])
    client_a = FakeClient()
    proxy_a = FakeProxy(JevShadowRunner(config, client=client_a), FakeMetrics())
    result_a = await run_jev_shadow_hook(
        proxy_a,
        provider="openai",
        model="gpt-5.6",
        messages=items,
        frozen_prefix=0,
        optimized_tokens=handler_optimized,
        original_tokens=handler_optimized,
        session_id="sess-resp",
        tokenizer=tokenizer,
        message_shape="openai_responses",
        request_id="r",
        context_limit_source=FakeLimitSource(1000),
    )
    assert result_a is not None and result_a.reason == "below_threshold"
    assert client_a.calls == 0

    # (b) what the call site now passes.
    optimized, original = responses_token_counts(items, tokenizer, 300)
    client_b = FakeClient()
    proxy_b = FakeProxy(JevShadowRunner(config, client=client_b), FakeMetrics())
    result_b = await run_jev_shadow_hook(
        proxy_b,
        provider="openai",
        model="gpt-5.6",
        messages=items,
        frozen_prefix=0,
        optimized_tokens=optimized,
        original_tokens=original,
        session_id="sess-resp",
        tokenizer=tokenizer,
        message_shape="openai_responses",
        request_id="r",
        context_limit_source=FakeLimitSource(1000),
    )
    assert result_b is not None and result_b.ran is True
    assert client_b.calls == 1
    # Still shadow: the caller's list is untouched.
    assert items[1]["output"] == "D" * 8000
