"""JevClient speaks the real System One state/questions->answers contract,
is bounded by timeout_ms, fails open to keep, and never leaks the API key."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx

from headroom.proxy.jev.client import JevAnswer, JevClient, build_request_payload
from headroom.proxy.jev.config import JevConfig

CONFIG = JevConfig(
    mode="shadow",
    api_key="sk-super-secret",
    endpoint="https://api.example.invalid/v1/systemone?token=leaky",
    model="jev-latest",
    timeout_ms=1000,
)

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _client(handler: Handler, config: JevConfig = CONFIG) -> JevClient:
    # The injected client deliberately carries no timeout of its own: the
    # per-call bound has to come from JevClient, not from the caller.
    return JevClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_build_request_payload_uses_the_state_questions_shape() -> None:
    payload = build_request_payload(CONFIG, {"candidates": []}, {"cand_0000": {}})
    assert set(payload) == {"state", "model", "questions"}
    assert payload["model"] == "jev-latest"


async def test_happy_path_parses_choices() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        seen["timeout"] = request.extensions.get("timeout")
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
    assert json.loads(seen["body"]) == {  # type: ignore[arg-type]
        "state": {"candidates": []},
        "model": "jev-latest",
        "questions": {"cand_0000": {}, "cand_0001": {}},
    }


async def test_per_request_timeout_bounds_an_injected_client() -> None:
    """The caller's AsyncClient has no timeout; timeout_ms must still apply."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"answers": {}})

    jev = _client(handler)
    await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert seen["timeout"] == {"connect": 1.0, "read": 1.0, "write": 1.0, "pool": 1.0}


async def test_a_hung_transport_is_cut_off_at_timeout_ms() -> None:
    """A transport that never answers must not outlive the retention budget."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")  # pragma: no cover

    jev = _client(handler, JevConfig(mode="shadow", api_key="k", timeout_ms=20))
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert (answer.error or "").startswith("TimeoutError")
    assert answer.latency_ms < 5000


async def test_unparseable_answer_falls_back_to_keep() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"cand_0000": {"choice": "obliterate"}}})

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000", "cand_0001"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep", "cand_0001": "keep"}
    assert answer.unparsed == 2


async def test_http_error_fails_open_to_keep_and_redacts_the_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": {"error_type": "max_tokens_exceeded"}})

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error is not None
    assert "max_tokens_exceeded" in answer.error
    assert "sk-super-secret" not in answer.error
    assert "token=leaky" not in answer.error


async def test_an_error_body_that_echoes_the_credentials_is_scrubbed() -> None:
    """Some gateways echo the Authorization header or the full URL back."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "detail": "bad key Bearer sk-super-secret for "
                "https://api.example.invalid/v1/systemone?token=leaky",
            },
        )

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error is not None
    assert "sk-super-secret" not in answer.error
    assert "token=leaky" not in answer.error


async def test_transport_failure_fails_open_and_scrubs_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "timed out reading https://api.example.invalid/v1/systemone?token=leaky"
        )

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error is not None
    assert answer.error.startswith("ReadTimeout:")
    assert "token=leaky" not in answer.error


async def test_non_json_body_fails_open_without_echoing_anything() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>sk-super-secret token=leaky</html>")

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert "non-JSON response" in (answer.error or "")
    assert "sk-super-secret" not in (answer.error or "")
    assert "token=leaky" not in (answer.error or "")


async def test_json_that_is_not_an_object_fails_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["sk-super-secret"])

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert answer.error == "unexpected response type: list"


async def test_missing_answers_block_fails_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "jev-1.2.3"})

    jev = _client(handler)
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])
    await jev.aclose()

    assert answer.decisions == {"cand_0000": "keep"}
    assert "no dict at 'answers'" in (answer.error or "")


async def test_decide_after_aclose_fails_open_instead_of_dialling_out() -> None:
    """aclose() must not let the next call resurrect a real network client."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"answers": {}})

    jev = _client(handler)
    await jev.aclose()
    await jev.aclose()  # idempotent
    answer = await jev.decide(state={}, questions={}, candidate_ids=["cand_0000"])

    assert calls == 0
    assert answer.decisions == {"cand_0000": "keep"}
    assert "closed" in (answer.error or "")


def test_repr_never_shows_the_api_key() -> None:
    jev = JevClient(CONFIG)
    assert "sk-super-secret" not in repr(jev)
    assert "sk-super-secret" not in repr(CONFIG)
    assert "sk-super-secret" not in repr(JevAnswer(error="boom"))
