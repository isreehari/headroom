"""Request-level gate for Jev active retention on POST /v1/compress.

The proven request shape (docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md,
"Track B") is: config.mode="ccr" + config.session_id + config.jev_compaction_boundary=true.
This gate is the only thing that opens the active path, so its rejections are part of
the route's public contract.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from headroom.proxy.jev.compress_gate import (
    JEV_COMPRESS_BRANCH_ID,
    JevGateError,
    parse_compaction_boundary,
)


def test_absent_flag_is_not_a_boundary() -> None:
    assert parse_compaction_boundary({}, "ccr") is False
    assert parse_compaction_boundary({"jev_compaction_boundary": None}, "ccr") is False
    assert parse_compaction_boundary({"jev_compaction_boundary": False}, None) is False


def test_proven_request_shape_is_a_boundary() -> None:
    config = {
        "mode": "ccr",
        "session_id": "caller-owned-session-id",
        "jev_compaction_boundary": True,
    }
    assert parse_compaction_boundary(config, "ccr") is True


def test_non_boolean_flag_is_rejected() -> None:
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary({"jev_compaction_boundary": "true"}, "ccr")
    assert "jev_compaction_boundary" in excinfo.value.message
    # An int 1 is not a JSON boolean either: accepting it would make a typo look
    # like consent to rewrite history.
    with pytest.raises(JevGateError):
        parse_compaction_boundary({"jev_compaction_boundary": 1}, "ccr")


@pytest.mark.parametrize("mode", [None, "lossy_inline", "lossless_then_lossy"])
def test_boundary_requires_ccr_mode(mode: str | None) -> None:
    config = {"jev_compaction_boundary": True, "session_id": "s1", "mode": mode}
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary(config, mode)
    assert 'config.mode="ccr"' in excinfo.value.message


@pytest.mark.parametrize("session_id", [None, "", "   ", 17])
def test_boundary_requires_session_id(session_id: object) -> None:
    config = {"jev_compaction_boundary": True, "mode": "ccr", "session_id": session_id}
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary(config, "ccr")
    assert "config.session_id" in excinfo.value.message


def test_branch_id_constant_is_stable() -> None:
    # Track A's identity store keys on (session_id, branch_id); compress turns
    # must not share a lane with proxy-path branches for the same session id.
    assert JEV_COMPRESS_BRANCH_ID == "compress"


# ─── Beyond the plan's verbatim cases ───────────────────────────────────


def test_missing_session_id_key_is_rejected() -> None:
    # Distinct from ``session_id: None``: the key is absent entirely, which is
    # what a caller that never adopted sidecar session mode actually sends.
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary({"jev_compaction_boundary": True, "mode": "ccr"}, "ccr")
    assert "config.session_id" in excinfo.value.message


def test_non_mapping_config_is_not_a_boundary() -> None:
    # The handler coerces a non-dict ``config`` to ``{}`` before it gets here,
    # but a second caller (or a refactor) must not be able to turn a malformed
    # body into an *active* turn via an AttributeError-free truthiness path.
    # Fail closed, exactly as the handler's ``{}`` coercion does.
    assert parse_compaction_boundary(cast(dict[str, Any], None), "ccr") is False
    assert parse_compaction_boundary(cast(dict[str, Any], []), "ccr") is False
    assert (
        parse_compaction_boundary(cast(dict[str, Any], "jev_compaction_boundary"), "ccr") is False
    )


def test_mode_argument_is_authoritative_over_the_config_copy() -> None:
    # The handler validates ``config.mode`` against COMPRESS_MODES and passes
    # the validated value; the raw dict entry is never re-read here, so the two
    # cannot drift into "the dict said ccr, so we retained".
    config = {"jev_compaction_boundary": True, "session_id": "s1", "mode": "ccr"}
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary(config, None)
    assert 'config.mode="ccr"' in excinfo.value.message


def test_rejection_message_bounds_an_oversized_flag_value() -> None:
    # The message is echoed verbatim in a 400 body. A caller that puts a 1 MB
    # blob where a bool belongs should get a diagnostic, not its own blob back.
    blob = "x" * 100_000
    with pytest.raises(JevGateError) as excinfo:
        parse_compaction_boundary({"jev_compaction_boundary": blob}, "ccr")
    message = excinfo.value.message
    assert len(message) < 300
    assert blob not in message
    assert "jev_compaction_boundary" in message


def test_rejection_messages_carry_no_server_state() -> None:
    # Everything in a 400 body must be either static prose or a value the
    # caller itself sent. Nothing here may name an env var or an endpoint.
    messages = []
    for config, mode in (
        ({"jev_compaction_boundary": "true"}, "ccr"),
        ({"jev_compaction_boundary": True, "session_id": "s1"}, "lossy_inline"),
        ({"jev_compaction_boundary": True, "mode": "ccr"}, "ccr"),
    ):
        with pytest.raises(JevGateError) as excinfo:
            parse_compaction_boundary(config, mode)
        messages.append(excinfo.value.message)
    for message in messages:
        assert "HEADROOM_" not in message
        assert "http" not in message
        assert "api_key" not in message.lower()


def test_error_is_a_value_error_for_handlers_that_catch_broadly() -> None:
    # ``/v1/compress`` already maps ValueError-shaped validation failures to a
    # 400; subclassing keeps a handler that forgets the specific except clause
    # from turning a malformed flag into a 500.
    assert issubclass(JevGateError, ValueError)
    error = JevGateError("boom")
    assert error.message == "boom"
    assert str(error) == "boom"
