"""Shadow lifecycle: threshold, cooldown, one call per session/branch, stale
revision, fail-open, and above all: the forwarded messages are never mutated."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import pytest

from headroom.proxy.jev.client import JevAnswer
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.shadow import (
    TRUNCATE_CHARS,
    JevShadowRunner,
    apply_decisions_to_copy,
)

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
        self.closed = 0

    async def decide(self, *, state, questions, candidate_ids) -> JevAnswer:
        self.calls += 1
        if self.error is not None:
            return JevAnswer(decisions=dict.fromkeys(candidate_ids, "keep"), error=self.error)
        return JevAnswer(decisions=dict.fromkeys(candidate_ids, self.decision))

    async def aclose(self) -> None:
        self.closed += 1


class FakeMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record_jev_event(self, event: str) -> None:
        self.events.append(event)


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c0", "content": "A" * 2000},
        {"role": "tool", "tool_call_id": "c1", "content": "B" * 2000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]


async def _run(
    runner: JevShadowRunner,
    messages=None,
    optimized_tokens=900,
    original_tokens=0,
    session_id="sess-1",
    count_text=_count_text,
    context_limit=1000,
):
    return await runner.maybe_run(
        provider="openai",
        model="gpt-5.6",
        messages=_messages() if messages is None else messages,
        frozen_prefix=1,
        optimized_tokens=optimized_tokens,
        original_tokens=original_tokens,
        context_limit=context_limit,
        session_id=session_id,
        count_text=count_text,
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


async def test_active_mode_is_not_track_a() -> None:
    # Track A owns `shadow` only; `active` is Track C's mode, not this runner's.
    runner = JevShadowRunner(JevConfig(mode="active", api_key="sk-test"), client=FakeClient())
    assert runner.enabled is False
    assert (await _run(runner)).reason == "disabled"


async def test_below_threshold_is_recorded_and_skipped() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner, optimized_tokens=100)
    assert result.reason == "below_threshold"
    assert client.calls == 0
    assert "shadow_below_threshold" in metrics.events


async def test_threshold_is_exact_percent_of_the_context_limit() -> None:
    # 50% of 1000 is 500: 499 is below, 500 is not. No float rounding slop.
    client = FakeClient()
    runner = JevShadowRunner(CONFIG, client=client)
    assert (await _run(runner, optimized_tokens=499)).reason == "below_threshold"
    assert (await _run(runner, optimized_tokens=500)).ran is True


async def test_missing_context_limit_is_recorded_and_skipped() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner, context_limit=0)
    assert result.reason == "no_context_limit"
    assert client.calls == 0
    assert "shadow_no_context_limit" in metrics.events


async def test_empty_message_list_is_recorded_and_skipped() -> None:
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner, messages=[])
    assert result.reason == "no_messages"
    assert client.calls == 0
    assert "shadow_no_messages" in metrics.events


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


async def test_an_all_keep_answer_still_projects_and_is_counted() -> None:
    client, metrics = FakeClient(decision="keep"), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner)
    assert result.ran is True
    assert result.keep == 2
    assert result.projected_savings == 0
    assert result.tokens_projected == result.tokens_headroom
    assert "shadow_all_keep" in metrics.events


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


async def test_cooldown_is_per_session_and_branch() -> None:
    client = FakeClient()
    runner = JevShadowRunner(CONFIG, client=client)
    assert (await _run(runner, session_id="a")).ran is True
    # A different session is a different cooldown bucket.
    assert (await _run(runner, session_id="b")).ran is True
    # ...and a different branch root inside the same session is too.
    forked = _messages()
    forked[0] = {"role": "system", "content": "a different system prompt"}
    assert (await _run(runner, messages=forked, session_id="a")).ran is True
    assert (await _run(runner, session_id="a")).reason == "cooldown"
    assert client.calls == 3


async def test_a_skipped_turn_below_threshold_does_not_burn_cooldown() -> None:
    client = FakeClient()
    runner = JevShadowRunner(CONFIG, client=client)
    assert (await _run(runner)).ran is True
    # Two cheap turns that never reach the cooldown gate.
    assert (await _run(runner, optimized_tokens=10)).reason == "below_threshold"
    assert (await _run(runner, optimized_tokens=10)).reason == "below_threshold"
    # The cooldown still has both of its turns to spend.
    assert (await _run(runner)).reason == "cooldown"
    assert (await _run(runner)).reason == "cooldown"
    assert (await _run(runner)).ran is True


async def test_zero_cooldown_runs_every_eligible_turn() -> None:
    client = FakeClient()
    runner = JevShadowRunner(
        JevConfig(
            mode="shadow",
            api_key="sk-test",
            threshold_percent=50,
            cooldown_turns=0,
            max_state_tokens=100000,
        ),
        client=client,
    )
    for _ in range(3):
        assert (await _run(runner)).ran is True
    assert client.calls == 3


async def test_the_cooldown_map_is_bounded() -> None:
    # session ids are client-controlled (x-headroom-session-id), so the
    # bookkeeping must evict rather than grow without limit.
    runner = JevShadowRunner(CONFIG, client=FakeClient(), max_branches=4)
    for i in range(40):
        assert (await _run(runner, session_id=f"sess-{i}")).ran is True
    assert runner.tracked_cooldowns <= 4


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


async def test_an_unaffordable_state_budget_skips_without_spending_a_call() -> None:
    # `enforce_state_budget` returns ([], floor, fixed_overhead) when even the
    # empty envelope does not fit: the third value is NOT a sendable payload.
    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(
        JevConfig(
            mode="shadow",
            api_key="sk-test",
            threshold_percent=50,
            max_state_tokens=1,
        ),
        client=client,
        metrics=metrics,
    )
    result = await _run(runner)
    assert result.ran is False
    assert result.reason == "state_budget_exhausted"
    assert result.candidates == 2
    assert result.candidates_sent == 0
    assert client.calls == 0
    assert "shadow_budget_exhausted" in metrics.events


async def test_call_error_fails_open_with_a_metric() -> None:
    client, metrics = FakeClient(error="HTTP 500: boom"), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner)
    assert result.ran is False
    assert result.reason == "call_error"
    assert result.error == "HTTP 500: boom"
    assert result.tokens_projected == 0
    assert "shadow_call_error" in metrics.events


async def test_a_failed_call_still_starts_the_cooldown() -> None:
    client = FakeClient(error="HTTP 500: boom")
    runner = JevShadowRunner(CONFIG, client=client)
    assert (await _run(runner)).reason == "call_error"
    assert (await _run(runner)).reason == "cooldown"
    assert client.calls == 1


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


async def test_a_second_call_on_the_same_branch_is_refused_while_one_is_inflight() -> None:
    metrics = FakeMetrics()
    gate = asyncio.Event()

    class SlowClient(FakeClient):
        async def decide(self, *, state, questions, candidate_ids):
            self.calls += 1
            await gate.wait()
            return JevAnswer(decisions=dict.fromkeys(candidate_ids, "keep"))

    client = SlowClient()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    first = asyncio.create_task(_run(runner))
    await asyncio.sleep(0)  # let the first attempt reach the await
    second = await _run(runner)
    gate.set()
    await first

    assert second.ran is False
    assert second.reason == "inflight"
    assert client.calls == 1
    assert "shadow_inflight" in metrics.events


async def test_a_broken_tokenizer_fails_open_instead_of_taking_the_turn_down() -> None:
    # candidates.py / request.py deliberately let a raising `count_text`
    # propagate; the runner is the fail-open guard that has to catch it.
    def boom(_text: str) -> int:
        raise RuntimeError("tokenizer exploded")

    client, metrics = FakeClient(), FakeMetrics()
    runner = JevShadowRunner(CONFIG, client=client, metrics=metrics)
    result = await _run(runner, count_text=boom)
    assert result.ran is False
    assert result.reason == "fail_open"
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert client.calls == 0
    assert "shadow_fail_open" in metrics.events


async def test_the_fail_open_error_and_log_are_scrubbed_of_credentials(caplog) -> None:
    # The caught exception is arbitrary -- a caller-supplied tokenizer can put
    # the endpoint (userinfo, query token) or the API key straight into its
    # message, and the result's `error` reaches /stats and the logs.
    config = JevConfig(
        mode="shadow",
        api_key="sk-super-secret-key",
        endpoint="https://user:pw@jev.example.com/v1/systemone?token=leaky",
        threshold_percent=50,
        max_state_tokens=100000,
    )

    def boom(_text: str) -> int:
        raise RuntimeError(f"boom {config.endpoint} {config.api_key}")

    runner = JevShadowRunner(config, client=FakeClient(), metrics=FakeMetrics())
    with caplog.at_level(logging.WARNING, logger="headroom.proxy.jev.shadow"):
        result = await _run(runner, count_text=boom)

    assert result.reason == "fail_open"
    assert result.error is not None
    assert "RuntimeError" in result.error
    for secret in (config.api_key, "token=leaky", "user:pw"):
        assert secret not in result.error
        assert all(secret not in record.getMessage() for record in caplog.records)
    assert "jev.example.com" in result.error  # still diagnosable


async def test_cancellation_is_never_treated_as_a_jev_failure() -> None:
    class CancellingClient(FakeClient):
        async def decide(self, *, state, questions, candidate_ids):
            self.calls += 1
            raise asyncio.CancelledError

    client = CancellingClient()
    runner = JevShadowRunner(CONFIG, client=client, metrics=FakeMetrics())
    with pytest.raises(asyncio.CancelledError):
        await _run(runner)
    # ...and the in-flight guard is released, so the branch is not wedged.
    assert (await _run(runner)).reason != "inflight"


async def test_aclose_closes_the_client() -> None:
    client = FakeClient()
    runner = JevShadowRunner(CONFIG, client=client)
    await runner.aclose()
    assert client.closed == 1


async def test_a_disabled_runner_owns_a_client_it_can_still_close() -> None:
    runner = JevShadowRunner(JevConfig())
    await runner.aclose()  # must not raise, and must not have dialled anything


def test_apply_decisions_to_copy_handles_drop_and_truncate() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _messages()
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    projected = apply_decisions_to_copy(
        messages, cands, {"cand_0000": "drop", "cand_0001": "truncate"}
    )
    assert len(projected) == len(messages) - 1
    truncated = [m for m in projected if m.get("tool_call_id") == "c1"][0]
    assert len(str(truncated["content"])) < 2000
    assert str(truncated["content"]).startswith("B" * 400)


def test_apply_decisions_to_copy_never_touches_its_input() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _messages()
    before = copy.deepcopy(messages)
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    apply_decisions_to_copy(messages, cands, {"cand_0000": "drop", "cand_0001": "truncate"})
    assert messages == before


def test_apply_decisions_to_copy_defaults_an_unnamed_candidate_to_keep() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _messages()
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    # A candidate Jev was never asked about (trimmed by the state budget) must
    # survive untouched, or TP claims savings that were never decided.
    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "drop"})
    assert len(projected) == len(messages) - 1
    assert [m for m in projected if m.get("tool_call_id") == "c1"][0]["content"] == "B" * 2000


def _anthropic_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here you go"},
                {
                    "type": "tool_result",
                    "tool_use_id": "t0",
                    "content": [{"type": "text", "text": "C" * 2000}],
                },
            ],
        },
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]


def test_apply_decisions_to_copy_truncates_an_anthropic_tool_result_block() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _anthropic_messages()
    before = copy.deepcopy(messages)
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    assert len(cands) == 1 and cands[0].block_index == 1

    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "truncate"})
    assert len(projected) == len(messages)
    block = projected[1]["content"][1]
    assert isinstance(block["content"], str)
    assert len(block["content"]) < 2000
    # The flattened block text is what the candidate showed Jev, so that is
    # what the projection truncates.
    assert block["content"][:TRUNCATE_CHARS] == cands[0].content[:TRUNCATE_CHARS]
    # The sibling text block is left alone.
    assert projected[1]["content"][0] == {"type": "text", "text": "here you go"}
    assert messages == before


def test_dropping_the_last_block_of_a_message_drops_the_message() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _anthropic_messages()
    messages[1]["content"] = [messages[1]["content"][1]]  # only the tool_result
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "drop"})
    assert len(projected) == len(messages) - 1
    assert all(not isinstance(m.get("content"), list) for m in projected)


def test_dropping_one_of_several_blocks_keeps_the_message() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _anthropic_messages()
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "drop"})
    assert len(projected) == len(messages)
    assert projected[1]["content"] == [{"type": "text", "text": "here you go"}]


def test_a_short_candidate_is_truncated_without_a_note() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages = _messages()
    messages[1]["content"] = "short"
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "truncate"})
    assert projected[1]["content"] == "short"


def test_apply_decisions_to_copy_truncates_a_responses_output_item() -> None:
    from headroom.proxy.jev.candidates import select_candidates

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"type": "function_call_output", "call_id": "fc0", "output": "D" * 2000},
        *[{"role": "assistant", "content": f"tail {i}"} for i in range(6)],
    ]
    cands = select_candidates(messages, frozen_prefix=1, count_text=_count_text, max_candidates=12)
    assert len(cands) == 1
    projected = apply_decisions_to_copy(messages, cands, {"cand_0000": "truncate"})
    assert projected[1]["output"].startswith("D" * TRUNCATE_CHARS)
    assert len(projected[1]["output"]) < 2000
    assert "content" not in projected[1]


def _bare_proxy():
    from headroom.proxy.server import HeadroomProxy, ProxyConfig

    return HeadroomProxy(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            log_requests=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            ccr_context_tracking=False,
        )
    )


def test_the_proxy_always_has_a_shadow_runner_wired_to_its_metrics() -> None:
    proxy = _bare_proxy()
    assert isinstance(proxy.jev_shadow, JevShadowRunner)
    # Default config is mode=off, so the runner is inert but present.
    assert proxy.jev_shadow.enabled is False
    proxy.jev_shadow._record("shadow_projected")
    assert proxy.metrics.jev_events_by_event["shadow_projected"] == 1


async def test_proxy_shutdown_closes_the_shadow_runner() -> None:
    proxy = _bare_proxy()
    client = FakeClient()
    proxy.jev_shadow._client = client
    await proxy.shutdown()
    assert client.closed == 1


def test_projected_savings_never_goes_negative() -> None:
    from headroom.proxy.jev.shadow import JevShadowResult

    assert JevShadowResult(ran=True, reason="projected").projected_savings == 0
    result = JevShadowResult(ran=True, reason="projected", tokens_headroom=10, tokens_projected=25)
    assert result.projected_savings == 0
