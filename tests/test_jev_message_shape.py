"""``_jev_message_shape`` names the shape of the list Jev is actually shown.

The helper exists because ``POST /v1/compress`` accepts an OpenAI Chat list and
an Anthropic list independently of the model name: a Claude-named model can
carry ``role: "tool"`` messages and a GPT-named one can carry ``tool_result``
blocks. Those two CROSS cases are the whole reason it replaced the old
model-name heuristic, so they are the cases pinned hardest here.

These are direct unit tests on a module-level pure function -- no app, no
TestClient, no fixtures. ``message_shape`` is metadata only
(``headroom.proxy.jev.request.build_retention_state``); selection and
application detect the shape structurally, so what this has to be is *honest*,
and what it must never be is *raising*: it runs inline on the request path.

Not covered here, deliberately: the ``"openai_responses"`` answer. That is not
a branch of this helper -- the call site chooses it from
``carries_responses_view(body)`` before calling in
(``headroom/proxy/handlers/openai.py`` around the ``jev_boundary`` block), and
driving it needs a real Responses request body and a full app.
"""

from __future__ import annotations

from typing import Any

import pytest

from headroom.proxy.handlers.openai import _jev_message_shape

CLAUDE = "claude-sonnet-4-5-20250929"
GPT = "gpt-4o"


def _openai_shaped() -> list[dict[str, Any]]:
    """An OpenAI Chat list: the tool result is a whole ``role="tool"`` message."""
    return [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt\nb.txt"},
    ]


def _anthropic_shaped() -> list[dict[str, Any]]:
    """An Anthropic list: the tool result is a BLOCK in a user message."""
    return [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "ls", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here you go"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt\nb.txt"},
            ],
        },
    ]


def _silent() -> list[dict[str, Any]]:
    """A conversation with no tool result of either shape."""
    return [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


# --------------------------------------------------------------------------
# The structure wins over the model name -- the reason the helper exists.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model", [GPT, CLAUDE, "us.anthropic.claude-opus-4-1-20250805-v1:0"])
def test_a_tool_role_message_is_openai_whatever_the_model_is_called(model: str) -> None:
    assert _jev_message_shape(_openai_shaped(), model) == "openai"


@pytest.mark.parametrize("model", [CLAUDE, GPT, "gpt-5.1-codex"])
def test_a_tool_result_block_is_anthropic_whatever_the_model_is_called(model: str) -> None:
    assert _jev_message_shape(_anthropic_shaped(), model) == "anthropic"


# --------------------------------------------------------------------------
# Ambiguous and silent lists fall back to the model name, in both directions.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (GPT, "openai"),
        (CLAUDE, "anthropic"),
        ("anthropic/claude-3-5-haiku", "anthropic"),
        ("Claude-3-Opus", "anthropic"),  # the heuristic is case-insensitive
        ("o3-mini", "openai"),
        ("", "openai"),
    ],
)
def test_a_silent_conversation_falls_back_to_the_model_name(model: str, expected: str) -> None:
    assert _jev_message_shape(_silent(), model) == expected
    assert _jev_message_shape([], model) == expected


@pytest.mark.parametrize(("model", "expected"), [(GPT, "openai"), (CLAUDE, "anthropic")])
def test_a_list_carrying_both_shapes_falls_back_to_the_model_name(
    model: str, expected: str
) -> None:
    """Neither structure can win outright, so the model name breaks the tie."""
    mixed = [*_openai_shaped(), *_anthropic_shaped()]
    assert _jev_message_shape(mixed, model) == expected


# --------------------------------------------------------------------------
# Robustness: this runs inline on the request path and must never raise.
# --------------------------------------------------------------------------


def test_non_dict_entries_and_non_list_content_are_ignored_not_fatal() -> None:
    messages: list[Any] = [
        None,
        "not a message",
        42,
        ["nested"],
        {"role": "user", "content": "a plain string, not a block list"},
        {"role": "user"},  # no content key at all
        {"role": "user", "content": None},
        {"role": "tool", "tool_call_id": "c1", "content": "payload"},
    ]
    # The one real signal in there is the role="tool" message; the junk around
    # it neither raises nor votes.
    assert _jev_message_shape(messages, CLAUDE) == "openai"


def test_junk_inside_a_user_content_list_is_ignored_not_fatal() -> None:
    messages: list[Any] = [
        {
            "role": "user",
            "content": [
                None,
                "bare string block",
                17,
                # An unhashable `type` is a body a client can actually send;
                # it must compare unequal rather than blow up.
                {"type": []},
                {"type": "text", "text": "hello"},
            ],
        }
    ]
    # Nothing in there is a tool_result, so this is the silent case.
    assert _jev_message_shape(messages, CLAUDE) == "anthropic"
    assert _jev_message_shape(messages, GPT) == "openai"


def test_a_real_tool_result_block_still_wins_past_surrounding_junk() -> None:
    messages: list[Any] = [
        None,
        {
            "role": "user",
            "content": [
                "bare string block",
                {"type": []},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "out"},
            ],
        },
    ]
    assert _jev_message_shape(messages, GPT) == "anthropic"


# --------------------------------------------------------------------------
# Characterization: where the helper deliberately does NOT look.
# --------------------------------------------------------------------------


def test_a_tool_result_block_outside_a_user_message_does_not_vote() -> None:
    """Only a USER message's content list is scanned for ``tool_result``.

    That matches the Anthropic API, where a ``tool_result`` block is only legal
    in a user turn -- a block somewhere else is not evidence of an Anthropic
    caller. Recorded as intended behaviour, not as a gap: ``message_shape`` is
    metadata, and ``select_candidates`` scans every list-valued ``content``
    regardless, so such a block is still a retention candidate.
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "out"}],
        }
    ]
    assert _jev_message_shape(messages, GPT) == "openai"
    assert _jev_message_shape(messages, CLAUDE) == "anthropic"
