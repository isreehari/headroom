"""Track C is only real if the hook is actually on the WS client->upstream path.

A WS connection has TWO client->upstream paths, and Track C has to be on both.
Frame 1 is handled inline in ``handle_openai_responses_ws``'s own body; frames
2..N go through the nested ``_client_to_upstream`` relay loop. A compaction
boundary can arrive on either -- as the first frame it is the shape a reconnect
replay produces -- so every wiring claim in this file is made twice, once per
path.

Two kinds of assertion live here, and they are deliberately kept apart because
they prove different things.

**Structural (AST over ``headroom/proxy/handlers/openai.py``).** The WS relay is
a ~2000-line closure inside ``handle_openai_responses_ws`` with no seam to call
directly, so the *shape* of the wiring -- the import, the module-level revision
store, the keyword arguments at each call site, and each call's position between
memory injection and that path's compression step -- is asserted against the
parsed source. This follows the source-assertion pattern already used for this
handler (``tests/test_codex_ws_savings_deferral.py``), but uses ``ast`` rather
than substring search so that a call inside a comment, a docstring or a dead
branch cannot satisfy it, and so the ordering claim is made over real call nodes
in the real function body. Deliberately NOT a source ``count(...) == 2``: each
call site is resolved to the *scope* it must live in, because two calls on the
same path would satisfy a count and leave the hole this file exists to close.

A reader should NOT conclude from the structural tests that the hook *runs*:
they prove only that the code says what it should say.

**Behavioural (the real relay, driven end to end).** That gap is closed by the
tests at the bottom of this file, which run ``handle_openai_responses_ws``
against the same fake client socket and fake upstream used by
``tests/test_openai_codex_ws_lifecycle.py``. On BOTH paths:

* with ``HEADROOM_JEV_MODE`` unset (no ``config.jev`` at all) a real Codex
  compaction-boundary frame reaches the upstream **byte-identical** -- default
  off is genuinely free;
* the hook is *reached*, it is handed the frame that ``_prepare_memory_frame``
  already rewrote, it is handed that frame *before* the path's compression step
  rewrites it, and the frame the hook *returns* is what the rest of the pipeline
  carries to the upstream. The two paths need different markers for the
  ordering claim: the relay loop uses the ChatGPT ``store=false`` rewrite that
  ``_maybe_compress_response_create_frame`` performs, which does not work for
  frame 1 (there that rewrite happens well before the hook), so the first-frame
  test spies on ``_compress_openai_responses_payload_in_executor`` instead and
  pins the order from both sides at once;
* frames whose type the guard must reject -- ``response.cancel``,
  ``session.update``, and a frame that is not JSON at all -- do NOT reach the
  hook, which is the guard's *polarity*: no AST check can establish that,
  because an inverted test would satisfy it just as well. **All three shapes are
  exercised separately on each path**, because the two paths have two different
  guards: the relay loop's is ``_inbound_frame_body.get("type") ==
  "response.create"``, the first frame's is ``body.get("type") ==
  "response.create" or ("type" not in body and "input" in body)``. A guard that
  wrongly accepted a cancel frame on one path while rejecting it on the other
  has to fail a test, so neither path may borrow the other's coverage --
  ``test_relay_skips_the_hook_for_non_create_frames`` covers frames 2..N,
  ``test_a_non_create_first_frame_does_not_reach_the_hook`` covers frame 1, and
  each is parametrised over all three shapes.

What none of it proves is the replay guarantee itself -- that the same boundary
arriving under a second session id comes back stale. That is behaviour of the
shared store, and it is covered by
``test_a_reconnect_replay_is_stale_under_a_new_session_id`` in
``tests/test_jev_compaction_hook.py``. What this file adds is that the first
frame now reaches the machinery that guarantee lives in.

``test_the_boundary_fixture_is_really_a_boundary`` checks the fixture those
behavioural tests lean on against Track C's real detector, so "a real Codex
compaction boundary" is verified rather than asserted in prose.

The orchestrator's own thirteen-reason fail-open matrix is covered by
``tests/test_jev_compaction_hook.py``; nothing here re-tests it.

Every assertion below states, in its name and docstring, exactly what it covers
and what it does not. Three rounds of review each found one assertion in this
file that read as a guarantee while checking something smaller; the convention
is deliberate.
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
from headroom.proxy.jev.compaction import (
    detect_compaction_boundary,
    has_recovery_tool,
    unwrap_response_create,
)
from headroom.proxy.jev.compaction_hook import REASON_DISABLED
from tests.test_openai_codex_ws_lifecycle import (
    _DummyOpenAIHandler,
    _FakeUpstream,
    _FakeWebSocket,
    _make_fake_websockets_module,
)

OPENAI_HANDLER = Path(__file__).parent.parent / "headroom" / "proxy" / "handlers" / "openai.py"

WIKI_PROXY = Path(__file__).parent.parent / "wiki" / "proxy.md"

HOOK = "apply_jev_compaction_boundary"
MEMORY_PREP = "_prepare_memory_frame"
COMPRESS = "_maybe_compress_response_create_frame"
RELAY = "_client_to_upstream"
STORE = "_JEV_COMPACTION_REVISIONS"
STORE_TYPE = "JevCompactionRevisionStore"
EXECUTOR = "_run_compression_in_executor"
# The connection-scoped coroutine. The relay loop and `_prepare_memory_frame`
# are nested inside it; the first-frame handling is in its OWN body.
WS_HANDLER = "handle_openai_responses_ws"
# What "compression" means on the first-frame path. The first frame does not go
# through `_maybe_compress_response_create_frame` (that helper is defined inside
# the relay loop's scope and only ever sees frames 2..N); the first frame is
# compressed by this call instead.
FIRST_FRAME_COMPRESS = "_compress_openai_responses_payload_in_executor"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _module() -> ast.Module:
    return ast.parse(OPENAI_HANDLER.read_text(encoding="utf-8"), filename=str(OPENAI_HANDLER))


def _called_name(node: ast.Call) -> str | None:
    """The name being invoked, collapsing ``a.b.name()`` to ``name``.

    Deliberately ignores what the attribute hangs off, so ``self.f()`` and
    ``f()`` both answer ``"f"``. That makes every caller WIDER than an exact
    match, never narrower, so it cannot produce a false pass. Returns ``None``
    for a call with no simple name (``f()()``, ``d["k"]()``).
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_level_imports(tree: ast.Module) -> dict[str, set[str]]:
    """Module-scope ``from X import a, b`` as ``{module: {names}}``.

    Names are UNIONED across repeated ``from`` statements for the same module;
    a dict comprehension would silently let a later statement shadow an earlier
    one and drop names it did not list.
    """
    imports: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return imports


