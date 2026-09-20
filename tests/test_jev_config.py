"""Jev configuration: default-off, strict once enabled, never leaks the key."""

from __future__ import annotations

import pytest

from headroom.proxy.jev.config import (
    DEFAULT_JEV_ENDPOINT,
    JevConfig,
    redact_endpoint,
)


def test_default_config_is_off_and_valid() -> None:
    config = JevConfig()
    assert config.mode == "off"
    assert config.enabled is False
    assert config.is_shadow is False
    config.validate()  # must not raise


def test_shadow_without_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        JevConfig(mode="shadow").validate()


def test_shadow_with_api_key_is_accepted() -> None:
    config = JevConfig(mode="shadow", api_key="sk-test")
    config.validate()
    assert config.enabled is True
    assert config.is_shadow is True
    assert config.endpoint == DEFAULT_JEV_ENDPOINT


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="jev mode must be one of"):
        JevConfig(mode="on").validate()


def test_from_env_off_ignores_every_other_var() -> None:
    # A typo in an unused HEADROOM_JEV_* var must never stop the proxy booting.
    config = JevConfig.from_env(
        {"HEADROOM_JEV_MODE": "off", "HEADROOM_JEV_TIMEOUT_MS": "not-a-number"}
    )
    assert config == JevConfig()


def test_from_env_shadow_reads_every_knob() -> None:
    config = JevConfig.from_env(
        {
            "HEADROOM_JEV_MODE": "shadow",
            "HEADROOM_JEV_API_KEY": "sk-test",
            "HEADROOM_JEV_ENDPOINT": "https://example.invalid/v1/systemone",
            "HEADROOM_JEV_MODEL": "jev-1.2.3",
            "HEADROOM_JEV_TIMEOUT_MS": "1500",
            "HEADROOM_JEV_THRESHOLD_PERCENT": "70",
            "HEADROOM_JEV_COOLDOWN_TURNS": "3",
            "HEADROOM_JEV_MAX_CANDIDATE_TOKENS": "4096",
            "HEADROOM_JEV_MAX_CANDIDATES": "8",
            "HEADROOM_JEV_MAX_STATE_TOKENS": "6000",
        }
    )
    assert config.mode == "shadow"
    assert config.api_key == "sk-test"
    assert config.model == "jev-1.2.3"
    assert config.timeout_ms == 1500
    assert config.threshold_percent == 70
    assert config.cooldown_turns == 3
    assert config.max_candidate_tokens == 4096
    assert config.max_candidates == 8
    assert config.max_state_tokens == 6000


def test_from_env_shadow_without_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        JevConfig.from_env({"HEADROOM_JEV_MODE": "shadow"})


def test_from_env_rejects_non_integer_knob_when_enabled() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_TIMEOUT_MS must be an integer"):
        JevConfig.from_env(
            {
                "HEADROOM_JEV_MODE": "shadow",
                "HEADROOM_JEV_API_KEY": "sk-test",
                "HEADROOM_JEV_TIMEOUT_MS": "half a second",
            }
        )


def test_redacted_never_contains_the_api_key() -> None:
    config = JevConfig(mode="shadow", api_key="sk-super-secret")
    payload = config.redacted()
    assert "sk-super-secret" not in repr(payload)
    assert payload["api_key_configured"] is True
    assert "api_key" not in payload


def test_redact_endpoint_strips_userinfo_and_query() -> None:
    assert (
        redact_endpoint("https://user:pw@api.example.invalid:8443/v1/systemone?token=abc")
        == "https://<redacted>@api.example.invalid:8443/v1/systemone?<redacted>"
    )
    assert redact_endpoint("") == "<unset>"


def test_redact_endpoint_survives_a_malformed_authority() -> None:
    # ``urlsplit`` parses the port lazily, so a bad authority raises on
    # attribute access rather than at split time. Redaction runs on logging and
    # error paths, so it must degrade instead of raising -- and must still not
    # echo the query string it failed to parse.
    shown = redact_endpoint("https://api.example.invalid:not-a-port/v1/systemone?token=abc")
    assert shown == "<unparseable endpoint>"
    assert "token=abc" not in shown


def test_redacted_survives_a_malformed_endpoint() -> None:
    payload = JevConfig(
        mode="shadow",
        api_key="sk-test",
        endpoint="https://api.example.invalid:not-a-port/v1/systemone",
    ).redacted()
    assert payload["endpoint"] == "<unparseable endpoint>"
