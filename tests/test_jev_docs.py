"""``wiki/configuration.md`` is bound to the Jev code it documents.

A retention feature that sends tool output to a third-party API and can replace
it with a retrieval marker is exactly the kind of thing an operator must be able
to read about before switching it on, so an undocumented knob here is a defect.

These tests are *drift detectors*, not proofreaders. What they bind to the code:
the set of ``HEADROOM_JEV_*`` names, every default value, the mode vocabulary,
the retention-lease duration, the ``/stats`` field names and the multi-worker
config env var -- each read out of the source at test time, so adding a knob or
changing a default without touching the wiki fails here.

What they cannot check, and what no test in this file should be read as
proving: that the English prose around those values is *true*. Whether
"fails open" describes what the code does is a claim about behaviour, verified
by the Track A/B/C test suites, not by string matching in a Markdown file.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path

import pytest

from headroom.proxy.jev import config as jev_config_module
from headroom.proxy.jev.config import JEV_MODES, JevConfig
from headroom.proxy.jev.retention_ccr import JEV_RETENTION_LEASE_SECONDS

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_CONFIG = REPO_ROOT / "wiki" / "configuration.md"
SECTION_HEADING = "## Jev Retention"

#: Fields of :class:`JevConfig` that are NOT read from an env var of their own.
#: Empty today; kept explicit so a future derived field is an edit here rather
#: than a silently missing doc row.
_NON_ENV_FIELDS: frozenset[str] = frozenset()


def _doc_text() -> str:
    return WIKI_CONFIG.read_text(encoding="utf-8")


def _jev_section() -> str:
    """The ``## Jev Retention`` section, up to the next ``##`` heading."""
    doc = _doc_text()
    start = doc.find(f"\n{SECTION_HEADING}")
    assert start != -1, (
        f"{WIKI_CONFIG} has no {SECTION_HEADING!r} section; the Jev "
        "configuration, privacy and fail-open disclosure is missing entirely"
    )
    body = doc[start + 1 :]
    end = body.find("\n## ", len(SECTION_HEADING))
    return body if end == -1 else body[:end]


def _env_name_for(field_name: str) -> str:
    return f"HEADROOM_JEV_{field_name.upper()}"


def _expected_env_names() -> set[str]:
    """Env var names derived from :class:`JevConfig`'s own fields."""
    return {
        _env_name_for(f.name)
        for f in dataclasses.fields(JevConfig)
        if f.name not in _NON_ENV_FIELDS
    }


def _table_rows() -> dict[str, tuple[str, str]]:
    """``{env name: (description cell, default cell)}`` from the section table."""
    rows: dict[str, tuple[str, str]] = {}
    for line in _jev_section().splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        match = re.fullmatch(r"`(HEADROOM_JEV_[A-Z_]+)`", cells[0])
        if match:
            rows[match.group(1)] = (cells[1], cells[2])
    return rows


def test_env_names_config_module_mentions_are_all_documented() -> None:
    """Every ``HEADROOM_JEV_*`` literal in ``jev/config.py`` appears in the wiki.

    Source-driven, so a knob added to ``from_env`` (or named in a validation
    message) with no wiki entry fails here. It only proves the NAME is present
    somewhere in the file; it says nothing about whether the description or the
    default beside it is correct -- the two tests below cover the defaults.
    """
    source = Path(jev_config_module.__file__).read_text(encoding="utf-8")
    doc = _doc_text()
    mentioned = sorted(set(re.findall(r"HEADROOM_JEV_[A-Z_]+", source)))
    assert mentioned, "no HEADROOM_JEV_* literal found in jev/config.py at all"
    for name in mentioned:
        assert name in doc, (
            f"{name} appears in {jev_config_module.__file__} but nowhere in {WIKI_CONFIG.name}"
        )


