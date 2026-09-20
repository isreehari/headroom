# Jev Retention — Fresh Design (Single-Machine Scope)

Status: Phase 0 complete with real measurements (see "Phase 0 Results" below).
Sections below are updated in place to reflect what was actually found, not
just what was planned. Approved for implementation planning on tracks A, B,
and C; track D's plugin-alone numbers need one more correctness check before
they're treated as proven (see Track D).

## Why This Document Exists

The 2026-09-19 plan (`docs/superpowers/plans/2026-09-19-jev-proactive-retention.md`)
started as a small, bounded PR1/PR2 feature. A later pilot design
(`docs/superpowers/specs/2026-09-19-jev-agent-pilot-design.md`) expanded it into a
multi-machine, multi-week rollout with a custom session-engine/coordinator, SSH
tunnels between two Macs, and native-compaction probes that were never wired to a
real route. The user was unhappy with that direction and asked for a fresh
implementation, scoped to this machine only, excluding all of the prior codex
branch's uncommitted work.

This document is the fresh design. It reuses the original plan's sound parts
(config contract, identity/revision handling, CCR safety rules, decision
contract, token accounting) and drops everything that caused the scope creep
(multi-machine pilot, durable cross-worker coordinator, session-engine).

## Non-Goals

- No multi-machine pilot, no cross-Mac state sharing, no SSH tunnels.
- No new session-engine or coordinator service.
- No cross-worker/multi-process admission ledger (this machine runs Headroom
  single-worker).
- No PII anonymization claim; Jev is a retention-decision service only.
- No native-summarization replacement; Jev is additive to Headroom's existing
  deterministic compression, never a substitute for it.
- No changes to Rust `headroom-core`; Python proxy only, Rust parity is a later
  follow-up exactly as the original plan stated.

## Architecture: Four Independent Tracks

| Track | What | Where | Risk |
|---|---|---:|---|
| A | Shadow-mode measurement | Headroom proxy, Anthropic + OpenAI handlers | Low — never mutates a request |
| B | Active mode via explicit `/v1/compress` CCR boundary | Headroom proxy | Medium — real mutation, but on a boundary Headroom already owns |
| C | Active mode via native Codex compaction boundary | Headroom proxy (Codex Responses path) | Unproven — gated on a feasibility probe |
| D | Claude Code active retention | `fast-jev-compaction` plugin (not Headroom code) | None to Headroom; separate machine config |

Each track ships independently. A ships regardless. B ships if Phase 0a shows
real savings. C ships only if Phase 0b finds a usable boundary. D is a doc +
local plugin install, done in parallel, no code review needed on the Headroom
side.

All Headroom-side tracks stay behind `HEADROOM_JEV_MODE=off` (default) and fail
open at every gate: timeout, malformed response, stale revision, rejected
candidate, or CCR write failure all fall back to forwarding Headroom's normal
compressed output unchanged.

## Phase 0: Benchmark Before Building

Both benchmarks run before any production proxy code is written. They report
real measurements; the user decides go/no-go per track, no preset threshold.

### 0a — Savings/decision-quality spike

A new standalone script, `benchmarks/jev_savings_spike.py`, with no dependency
on unbuilt proxy code:

- Loads the existing scenario corpus (`benchmarks/scenarios/conversations.py`,
  `benchmarks/scenarios/tool_outputs.py`).
- Runs it through Headroom's existing compression path to get the post-Headroom
  token count (`TH`), reusing the pattern in `benchmarks/compression_benchmark.py`.
- Selects eligible historical tool-result candidates using the plan's
  eligibility rules (§3 of the original plan: allowlisted types, outside the
  recent-tail exclusion, outside the protected/frozen prefix).
- Builds the bounded retention view (candidate hashes, content, protected-range
  metadata) exactly as specified in the original plan's Jev retention view
  contract.
- Makes real Jev API calls (using the already-configured
  `HEADROOM_JEV_API_KEY`/`HEADROOM_JEV_MODEL`/`HEADROOM_JEV_ENDPOINT` in this
  shell) — a small, bounded number of calls per scenario, not load testing.
- Applies the returned decisions to a private in-memory copy (never forwarded
  anywhere) to compute the projected token count (`TP`).
- Reports per scenario: candidate count, decisions returned (keep/truncate/drop),
  incremental projected savings (`TH - TP`), Jev call latency, and whether the
  known "unseen results are always kept" pattern from the old
  `metadata-keep-v2` policy shows up again.

