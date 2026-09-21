"""Jev configuration: default-off, strict once enabled, never leaks the key."""

from __future__ import annotations

import pytest

from headroom.proxy.jev.config import (
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_MODEL,
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


def _enabled(**overrides: str) -> dict[str, str]:
    env = {"HEADROOM_JEV_MODE": "shadow", "HEADROOM_JEV_API_KEY": "sk-test"}
    env.update(overrides)
    return env


# --------------------------------------------------------------------------
# HEADROOM_JEV_MODEL: the one knob a whitespace typo used to slip through.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_a_blank_or_whitespace_only_model_falls_back_to_the_default(raw: str) -> None:
    """Whitespace must reach the default, not an empty model name.

    The fallback used to be applied BEFORE ``.strip()``, so ``""`` became the
    default but ``"   "`` was truthy, the ``or`` never fired, and ``.strip()``
    handed back ``""`` -- an empty model that passed validation and was then
    rejected on every single turn while the proxy silently failed open. Every
    numeric knob raises on a malformed value; this was the one that did not.
    """
    assert JevConfig.from_env(_enabled(HEADROOM_JEV_MODEL=raw)).model == DEFAULT_JEV_MODEL


def test_an_empty_model_is_rejected_by_validate() -> None:
    """``from_env`` is not the only way in; ``ProxyConfig`` validates directly."""
    with pytest.raises(ValueError, match="HEADROOM_JEV_MODEL must not be empty"):
        JevConfig(mode="shadow", api_key="sk-test", model="").validate()
    with pytest.raises(ValueError, match="HEADROOM_JEV_MODEL must not be empty"):
        JevConfig(mode="shadow", api_key="sk-test", model="   ").validate()


def test_a_surrounding_space_is_stripped_from_a_real_model_name() -> None:
    assert JevConfig.from_env(_enabled(HEADROOM_JEV_MODEL="  jev-1.2.3 ")).model == "jev-1.2.3"


def test_a_malformed_model_still_cannot_stop_an_unconfigured_proxy_booting() -> None:
    """Mode ``off`` reads nothing, so a whitespace typo is inert."""
    assert JevConfig.from_env({"HEADROOM_JEV_MODE": "off", "HEADROOM_JEV_MODEL": "   "}) == (
        JevConfig()
    )


# --------------------------------------------------------------------------
# HEADROOM_JEV_ENDPOINT: parsed, not merely prefix-matched.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("https://", id="scheme_only"),
        pytest.param("https:///path", id="empty_authority"),
        pytest.param("http://", id="http_scheme_only"),
        pytest.param("https://jev.example:not-a-port/v1", id="non_numeric_port"),
        pytest.param("https://jev.example:99999/v1", id="port_out_of_range"),
        pytest.param("https://:8080/v1", id="port_without_host"),
        pytest.param("https://user:pw@/v1", id="userinfo_without_host"),
        pytest.param("ftp://jev.example/v1", id="wrong_scheme"),
        pytest.param("jev.example/v1", id="no_scheme"),
        pytest.param("", id="empty"),
    ],
)
def test_a_well_formed_looking_but_unusable_endpoint_is_rejected_at_startup(
    endpoint: str,
) -> None:
    """A prefix check accepted all of these and then failed open on every turn.

    ``startswith(("http://", "https://"))`` cannot see an empty host or an
    unparseable port, so the proxy booted happily and every single turn made a
    doomed request that fell back to "no decision". A silent every-turn failure
    is the operability trap that justifies parsing the URL instead.

    The failure is a ``ValueError`` -- the only exception
    ``ProxyConfig.__post_init__`` expects from here -- and the message shows the
    endpoint only through ``redact_endpoint``.
    """
    with pytest.raises(ValueError, match="HEADROOM_JEV_ENDPOINT"):
        JevConfig(mode="shadow", api_key="sk-test", endpoint=endpoint).validate()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.typesafe.ai/v1/systemone",
        "http://localhost:8080/v1/systemone",
        "http://127.0.0.1:9/v1",
        "https://jev.example",
        "https://user:pw@jev.example/v1/systemone?token=abc",
        "https://[::1]:8443/v1",
        # A typo'd but well-formed host is NOT detectable without DNS, and this
        # validator deliberately makes no network probe: it stays a runtime
        # fail-open.
        "https://jev.exmaple.invalid/v1/systemone",
    ],
)
def test_a_well_formed_endpoint_is_accepted(endpoint: str) -> None:
    JevConfig(mode="shadow", api_key="sk-test", endpoint=endpoint).validate()


def test_redact_endpoint_never_raises_on_anything_validate_rejects() -> None:
    """The rejection message renders the endpoint, so redaction must survive it.

    ``redact_endpoint`` is a logging helper: if it raised on exactly the values
    the new validation rejects, the startup error would be replaced by an
    unrelated traceback from inside the error path.
    """
    for endpoint in (
        "https://",
        "https:///path",
        "https://jev.example:not-a-port/v1",
        "https://jev.example:99999/v1",
        "https://:8080/v1",
        "https://user:pw@/v1",
        "ftp://jev.example/v1",
        "jev.example/v1",
        "",
    ):
        assert isinstance(redact_endpoint(endpoint), str)


