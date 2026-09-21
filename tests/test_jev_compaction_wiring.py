"""Track C is only real if the hook is actually on the WS client->upstream path.

Two kinds of assertion live here, and they are deliberately kept apart because
they prove different things.

**Structural (AST over ``headroom/proxy/handlers/openai.py``).** The WS relay is
a ~2000-line closure inside ``handle_openai_responses_ws`` with no seam to call
directly, so the *shape* of the wiring -- the import, the module-level revision
store, the keyword arguments at the call site, and the position of the call
between ``_prepare_memory_frame`` and ``_maybe_compress_response_create_frame``
-- is asserted against the parsed source. This follows the source-assertion
pattern already used for this handler (``tests/test_codex_ws_savings_deferral.py``),
but uses ``ast`` rather than substring search so that a call inside a comment, a
docstring or a dead branch cannot satisfy it, and so the ordering claim is made
over real call nodes in the real function body.

A reader should NOT conclude from the structural tests that the hook *runs*: they
prove only that the code says what it should say.

**Behavioural (the real relay, driven end to end).** That gap is closed by the
tests at the bottom of this file, which run ``handle_openai_responses_ws``
against the same fake client socket and fake upstream used by
``tests/test_openai_codex_ws_lifecycle.py``:

* with ``HEADROOM_JEV_MODE`` unset (no ``config.jev`` at all) a real Codex
  compaction-boundary frame reaches the upstream **byte-identical** -- default
  off is genuinely free;
* the hook is *reached* on a live relayed frame, it is handed the frame that
  ``_prepare_memory_frame`` already rewrote, it is handed that frame *before*
  ``_maybe_compress_response_create_frame`` rewrites it (proved by the ChatGPT
  ``store=false`` rewrite, which compression performs and which the hook must
  therefore not see), and the frame the hook *returns* is what the rest of the
  pipeline carries to the upstream.

The orchestrator's own thirteen-reason fail-open matrix is covered by
``tests/test_jev_compaction_hook.py``; nothing here re-tests it.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import headroom.proxy.handlers.openai as openai_module
from headroom.ccr import CCR_TOOL_NAME
from headroom.proxy.jev.compaction_hook import REASON_DISABLED
from tests.test_openai_codex_ws_lifecycle import (
    _DummyOpenAIHandler,
    _FakeUpstream,
    _FakeWebSocket,
    _make_fake_websockets_module,
)

OPENAI_HANDLER = Path(__file__).parent.parent / "headroom" / "proxy" / "handlers" / "openai.py"

HOOK = "apply_jev_compaction_boundary"
MEMORY_PREP = "_prepare_memory_frame"
COMPRESS = "_maybe_compress_response_create_frame"
RELAY = "_client_to_upstream"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _module() -> ast.Module:
    return ast.parse(OPENAI_HANDLER.read_text(encoding="utf-8"), filename=str(OPENAI_HANDLER))


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _relay_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == RELAY
    ]
    assert len(matches) == 1, f"expected exactly one `{RELAY}` in {OPENAI_HANDLER}"
    return matches[0]


def _calls_in(node: ast.AST, name: str) -> list[ast.Call]:
    """Every ``ast.Call`` in the subtree that *invokes* ``name``.

    Deliberately narrow: this answers "where is it called", which is what the
    ordering assertions need. It does NOT see the name used as a value, bound
    to a local, or reached through an alias. Use :func:`_references_in` for
    "is this name mentioned at all".
    """
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _called_name(child) == name
    ]


def _references_in(node: ast.AST, name: str) -> list[ast.AST]:
    """Every mention of ``name`` in the subtree, in any form.

    Wider than :func:`_calls_in` on purpose, because some invariants are about
    *reachability*, not about a call. Matches:

    * ``ast.Name`` -- a bare reference, including one bound to a local or
      passed as a value (``fn = _run_compression_in_executor``);
    * ``ast.Attribute`` -- ``self._run_compression_in_executor``, and any other
      object it might be reached through;
    * ``ast.Constant`` -- the name as a string, which is how ``getattr`` and
      ``functools.partial`` style indirection would spell it.

    An alias bound *outside* the inspected subtree still escapes this, which no
    single-subtree AST check can close; the behavioural tests are what cover
    what actually runs.
    """
    found: list[ast.AST] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == name:
            found.append(child)
        elif isinstance(child, ast.Attribute) and child.attr == name:
            found.append(child)
        elif isinstance(child, ast.Constant) and child.value == name:
            found.append(child)
    return found


def _position(node: ast.expr) -> tuple[int, int]:
    """Textual position, so call nodes can be ordered within one function body."""
    return (node.lineno, node.col_offset)


def _sole_hook_call(relay: ast.AsyncFunctionDef) -> ast.Call:
    calls = _calls_in(relay, HOOK)
    assert len(calls) == 1, f"expected exactly one {HOOK} call in {RELAY}, found {len(calls)}"
    return calls[0]


def _normalized(node: ast.AST) -> str:
    """``ast.unparse`` output with quote style made irrelevant.

    ``ast.unparse`` prefers single quotes, so a source-faithful comparison
    would fail on quote style alone. Only used on short expressions whose
    string literals contain no quote characters of their own.
    """
    return ast.unparse(node).replace('"', "'")


# ---------------------------------------------------------------------------
# Structural: the import and the process-wide revision store
# ---------------------------------------------------------------------------


def test_relay_module_imports_the_hook_from_the_orchestrator() -> None:
    """STRUCTURAL. The names come from Task 25's module, at module scope."""
    tree = _module()
    module_level = {
        node.module: {alias.name for alias in node.names}
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert "headroom.proxy.jev.compaction_hook" in module_level, (
        "the relay must import Track C's orchestrator at module scope, "
        "not lazily inside the request path"
    )
    assert {HOOK, "resolve_jev_client"} <= module_level["headroom.proxy.jev.compaction_hook"]
    assert "JevCompactionRevisionStore" in module_level.get(
        "headroom.proxy.jev.compaction_state", set()
    )


def test_revision_store_is_a_process_wide_module_level_singleton() -> None:
    """STRUCTURAL. A per-connection store could never see a reconnect replay.

    The WS handler assigns ``session_id = uuid.uuid4().hex`` per accepted
    socket, so a boundary replayed on a *new* connection arrives under a new
    session id. The store is keyed on ``previous_response_id`` alone precisely
    so that replay is still recognized -- which only works if the store outlives
    the connection.
    """
    tree = _module()
    module_level_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_JEV_COMPACTION_REVISIONS"
            for target in node.targets
        )
    ]
    assert len(module_level_assignments) == 1, (
        "_JEV_COMPACTION_REVISIONS must be assigned exactly once, at module scope "
        "(a store built per connection or per frame would never recognize a replay)"
    )
    value = module_level_assignments[0].value
    assert isinstance(value, ast.Call) and _called_name(value) == "JevCompactionRevisionStore"

    # And nowhere else: no inner scope may rebind or rebuild it.
    for node in ast.walk(tree):
        if node in module_level_assignments:
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Name) and target.id == "_JEV_COMPACTION_REVISIONS"
                ), "the revision store must not be rebound inside a function or class body"

    # The live singleton is importable and really is the store type.
    assert isinstance(
        openai_module._JEV_COMPACTION_REVISIONS,
        openai_module.JevCompactionRevisionStore,
    )