def _rebindings_of(node: ast.AST, name: str) -> list[ast.AST]:
    """Every place ``name`` is bound in the subtree, in any binding form.

    Matches on ``ast.Name`` with a ``Store`` context, which is what CPython
    emits for ALL of: plain assignment, annotated assignment, augmented
    assignment, walrus, ``for`` targets, ``with ... as``, ``except ... as`` and
    unpacking. Checking only ``ast.Assign`` -- as an earlier version of this
    file did -- would have let ``_JEV_COMPACTION_REVISIONS: Store = ...`` or a
    ``for`` target rebind the singleton unnoticed.
    """
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, ast.Store)
    ]


def _relay_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == RELAY
    ]
    assert len(matches) == 1, f"expected exactly one `{RELAY}` in {OPENAI_HANDLER}"
    return matches[0]


def _ws_handler_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == WS_HANDLER
    ]
    assert len(matches) == 1, f"expected exactly one `{WS_HANDLER}` in {OPENAI_HANDLER}"
    return matches[0]


def _own_scope_nodes(fn: ast.AST) -> set[int]:
    """``id()`` of every node in ``fn`` that is NOT inside a nested function.

    ``handle_openai_responses_ws`` contains a dozen nested ``def``s, including
    the relay loop and ``_prepare_memory_frame``. The first-frame path is the
    code in the handler's *own* body, so "is this call on the first-frame path"
    is exactly "is it in ``fn``'s own scope". Lambdas count as nested too.
    """
    nested: set[int] = set()
    for child in ast.walk(fn):
        if child is fn:
            continue
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            nested.update(id(node) for node in ast.walk(child))
    return {id(node) for node in ast.walk(fn)} - nested


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


def _sole_first_frame_hook_call(tree: ast.Module) -> ast.Call:
    """The hook call on the connection's first-frame path.

    Identified structurally -- a call in ``handle_openai_responses_ws``'s own
    scope, outside every nested function -- not by a line number or a substring,
    because this handler's line numbers move constantly.
    """
    handler = _ws_handler_function(tree)
    own = _own_scope_nodes(handler)
    calls = [call for call in _calls_in(handler, HOOK) if id(call) in own]
    assert len(calls) == 1, (
        f"expected exactly one {HOOK} call in {WS_HANDLER}'s own scope "
        f"(the first-frame path), found {len(calls)}"
    )
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
    module_level = _module_level_imports(_module())
    assert "headroom.proxy.jev.compaction_hook" in module_level, (
        "the relay must import Track C's orchestrator at module scope, "
        "not lazily inside the request path"
    )
    assert HOOK in module_level["headroom.proxy.jev.compaction_hook"]
    assert "JevCompactionRevisionStore" in module_level.get(
        "headroom.proxy.jev.compaction_state", set()
    )