def test_the_documented_table_is_exactly_the_set_of_jevconfig_fields() -> None:
    """The table's rows match ``JevConfig``'s fields one for one.

    Both directions: a new field with no row, and a row for a knob that no
    longer exists. It checks the SET of names only -- a row in the wrong place
    in the table, or with a wrong description, still passes.
    """
    documented = set(_table_rows())
    expected = _expected_env_names()
    assert documented == expected, (
        "the HEADROOM_JEV_* table in wiki/configuration.md has drifted from "
        f"JevConfig's fields; undocumented={sorted(expected - documented)}, "
        f"documented-but-not-a-field={sorted(documented - expected)}"
    )


@pytest.mark.parametrize(
    "field_name",
    [f.name for f in dataclasses.fields(JevConfig) if f.name not in _NON_ENV_FIELDS],
)
def test_documented_default_matches_the_code_default(field_name: str) -> None:
    """The table's Default cell carries the value ``JevConfig()`` actually has.

    ``api_key`` defaults to the empty string -- there is no value to print --
    so its row is required to say the knob is required instead. For every other
    field this is a substring check against the real default, so bumping
    ``DEFAULT_JEV_TIMEOUT_MS`` without editing the wiki fails here. It does NOT
    prove the Description cell is accurate, only the Default cell.
    """
    rows = _table_rows()
    name = _env_name_for(field_name)
    assert name in rows, f"{name} has no row in the wiki table"
    description, default_cell = rows[name]

    default_value = getattr(JevConfig(), field_name)
    if field_name == "api_key":
        assert default_value == "", (
            "this test assumes api_key has no usable default; it now defaults "
            f"to {default_value!r} and the wiki row must be revisited"
        )
        assert "equired" in description, (
            "HEADROOM_JEV_API_KEY has no default, so its row must say it is "
            f"required; description reads: {description!r}"
        )
        return

    assert str(default_value) in default_cell, (
        f"{name} defaults to {default_value!r} in JevConfig but the wiki's "
        f"Default cell reads {default_cell!r}"
    )


def test_every_mode_in_the_code_vocabulary_is_documented() -> None:
    """Each string in ``JEV_MODES`` is named in the section.

    Adding a fourth mode without documenting it fails here. It does not check
    that the section describes each mode CORRECTLY, only that it names it.
    """
    section = _jev_section()
    for mode in JEV_MODES:
        assert f"`{mode}`" in section, (
            f"mode {mode!r} is in JEV_MODES but is never named in the {SECTION_HEADING} section"
        )


def test_the_documented_lease_duration_matches_the_code() -> None:
    """The stated retention-lease window matches ``JEV_RETENTION_LEASE_SECONDS``.

    Binds one number an operator will plan around. It proves the wiki and the
    constant agree, not that the lease is actually taken -- that is
    ``tests/test_jev_retention_ccr.py``'s job.
    """
    hours = JEV_RETENTION_LEASE_SECONDS // 3600
    assert JEV_RETENTION_LEASE_SECONDS % 3600 == 0, (
        "the lease is no longer a whole number of hours; the wiki's "
        f"'{hours}-hour' phrasing needs revisiting"
    )
    assert f"{hours}-hour" in _jev_section(), (
        f"JEV_RETENTION_LEASE_SECONDS is {JEV_RETENTION_LEASE_SECONDS}s "
        f"({hours}h) but the section does not say '{hours}-hour'"
    )


def test_the_stats_field_names_the_section_cites_are_real() -> None:
    """``projected_savings`` / ``realized_savings*`` are really built by ``/stats``.

    Read out of ``jev_snapshot``'s source, so renaming a key there without
    editing the wiki fails. It proves the names exist in that function, not
    that the section's explanation of what they mean is right.
    """
    from headroom.proxy.prometheus_metrics import PrometheusMetrics

    snapshot_source = inspect.getsource(PrometheusMetrics.jev_snapshot)
    section = _jev_section()
    for name in ("projected_savings", "realized_savings", "realized_savings_estimated"):
        assert f'"{name}"' in snapshot_source, (
            f"{name} is cited in the wiki but jev_snapshot() no longer sets it"
        )
        assert f"`{name}`" in section, (
            f"jev_snapshot() reports {name} but the {SECTION_HEADING} section does not mention it"
        )