This answers both "is track A worth wiring into the proxy" and "does track B's
decision quality look real" from the same run.

### 0b — Codex native-boundary probe

A minimal, single-machine, read-only script. Explicitly not the abandoned
two-Mac `codex_compaction_probe.py` (no SSH tunnels, no aggregate-only
allowlist infra needed for a single local session):

- Runs `headroom wrap codex` locally against a loopback Headroom instance.
- Drives a short scripted session: a few tool-call turns to build up history,
  then triggers Codex's native compaction (manual `/compact` or filling context
  to force auto-compaction).
- Logs (locally only, never transmitted) the shape of every request Headroom's
  proxy actually receives around that compaction point: does any request
  expose prior tool-call/result content in a form Headroom could recognize and
  rewrite, or does Codex's compaction happen entirely server-side without ever
  sending the old transcript back through the wire.
- No Jev calls, no mutation. Output is a short pass/fail report with the actual
  observed request shape if a boundary is found.

If 0b finds no usable boundary, track C is documented as "not currently
possible from the proxy" and Codex traffic stays on track A (shadow) and B (only
for explicit `/v1/compress` callers) — the same conclusion the prior results
doc already reached once, now confirmed cleanly instead of via a half-wired,
untested module.

## Phase 0 Results (real measurements, 2026-09-20)

Both scripts were built by an Opus 5 implementer, independently reviewed by
Codex CLI, and revised to fix real bugs Codex found — this is not
self-reported vibes, it's adversarially checked. All numbers below are real,
billed Jev API calls or real Codex CLI sessions, not simulations.

**0a — savings spike, widened to 6 scenarios:** real incremental savings on
top of Headroom's existing compression, 12.8%–24.7% per scenario
(15.92% weighted total: 604,179 → 507,990 tokens across the 5 scenarios with
eligible candidates; the 6th correctly made no Jev call since it had no
eligible candidates). Codex found and forced fixes for three real bugs before
this number was trustworthy: a shared-CCR-store contradiction (the spike could
silently write into the same `~/.headroom/ccr_store.db` a real running proxy
uses — fixed by isolating to a temp workspace + forced in-memory CCR backend),
a request-budget bug that could re-trigger the exact `max_tokens_exceeded`
error it was meant to prevent, and a token-accounting bug that silently zeroed
savings for Responses-shaped tool results. **Verdict: track B is a go.**

**0b — Codex native-boundary probe, extended with WebSocket support:** the
first HTTP-only run found nothing; the extended run (adding a WebSocket relay
and forcing a real auto-compact crossing) found a real, reachable boundary.
Two structural facts change the original plan's assumptions:

- The boundary is **WebSocket-only**. `headroom wrap codex` traffic uses WS by
  default for `/v1/responses`; an HTTP-only proxy path (like the original
  plan's Anthropic/OpenAI-handler assumption) would never see it.
- The boundary carries **exactly one candidate per compaction event**
  (`input = [custom_tool_call_output, compaction_trigger]`, with a
  `previous_response_id` marker), not a bounded multi-candidate retention view
  like the original plan's Jev retention view contract (§3) assumed. Track C's
  design is revised accordingly — see below.
- `additional_tools`, `custom_tool_call`, and `custom_tool_call_output` are
  real wire item types outside the item-type vocabulary the original plan and
  its abandoned probe assumed; any code that inspects item types for Track C
  must include them.

**Verdict: track C is technically feasible**, scope narrowed to single-candidate
replacement at the WS compaction boundary.

**Plugin-alone comparison (no Headroom):** run on the same corpus, same
tokenizer, one shared `T0` baseline, to answer "how much of this could we get
without building anything in Headroom":

| | Headroom alone | Headroom + Jev (track B) | fast-jev-compaction alone (no Headroom) |
|---|---:|---:|---:|
| Reduction off T0 | 29.85% | 40.45% | 87.27% |

The 87% is real, but it is **not the same kind of result** as the other two
columns: it comes from Jev answering `drop_call` on 136 of 136 non-pinned tool
calls across all five agentic scenarios — permanent deletion (only a tool
re-run recovers it), versus Headroom's CCR-backed, retrievable compression.
On a synthetic corpus, "everything looks equally disposable" is a decision-
quality question, not a free win — the mirror image of the old "always keep"
bias the original plan warned about. This number does not change the recommended
architecture (see Track D), but it is a strong reason to run a real
task-correctness check before treating fast-jev-compaction's numbers as safe
to rely on, the same way the 2026-09-20 native-agent test checked exact-field
recovery rather than trusting token counts alone.

## Track A: Shadow Mode

Reuses the original plan's PR1 design nearly as-is:

- Config: `HEADROOM_JEV_MODE=off|shadow|active`, `HEADROOM_JEV_API_KEY`,
  `HEADROOM_JEV_ENDPOINT`, `HEADROOM_JEV_MODEL`, `HEADROOM_JEV_TIMEOUT_MS`,
  `HEADROOM_JEV_THRESHOLD_PERCENT` (default 80), `HEADROOM_JEV_COOLDOWN_TURNS`
  (default 5), `HEADROOM_JEV_MAX_CANDIDATE_TOKENS`.
- Identity: `session_id`/`branch_id`/`revision`/`event_id` exactly as defined
  in the original plan §1 (PR1 identity and revision contract), **minus** the
  shared cross-worker admission-sequence machinery — single worker means the
  in-process latest-revision store is sufficient.
- Trigger: soft-threshold check after existing Headroom compression, cooldown
  enforced, one bounded call, no-candidate skip recorded.
- Never mutates the forwarded request. Records a projection (`TP`) only.
- Wired into the Anthropic Messages and OpenAI Chat/Responses handler paths
  (the two provider adapters the original plan scoped for PR1). Unsupported
  routes fail open with an explicit metric.

## Track B: Active Mode via `/v1/compress` CCR

Reuses the original plan's PR2 CCR-safety contract (§5) and the already-proven
`jev-selected-evidence.md` request shape:

```json
{
  "config": {
    "mode": "ccr",
    "session_id": "caller-owned-session-id",
    "jev_compaction_boundary": true
  }
}
```

- Before applying `drop`/`truncate`: write original to CCR → require
  acknowledged success → bind to session/branch/candidate hash + retention
  lease → commit atomically. Any failed step keeps the original.
- Single-worker SQLite CCR backend (`headroom/cache/backends/sqlite.py`); no
  cross-worker lease renewal or shared ledger.
- Applies to both Anthropic- and OpenAI-shaped `/v1/compress` callers per the
  earlier "Anthropic + OpenAI both active" decision.
- Retrieval via the existing `POST /v1/retrieve` path.

## Track C: Active Mode via Native Codex Boundary

Phase 0b confirmed a real boundary, over WebSocket only, carrying exactly one
candidate per compaction event. This changes the design from a bounded
multi-candidate retention view (what the original plan and the unwired
`jev_compact.py` assumed) to a single-candidate keep/drop decision:

- **Where it lives:** the WS relay/interception between Codex and OpenAI in
  Headroom's proxy (`headroom/providers/codex/`). Only Headroom can see this
  boundary — Codex's plugin/hook system (`SessionStart`, `UserPromptSubmit`,
  `PreToolUse`, `PermissionRequest`, `PostToolUse`, `SubagentStart`,
  `SubagentStop`, `Stop`) has no compaction-lifecycle hook, so there is no
  plugin-level alternative to reaching this boundary. (Considered and
  rejected: forking `fast-jev-compaction` to also cover Codex. It only works
  in Claude Code because Claude Code exposes a `/compact`-lifecycle function
  hook a plugin can attach to; Codex has no equivalent, so a fork would still
  need Headroom's wire-level access to do anything — it would not remove the
  need to build this in Headroom.)
- **Decision logic stays native Python, not a forked library call.** The
  batch/multi-candidate decision engine `fast-jev-compaction` implements
  (`preserveRecentMessages`, budget-trimming across many candidates) is
  solving a bigger problem than Track C has — Track C only ever asks about one
  candidate at a time, so the multi-candidate machinery (and most of the bugs
  Codex found in it during Phase 0a) doesn't apply. Reuse the already-reviewed,
  already-fixed Jev-calling pattern from `benchmarks/jev_savings_spike.py`
  (correct `state`/`questions`/`answers` API shape, credential redaction,
  bounded timeout, fail-open handling), trimmed to a single-candidate call, in
  `headroom/proxy/jev_compact.py`. No new runtime dependency (Node subprocess,
  vendored fork) in Headroom's request path.