def test_neither_call_site_resolves_the_jev_client_itself() -> None:
    """STRUCTURAL. Client resolution belongs to the orchestrator, not the relay.

    Python evaluates arguments before the callee, so
    ``client=resolve_jev_client(self)`` made every ``response.create`` frame pay
    for the guarded proxy/client lookups even with Jev off -- the orchestrator's
    mode check cannot run early enough to prevent that, because it has not been
    entered yet. The orchestrator takes ``proxy=self`` and resolves after its
    gates instead, which keeps it the single owner of all gating; a resolution
    hoisted back to a call site would also be a second gate there.

    Checked over the WHOLE handler module, in every reference form, so an
    ``import`` that survived, an attribute lookup or a ``getattr`` string all
    fail it.
    """
    tree = _module()
    offending = [ast.unparse(node) for node in _references_in(tree, "resolve_jev_client")]
    assert not offending, (
        f"{OPENAI_HANDLER.name} still names resolve_jev_client ({offending}): "
        "the orchestrator resolves the client itself, after its mode check"
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
        node for node in tree.body if isinstance(node, ast.Assign) and _rebindings_of(node, STORE)
    ]
    assert len(module_level_assignments) == 1, (
        f"{STORE} must be assigned exactly once, at module scope "
        "(a store built per connection or per frame would never recognize a replay)"
    )
    value = module_level_assignments[0].value
    assert isinstance(value, ast.Call) and _called_name(value) == "JevCompactionRevisionStore"

    # And nowhere else. `_rebindings_of` matches Store-context names, so this
    # covers annotated, augmented, walrus, `for`-target and `with ... as`
    # rebinding too -- not just plain `=`.
    module_level_targets = set(_rebindings_of(module_level_assignments[0], STORE))
    stray = [node for node in _rebindings_of(tree, STORE) if node not in module_level_targets]
    assert not stray, (
        f"{STORE} is rebound at line(s) "
        f"{sorted(node.lineno for node in stray)}: it must be built exactly once, "
        "at module scope, in any binding form"
    )

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


def test_hook_call_sits_inside_an_if_mentioning_response_create() -> None:
    """STRUCTURAL, and narrower than it may look.

    It proves the hook call is lexically inside an ``ast.If`` whose test
    mentions the constant ``"response.create"``. It does NOT prove the guard's
    *polarity*: an inverted test would satisfy this just as well. The claim
    that non-create frames really do skip the hook is behavioural, and is made
    by ``test_relay_skips_the_hook_for_non_create_frames``.
    """
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


def test_hook_call_passes_the_session_identity_config_and_revisions() -> None:
    """STRUCTURAL. The six keyword arguments the orchestrator's contract needs.

    Named for what it checks: ``revisions``, not the orchestrator's optional
    ``store=`` parameter, which this call site deliberately does not pass (it
    lets the orchestrator reach for the shared compression store itself).
    """
    call = _sole_hook_call(_relay_function(_module()))
    keywords = {kw.arg: _normalized(kw.value) for kw in call.keywords if kw.arg}
    assert keywords.get("jev_config") == "getattr(self.config, 'jev', None)"
    # The PROXY, not a resolved client: resolution is a gated step the
    # orchestrator owns (see `test_neither_call_site_resolves_the_jev_client_itself`).
    assert keywords.get("proxy") == "self"
    assert keywords.get("session_id") == "session_id"
    assert keywords.get("request_id") == "request_id"
    assert keywords.get("revisions") == STORE
    assert keywords.get("metrics") == "getattr(self, 'metrics', None)"
    assert len(call.args) == 1, "the frame is the sole positional argument"


