"""Phase 0a research spike: does Jev buy real savings on top of Headroom?

ONE-OFF RESEARCH SPIKE -- NOT PRODUCTION CODE.

This script exists to answer a single go/no-go question from
``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`` (Phase 0a):
after Headroom's own deterministic compression has already run, does asking Jev
(TypeSafe's System One retention-decision API) which historical tool results to
keep / truncate / drop produce *incremental* token savings worth wiring into the
proxy at all.

It is deliberately standalone:

- It is **not** registered in ``run_benchmarks.py``. It is not a suite member.
- It **never** touches the running proxy and never mutates a forwarded request.
  Decisions are applied to a private in-memory copy of the message list and
  then thrown away.
- It does not write to the machine's real Headroom state. Running Headroom's
  compression pipeline is *not* side-effect free -- SmartCrusher mirrors CCR
  entries into the compression store (SQLite at
  ``$HEADROOM_WORKSPACE_DIR/ccr_store.db``, the very file a running proxy on
  this machine uses) and records into TOIN's filesystem learning store. Both
  resolve their paths from the environment at call time, so this module
  redirects ``HEADROOM_WORKSPACE_DIR`` to a throwaway temp directory and forces
  ``HEADROOM_CCR_BACKEND=memory`` *before* importing ``headroom``. That
  redirect is what makes the no-shared-state claim true; see
  ``_isolate_headroom_state()`` below.
- It makes **no** provider (Anthropic / OpenAI) model calls. The only network
  call it makes is to the Jev endpoint.

**WARNING: running this makes REAL, BILLED calls to the Jev API**, one per
scenario, using ``HEADROOM_JEV_API_KEY`` from the environment. Keep ``--limit``
small. The API key is read from the environment and is never printed or logged,
and the endpoint URL is printed origin+path only (a custom endpoint may carry
credentials in its userinfo or query string).

Usage::

    uv run python benchmarks/jev_savings_spike.py --limit 1 --timeout-ms 20000
    uv run python benchmarks/jev_savings_spike.py --limit 5 --timeout-ms 30000

The timeout defaults to ``HEADROOM_JEV_TIMEOUT_MS`` (500ms if unset, matching
the production default); a spike wants a far more generous ``--timeout-ms``.

HTTP contract
-------------
Taken from https://docs.typesafe.ai/introduction/quickstart -- System One is a
*structured question answering* API, not a bespoke retention API. So the
retention view goes in ``state`` and each candidate becomes one ``choice``
question whose criteria are keep / truncate / drop::

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <key>
    {"state": {...retention view...}, "model": "jev-latest",
     "questions": {"cand_0000": {"type": "choice",
                                 "instructions": "...",
                                 "criteria": {"keep": "...", "truncate": "...",
                                              "drop": "..."}}}}

    -> {"model": "jev-1.x.y",
        "answers": {"cand_0000": {"type": "choice", "choice": "drop",
                                  "confidence": 0.78, "probabilities": {...}}},
        "usage": {"input_tokens": 392, "output_tokens": 65}}

Every field name in that contract is overridable from the CLI
(``--state-field``, ``--questions-field``, ``--answers-field``,
``--decision-field``, ``--auth-header``, ``--auth-scheme``) so the shape can be
corrected against the real docs without editing this file. Anything the parser
cannot understand falls back to ``keep`` -- this spike never guess-mutates on
ambiguous output.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _isolate_headroom_state() -> str:
    """Point every Headroom write at a throwaway directory. MUST run first.

    ``compress()`` is not side-effect free. ``CompressConfig`` exposes no
    lossless / no-CCR switch, and the content router enables CCR markers by
    default (``ContentRouterConfig.ccr_inject_marker=True``), so a SmartCrusher
    path can mirror original content into the shared compression store --
    SQLite at ``workspace_dir()/ccr_store.db`` by default, which is exactly the
    file a real proxy running on this machine uses. TOIN's learning store is
    written the same way.

    Both resolve from the environment on every call (``headroom.paths`` caches
    nothing), so redirecting the canonical workspace root here -- before
    ``headroom`` is imported -- contains every write to a temp directory that is
    deleted on exit. ``HEADROOM_CONFIG_DIR`` is pinned to the real (read-mostly)
    config root so the redirect does not also hide the user's model catalog,
    and the per-resource legacy overrides are dropped because they would take
    precedence over the workspace root.
    """
    workspace = tempfile.mkdtemp(prefix="headroom-jev-spike-")
    os.environ.setdefault("HEADROOM_CONFIG_DIR", str(Path.home() / ".headroom" / "config"))
    os.environ["HEADROOM_WORKSPACE_DIR"] = workspace
    # Forced, not setdefault: an inherited redis/sqlite backend would write to
    # shared state no matter where the workspace points.
    os.environ["HEADROOM_CCR_BACKEND"] = "memory"
    for leaked in (
        "HEADROOM_CCR_SQLITE_PATH",
        "HEADROOM_TOIN_PATH",
        "HEADROOM_TOIN_BACKEND",
        "HEADROOM_SAVINGS_PATH",
        "HEADROOM_SAVINGS_EVENTS_PATH",
    ):
        os.environ.pop(leaked, None)
    atexit.register(shutil.rmtree, workspace, True)
    return workspace


SPIKE_WORKSPACE = _isolate_headroom_state()

from scenarios.conversations import (  # noqa: E402
    generate_agentic_conversation,
    generate_anthropic_agentic_conversation,
    generate_rag_conversation,
)

from headroom import CompressConfig, compress  # noqa: E402
from headroom.providers.openai_compatible import OpenAICompatibleTokenCounter  # noqa: E402

# --- Fixed spike parameters -------------------------------------------------

#: Target model the corpus is being compressed *for*. Only used for tokenizing
#: and for the provider/model identifiers in the retention view -- no model call.
TARGET_MODEL = "gpt-5.6"
TARGET_PROVIDER = "openai"

#: Corpus seed, so two runs compare like for like. Near-deterministic, not
#: exactly: the generators mint tool-call ids with ``uuid.uuid4()``, which is not
#: seeded, so token counts wobble by a few tokens between runs.
DEFAULT_SEED = 1337

#: Recent-tail exclusion. Nothing in the last N messages of a scenario is ever a
#: candidate. Headroom has its own ``protect_recent`` router guard, but it is a
#: compressor knob with a different default (4), not a retention-eligibility
#: rule, so the 6-message exclusion is implemented directly here as the design
#: asks. No broader protected-range mechanism is invented.
RECENT_TAIL_EXCLUSION = 6

#: What a ``truncate`` decision actually does to candidate content, in chars.
TRUNCATE_CHARS = 400

DECISIONS = ("keep", "truncate", "drop")

DECISION_CRITERIA = {
    "keep": (
        "This tool result still carries information the assistant is likely to "
        "need again later in the conversation; removing or shortening it would "
        "lose facts that are not recoverable from the surrounding messages."
    ),
    "truncate": (
        "Only the beginning / shape of this tool result matters from here on "
        "(a header, a count, the first few rows). The bulk of its body is "
        "redundant detail that can be cut without losing the thread."
    ),
    "drop": (
        "This tool result has been fully superseded, summarized by a later "
        "assistant message, or is simply no longer referenced. Removing it "
        "entirely would not change what the assistant can answer."
    ),
}

QUESTION_INSTRUCTIONS = (
    "Decide what to do with the historical tool result identified by this "
    "question's key ({cid}) in the `candidates` array of the state. It is "
    "message #{idx} of {total}, {from_end} messages from the end of the "
    "conversation, and costs about {tokens} tokens. Choose `keep` only if the "
    "content is genuinely still needed."
)


# --- Scenario corpus --------------------------------------------------------


@dataclass
class Scenario:
    """One corpus entry plus the cache-frozen prefix it declares."""

    name: str
    messages: list[dict[str, Any]]
    #: Number of leading messages this scenario declares cache-frozen. Passed to
    #: Headroom as ``frozen_message_count`` (the repo's existing protected-prefix
    #: concept) and excluded from candidate selection.
    frozen_prefix: int
    shape: str  # "openai" | "anthropic"


def build_corpus(seed: int) -> list[Scenario]:
    """Build the deterministic scenario corpus from the existing generators."""
    scenarios: list[Scenario] = []

    def _seeded(fn, *args, **kwargs):
        random.seed(seed)
        return fn(*args, **kwargs)

    scenarios.append(
        Scenario(
            name="agentic_12turns_2calls",
            messages=_seeded(generate_agentic_conversation, 12, 2, 40),
            frozen_prefix=1,
            shape="openai",
        )
    )
    scenarios.append(
        Scenario(
            name="agentic_30turns_1call",
            messages=_seeded(generate_agentic_conversation, 30, 1, 60),
            frozen_prefix=1,
            shape="openai",
        )
    )
    scenarios.append(
        Scenario(
            name="agentic_8turns_heavy_results",
            messages=_seeded(generate_agentic_conversation, 8, 3, 150),
            frozen_prefix=1,
            shape="openai",
        )
    )
    scenarios.append(
        Scenario(
            name="anthropic_agentic_10turns",
            messages=_seeded(generate_anthropic_agentic_conversation, 10, 2, 40),
            frozen_prefix=1,
            shape="anthropic",
        )
    )
    scenarios.append(
        Scenario(
            name="agentic_20turns_mixed",
            messages=_seeded(generate_agentic_conversation, 20, 2, 25),
            frozen_prefix=1,
            shape="openai",
        )
    )
    # Deliberately last: a RAG conversation has no tool results at all, so it
    # exercises the zero-candidate path (no Jev call, no spend).
    scenarios.append(
        Scenario(
            name="rag_20k_context",
            messages=_seeded(generate_rag_conversation, 20000, 5),
            frozen_prefix=2,
            shape="openai",
        )
    )
    return scenarios


# --- Token accounting -------------------------------------------------------


def count_tokens(tok: OpenAICompatibleTokenCounter, messages: list[dict[str, Any]]) -> int:
    """Token count for a message list, defensively handling block content.

    ``OpenAICompatibleTokenCounter.count_message`` only ever reads a message's
    ``content``; an OpenAI Responses ``function_call_output`` item carries its
    payload in ``output`` instead, so the counter prices it at ~0. That is the
    exact field ``select_candidates`` / ``apply_decisions`` operate on, so
    without the correction below a drop/truncate of such a candidate would move
    real tokens while TH and TP both stayed put -- savings silently reported as
    zero. Added here rather than patched into the provider: this is a spike.
    """
    try:
        total = int(tok.count_messages(messages))
    except Exception:
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += tok.count_text(content)
            elif content is not None:
                total += tok.count_text(json.dumps(content, default=str))

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") != "function_call_output":
            continue
        output = msg.get("output")
        if output is not None:
            total += tok.count_text(_text_of(output))
    return total


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


# --- Candidate selection ----------------------------------------------------


@dataclass
class Candidate:
    """An eligible historical tool result in the post-compression message list."""

    cid: str
    message_index: int
    block_index: int | None
    candidate_type: str
    role: str
    tool_call_id: str | None
    content: str
    est_tokens: int

    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8", "replace")).hexdigest()


def select_candidates(
    messages: list[dict[str, Any]],
    frozen_prefix: int,
    tok: OpenAICompatibleTokenCounter,
    max_candidates: int,
) -> list[Candidate]:
    """Eligible tool-result candidates from the post-Headroom message list.

    Eligible means: a tool_result / function_call_output (or provider
    equivalent), outside the last ``RECENT_TAIL_EXCLUSION`` messages, and
    outside the scenario's declared cache-frozen prefix.
    """
    total = len(messages)
    tail_start = total - RECENT_TAIL_EXCLUSION
    out: list[Candidate] = []

    for idx, msg in enumerate(messages):
        if idx < frozen_prefix or idx >= tail_start:
            continue
        if not isinstance(msg, dict):
            continue

        role = str(msg.get("role") or "")

        # OpenAI Chat Completions: a whole message with role="tool".
        if role == "tool" and msg.get("content") is not None:
            text = _text_of(msg["content"])
            out.append(
                Candidate(
                    cid="",
                    message_index=idx,
                    block_index=None,
                    candidate_type="tool_result",
                    role=role,
                    tool_call_id=msg.get("tool_call_id"),
                    content=text,
                    est_tokens=tok.count_text(text),
                )
            )
            continue

        # OpenAI Responses: a bare {"type": "function_call_output", ...} item.
        if msg.get("type") == "function_call_output":
            text = _text_of(msg.get("output", ""))
            out.append(
                Candidate(
                    cid="",
                    message_index=idx,
                    block_index=None,
                    candidate_type="function_call_output",
                    role=role or "tool",
                    tool_call_id=msg.get("call_id") or msg.get("id"),
                    content=text,
                    est_tokens=tok.count_text(text),
                )
            )
            continue

        # Anthropic: tool_result blocks inside a user message's content list.
        content = msg.get("content")
        if isinstance(content, list):
            for bidx, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _text_of(block.get("content", ""))
                out.append(
                    Candidate(
                        cid="",
                        message_index=idx,
                        block_index=bidx,
                        candidate_type="tool_result",
                        role=role or "user",
                        tool_call_id=block.get("tool_use_id"),
                        content=text,
                        est_tokens=tok.count_text(text),
                    )
                )

    # Bound the request: keep the oldest N (the ones most likely to be stale).
    out = out[:max_candidates]
    for i, cand in enumerate(out):
        cand.cid = f"cand_{i:04d}"
    return out


#: Floor for the per-candidate content bound. Below this a candidate's view is
#: too thin for a retention decision to mean anything.
MIN_VIEW_TOKENS = 256


def enforce_state_budget(
    candidates: list[Candidate],
    tok: OpenAICompatibleTokenCounter,
    make_payload: Any,
    max_candidate_tokens: int,
    max_state_tokens: int,
) -> tuple[list[Candidate], int, int]:
    """Fit the request inside Jev's input limit, measured not estimated.

    Jev rejects an oversized request outright with
    ``{"detail": {"error_type": "max_tokens_exceeded"}}`` -- the whole call is
    lost, not just the overflow. ``HEADROOM_JEV_MAX_CANDIDATE_TOKENS`` bounds
    each candidate individually and says nothing about the total, so this is the
    second, total bound. Trimming here is honest: the trimmed candidates are
    still reported as candidates and counted as ``keep``.

    The bound is measured on the **actual serialized request**, not on candidate
    content alone: the candidate metadata, the per-question instructions and the
    three criteria descriptions are a large fraction of the payload, and a fixed
    per-candidate overhead constant underestimated them by ~50%. Nor is the
    first candidate exempt -- one oversized candidate on its own is exactly the
    request Jev would reject.

    Two knobs, applied in order:

    1. The per-candidate *view* bound. ``HEADROOM_JEV_MAX_CANDIDATE_TOKENS``
       (20k) is far larger than the whole request budget (8k), so at full size a
       single tool result eats the entire request and Jev is asked about one
       candidate -- which is not a retention question at all. So the view bound
       drops to an equal share of the budget (floored at ``MIN_VIEW_TOKENS``)
       and every candidate is shown a truncated head. The state marks each one
       with ``content_truncated_for_view`` and still reports its true
       ``estimated_tokens``, so neither Jev nor the human reading the report is
       misled about what was shown. This is a real limitation of the spike:
       drop/truncate decisions are made on a preview, not the full body.
    2. Trailing candidates are then dropped until the serialized payload
       actually fits. Trimming is honest: a trimmed candidate still counts in
       the reported candidate total and is left untouched in the projection
       (i.e. effectively kept), so TP never claims savings on a candidate Jev
       was never asked about.

    Returns ``(kept, per_candidate_view_tokens, serialized_tokens)``.
    """
    share = max_state_tokens // max(1, len(candidates))
    view_bound = max(MIN_VIEW_TOKENS, min(max_candidate_tokens, share))

    def _size(cands: list[Candidate]) -> int:
        return tok.count_text(json.dumps(make_payload(cands, view_bound), default=str))

    # Price each candidate by what it actually adds to the serialized payload.
    # One tokenization pass per candidate; the skeleton is measured once.
    base = _size([])
    kept: list[Candidate] = []
    total = base
    for cand in candidates:
        delta = _size([cand]) - base
        if total + delta > max_state_tokens:
            break
        kept.append(cand)
        total += delta

    # Deltas miss a handful of separator tokens, so settle on the exact size of
    # the payload actually being sent. Cheap -- it is <= max_state_tokens by
    # construction -- and it makes the bound a measurement, not an estimate.
    while kept:
        exact = _size(kept)
        if exact <= max_state_tokens:
            return kept, view_bound, exact
        kept.pop()
    return [], view_bound, base


# --- Retention view ---------------------------------------------------------


def build_retention_view(
    scenario: Scenario,
    messages: list[dict[str, Any]],
    candidates: list[Candidate],
    max_candidate_tokens: int,
    jev_model: str,
) -> dict[str, Any]:
    """The bounded retention view sent as the Jev ``state``."""
    # ~4 chars per token is the same approximation the corpus generators use.
    max_chars = max(1, max_candidate_tokens) * 4

    canonical = json.dumps(
        [
            [c.cid, c.message_index, c.block_index, c.candidate_type, c.content_sha256()]
            for c in candidates
        ],
        sort_keys=True,
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    session_id = hashlib.sha256(f"headroom-jev-spike:{scenario.name}".encode()).hexdigest()[:32]

    total = len(messages)
    return {
        "provider": TARGET_PROVIDER,
        "model": TARGET_MODEL,
        "jev_model": jev_model,
        "session_id": session_id,
        "branch_id": "spike",
        "revision": revision,
        "message_shape": scenario.shape,
        "total_messages": total,
        "protected_prefix_messages": scenario.frozen_prefix,
        "recent_tail_excluded_messages": RECENT_TAIL_EXCLUSION,
        "task": (
            "These are historical tool results from an agent conversation that "
            "has already been deterministically compressed. Decide, per "
            "candidate, whether its content must still be kept verbatim, can be "
            "truncated to its first lines, or can be dropped entirely."
        ),
        "candidates": [
            {
                "candidate_id": c.cid,
                "candidate_type": c.candidate_type,
                "role": c.role,
                "tool_call_id": c.tool_call_id,
                "message_index": c.message_index,
                "block_index": c.block_index,
                "order_from_end": total - c.message_index,
                "estimated_tokens": c.est_tokens,
                "content_bytes": len(c.content.encode("utf-8", "replace")),
                "content_sha256": c.content_sha256(),
                "content_truncated_for_view": len(c.content) > max_chars,
                "content": c.content[:max_chars],
            }
            for c in candidates
        ],
    }


def build_questions(candidates: list[Candidate], total_messages: int) -> dict[str, dict[str, Any]]:
    return {
        c.cid: {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS.format(
                cid=c.cid,
                idx=c.message_index,
                total=total_messages,
                from_end=total_messages - c.message_index,
                tokens=c.est_tokens,
            ),
            "criteria": dict(DECISION_CRITERIA),
        }
        for c in candidates
    }


def build_payload(
    scenario: Scenario,
    messages: list[dict[str, Any]],
    candidates: list[Candidate],
    args: argparse.Namespace,
    max_candidate_tokens: int,
) -> dict[str, Any]:
    """The full Jev request body. Single source of truth for size + send."""
    view = build_retention_view(
        scenario, messages, candidates, max_candidate_tokens, args.jev_model
    )
    return {
        args.state_field: view if args.state_format == "json" else json.dumps(view, default=str),
        "model": args.jev_model,
        args.questions_field: build_questions(candidates, len(messages)),
    }


# --- Jev call ---------------------------------------------------------------


def redact_url(url: str) -> str:
    """Scheme + host + path only.

    The endpoint is printed at startup and can appear inside httpx error
    strings. A custom ``--endpoint`` / ``HEADROOM_JEV_ENDPOINT`` may carry
    credentials in its userinfo or an API token in its query string, so neither
    is ever shown. The key itself only ever travels in a header.
    """
    if not url:
        return "<unset>"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable endpoint>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
        host = f"<redacted>@{host}"
    shown = urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    if parts.query:
        shown += "?<redacted>"
    return shown or "<redacted endpoint>"


def _scrub(text: str, endpoint: str) -> str:
    """Replace any verbatim endpoint echoed back by httpx or the server."""
    return text.replace(endpoint, redact_url(endpoint)) if endpoint else text


@dataclass
class JevOutcome:
    decisions: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str | None = None
    jev_model: str | None = None
    usage: dict[str, Any] | None = None


def call_jev(
    payload: dict[str, Any],
    args: argparse.Namespace,
    api_key: str,
    candidates: list[Candidate],
) -> JevOutcome:
    """One bounded Jev call. Never raises; unparseable answers fall back to keep."""
    outcome = JevOutcome(decisions={c.cid: "keep" for c in candidates})
    headers = {
        args.auth_header: f"{args.auth_scheme} {api_key}".strip(),
        "Content-Type": "application/json",
    }

    started = time.perf_counter()
    try:
        response = httpx.post(
            args.endpoint,
            json=payload,
            headers=headers,
            timeout=args.timeout_ms / 1000.0,
        )
    except Exception as exc:  # timeout, DNS, TLS, connection reset
        outcome.latency_ms = (time.perf_counter() - started) * 1000.0
        # httpx error strings routinely embed the request URL.
        outcome.error = _scrub(f"{type(exc).__name__}: {exc}", args.endpoint)
        return outcome
    outcome.latency_ms = (time.perf_counter() - started) * 1000.0

    if response.status_code >= 400:
        outcome.error = _scrub(f"HTTP {response.status_code}: {response.text[:400]}", args.endpoint)
        return outcome

    try:
        body = response.json()
    except Exception as exc:
        outcome.error = f"non-JSON response: {type(exc).__name__}: {exc}"
        return outcome

    # Log the raw response *shape* only -- stdout, never persisted, no content.
    print(
        f"    [shape] top-level keys: {sorted(body)[:12] if isinstance(body, dict) else type(body).__name__}"
    )

    if not isinstance(body, dict):
        outcome.error = f"unexpected response type: {type(body).__name__}"
        return outcome

    outcome.jev_model = body.get("model")
    if isinstance(body.get("usage"), dict):
        outcome.usage = body["usage"]

    answers = body.get(args.answers_field)
    if not isinstance(answers, dict):
        outcome.error = (
            f"no dict at '{args.answers_field}' (keys were {sorted(body)[:12]}); "
            "falling back to keep for every candidate"
        )
        return outcome

    sample = next(iter(answers.values()), None)
    if isinstance(sample, dict):
        print(f"    [shape] answer keys: {sorted(sample)[:12]}")

    unparsed = 0
    for cand in candidates:
        answer = answers.get(cand.cid)
        decision = None
        if isinstance(answer, dict):
            raw = answer.get(args.decision_field)
            if isinstance(raw, str) and raw.strip().lower() in DECISIONS:
                decision = raw.strip().lower()
        elif isinstance(answer, str) and answer.strip().lower() in DECISIONS:
            decision = answer.strip().lower()
        if decision is None:
            unparsed += 1
            decision = "keep"  # never guess-mutate on ambiguous output
        outcome.decisions[cand.cid] = decision

    if unparsed:
        print(f"    [shape] {unparsed}/{len(candidates)} answers unparseable -> forced keep")
    return outcome


# --- Applying decisions (private in-memory copy only) -----------------------


def apply_decisions(
    messages: list[dict[str, Any]],
    candidates: list[Candidate],
    decisions: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply keep/truncate/drop to a DEEP COPY. Nothing here is ever forwarded."""
    projected = json.loads(json.dumps(messages, default=str))

    drop_messages: set[int] = set()
    drop_blocks: set[tuple[int, int]] = set()

    for cand in candidates:
        decision = decisions.get(cand.cid, "keep")
        if decision == "keep":
            continue

        if cand.block_index is None:
            msg = projected[cand.message_index]
            if decision == "drop":
                drop_messages.add(cand.message_index)
            elif decision == "truncate":
                key = "output" if cand.candidate_type == "function_call_output" else "content"
                original = _text_of(msg.get(key, ""))
                msg[key] = original[:TRUNCATE_CHARS] + (
                    "\n…[truncated by Jev retention decision]"
                    if len(original) > TRUNCATE_CHARS
                    else ""
                )
        else:
            block = projected[cand.message_index]["content"][cand.block_index]
            if decision == "drop":
                drop_blocks.add((cand.message_index, cand.block_index))
            elif decision == "truncate":
                original = _text_of(block.get("content", ""))
                block["content"] = original[:TRUNCATE_CHARS] + (
                    "\n…[truncated by Jev retention decision]"
                    if len(original) > TRUNCATE_CHARS
                    else ""
                )

    # Remove dropped blocks first (indices shift once the message list changes).
    for midx in sorted({m for m, _ in drop_blocks}):
        keep_blocks = [
            b
            for bidx, b in enumerate(projected[midx]["content"])
            if (midx, bidx) not in drop_blocks
        ]
        if keep_blocks:
            projected[midx]["content"] = keep_blocks
        else:
            drop_messages.add(midx)

    return [m for i, m in enumerate(projected) if i not in drop_messages]