- **Item-type vocabulary must include** `additional_tools`, `custom_tool_call`,
  and `custom_tool_call_output` — the real wire types Phase 0b observed, wider
  than the original plan's and the abandoned probe's assumed set.
- Same fail-open rules as tracks A/B: missing recovery tool, missing turn
  identity, stale revision, or CCR failure all preserve the original.
- Scoped to this machine, single worker, no durable cross-restart replay beyond
  what the existing CCR backend already provides.
- Known gap from the probe: only the client→proxy direction was observed:
  a compaction signal carried solely in the provider's *response* would not be
  visible to this design either. Acceptable for PR1 of this track; revisit if
  it turns out to matter.

## Track D: Claude Code — `fast-jev-compaction` Plugin

Not Headroom code, and not forked — used as-is. Deliverable for this task:

- A short doc (in `wiki/` or a new `docs/jev-claude-code-plugin.md`) explaining
  why Claude Code uses the plugin instead of a Headroom-side feature: it owns a
  real `/compact`-lifecycle function hook Headroom structurally cannot reach
  for ordinary passthrough traffic, and Codex has no equivalent hook, so this
  is genuinely Claude-Code-only, not a stopgap.
- Actually register the plugin marketplace, set `TYPESAFE_API_KEY`, and enable
  function hooks in this machine's Claude Code settings.
- **Gate before treating the plugin as "the big win":** the Phase 0 comparison
  found the plugin's 87% reduction comes from dropping every non-pinned tool
  call outright (permanent), not from compression. Before relying on that
  number, run one task-correctness check in the style of the 2026-09-20
  native-agent test (exact-field recovery across a repeatable and a one-time
  case) specifically against the drop-everything behavior observed here, not
  just the single earlier test. If it holds up, the plugin is the right tool
  for Claude Code's episodic compaction, run alongside Headroom's continuous
  per-turn compression (they are complementary, not competing — Headroom
  compresses every turn; the plugin only acts at discrete compaction events).
  If it doesn't hold up, document the failure mode and reconsider before
  recommending the plugin as configured.

## Dashboard and Metrics

Extends `/stats`, `/stats-history`, `/metrics` with a `jev` block, reusing the
original plan's token-accounting model:

- `T0` (pre-Headroom), `TH` (post-Headroom), `TF` (post-active-retention,
  measured), `TP` (shadow projection, reported separately, never added to `TF`
  savings).
- Calls attempted/completed/timed out/rejected; candidate count/tokens;
  keep/truncate/drop counts; CCR staged/acknowledged/failed; fallback
  count/reason; configured model/endpoint label (no secrets).
- Single-worker event accounting: existing local persistence path is
  sufficient (no shared SQLite ledger requirement, since that requirement in
  the original plan only exists for multi-worker deployments).

## Testing

- Unit tests per module: config validation (default-off, missing-key
  rejection), shadow lifecycle (timeout/malformed/stale/duplicate responses),
  CCR ack/lease/rollback, event/dashboard accounting.
- Acceptance tests from the original plan §9, trimmed to drop
  multi-worker/cross-restart-race cases (not applicable at single-worker
  scope).
- Phase 0 benchmark/probe results are recorded as their own report, not
  claimed as an automated test suite guarantee.

## Documentation

- `wiki/configuration.md`, `wiki/ccr.md`, `wiki/proxy.md`: track A/B
  configuration, privacy disclosure, fail-open behavior.
- New doc for track D (plugin pointer + install steps actually performed on
  this machine).
- Track C gets either its implementation docs or a "not currently possible"
  note, depending on Phase 0b's result.

## Rollout Order

1. ~~Phase 0a + 0b benchmarks~~ — done, see "Phase 0 Results" above.
2. ~~User reviews Phase 0 results and confirms go/no-go per track~~ — done: A
   and B are go; C is go with narrowed (single-candidate) scope; D proceeds
   with a correctness-check gate before its numbers are trusted.
3. Track D's correctness check (task-correctness on the drop-everything
   behavior) and plugin install on this machine — can run in parallel with 4-5.
4. Write the implementation plan (`superpowers:writing-plans`) for tracks A,
   B, and C, then implement task-by-task.
5. Track A ships first (safe measurement backbone), then B, then C.

## Open Items Deferred Out of Scope

- Multi-machine pilot, durable multi-worker coordinator, native-compaction
  work beyond this machine: explicitly out of scope. If a future need arises,
  it gets its own fresh design doc, not an extension of this one.