def test_the_multi_worker_config_env_var_name_is_the_real_one() -> None:
    """The env var the section names is ``server._MULTI_WORKER_CONFIG_ENV``.

    The key is deliberately kept OUT of that payload, so an operator running
    workers has to export ``HEADROOM_JEV_*`` instead. Read from the server
    source rather than imported, to keep this test off the proxy's import cost.
    It proves the name is current; the exclusion itself is asserted by
    ``tests/test_jev_proxy_config.py``.
    """
    server_source = (REPO_ROOT / "headroom" / "proxy" / "server.py").read_text(encoding="utf-8")
    match = re.search(r'_MULTI_WORKER_CONFIG_ENV\s*=\s*"([A-Z_]+)"', server_source)
    assert match is not None, "could not find _MULTI_WORKER_CONFIG_ENV in server.py"
    assert f"`{match.group(1)}`" in _jev_section(), (
        f"the multi-worker config payload env var is {match.group(1)} but the "
        f"{SECTION_HEADING} section names something else (or nothing)"
    )


def test_redacted_really_omits_the_key_the_section_promises_it_omits() -> None:
    """``repr`` and ``redacted()`` behave exactly as the section describes them.

    This one checks BEHAVIOUR, not prose: these are the two mechanisms the
    section names by hand, so the claim and the code are asserted together.
    The key is absent from both. The ENDPOINT is only redacted by
    ``redacted()`` -- the dataclass ``repr`` prints it verbatim, userinfo and
    query included -- which is asserted here so the wiki cannot quietly start
    claiming otherwise.

    Scope: these two methods only. The ``/stats`` payload
    (``tests/test_jev_stats_block.py``) and the multi-worker payload
    (``tests/test_jev_proxy_config.py``) have their own tests.
    """
    config = JevConfig(
        mode="shadow",
        api_key="sk-jev-doc-test-secret",
        endpoint="https://user:pw@jev.example/v1/systemone?token=abc",
    )
    redacted = config.redacted()
    rendered = repr(sorted(redacted.items()))
    assert "sk-jev-doc-test-secret" not in rendered + repr(config), (
        "JevConfig.redacted()/repr() leaked the API key, which the wiki says "
        "never leaves the process"
    )
    assert "pw@" not in rendered and "token=abc" not in rendered, (
        f"JevConfig.redacted() leaked endpoint userinfo or query: {rendered}"
    )
    assert redacted["api_key_configured"] is True
    # The counterpart the wiki must not overstate: the raw repr is NOT redacted.
    assert "token=abc" in repr(config), (
        "JevConfig's repr no longer prints the endpoint verbatim; the wiki "
        "says only redacted() redacts it and must be updated"
    )


def test_privacy_and_fail_open_are_disclosed() -> None:
    """The section makes the four disclosures the design doc requires.

    Keyword presence only: default-off, the shadow/active split, what content
    leaves the machine, the no-anonymization position, fail-open, and that Jev
    is additive to Headroom's own compression. This CANNOT detect a sentence
    that names the topic while describing it wrongly -- it is a floor, not a
    review.
    """
    section = _jev_section()
    for phrase in (
        "default off",
        "tool result",
        "never logged",
        "fails open",
        "additive",
        "No PII anonymization",
    ):
        assert phrase in section, (
            f"the {SECTION_HEADING} section never says {phrase!r}; the "
            "privacy / fail-open disclosure is incomplete"
        )


def test_the_section_sits_between_environment_variables_and_settings_gui() -> None:
    """Placement, located by heading text rather than by line number.

    The section belongs with the other env-var documentation. Nothing about
    the content is checked here.
    """
    doc = _doc_text()
    env_vars = doc.find("\n## Environment Variables")
    jev = doc.find(f"\n{SECTION_HEADING}")
    settings_gui = doc.find("\n## Settings GUI")
    assert env_vars != -1 and settings_gui != -1, (
        "wiki/configuration.md no longer has the headings this section is anchored between"
    )
    assert env_vars < jev < settings_gui, (
        "the Jev section must sit after '## Environment Variables' and before "
        f"'## Settings GUI' (offsets: env={env_vars}, jev={jev}, "
        f"gui={settings_gui})"
    )