# --- Report -----------------------------------------------------------------


def run_scenario(
    scenario: Scenario,
    tok: OpenAICompatibleTokenCounter,
    args: argparse.Namespace,
    api_key: str,
) -> dict[str, Any]:
    print(
        f"\n[{scenario.name}] {len(scenario.messages)} raw messages, "
        f"frozen_prefix={scenario.frozen_prefix}"
    )

    # 1. Headroom's existing compression path (same pattern as index_proof_table.py).
    result = compress(
        scenario.messages,
        model=TARGET_MODEL,
        config=CompressConfig(frozen_message_count=scenario.frozen_prefix),
    )
    compressed = result.messages
    th = count_tokens(tok, compressed)
    print(
        f"    T0={result.tokens_before:,} -> TH={th:,} ({len(compressed)} messages after Headroom)"
    )

    # 2. Eligible candidates from the post-compression list.
    eligible = select_candidates(compressed, scenario.frozen_prefix, tok, args.max_candidates)

    def make_payload(cands: list[Candidate], view_tokens: int) -> dict[str, Any]:
        return build_payload(scenario, compressed, cands, args, view_tokens)

    candidates, view_bound, state_tokens = enforce_state_budget(
        eligible, tok, make_payload, args.max_candidate_tokens, args.max_state_tokens
    )
    if len(candidates) < len(eligible):
        print(
            f"    state budget ({args.max_state_tokens:,} tok) trims "
            f"{len(eligible)} eligible candidates to {len(candidates)} sent"
        )
    if eligible and view_bound < args.max_candidate_tokens:
        print(
            f"    per-candidate view bounded to {view_bound:,} tok "
            f"(of {args.max_candidate_tokens:,}) to fit the state budget -- "
            "decisions are made on a truncated head, not the full body"
        )
    row: dict[str, Any] = {
        "scenario": scenario.name,
        "messages": len(compressed),
        "candidates": len(eligible),
        "candidates_sent": len(candidates),
        "state_tokens": state_tokens if candidates else 0,
        "view_tokens_per_candidate": view_bound,
        "keep": 0,
        "truncate": 0,
        "drop": 0,
        "TH": th,
        "TP": th,
        "saved": 0,
        "saved_pct": 0.0,
        "latency_ms": 0.0,
        "all_keep": False,
        "jev_error": None,
    }

    if not candidates:
        if eligible:
            print(
                f"    every one of {len(eligible)} eligible candidates is too large for the "
                f"{args.max_state_tokens:,}-token state budget -- skipping Jev call (no spend)"
            )
        else:
            print("    no eligible candidates -- skipping Jev call (no spend)")
        return row

    # 3. Bounded retention view + one Jev call.
    payload = make_payload(candidates, view_bound)
    print(
        f"    sending {len(candidates)} candidates, serialized request "
        f"{state_tokens:,} tok (bound {args.max_state_tokens:,}) "
        f"-> POST {redact_url(args.endpoint)}"
    )

    outcome = call_jev(payload, args, api_key, candidates)
    row["latency_ms"] = round(outcome.latency_ms, 1)
    row["jev_error"] = outcome.error
    row["jev_model"] = outcome.jev_model
    row["jev_usage"] = outcome.usage
    if outcome.error:
        print(f"    [error] {outcome.error}")

    for decision in outcome.decisions.values():
        row[decision] = row.get(decision, 0) + 1

    # 4. Apply to a private in-memory copy and re-count.
    projected = apply_decisions(compressed, candidates, outcome.decisions)
    tp = count_tokens(tok, projected)
    row["TP"] = tp
    row["saved"] = th - tp
    row["saved_pct"] = round((th - tp) / th * 100, 2) if th else 0.0
    row["all_keep"] = row["keep"] == len(candidates)

    if row["all_keep"] and outcome.error is None:
        print(
            "    [FLAG] every candidate came back 'keep' with no API error -- "
            "possible 'metadata-keep-v2 always keeps unseen results' bias, "
            "NOT necessarily a real retention signal"
        )
    return row