# ---------------------------------------------------------------------------
# Structural: where the call sits
# ---------------------------------------------------------------------------


def test_hook_runs_after_memory_prep_and_before_compression() -> None:
    """STRUCTURAL. The ordering is load-bearing in both directions.

    After ``_prepare_memory_frame`` so Jev sees the frame Headroom will really
    send; before ``_maybe_compress_response_create_frame`` so Jev stages the
    ORIGINAL tool output in CCR. Running it after compression would stage an
    already-compressed marker as if it were content -- silently unrecoverable.
    """
    relay = _relay_function(_module())
    hook_at = _position(_sole_hook_call(relay))

    memory_calls = _calls_in(relay, MEMORY_PREP)
    compress_calls = _calls_in(relay, COMPRESS)
    assert memory_calls, f"{MEMORY_PREP} is no longer called in {RELAY}; re-anchor this test"
    assert compress_calls, f"{COMPRESS} is no longer called in {RELAY}; re-anchor this test"

    assert max(_position(call) for call in memory_calls) < hook_at, (
        f"{HOOK} must run AFTER {MEMORY_PREP} so Jev sees the frame Headroom sends"
    )
    assert hook_at < min(_position(call) for call in compress_calls), (
        f"{HOOK} must run BEFORE {COMPRESS}: Jev must stage the ORIGINAL tool "
        "output in CCR, and staging an already-compressed marker is unrecoverable"
    )


def test_hook_call_sits_inside_the_response_create_guard() -> None:
    """STRUCTURAL. Non-create frames must not pay for the hook at all."""
    relay = _relay_function(_module())
    hook_call = _sole_hook_call(relay)
    guards = [
        node
        for node in ast.walk(relay)
        if isinstance(node, ast.If) and hook_call in set(ast.walk(node))
    ]
    assert guards, f"{HOOK} is not inside any `if` in {RELAY}"
    assert any(
        any(
            isinstance(const, ast.Constant) and const.value == "response.create"
            for const in ast.walk(guard.test)
        )
        for guard in guards
    ), f"{HOOK} must be guarded by the `response.create` frame-type check"