def test_jev_call_site_never_reaches_the_shared_compression_executor() -> None:
    """STRUCTURAL. Its timeout path quarantines compression for all later traffic.

    ``_run_compression_in_executor``'s timeout path marks timeout debt and
    quarantines the shared pool, which would switch Headroom's own compression
    off for unrelated traffic. No Jev step may add a new way to arm that -- the
    orchestrator owns its own bound.

    **What is checked:** every ``_references_in`` node kind -- ``ast.Name``,
    ``ast.Attribute`` and ``ast.Constant`` -- with nothing filtered back out.
    A bare reference, an attribute lookup and the name spelled as a string for
    ``getattr`` all reach the same object, so all three fail the test.

    **Over what scope:** the innermost ``ast.If`` containing the hook call --
    that is, the whole ``response.create`` branch Track C added. One assertion,
    not two: this block strictly contains the hook call's own subtree, so a
    separate subtree assertion would be a second claim covering a subset of the
    same guarantee.

    **What it does NOT prove:** an alias bound elsewhere in the enclosing
    closure and handed into this block escapes it, as it would escape any
    single-subtree AST check. The behavioural tests are what cover what runs.

    **Accepted consequence:** because ``ast.Constant`` is included over a whole
    block, a future *log line* inside this branch that merely names the executor
    would fail this test. That is intended. A string naming the shared executor
    in Track C's branch is a strong hint someone is reaching for it, and is
    worth a deliberate look. No such string exists in the block today -- this
    was checked before the filter was removed.
    """
    relay = _relay_function(_module())
    call = _sole_hook_call(relay)

    guards = [
        node for node in ast.walk(relay) if isinstance(node, ast.If) and call in set(ast.walk(node))
    ]
    assert guards, "the hook call must sit inside a guard (see the guard test)"
    jev_block = min(guards, key=lambda node: len(list(ast.walk(node))))

    offending = [ast.unparse(node) for node in _references_in(jev_block, EXECUTOR)]
    assert not offending, (
        f"the relay branch carrying the Jev hook references {EXECUTOR} "
        f"({offending}): its timeout path quarantines compression for every "
        "later frame, so no Jev step may reach it in any form"
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
# Structural: the WS FIRST frame, which is not handled by the relay loop
# ---------------------------------------------------------------------------


def test_the_hook_is_called_on_exactly_two_ws_paths_and_both_are_identified() -> None:
    """STRUCTURAL. Two call sites, and each is pinned to the path it belongs to.

    Deliberately NOT ``source.count("await apply_jev_compaction_boundary(") ==
    2``: that form is satisfied by any two calls anywhere in a 10,000-line file,
    including two on the same path. This resolves each call to a *scope* --
    one inside ``_client_to_upstream`` (frames 2..N) and one in
    ``handle_openai_responses_ws``'s own body (frame 1, the shape a reconnect
    replay produces) -- and asserts those are the only two in the module.

    **Does NOT prove** either call runs, nor that the first-frame one is
    reached only for ``response.create``; see the behavioural tests below.
    """
    tree = _module()
    all_calls = _calls_in(tree, HOOK)
    relay_call = _sole_hook_call(_relay_function(tree))
    first_frame_call = _sole_first_frame_hook_call(tree)

    assert first_frame_call is not relay_call, (
        "the first-frame call site and the relay-loop call site must be two "
        "distinct calls: one coroutine cannot cover both"
    )
    assert {id(call) for call in all_calls} == {id(relay_call), id(first_frame_call)}, (
        f"{HOOK} is called at {len(all_calls)} places in {OPENAI_HANDLER.name}; "
        "exactly two are expected -- the relay loop and the WS first frame"
    )


def test_first_frame_hook_runs_after_memory_prep_and_before_first_frame_compression() -> None:
    """STRUCTURAL. The first-frame ordering mirrors the relay loop's, exactly.

    On the first-frame path "compression" is
    ``_compress_openai_responses_payload_in_executor``, not
    ``_maybe_compress_response_create_frame`` (that helper is nested inside the
    relay loop and never sees frame 1). Running the hook after it would stage an
    already-compressed marker in CCR as though it were the original tool output.

    Ordering is compared only among calls in the handler's OWN scope, so a
    ``_prepare_memory_frame`` call inside the relay loop -- which is textually
    *later* in the file -- cannot corrupt the comparison.

    **Does NOT prove** the ordering at runtime; that is
    ``test_first_frame_hook_is_handed_the_frame_before_first_frame_compression``.
    """
    tree = _module()
    handler = _ws_handler_function(tree)
    own = _own_scope_nodes(handler)
    hook_at = _position(_sole_first_frame_hook_call(tree))

    memory_calls = [c for c in _calls_in(handler, MEMORY_PREP) if id(c) in own]
    compress_calls = [c for c in _calls_in(handler, FIRST_FRAME_COMPRESS) if id(c) in own]
    assert memory_calls, (
        f"{MEMORY_PREP} is no longer called on the first-frame path; re-anchor this test"
    )
    assert compress_calls, (
        f"{FIRST_FRAME_COMPRESS} is no longer called on the first-frame path; re-anchor this test"
    )

    assert max(_position(call) for call in memory_calls) < hook_at, (
        f"on the first frame {HOOK} must run AFTER {MEMORY_PREP}, so Jev sees "
        "the frame Headroom actually sends upstream"
    )
    assert hook_at < min(_position(call) for call in compress_calls), (
        f"on the first frame {HOOK} must run BEFORE {FIRST_FRAME_COMPRESS}: Jev "
        "must stage the ORIGINAL tool output in CCR, and staging an "
        "already-compressed marker is unrecoverable"
    )


def test_first_frame_hook_sits_inside_a_guard_mentioning_response_create() -> None:
    """STRUCTURAL, and narrower than it may look.

    Proves only that the first-frame call is lexically inside an ``ast.If``
    whose test mentions ``"response.create"``. It does NOT prove the guard's
    *polarity* -- an inverted condition would satisfy it just as well. The claim
    that a non-create first frame really does skip the hook is behavioural, made
    by ``test_a_non_create_first_frame_does_not_reach_the_hook``.
    """
    tree = _module()
    call = _sole_first_frame_hook_call(tree)
    guards = [
        node
        for node in ast.walk(_ws_handler_function(tree))
        if isinstance(node, ast.If) and call in set(ast.walk(node))
    ]
    assert guards, f"the first-frame {HOOK} call is not inside any `if`"
    assert any(
        any(
            isinstance(const, ast.Constant) and const.value == "response.create"
            for const in ast.walk(guard.test)
        )
        for guard in guards
    ), f"the first-frame {HOOK} call must be guarded by the frame-type check"


def test_both_call_sites_pass_identical_keywords_including_the_one_store() -> None:
    """STRUCTURAL. Same orchestrator arguments, and the SAME revision store.

    The shared store is the whole point of this task: it is keyed by
    ``previous_response_id``, never by the per-socket ``session_id``
    (``uuid.uuid4().hex`` per accepted socket), so a boundary replayed as the
    first frame of a *new* connection is recognized as already claimed and comes
    back stale rather than dropped a second time. A second store object -- even
    one of the right type, even one also at module scope -- silently
    reintroduces the double drop, so this also asserts the store type is
    constructed exactly once in the whole module.

    **Does NOT prove** the store behaves correctly across a replay; that is
    ``test_a_reconnect_replay_is_stale_under_a_new_session_id`` in
    ``tests/test_jev_compaction_hook.py``.
    """
    tree = _module()
    relay_kwargs = {
        kw.arg: _normalized(kw.value) for kw in _sole_hook_call(_relay_function(tree)).keywords
    }
    first_frame_kwargs = {
        kw.arg: _normalized(kw.value) for kw in _sole_first_frame_hook_call(tree).keywords
    }
    assert first_frame_kwargs == relay_kwargs, (
        "the first-frame call site must hand the orchestrator exactly what the "
        f"relay-loop site does; it differs: {first_frame_kwargs} != {relay_kwargs}"
    )
    assert first_frame_kwargs.get("revisions") == STORE
    assert len(_sole_first_frame_hook_call(tree).args) == 1, (
        "the frame is the sole positional argument"
    )

    constructions = _calls_in(tree, STORE_TYPE)
    assert len(constructions) == 1, (
        f"{STORE_TYPE} is constructed at line(s) "
        f"{sorted(node.lineno for node in constructions)}: both WS paths must "
        "share the ONE process-wide store, or a replayed boundary is dropped twice"
    )


def test_first_frame_branch_never_reaches_the_shared_compression_executor() -> None:
    """STRUCTURAL. Same guarantee as the relay-site test, on the other path.

    ``_run_compression_in_executor``'s timeout path marks timeout debt and
    quarantines the shared pool, switching Headroom's own compression off for
    unrelated traffic. Checked over the innermost ``ast.If`` containing the
    first-frame hook call, with every ``_references_in`` node kind -- ``Name``,
    ``Attribute`` and ``Constant`` -- so an attribute lookup or the name spelled
    as a string for ``getattr`` fails it too.

    **Does NOT prove** anything about an alias bound outside that block, which
    no single-subtree AST check can close.
    """
    tree = _module()
    call = _sole_first_frame_hook_call(tree)
    guards = [
        node
        for node in ast.walk(_ws_handler_function(tree))
        if isinstance(node, ast.If) and call in set(ast.walk(node))
    ]
    assert guards, "the first-frame hook call must sit inside a guard (see the guard test)"
    jev_block = min(guards, key=lambda node: len(list(ast.walk(node))))

    offending = [ast.unparse(node) for node in _references_in(jev_block, EXECUTOR)]
    assert not offending, (
        f"the first-frame branch carrying the Jev hook references {EXECUTOR} "
        f"({offending}): its timeout path quarantines compression for every "
        "later frame, so no Jev step may reach it in any form"
    )


def test_neither_ws_path_retypes_the_dropped_reason_literal() -> None:
    """STRUCTURAL. The reason vocabulary is a contract, not a set of literals.

    ``test_relay_branches_on_the_imported_reason_constant`` covers the relay
    loop; this widens the same claim to the whole WS handler, which now includes
    the first-frame branch. Matched as constant *nodes*, so quote style is
    irrelevant.
    """
    tree = _module()
    handler = _ws_handler_function(tree)
    offending = [node.lineno for node in _references_in(handler, "jev_compaction_dropped")]
    assert not offending, (
        f"line(s) {sorted(offending)} re-type the literal reason string: both WS "
        "call sites must branch on the imported REASON_DROPPED constant"
    )


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_wiki_documents_the_codex_compaction_boundary() -> None:
    """DOCUMENTATION. The section exists and names the load-bearing facts.

    A keyword check, nothing more: it cannot tell whether the prose is *true*.
    The phrases chosen are the ones an operator would search for, plus the three
    honesty caveats an earlier task in this plan had to add after documentation
    overstated a guarantee -- the store is in-process, its eviction is bounded,
    and a failed Jev call is not retried.
    """
    wiki = WIKI_PROXY.read_text(encoding="utf-8")
    assert "## Jev Compaction Boundary (Codex WebSocket)" in wiki
    assert "HEADROOM_JEV_MODE=active" in wiki
    assert "headroom_retrieve" in wiki
    assert "fails open" in wiki
    for caveat in ("in-process", "evicted", "not retried"):
        assert caveat in wiki, (
            f"the section must state the {caveat!r} caveat rather than implying "
            "a guarantee the code does not make"
        )


# ---------------------------------------------------------------------------
# Behavioural: drive the real relay
# ---------------------------------------------------------------------------


ORIGINAL_OUTPUT = "original stdout body"


def _boundary_frame(*, output: str = ORIGINAL_OUTPUT) -> str:
    """A Codex native compaction boundary, in the shape Phase 0b observed.

    ``test_the_boundary_fixture_is_really_a_boundary`` checks that claim
    against the real detector rather than leaving it as prose.
    """
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
                        "output": output,
                    },
                    {"type": "compaction_trigger"},
                ],
            },
        }
    )


