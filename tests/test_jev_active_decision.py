"""Track B's decision step: Track A's selection + Track A's client, no forks.

The only thing stubbed here is the Jev HTTP call itself (a fake ``decide``).
Selection, the identity/revision hash, the retention state, the questions and
the measured request budget are the real Track A code, driven by a cheap
``count_text`` so the budget arithmetic stays legible in the test.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from headroom.proxy.jev.active import JevActiveDecision, decide_active_retention
from headroom.proxy.jev.config import JevConfig

ENDPOINT = "https://user:pw@jev.example/v1/decide?token=abc"
API_KEY = "sk-jev-secret-key"


@dataclass
class _FakeAnswer:
    decisions: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 12.5
    jev_model: str | None = "jev-test"


class _FakeClient:
    """Stands in for ``JevClient`` with the same ``decide`` signature."""

    def __init__(self, decision: str = "drop", error: str | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def decide(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        candidate_ids: Any,
    ) -> _FakeAnswer:
        ids = list(candidate_ids)
        self.calls.append({"state": state, "questions": questions, "ids": ids})
        if self.error is not None:
            # The real client fails open: `decisions` is fully populated even
            # on an error, and the error travels alongside it.
            return _FakeAnswer(decisions=dict.fromkeys(ids, "keep"), error=self.error)
        return _FakeAnswer(decisions=dict.fromkeys(ids, self.decision))


class _CheapTokenizer:
    """A ``TokenCounter``-shaped stand-in: four characters to the token."""

    def count_text(self, text: str) -> int:
        return max(1, len(text) // 4)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count_text(str(m.get("content") or "")) for m in messages)


class _BrokenTokenizer:
    def count_text(self, text: str) -> int:
        raise RuntimeError("tokenizer exploded")

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        raise RuntimeError("tokenizer exploded")


def _use_tokenizer(monkeypatch: pytest.MonkeyPatch, tokenizer: Any) -> None:
    monkeypatch.setattr(
        "headroom.proxy.jev.active.get_tokenizer",
        lambda model, *args, **kwargs: tokenizer,
    )


def _config(**overrides: Any) -> JevConfig:
    values: dict[str, Any] = {
        "mode": "active",
        "api_key": API_KEY,
        "endpoint": ENDPOINT,
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


async def test_candidates_are_selected_and_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
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
    assert isinstance(decision, JevActiveDecision)
    assert decision.called is True
    assert decision.error is None
    assert decision.latency_ms == 12.5
    assert len(decision.candidates) == 2
    assert set(decision.decisions.values()) == {"drop"}
    assert len(client.calls) == 1
    # The state is the Track A retention state, carrying this turn's identity.
    state = client.calls[0]["state"]
    assert state["session_id"] == "s1"
    assert state["branch_id"] == "compress"
    assert state["jev_model"] == "jev-test"
    assert state["model"] == "gpt-4o"
    assert state["provider"] == "compress"
    assert state["message_shape"] == "openai"
    assert state["revision"]
    # One question per candidate, keyed by candidate id.
    assert sorted(client.calls[0]["questions"]) == sorted(
        c.candidate_id for c in decision.candidates
    )
    assert client.calls[0]["ids"] == [c.candidate_id for c in decision.candidates]


async def test_the_frozen_prefix_and_shape_arguments_reach_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
    client = _FakeClient()
    messages = _conversation()
    await decide_active_retention(
        config=_config(),
        client=client,
        messages=messages,
        frozen_prefix=1,
        model="claude-3-5-sonnet",
        session_id="s2",
        branch_id="b2",
        provider="anthropic",
        message_shape="anthropic",
    )
    state = client.calls[0]["state"]
    assert state["protected_prefix_messages"] == 1
    assert state["recent_tail_excluded_messages"] == 6
    assert state["total_messages"] == len(messages)
    assert state["provider"] == "anthropic"
    assert state["message_shape"] == "anthropic"


async def test_the_operator_configured_bounds_are_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HEADROOM_JEV_MAX_CANDIDATES / HEADROOM_JEV_MAX_STATE_TOKENS are documented
    # as general operator controls, so active mode must not substitute a
    # hard-coded ceiling of its own.
    _use_tokenizer(monkeypatch, _CheapTokenizer())
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


async def test_an_impossible_budget_makes_no_call_and_keeps_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tiny measured state budget trims the request instead of ignoring it:
    # nothing fits, so no call is made at all. The candidates are still
    # reported (honest accounting) and every one of them is `keep`.
    _use_tokenizer(monkeypatch, _CheapTokenizer())
    client = _FakeClient(decision="drop")
    decision = await decide_active_retention(
        config=_config(max_state_tokens=1),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert client.calls == []
    assert decision.called is False
    assert decision.error is None
    assert decision.latency_ms == 0.0
    assert len(decision.candidates) == 2
    assert decision.decisions == {c.candidate_id: "keep" for c in decision.candidates}


async def test_a_budget_trimmed_candidate_defaults_to_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The budget fits one candidate but not both. The candidate Jev was never
    # asked about is still reported, and it is `keep` -- never a licence to
    # remove content nobody ruled on.
    _use_tokenizer(monkeypatch, _CheapTokenizer())
    client = _FakeClient(decision="drop")
    decision = await decide_active_retention(
        config=_config(max_state_tokens=4000),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert decision.called is True
    assert len(decision.candidates) == 2
    asked = client.calls[0]["ids"]
    assert len(asked) == 1
    assert set(decision.decisions) == {c.candidate_id for c in decision.candidates}
    assert decision.decisions[asked[0]] == "drop"
    unasked = [c.candidate_id for c in decision.candidates if c.candidate_id not in asked]
    assert [decision.decisions[cid] for cid in unasked] == ["keep"]


async def test_no_candidates_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
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
    assert decision.decisions == {}
    assert client.calls == []


async def test_client_error_falls_open_to_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
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
    assert decision.called is True
    assert decision.error == "HTTP 503"
    assert set(decision.decisions.values()) == {"keep"}


async def test_an_answer_missing_an_id_falls_open_to_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SilentClient(_FakeClient):
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> _FakeAnswer:
            ids = list(candidate_ids)
            self.calls.append({"state": state, "questions": questions, "ids": ids})
            return _FakeAnswer(decisions={ids[0]: "drop"})

    _use_tokenizer(monkeypatch, _CheapTokenizer())
    client = _SilentClient()
    decision = await decide_active_retention(
        config=_config(),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    asked = client.calls[0]["ids"]
    assert decision.decisions[asked[0]] == "drop"
    assert [decision.decisions[cid] for cid in asked[1:]] == ["keep"]


async def test_a_decision_outside_the_vocabulary_falls_open_to_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `decisions` is what later steps act on to remove content, so a word the
    # decision vocabulary does not contain must not travel out of here.
    class _RogueClient(_FakeClient):
        async def decide(self, *, state: Any, questions: Any, candidate_ids: Any) -> _FakeAnswer:
            ids = list(candidate_ids)
            self.calls.append({"state": state, "questions": questions, "ids": ids})
            return _FakeAnswer(decisions=dict.fromkeys(ids, "obliterate"))

    _use_tokenizer(monkeypatch, _CheapTokenizer())
    decision = await decide_active_retention(
        config=_config(),
        client=_RogueClient(),
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert set(decision.decisions.values()) == {"keep"}


async def test_a_raising_tokenizer_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Task 16's orchestrator is the single fail-open guard: a broken tokenizer
    # must stay observable rather than be turned into a fabricated budget here.
    _use_tokenizer(monkeypatch, _BrokenTokenizer())
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="tokenizer exploded"):
        await decide_active_retention(
            config=_config(),
            client=client,
            messages=_conversation(),
            frozen_prefix=0,
            model="gpt-4o",
            session_id="s1",
            branch_id="compress",
        )
    assert client.calls == []


async def test_a_failing_tokenizer_lookup_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(model: str, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no tokenizer for model")

    monkeypatch.setattr("headroom.proxy.jev.active.get_tokenizer", _boom)
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="no tokenizer"):
        await decide_active_retention(
            config=_config(),
            client=client,
            messages=_conversation(),
            frozen_prefix=0,
            model="gpt-4o",
            session_id="s1",
            branch_id="compress",
        )
    assert client.calls == []


async def test_messages_are_never_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
    messages = _conversation()
    before = copy.deepcopy(messages)
    await decide_active_retention(
        config=_config(),
        client=_FakeClient(decision="drop"),
        messages=messages,
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    assert messages == before


async def test_no_credentials_reach_the_request_or_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_tokenizer(monkeypatch, _CheapTokenizer())
    client = _FakeClient(error="HTTP 500: upstream said no")
    decision = await decide_active_retention(
        config=_config(),
        client=client,
        messages=_conversation(),
        frozen_prefix=0,
        model="gpt-4o",
        session_id="s1",
        branch_id="compress",
    )
    sent = json.dumps(client.calls[0], default=str)
    rendered = json.dumps(
        {
            "decisions": decision.decisions,
            "error": decision.error,
            "candidates": [c.content[:64] for c in decision.candidates],
        },
        default=str,
    )
    for secret in (API_KEY, ENDPOINT, "user:pw", "token=abc"):
        assert secret not in sent
        assert secret not in rendered


async def test_the_real_tokenizer_registry_is_used_by_default() -> None:
    # No monkeypatch: the model's own tokenizer is resolved, exactly as the
    # request path already resolved it.
    client = _FakeClient(decision="truncate")
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
    assert set(decision.decisions.values()) == {"truncate"}
    assert all(c.est_tokens > 0 for c in decision.candidates)