def test_hook_call_passes_the_session_identity_config_and_store() -> None:
    """STRUCTURAL. The orchestrator's whole contract depends on these six."""
    call = _sole_hook_call(_relay_function(_module()))
    keywords = {kw.arg: _normalized(kw.value) for kw in call.keywords if kw.arg}
    assert keywords.get("jev_config") == "getattr(self.config, 'jev', None)"
    assert keywords.get("client") == "resolve_jev_client(self)"
    assert keywords.get("session_id") == "session_id"
    assert keywords.get("request_id") == "request_id"
    assert keywords.get("revisions") == "_JEV_COMPACTION_REVISIONS"
    assert keywords.get("metrics") == "getattr(self, 'metrics', None)"
    assert len(call.args) == 1, "the frame is the sole positional argument"


def test_jev_call_site_never_reaches_the_shared_compression_executor() -> None:
    """STRUCTURAL. Its timeout path quarantines compression for all later traffic.

    ``_run_compression_in_executor``'s timeout path marks timeout debt and
    quarantines the shared pool, which would switch Headroom's own compression
    off for unrelated traffic. No Jev step may add a new way to arm that -- the
    orchestrator owns its own bound.

    The check is for any *reference*, not just a call: passing the executor as a
    value, binding it to a local or reaching it through an attribute or a
    ``getattr`` string would arm the same quarantine just as effectively as
    calling it inline, so ``ast.Name``, ``ast.Attribute`` and ``ast.Constant``
    are all matched.

    Scope, stated precisely: this covers the hook call's own subtree and the
    whole ``if`` block Track C added to the relay. It cannot see an alias bound
    elsewhere in the closure and handed in, which no single-subtree AST check
    can; that residue is why the behavioural tests exist.
    """
    relay = _relay_function(_module())
    call = _sole_hook_call(relay)
    executor = "_run_compression_in_executor"

    assert not _references_in(call, executor), (
        "Jev work must never reference the shared compression executor: its "
        "timeout path quarantines compression for every later frame"
    )

    # Widen to the whole statement Track C added, so a line like
    # `fn = self._run_compression_in_executor` sitting beside the await is
    # caught too.
    jev_block = [
        node for node in ast.walk(relay) if isinstance(node, ast.If) and call in set(ast.walk(node))
    ]
    assert jev_block, "the hook call must sit inside a guard (see the guard test)"
    innermost = min(jev_block, key=lambda node: len(list(ast.walk(node))))
    offending = [
        ast.unparse(node)
        for node in _references_in(innermost, executor)
        if isinstance(node, ast.Name | ast.Attribute)
    ]
    assert not offending, (
        f"the relay branch carrying the Jev hook references {executor}: {offending}"
    )


def test_relay_branches_on_the_imported_reason_constant() -> None:
    """STRUCTURAL. The reason vocabulary is a contract, not a set of literals."""
    tree = _module()
    module_level = {
        node.module: {alias.name for alias in node.names}
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert "REASON_DROPPED" in module_level.get("headroom.proxy.jev.compaction_hook", set()), (
        "branch on the exported REASON_* constant rather than re-typing the literal"
    )
    # Matched as a *constant node*, not as text in the unparsed source: an
    # earlier version of this test compared both quote styles against
    # ``ast.unparse`` output, and since ``unparse`` normalizes to single quotes
    # the double-quoted half could never have fired. A node walk has no such
    # blind spot.
    relay = _relay_function(tree)
    assert not _references_in(relay, "jev_compaction_dropped"), (
        "branch on the imported REASON_DROPPED constant rather than re-typing "
        "the literal reason string in the relay"
    )


# ---------------------------------------------------------------------------
# Behavioural: drive the real relay
# ---------------------------------------------------------------------------


def _boundary_frame(*, marker: str = "original stdout body") -> str:
    """A real Codex native compaction boundary, as Phase 0b observed it."""
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.6-sol",
                "previous_response_id": "resp_wiring_1",
                "tools": [{"type": "function", "name": CCR_TOOL_NAME}],
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_wiring",
                        "output": marker,
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )


def _handshake_frame() -> str:
    return json.dumps({"type": "response.create", "response": {"model": "gpt-5.6-sol"}})


def _upstream_events() -> list[str]:
    return [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]


MEMORY_SENTINEL_TOOL = "memory_sentinel_search"