def test_the_boundary_fixture_is_really_a_boundary() -> None:
    """The fixture every behavioural test leans on is what it says it is.

    Without this, a fixture that quietly stopped matching Track C's detector
    would leave the behavioural tests passing while exercising an ordinary
    create frame -- proving the wiring on a frame Jev would never have acted on.
    """
    inner, wrapped = unwrap_response_create(json.loads(_boundary_frame()))
    assert inner is not None and wrapped
    boundary = detect_compaction_boundary(inner)
    assert boundary is not None, "the fixture no longer matches the real detector"
    assert boundary.previous_response_id == "resp_wiring_1"
    assert has_recovery_tool(inner), (
        "without the recovery tool the orchestrator declines every boundary, "
        "so the fixture would exercise a gate the wiring tests do not mean to hit"
    )


def _handshake_frame() -> str:
    return json.dumps({"type": "response.create", "response": {"model": "gpt-5.6-sol"}})


def _cancel_frame() -> str:
    return json.dumps({"type": "response.cancel"})


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
    through the real orchestrator at the real call site, and out to the fake
    upstream. What is asserted here is only the observable -- the bytes are
    unchanged. That the orchestrator gets there by short-circuiting on
    ``jev_compaction_disabled`` before it parses is Task 25's claim, checked in
    ``tests/test_jev_compaction_hook.py``, not re-checked here.
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
        # The handler itself, so the orchestrator can resolve a client after
        # its own mode check rather than the call site paying for it per frame.
        assert kwargs["proxy"] is not None
        assert hasattr(kwargs["proxy"], "config")
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

    # Two create frames cross this session -- the handshake on the first-frame
    # path and the boundary through the relay loop -- and BOTH now reach the
    # hook. `seen[1]` is the relay-loop one, which is what this test is about.
    assert len(seen) == 2, (
        "both create frames must reach the hook: the handshake on the "
        f"first-frame path and the boundary through the relay loop; saw {len(seen)}"
    )
    assert json.loads(seen[1])["response"].get("previous_response_id") == "resp_wiring_1", (
        "seen[1] must be the boundary relayed through _client_to_upstream"
    )
    inner = json.loads(seen[1])["response"]
    assert "store" not in inner, (
        "the hook must run BEFORE _maybe_compress_response_create_frame -- it saw "
        "a frame compression had already rewritten"
    )
    assert inner["input"][0]["output"] == ORIGINAL_OUTPUT, (
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

    assert len(seen) == 2, (
        "both create frames must reach the hook: the handshake on the "
        f"first-frame path and the boundary through the relay loop; saw {len(seen)}"
    )
    inner = json.loads(seen[1])["response"]
    assert inner.get("previous_response_id") == "resp_wiring_1", (
        "seen[1] must be the boundary relayed through _client_to_upstream"
    )
    tool_names = {t.get("name") for t in inner.get("tools", []) if isinstance(t, dict)}
    assert MEMORY_SENTINEL_TOOL in tool_names, (
        "the hook must run AFTER _prepare_memory_frame -- it saw a frame memory "
        "injection had not yet touched"
    )
    assert CCR_TOOL_NAME in tool_names, (
        "the client's own recovery tool must survive memory injection, or the "
        "orchestrator's recovery-tool gate would decline every boundary"
    )


@pytest.mark.asyncio
async def test_relay_skips_the_hook_for_non_create_frames() -> None:
    """BEHAVIOURAL. The guard's polarity, which the AST test cannot establish.

    ``test_hook_call_sits_inside_an_if_mentioning_response_create`` proves only
    that the call sits inside an ``if`` naming the constant -- an inverted test
    would satisfy it too. This drives three non-create frames through the real
    relay and shows the hook is not reached for any of them, while the two
    create frames in the same session do reach it. That is what "non-create
    frames must not pay for the hook" actually means.

    Scoped to the relay loop: the *first* frame here is a create frame, so this
    says nothing about a non-create FIRST frame. That is
    ``test_a_non_create_first_frame_does_not_reach_the_hook``.
    """
    seen: list[str] = []
    boundary = _boundary_frame()
    non_create = [
        _cancel_frame(),
        json.dumps({"type": "session.update", "session": {"model": "gpt-5.6-sol"}}),
        "this frame is not JSON at all",
    ]
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[_handshake_frame(), *non_create, boundary])
    handler = _DummyOpenAIHandler()

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(seen, boundary)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    # Five frames cross: the handshake (create, first-frame path), three
    # non-create frames, and the boundary (create, relay loop). Only the two
    # create frames may reach the hook.
    assert len(seen) == 2, (
        f"the hook must be reached only for response.create frames, but it saw "
        f"{len(seen)} of the {len(non_create) + 2} relayed frames"
    )
    assert [json.loads(frame)["type"] for frame in seen] == [
        "response.create",
        "response.create",
    ]
    # And the non-create frames still crossed, untouched.
    assert upstream.sent[1:4] == non_create


