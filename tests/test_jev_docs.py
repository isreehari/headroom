"""``wiki/configuration.md`` is bound to the Jev code it documents.

A retention feature that sends tool output to a third-party API and can replace
it with a retrieval marker is exactly the kind of thing an operator must be able
to read about before switching it on, so an undocumented knob here is a defect.

These tests are *drift detectors*, not proofreaders. What they bind to the code,
each side EXTRACTED at test time rather than hardcoded: the set of
``HEADROOM_JEV_*`` names, every default value (exact, not substring), the mode
vocabulary, the retention-lease duration (digit-bounded, so ``4-hour`` cannot
match inside ``24-hour``), the savings figures ``jev_snapshot`` derives, the
``/stats`` route's wiring to it, and the multi-worker config env var. Adding a
knob, changing a default or renaming a reported figure without touching the wiki
fails here.

Two tests check behaviour rather than text: that ``repr`` and ``redacted()``
really do withhold the API key and the endpoint's credentials.

What the rest cannot check, and what no test here should be read as proving:
that the English prose around those values is *true*. Whether "fails open"
describes what the code does is a claim about behaviour, verified by the Track
A/B/C suites, not by string matching in a Markdown file.
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


def _subsection(heading_fragment: str) -> str:
    """One ``###`` subsection of the Jev section, up to the next heading."""
    section = _jev_section()
    match = re.search(rf"^### .*{re.escape(heading_fragment)}.*$", section, re.MULTILINE)
    assert match is not None, (
        f"the {SECTION_HEADING} section has no '### ...{heading_fragment}...' subsection"
    )
    rest = section[match.end() :]
    end = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]


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
    """The table's Default cell equals the value ``JevConfig()`` actually has.

    The comparison is EXACT on the cell's text with its Markdown backticks
    stripped, not a substring: a substring check would let a code default of
    ``50`` go on matching a stale documented ``500``.

    ``api_key`` has no printable default, so its row is required to say the
    knob is required -- and required in the affirmative, since a bare
    ``"required" in text`` check would also accept "not required".

    It does NOT prove the Description cell is accurate, only the Default cell
    (and, for ``api_key``, that its description is not negated).
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
        assert re.search(r"\brequired\b", description, re.IGNORECASE), (
            "HEADROOM_JEV_API_KEY has no default, so its row must say it is "
            f"required; description reads: {description!r}"
        )
        negated = re.search(
            r"\b(not required|no longer required|optional)\b", description, re.IGNORECASE
        )
        assert negated is None, (
            "HEADROOM_JEV_API_KEY's row says it is NOT required, but "
            "JevConfig.validate() raises without it whenever the mode is not "
            f"'off'; offending wording: {negated.group(0)!r}"  # type: ignore[union-attr]
        )
        return

    documented = default_cell.strip().strip("`").strip()
    assert documented == str(default_value), (
        f"{name} defaults to {str(default_value)!r} in JevConfig but the "
        f"wiki's Default cell reads {documented!r}"
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
    # Digit-bounded on BOTH sides. A bare substring check would let a code
    # value of 4 hours go on matching a stale documented "24-hour", and a
    # value of 2 hours match a stale "24-hour" the other way round.
    section = _jev_section()
    assert re.search(rf"(?<!\d){hours}-hour(?!s?\d)", section), (
        f"JEV_RETENTION_LEASE_SECONDS is {JEV_RETENTION_LEASE_SECONDS}s "
        f"({hours}h) but the section states no '{hours}-hour' lease"
    )
    stale = {
        match.group(1)
        for match in re.finditer(r"\b(\d+)-hour\b", section)
        if match.group(1) != str(hours)
    }
    assert not stale, (
        f"the section also states {sorted(stale)} -hour durations while the "
        f"lease is {hours}h; one of them is stale"
    )


def test_the_savings_names_cited_in_the_section_match_jev_snapshot() -> None:
    """The savings figures the section cites are exactly those ``jev_snapshot`` derives.

    Both sides are EXTRACTED, neither is hardcoded: the cited names come from
    the backticked ``*_savings*`` identifiers in the section's ``/stats``
    subsection, and the produced names from the ``totals["..."] = `` lines in
    ``PrometheusMetrics.jev_snapshot``. Set equality, so citing a figure that
    no longer exists and adding a figure nobody documented both fail here.

    Scope, deliberately narrower than the old name of this test: this reads
    ``jev_snapshot``'s SOURCE and does not exercise the ``/stats`` route. That
    the route actually serves these values is
    ``tests/test_jev_stats_block.py``; that the route composes this function is
    the separate wiring test below.
    """
    from headroom.proxy.prometheus_metrics import PrometheusMetrics

    snapshot_source = inspect.getsource(PrometheusMetrics.jev_snapshot)
    produced = {
        name
        for name in re.findall(r'totals\[\s*"([a-z_]+)"\s*\]\s*=', snapshot_source)
        if "savings" in name
    }
    assert produced, "jev_snapshot() no longer derives any *_savings total"

    stats_subsection = _subsection("Reading the numbers")
    cited = set(re.findall(r"`([a-z_]*savings[a-z_]*)`", stats_subsection))

    assert cited == produced, (
        "the section's /stats subsection and jev_snapshot() disagree about the "
        f"savings figures; cited-but-not-produced={sorted(cited - produced)}, "
        f"produced-but-not-cited={sorted(produced - cited)}"
    )


def test_the_stats_route_really_composes_jev_snapshot() -> None:
    """``/stats`` builds its ``jev`` block from ``jev_snapshot()`` + ``redacted()``.

    A source-level wiring check on ``server.py``, which is what lets the
    section talk about these figures as things an operator reads off
    ``/stats``. It does NOT issue a request; ``tests/test_jev_stats_block.py``
    exercises the route.
    """
    server_source = (REPO_ROOT / "headroom" / "proxy" / "server.py").read_text(encoding="utf-8")
    assert re.search(
        r'"jev":\s*\{\s*\*\*proxy\.metrics\.jev_snapshot\(\),\s*'
        r'"config":\s*proxy\.config\.jev\.redacted\(\),',
        server_source,
    ), (
        "the /stats payload no longer builds its 'jev' block from "
        "jev_snapshot() plus JevConfig.redacted(); the wiki's claims about "
        "the /stats figures and about the endpoint being redacted there need "
        "rechecking"
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


def test_repr_and_redacted_withhold_the_key_and_the_endpoint_credentials() -> None:
    """Neither ``repr`` nor ``redacted()`` renders the key or endpoint secrets.

    BEHAVIOUR, not prose. ``repr`` matters as much as ``redacted()``: it is
    what a stray ``logger.debug("%r", config)`` and every traceback frame
    holding the config will print, so an endpoint carrying credentials in its
    userinfo or a token in its query must not survive it either.

    Scope: these two methods only. The ``/stats`` payload
    (``tests/test_jev_stats_block.py``) and the multi-worker payload
    (``tests/test_jev_proxy_config.py``) have their own tests.
    """
    config = JevConfig(
        mode="shadow",
        api_key="sk-jev-doc-test-secret",
        endpoint="https://user:pw@jev.example/v1/systemone?token=abc123",
    )
    rendered_repr = repr(config)
    rendered_redacted = repr(sorted(config.redacted().items()))

    for label, rendered in (("repr", rendered_repr), ("redacted()", rendered_redacted)):
        assert "sk-jev-doc-test-secret" not in rendered, (
            f"JevConfig.{label} leaked the API key, which must never reach a "
            f"log line, an exception message or a serialized payload: {rendered}"
        )
        assert "pw@" not in rendered, (
            f"JevConfig.{label} leaked the endpoint's userinfo credentials: {rendered}"
        )
        assert "token=abc123" not in rendered, (
            f"JevConfig.{label} leaked the endpoint's query token: {rendered}"
        )

    # Redaction, not deletion: the host must still be there to debug against.
    assert "jev.example" in rendered_repr and "jev.example" in rendered_redacted
    assert config.redacted()["api_key_configured"] is True


def test_the_section_names_the_two_mechanisms_that_withhold_the_secrets() -> None:
    """The prose credits the mechanisms the test above exercises.

    The behavioural test proves the code is safe; this proves the wiki tells
    an operator *why*, by naming ``redact_endpoint()``, ``JevConfig.redacted()``
    and the ``repr``. It is presence-checking, so it cannot tell a correct
    explanation from an incorrect one that uses the same words.
    """
    section = _jev_section()
    for mechanism in ("`redact_endpoint()`", "`JevConfig.redacted()`", "`repr`"):
        assert mechanism in section, (
            f"the {SECTION_HEADING} section never names {mechanism}, so its "
            "secret-handling claims are unattributed"
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
