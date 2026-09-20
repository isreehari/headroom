"""ProxyConfig carries the Jev config, validates it at startup, and never
serializes the API key into the multi-worker env payload."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.jev.config import JevConfig
from headroom.proxy.models import ProxyConfig
from headroom.proxy.server import _proxy_config_payload


def test_default_proxy_config_has_jev_off() -> None:
    config = ProxyConfig()
    assert config.jev.mode == "off"
    assert config.jev.enabled is False


def test_shadow_mode_without_api_key_fails_proxy_config_construction() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_API_KEY is required"):
        ProxyConfig(jev=JevConfig(mode="shadow"))


def test_shadow_mode_with_api_key_constructs() -> None:
    config = ProxyConfig(jev=JevConfig(mode="shadow", api_key="sk-test"))
    assert config.jev.is_shadow is True


def test_multi_worker_payload_omits_the_jev_block_entirely() -> None:
    config = ProxyConfig(jev=JevConfig(mode="shadow", api_key="sk-super-secret"))
    payload = _proxy_config_payload(config)
    assert "jev" not in payload
    assert "sk-super-secret" not in json.dumps(payload)