class _JevMemoryHandler:
    """Minimal memory handler: proves ``_prepare_memory_frame`` ran first.

    It injects a *tool*, not context: the boundary frame's ``input`` is
    list-shaped, and the WS path deliberately leaves list-shaped input
    un-injected (PR-C5 -- the Rust handler owns it). Tool injection rewrites
    ``response.tools`` for every create frame, so it is the observable that
    actually fires on a compaction boundary.
    """

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            inject_context=False,
            inject_tools=True,
            project_root_override="",
        )

    async def search_and_format_context(self, _user_id: Any, _messages: Any, **_kw: Any) -> str:
        return ""

    def compute_memory_tool_definitions(self, _provider: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": MEMORY_SENTINEL_TOOL,
                    "description": "sentinel",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]


@pytest.mark.asyncio
async def test_default_off_relay_forwards_a_boundary_frame_byte_identical() -> None:
    """BEHAVIOURAL. With no ``config.jev`` the boundary crosses untouched.

    Not a mock: the frame goes through the real ``_client_to_upstream`` loop,
    through the real hook call site, and out to the fake upstream. The
    orchestrator short-circuits on ``jev_compaction_disabled`` before it parses
    anything, so default-off costs nothing and changes nothing.
    """
    boundary = _boundary_frame()
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[_handshake_frame(), boundary])
    handler = _DummyOpenAIHandler()
    assert getattr(handler.config, "jev", None) is None

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[1] == boundary, (
        "an unconfigured proxy must relay the boundary frame byte-identically"
    )


def _spy(seen: list[str], returns: str) -> Callable[..., Awaitable[tuple[str, str]]]:
    """Stand in for the orchestrator, recording exactly what it was handed."""

    async def _hook(raw_msg: str, **kwargs: Any) -> tuple[str, str]:
        seen.append(raw_msg)
        assert kwargs["jev_config"] is None
        assert kwargs["session_id"]
        assert kwargs["request_id"]
        assert kwargs["revisions"] is openai_module._JEV_COMPACTION_REVISIONS
        return returns, REASON_DISABLED

    return _hook


@pytest.mark.asyncio
async def test_relay_hands_the_hook_the_frame_before_compression_rewrites_it() -> None:
    """BEHAVIOURAL. The hook is really called, and strictly before compression.

    ``ChatGPT-Account-ID`` makes ``_maybe_compress_response_create_frame``
    perform its ``store=false`` rewrite. The frame handed to the hook must
    therefore NOT carry ``store`` -- if the hook ran after compression it would.
    The frame the hook *returns* is what the rest of the pipeline carries, and
    Headroom's own frame handling still runs over it.
    """
    seen: list[str] = []
    rewritten = json.dumps({"type": "response.create", "response": {"input": "REWRITTEN-BY-JEV"}})
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(
        frames=[_handshake_frame(), _boundary_frame()],
        headers={"authorization": "Bearer test", "ChatGPT-Account-ID": "acct-123"},
    )
    handler = _DummyOpenAIHandler()

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(seen, rewritten)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(seen) == 1, "the relay must reach the hook exactly once per create frame"
    inner = json.loads(seen[0])["response"]
    assert "store" not in inner, (
        "the hook must run BEFORE _maybe_compress_response_create_frame -- it saw "
        "a frame compression had already rewritten"
    )
    assert inner["input"][0]["output"] == "original stdout body", (
        "Jev must be shown the ORIGINAL tool output, never a compressed marker"
    )

    forwarded = json.loads(upstream.sent[1])
    assert "REWRITTEN-BY-JEV" in json.dumps(forwarded), (
        "the relay must forward the frame the hook RETURNS, not the one it was given"
    )
    assert forwarded["response"]["store"] is False, (
        "Headroom's own frame handling must still run over whatever Jev leaves"
    )


@pytest.mark.asyncio
async def test_relay_hands_the_hook_the_frame_memory_prep_already_rewrote() -> None:
    """BEHAVIOURAL. The hook runs AFTER ``_prepare_memory_frame``.

    Memory *tool* injection is the observable: it rewrites ``response.tools``
    on every create frame, including one with list-shaped input. It is
    disallowed under ChatGPT auth, which is why this is a separate test from
    the compression-ordering one above.
    """
    seen: list[str] = []
    upstream = _FakeUpstream(_upstream_events())
    boundary = _boundary_frame()
    client_ws = _FakeWebSocket(frames=[_handshake_frame(), boundary])
    handler = _DummyOpenAIHandler()
    handler.memory_handler = _JevMemoryHandler()

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(seen, boundary)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(seen) == 1
    inner = json.loads(seen[0])["response"]
    tool_names = {t.get("name") for t in inner.get("tools", []) if isinstance(t, dict)}
    assert MEMORY_SENTINEL_TOOL in tool_names, (
        "the hook must run AFTER _prepare_memory_frame -- it saw a frame memory "
        "injection had not yet touched"
    )
    assert CCR_TOOL_NAME in tool_names, (
        "the client's own recovery tool must survive memory injection, or the "
        "orchestrator's recovery-tool gate would decline every boundary"
    )