# ---------------------------------------------------------------------------
# Behavioural: the WS FIRST frame
# ---------------------------------------------------------------------------

HOOK_RETURNED_OUTPUT = "returned by the jev hook"
COMPRESSED_MARKER = "rewritten by headroom compression"


@pytest.mark.asyncio
async def test_default_off_relay_forwards_a_first_frame_boundary_byte_identical() -> None:
    """BEHAVIOURAL. Default off is still free on the path this task added.

    The boundary is the connection's FIRST frame -- the shape a reconnect
    replay produces -- and with no ``config.jev`` it must reach the upstream
    unchanged. The real orchestrator runs; nothing is mocked.

    **Does NOT prove** that no work was done before the short-circuit; that the
    orchestrator returns on ``jev_compaction_disabled`` before parsing is Task
    25's claim, checked in ``tests/test_jev_compaction_hook.py``.
    """
    boundary = _boundary_frame()
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[boundary])
    handler = _DummyOpenAIHandler()
    assert getattr(handler.config, "jev", None) is None

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == boundary, (
        "an unconfigured proxy must relay a first-frame boundary byte-identically"
    )


@pytest.mark.asyncio
async def test_a_first_frame_boundary_reaches_the_hook_after_memory_prep() -> None:
    """BEHAVIOURAL. The hole this task closes, driven end to end.

    Before this task a compaction boundary arriving as the FIRST frame of a
    socket -- exactly what a reconnect replay looks like -- never reached Track
    C at all. Here the boundary is frame 1, and the assertions are: the hook is
    reached once, it is handed the frame ``_prepare_memory_frame`` already
    rewrote (the sentinel tool proves that), it is handed the ORIGINAL tool
    output, it is handed the one process-wide revision store (checked inside
    ``_spy``), and the frame it RETURNS is what continues upstream.

    **Does NOT prove** the replay-is-stale behaviour itself -- that is
    ``test_a_reconnect_replay_is_stale_under_a_new_session_id`` in
    ``tests/test_jev_compaction_hook.py``. It proves only that the first frame
    now reaches the machinery that behaviour lives in.
    """
    seen: list[str] = []
    returned = _boundary_frame(output=HOOK_RETURNED_OUTPUT)
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[_boundary_frame()])
    handler = _DummyOpenAIHandler()
    handler.memory_handler = _JevMemoryHandler()

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(seen, returned)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(seen) == 1, (
        f"a boundary arriving as the first frame must reach the hook exactly "
        f"once, not {len(seen)} times"
    )
    inner = json.loads(seen[0])["response"]
    assert inner["input"][0]["output"] == ORIGINAL_OUTPUT, (
        "Jev must be shown the ORIGINAL tool output on the first frame too"
    )
    tool_names = {t.get("name") for t in inner.get("tools", []) if isinstance(t, dict)}
    assert MEMORY_SENTINEL_TOOL in tool_names, (
        "the first-frame hook must run AFTER _prepare_memory_frame -- it saw a "
        "frame memory injection had not yet touched"
    )
    assert CCR_TOOL_NAME in tool_names, (
        "the client's own recovery tool must survive memory injection, or the "
        "orchestrator's recovery-tool gate would decline every boundary"
    )
    assert json.loads(upstream.sent[0])["response"]["input"][0]["output"] == (
        HOOK_RETURNED_OUTPUT
    ), "the first frame forwarded upstream must be the one the hook RETURNS"


