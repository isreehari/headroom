"""The shadow hook is actually called from all three compressed-request paths.

These assert against the handler source rather than driving a full request:
booting a real Anthropic/OpenAI turn pulls in the whole compression pipeline,
while what can silently regress here is the *call site* -- someone refactoring
the post_compress region and dropping the hook. Behaviour is covered by
tests/test_jev_hook.py and tests/test_jev_shadow.py.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

import pytest

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
    # The baseline still reaches the hook, but NOT as the handler's own
    # `original_tokens`: that pair is counted from a synthetic `messages` list
    # holding only `instructions` plus a *string* `input`, so for a
    # list-valued `input` it is ~0 and every turn would trip the runner's
    # below-threshold gate. `responses_token_counts` recounts the real item
    # list (pricing `function_call_output` payloads) and reconstructs the
    # baseline from `tokens_saved`.
    assert "responses_token_counts(" in source
    assert "original_tokens=_jev_original" in source
    assert "optimized_tokens=_jev_optimized" in source


def test_no_call_site_assigns_from_the_hook() -> None:
    # Shadow mode must never feed anything back into the forwarded request.
    for fn in (
        AnthropicHandlerMixin.handle_anthropic_messages,
        OpenAIHandlerMixin.handle_openai_chat,
        OpenAIHandlerMixin.handle_openai_responses,
    ):
        source = inspect.getsource(fn)
        assert "= await run_jev_shadow_hook(" not in source


def _hook_call_statements(fn: Any) -> list[ast.Expr]:
    """Every `await run_jev_shadow_hook(...)` used as a bare statement."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    found: list[ast.Expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Await):
            continue
        call = node.value.value
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "run_jev_shadow_hook":
            found.append(node)
    return found


def _hook_call_nodes(fn: Any) -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_jev_shadow_hook"
    ]


@pytest.mark.parametrize(
    "fn",
    [
        AnthropicHandlerMixin.handle_anthropic_messages,
        OpenAIHandlerMixin.handle_openai_chat,
        OpenAIHandlerMixin.handle_openai_responses,
    ],
    ids=["anthropic", "openai_chat", "openai_responses"],
)
def test_every_call_site_is_a_bare_awaited_statement(fn: Any) -> None:
    # Stronger than the string check above: the result is not bound to a name,
    # not an argument, not a comprehension element -- it is discarded, so it
    # cannot reach the forwarded request by any route.
    calls = _hook_call_nodes(fn)
    assert len(calls) == 1
    assert len(_hook_call_statements(fn)) == 1


@pytest.mark.asyncio
async def test_the_hook_is_a_no_op_on_a_default_proxy() -> None:
    # Behavioural counterpart to the source inspection: with the shipped
    # defaults (Jev mode=off) the call the handlers now make returns None and
    # touches nothing.
    from headroom.proxy.jev.hook import run_jev_shadow_hook
    from headroom.proxy.server import HeadroomProxy, ProxyConfig

    proxy = HeadroomProxy(
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
    assert proxy.jev_shadow.enabled is False
    messages = [{"role": "user", "content": "hi"}]
    result = await run_jev_shadow_hook(
        proxy,
        provider="anthropic",
        model="claude-sonnet-4-5",
        messages=messages,
        frozen_prefix=0,
        optimized_tokens=10,
        original_tokens=12,
        session_id="s",
        tokenizer=None,
        message_shape="anthropic",
        request_id="r",
        context_limit_source=proxy.anthropic_provider,
    )
    assert result is None
    assert messages == [{"role": "user", "content": "hi"}]