def test_endpoint_validation_raises_only_value_error() -> None:
    """``ProxyConfig.__post_init__`` catches nothing else, so nothing else may escape."""
    for endpoint in ("https://jev.example:not-a-port/v1", "https://", "\x00://x", "http://[oops"):
        with pytest.raises(ValueError):
            JevConfig(mode="shadow", api_key="sk-test", endpoint=endpoint).validate()


def test_from_env_rejects_an_unusable_endpoint() -> None:
    with pytest.raises(ValueError, match="HEADROOM_JEV_ENDPOINT"):
        JevConfig.from_env(_enabled(HEADROOM_JEV_ENDPOINT="https://"))


def test_a_malformed_endpoint_still_cannot_stop_an_unconfigured_proxy_booting() -> None:
    assert JevConfig.from_env(
        {"HEADROOM_JEV_MODE": "off", "HEADROOM_JEV_ENDPOINT": "https://"}
    ) == (JevConfig())


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


def test_repr_never_contains_the_api_key() -> None:
    # A stray ``logger.debug("%r", config)`` -- or a traceback frame that closes
    # over the config -- must not be able to leak the key.
    config = JevConfig(mode="shadow", api_key="sk-super-secret")
    shown = repr(config)
    assert "sk-" not in shown
    assert "sk-super-secret" not in shown
    assert "sk-super-secret" not in str(config)
    # The rest of the config stays introspectable.
    assert "shadow" in shown


def test_repr_never_contains_endpoint_credentials() -> None:
    """The other half of the same leak: ``repr=False`` on the key stopped short.

    An operator-supplied ``HEADROOM_JEV_ENDPOINT`` may carry credentials in its
    userinfo or a token in its query -- that is why ``redact_endpoint`` exists
    -- and the generated dataclass repr printed the field verbatim, so the same
    stray ``logger.debug("%r", config)`` that cannot leak the key could leak
    those. This asserts the display only; ``redacted()``, the ``/stats`` block
    and the multi-worker payload each have their own tests.
    """
    config = JevConfig(
        mode="shadow",
        api_key="sk-super-secret",
        endpoint="https://user:pw@api.example.invalid:8443/v1/systemone?token=abc",
    )
    shown = repr(config)
    assert "pw@" not in shown
    assert "user:pw" not in shown
    assert "token=abc" not in shown
    assert "sk-super-secret" not in shown
    # Redaction, not suppression: host, port and path stay debuggable, and the
    # key's PRESENCE is still reported the way ``redacted()`` reports it.
    assert "api.example.invalid:8443/v1/systemone" in shown
    assert "api_key_configured=True" in shown


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        b"https://user:pw@api.example.invalid/path?token=abc",
        12345,
        object(),
        ["https://user:pw@api.example.invalid/path?token=abc"],
    ],
    ids=["none", "bytes", "int", "object", "list"],
)
def test_repr_neither_raises_nor_leaks_on_a_non_str_endpoint(endpoint: object) -> None:
    """A non-``str`` endpoint is refused, not passed to ``redact_endpoint``.

    ``JevConfig`` is constructed in-process by callers and tests, not parsed
    from a wire payload, so a wrong-typed field needs no attacker. ``bytes`` is
    the case that matters most: ``urlsplit`` accepts it, so whether the raw
    value survives depends on CPython internals rather than on this module.

    Both halves are asserted: the repr does not raise, AND no fragment of the
    credential-bearing value appears in it.
    """
    shown = repr(JevConfig(mode="shadow", api_key="k", endpoint=endpoint))  # type: ignore[arg-type]
    assert shown.startswith("JevConfig(")
    assert "user:pw" not in shown
    assert "token=abc" not in shown
    assert "api.example.invalid" not in shown


def test_repr_does_not_raise_when_the_api_key_object_rejects_bool() -> None:
    """``bool(self.api_key)`` is the last field rendered, so it is guarded.

    An unguarded truthiness test there would discard the entire repr -- and it
    runs inside exception rendering, where that means losing the diagnostic
    the repr was called to produce.
    """

    class HostileKey:
        def __bool__(self) -> bool:
            raise RuntimeError("boom")

        def __repr__(self) -> str:  # pragma: no cover - must never be reached
            return "sk-leaked-through-repr"

    shown = repr(JevConfig(mode="shadow", api_key=HostileKey()))  # type: ignore[arg-type]
    assert shown.startswith("JevConfig(")
    assert "sk-leaked-through-repr" not in shown
    assert "unreadable" in shown


def test_repr_falls_back_safely_when_another_field_cannot_be_rendered() -> None:
    """``!r`` on any field invokes that object's own repr, which can raise.

    The whole assembly is wrapped for that reason. The fallback names the type
    and nothing else -- a rendering that cannot be proven clean is not printed.
    """

    class HostileModel(str):
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    shown = repr(JevConfig(mode="shadow", api_key="k", model=HostileModel("m")))
    assert shown == "<JevConfig (unrenderable)>"


def test_repr_change_did_not_alter_equality_or_hashing() -> None:
    """Only display was changed: the key is still part of identity.

    Guards the obvious wrong fix -- dropping the field or excluding it from
    ``eq`` -- which would make two configs with different credentials compare
    equal and collide in a dict.
    """
    base = JevConfig(mode="shadow", api_key="sk-a")
    same = JevConfig(mode="shadow", api_key="sk-a")
    other = JevConfig(mode="shadow", api_key="sk-b")
    assert base == same
    assert base != other
    assert len({base, same, other}) == 2


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