def _compression_spy(seen: list[dict[str, Any]]):
    """Stand in for first-frame compression, recording what it was handed."""

    async def _compress(inner: dict[str, Any], **_kwargs: Any) -> tuple[Any, ...]:
        seen.append(json.loads(json.dumps(inner)))
        rewritten = json.loads(json.dumps(inner))
        for item in rewritten.get("input") or []:
            if isinstance(item, dict) and "output" in item:
                item["output"] = COMPRESSED_MARKER
        return (rewritten, True, 9, [], None, 100, 40, 9, {})

    return _compress


@pytest.mark.asyncio
async def test_first_frame_hook_is_handed_the_frame_before_first_frame_compression() -> None:
    """BEHAVIOURAL. The first-frame ordering, proved from both directions.

    ``config.optimize`` is on and first-frame compression is replaced by a spy
    that rewrites every tool output to a marker. Three facts together pin the
    order: the hook saw the ORIGINAL output (so compression had not run yet),
    compression saw the hook's RETURNED output (so the hook had run, and its
    result is what flowed on), and the upstream got the compressed marker (so
    compression still ran afterwards and Jev did not displace it).

    This is the first-frame equivalent of
    ``test_relay_hands_the_hook_the_frame_before_compression_rewrites_it``. That
    test uses the ChatGPT ``store=false`` rewrite as its marker, which does not
    work here: on the first-frame path that rewrite happens well *before* the
    hook, not as part of compression.

    **Does NOT prove** anything about real compression behaviour -- the
    compressor is a spy. It proves only the relative order of the two steps.
    """
    hook_seen: list[str] = []
    compression_seen: list[dict[str, Any]] = []
    returned = _boundary_frame(output=HOOK_RETURNED_OUTPUT)
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[_boundary_frame()])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler._compress_openai_responses_payload_in_executor = _compression_spy(  # type: ignore[method-assign]
        compression_seen
    )

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(hook_seen, returned)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(hook_seen) == 1, f"the hook must see the first frame once, not {len(hook_seen)}"
    assert len(compression_seen) == 1, (
        f"first-frame compression must run once, not {len(compression_seen)}"
    )
    assert json.loads(hook_seen[0])["response"]["input"][0]["output"] == ORIGINAL_OUTPUT, (
        "the hook must run BEFORE first-frame compression -- it saw a frame "
        "compression had already rewritten, which would stage an unrecoverable "
        "marker in CCR as though it were the original tool output"
    )
    assert compression_seen[0]["input"][0]["output"] == HOOK_RETURNED_OUTPUT, (
        "first-frame compression must run on what the hook RETURNED, which is "
        "only possible if the hook ran first"
    )
    assert json.loads(upstream.sent[0])["response"]["input"][0]["output"] == COMPRESSED_MARKER, (
        "Headroom's own first-frame compression must still run after Jev: Jev "
        "is additive to deterministic compression, never a substitute for it"
    )