def main() -> int:
    # 500ms is the production default the design spec names; the env var wins
    # when set. A spike run almost always wants --timeout-ms well above it.
    env_timeout = os.environ.get("HEADROOM_JEV_TIMEOUT_MS")
    try:
        default_timeout = int(env_timeout) if env_timeout else 500
    except ValueError:
        default_timeout = 500

    env_max_tokens = os.environ.get("HEADROOM_JEV_MAX_CANDIDATE_TOKENS")
    try:
        default_max_tokens = int(env_max_tokens) if env_max_tokens else 20000
    except ValueError:
        default_max_tokens = 20000

    ap = argparse.ArgumentParser(
        description="Phase 0a spike: incremental Jev savings on top of Headroom. "
        "Makes REAL billed Jev API calls.",
    )
    ap.add_argument("--limit", type=int, default=5, help="max scenarios to process (default 5)")
    ap.add_argument(
        "--timeout-ms",
        type=int,
        default=default_timeout,
        help=f"per-call timeout (default {default_timeout}, from HEADROOM_JEV_TIMEOUT_MS or 500)",
    )
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument(
        "--max-candidates",
        type=int,
        default=12,
        help="max candidates (=questions) per Jev call (default 12)",
    )
    ap.add_argument(
        "--max-state-tokens",
        type=int,
        default=8000,
        help="total request bound; candidates past it are trimmed rather than "
        "letting Jev reject the whole call with max_tokens_exceeded (default 8000)",
    )
    ap.add_argument(
        "--max-candidate-tokens",
        type=int,
        default=default_max_tokens,
        help="per-candidate content bound, in tokens "
        f"(default {default_max_tokens}, from HEADROOM_JEV_MAX_CANDIDATE_TOKENS or 20000)",
    )
    # --- HTTP contract, explicit and overridable ---
    ap.add_argument(
        "--endpoint",
        default=os.environ.get("HEADROOM_JEV_ENDPOINT", "https://api.typesafe.ai/v1/systemone"),
    )
    ap.add_argument("--jev-model", default=os.environ.get("HEADROOM_JEV_MODEL", "jev-latest"))
    ap.add_argument("--state-field", default="state")
    ap.add_argument("--state-format", choices=("json", "text"), default="json")
    ap.add_argument("--questions-field", default="questions")
    ap.add_argument("--answers-field", default="answers")
    ap.add_argument("--decision-field", default="choice")
    ap.add_argument("--auth-header", default="Authorization")
    ap.add_argument("--auth-scheme", default="Bearer")
    args = ap.parse_args()

    api_key = os.environ.get("HEADROOM_JEV_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: HEADROOM_JEV_API_KEY is not set. This spike only measures "
            "anything by calling the real Jev API; refusing to run.",
            file=sys.stderr,
        )
        return 2

    print("Jev savings spike (Phase 0a) -- ONE-OFF RESEARCH SCRIPT, REAL BILLED JEV CALLS")
    # Endpoint is redacted to origin+path: a custom endpoint may embed
    # credentials or a query-string token.
    print(
        f"endpoint={redact_url(args.endpoint)}  jev_model={args.jev_model}  "
        f"timeout={args.timeout_ms}ms  target_model={TARGET_MODEL}"
    )
    print(
        f"seed={args.seed}  limit={args.limit}  max_candidates={args.max_candidates}  "
        f"max_candidate_tokens={args.max_candidate_tokens}  "
        f"max_state_tokens={args.max_state_tokens}  "
        f"recent_tail_excluded={RECENT_TAIL_EXCLUSION}"
    )
    print(
        f"headroom state isolated to {SPIKE_WORKSPACE} (deleted on exit); "
        "CCR backend forced to memory -- nothing touches ~/.headroom"
    )

    tok = OpenAICompatibleTokenCounter(model=TARGET_MODEL)
    corpus = build_corpus(args.seed)[: max(0, args.limit)]

    rows = [run_scenario(s, tok, args, api_key) for s in corpus]

    print("\n=== Per-scenario report ===")
    header = (
        f"{'scenario':<30} {'cand':>5} {'sent':>5} {'keep':>5} {'trunc':>6} {'drop':>5} "
        f"{'TH':>9} {'TP':>9} {'saved':>8} {'saved%':>7} {'ms':>8}  flags"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        flags = []
        if r["all_keep"]:
            flags.append("ALL-KEEP(metadata-keep-v2 bias?)")
        if r["jev_error"]:
            flags.append("JEV-ERROR")
        print(
            f"{r['scenario']:<30} {r['candidates']:>5} {r.get('candidates_sent', 0):>5} "
            f"{r['keep']:>5} {r['truncate']:>6} "
            f"{r['drop']:>5} {r['TH']:>9,} {r['TP']:>9,} {r['saved']:>8,} "
            f"{r['saved_pct']:>6.2f}% {r['latency_ms']:>8.1f}  {' '.join(flags)}"
        )

    th_total = sum(r["TH"] for r in rows)
    tp_total = sum(r["TP"] for r in rows)
    print("-" * len(header))
    print(
        f"{'TOTAL':<30} {sum(r['candidates'] for r in rows):>5} "
        f"{sum(r.get('candidates_sent', 0) for r in rows):>5} "
        f"{sum(r['keep'] for r in rows):>5} {sum(r['truncate'] for r in rows):>6} "
        f"{sum(r['drop'] for r in rows):>5} {th_total:>9,} {tp_total:>9,} "
        f"{th_total - tp_total:>8,} "
        f"{((th_total - tp_total) / th_total * 100 if th_total else 0):>6.2f}%"
    )

    print("\n=== JSON lines ===")
    for r in rows:
        print(json.dumps(r, default=str))

    scored = [r for r in rows if r["candidates"] and not r["jev_error"]]
    if scored and all(r["all_keep"] for r in scored):
        print(
            "\n[FLAG] EVERY scenario returned keep for EVERY candidate. Treat this as a "
            "likely repeat of the old 'metadata-keep-v2 always keeps unseen results' "
            "bias before treating it as evidence that nothing is droppable."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