#: The three shapes the first-frame guard must reject, each named for what makes
#: it distinct. They are NOT interchangeable: the first-frame guard is
#: ``body.get("type") == "response.create" or ("type" not in body and "input" in
#: body)``, so ``session.update`` is rejected by the first disjunct, a cancel
#: frame by the first disjunct with a different type value, and a non-JSON
#: payload only because ``body`` stays ``{}`` and the SECOND disjunct's
#: ``"input" in body`` is false. Three different sub-expressions, so three cases.
NON_CREATE_FIRST_FRAMES = [
    pytest.param(
        json.dumps({"type": "session.update", "session": {"model": "gpt-5.6-sol"}}),
        id="session_update",
    ),
    pytest.param(_cancel_frame(), id="response_cancel"),
    pytest.param("this frame is not JSON at all", id="not_json"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", NON_CREATE_FIRST_FRAMES)
async def test_a_non_create_first_frame_does_not_reach_the_hook(first: str) -> None:
    """BEHAVIOURAL. The FIRST-frame guard's polarity, for all three shapes.

    ``test_first_frame_hook_sits_inside_a_guard_mentioning_response_create``
    proves only that the call is inside an ``if`` naming the constant -- an
    inverted condition satisfies that too.

    Deliberately parametrised rather than leaning on
    ``test_relay_skips_the_hook_for_non_create_frames``, which drives the same
    three shapes through the OTHER call site. The two guards are different
    expressions (see :data:`NON_CREATE_FIRST_FRAMES`), so a first-frame guard
    that wrongly accepted a cancel frame, or a non-JSON payload, while still
    rejecting ``session.update`` would have satisfied the earlier single-shape
    version of this test with the file's coverage claim still reading as met.

    Each case asserts both halves: the frame is forwarded to the upstream
    untouched, AND the hook was not reached for it -- while the
    ``response.create`` that follows on the same socket still is, so a guard
    that simply never fires cannot pass.

    **Does NOT prove** anything about frames 2..N; that is the relay-loop test.
    """
    seen: list[str] = []
    boundary = _boundary_frame()
    upstream = _FakeUpstream(_upstream_events())
    client_ws = _FakeWebSocket(frames=[first, boundary])
    handler = _DummyOpenAIHandler()

    with (
        patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}),
        patch.object(openai_module, HOOK, _spy(seen, boundary)),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(seen) == 1, (
        f"the non-create first frame {first!r} must not reach the hook; only the "
        f"response.create that followed it may, but the hook saw {len(seen)} frames"
    )
    assert json.loads(seen[0])["type"] == "response.create", (
        "the one frame that reached the hook must be the create frame, not the "
        "non-create first frame"
    )
    assert upstream.sent[0] == first, (
        "the non-create first frame must cross to the upstream untouched"
    )
