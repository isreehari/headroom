# Jev Retention — Track E: Active Retention on Claude Code Traffic

Status: design for review. Extends
`docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md` (the "fresh
design") with a fifth track; nothing in the fresh design is changed by this
document except where it says so explicitly (the Track D framing, below). No
implementation, no task plan — those are the next steps once this is approved.

## Why This Document Exists

Both deployed machines run `HEADROOM_JEV_MODE=active`. Under that setting the
Anthropic Messages path — which is what `headroom wrap claude` and every Claude
Code session actually sends — gets **no Jev participation at all**:

| Track | What it does | Active on Claude Code traffic today? |
|---|---|---|
| A (shadow) | Measures a projection on Anthropic + OpenAI handlers | No — only runs under `mode=shadow`, and `active` excludes shadow by documented contract |
| B (active, `/v1/compress`) | Real mutation at a caller-declared CCR boundary | No — needs an explicit `POST /v1/compress` caller; nothing in the proxy path makes one |
| C (active, Codex WS) | Real mutation at Codex's native `compaction_trigger` frame | No — Codex WebSocket only |
| D (plugin) | `fast-jev-compaction` inside Claude Code at `/compact` | No — install was refused by the permission system; its one-time-data path is unproven (see the Track D correctness check) |

So the only track that is live is Codex-specific, and the client this project
is most used with is the one client getting nothing. This document designs the
missing piece: active, CCR-backed retention on Anthropic Messages traffic, in
the spirit of Track C, without Codex's client-declared compaction signal —
because Anthropic's Messages API has no such signal to lean on.

### Reconciling with Track D

`docs/jev-claude-code-plugin.md` currently says "there is no Headroom-side
equivalent planned for Claude Code" and argues that Claude Code's `/compact`
boundary is reachable only by a plugin. Both halves of that argument survive
this document unchanged: Headroom still cannot see `/compact`, and this track
does not claim to. Track E stands on a *different* boundary — one only the
proxy can see, because only the proxy knows the provider prompt-cache state,
the idle gap, and the CCR store. The plugin (episodic, lossy, at `/compact`) and
Track E (episodic, retrievable, at a cache-cold turn) remain complementary. The
"no Headroom-side equivalent planned" sentence in that doc is superseded and is
listed under Documentation as a required edit.

## Non-Goals

- No new client-side hook, plugin, or Claude Code setting. Track E is proxy
  code on the existing `handle_anthropic_messages` path.
- No mutation on a *warm* provider prompt cache to chase savings. Considered
  and rejected on economics (see "Why not bust a warm cache"); the one place it
  could be revisited is named there.
- No change to Track C's behaviour, gates, metrics or reason vocabulary.
- No change to Track B's `/v1/compress` contract.
- Not wired into the OpenAI Chat / Responses HTTP handlers. Track A runs there
  too and the same design would apply, but this document scopes to the
  Anthropic Messages path, which is where the gap is. A follow-up can lift it.
- No cache-mode support (`HEADROOM_MODE=cache`). Cache mode's contract is
  "prior turns are never rewritten", and Track E declines there by design.
- No multi-worker, cross-restart or Rust-core work, exactly as the fresh design
  scoped.

## The Crux: A Boundary Without a Client Signal

### What Codex's signal actually buys

Track C's safety story is usually summarised as "the client asked". It is worth
being precise about *what* the client asking delivers, because the Anthropic
path has to obtain each of those properties by other means:

1. **The provider cache is about to be rebuilt anyway.** Compaction replaces
   the transcript; whatever prompt-cache prefix existed is dead after this
   turn regardless of what Headroom does. Rewriting history at that moment
   costs no cache economics it was not already going to cost.
2. **The moment is single-shot and identifiable.** `previous_response_id`
   names the boundary, so a retry or a reconnect replay can be recognised and
   left alone (claim-once revision store).
3. **The model can redeem a marker.** The frame carries the tool list, so
   `has_recovery_tool` can refuse a drop the model could never recover.
4. **Nothing is lost.** This one is not the client's gift at all — it comes from
   CCR: write, acknowledge, lease, then rewrite. It would be equally true at
   any other boundary.

Property 4 is what protects content, and it transfers wholesale. Property 3 is
*stronger* on the Anthropic path, because Headroom controls the tool list there
(sticky CCR tool injection) rather than merely inspecting it. Property 2 turns
out not to be needed on an HTTP request path (below). Property 1 is the real
crux: it is a statement about the **provider prompt cache**, and the question
is whether Headroom can observe that property itself on Anthropic traffic
without anybody telling it.

It can. It already does.

### Evaluating Track A's heuristic as the trigger

The obvious starting point is to let Track A's existing gates —
`HEADROOM_JEV_THRESHOLD_PERCENT` (post-Headroom tokens vs. the model's context
window), `HEADROOM_JEV_COOLDOWN_TURNS` and the per-`(session, branch)` in-flight
guard, `HEADROOM_JEV_MAX_CANDIDATES` — decide *when* to make an active decision
instead of a measurement. Reading `headroom/proxy/jev/shadow.py`, those gates
answer exactly one question: *is this conversation under enough pressure that
asking Jev is worth the call?* They say nothing about *where* in the transcript
a rewrite is safe. Track A never needed to, because it never rewrites.

So the finding is: **Track A's gates are necessary but not sufficient.** They
are kept, unchanged in semantics, as the pressure gate. The missing gate is a
cache-state gate, and that gate is the boundary.

### The boundary: a cold prefix

The Anthropic handler already computes, on every turn, everything needed to
know whether the provider prompt cache is alive for this conversation lineage:

- `prefix_tracker.get_frozen_message_count()` — the provider-**confirmed**
  cached prefix, derived from the previous response's
  `cache_read_input_tokens + cache_creation_input_tokens`
  (`headroom/cache/prefix_tracker.py`, `update_from_response`). Zero on the
  first turn this process sees of a lineage, or when nothing above
  `min_cached_tokens` was confirmed.
- `_cc_ttl = anthropic_cache_ttl_seconds(model, messages, system)` — the TTL
  Claude Code is *actually* using (5 m or 1 h, read off the request's own
  `cache_control.ttl`), or `None` when the client has prompt caching off
  (`headroom/transforms/cold_prefix.py`).
- `is_cold_prefix(prefix_tracker, ttl_seconds=_cc_ttl)` — true when the idle
  gap since the previous turn's response exceeds that TTL **plus a 60 s
  margin**, so a still-warm cache is never misjudged cold on the boundary.

`HEADROOM_COLD_RECOMPACT` already treats `_cc_ttl is None or is_cold_prefix(...)`
as "the safe moment for rewrites that would otherwise bust a warm cache" and
recompacts the whole prefix on that turn. That is a Headroom-observed
equivalent of Codex's property 1, proven in the same handler, and it is the
boundary Track E fires on.

**Definition — the Claude Code retention boundary.** An Anthropic Messages
turn is a retention boundary when all of the following hold:

| # | Gate | Source of truth |
|---|---|---|
| 1 | Track E is armed (`mode=active` and `HEADROOM_JEV_ANTHROPIC_ACTIVE` on) | `JevConfig` |
| 2 | Token mode (`not is_cache_mode(self.config.mode)`) | handler |
| 3 | Compression ran this turn: `_decision.should_compress`, `not _bypass`, `not _skip_compression_for_backpressure`, and the compression pipeline did not fail open (`not _compression_failed`) | handler |
| 4 | Pressure: `optimized_tokens * 100 >= context_limit * threshold_percent` (Track A's integer test, on the post-Headroom count) | Track A gate |
| 5 | Not in-flight and not in cooldown for this `(session_id, branch_id)` | Track A gates |
| 6 | The model will have `headroom_retrieve` this turn (see "Recovery tool") | handler / `SessionCcrTracker` |
| 7 | The **retention zone** is non-empty and yields at least one eligible candidate | this design |

The retention zone is the half-open index range `[zone_start, len(messages) -
RECENT_TAIL_EXCLUSION)` over the post-Headroom message list, where

```
cold        = tracker_frozen_count == 0
              or _cc_ttl is None
              or is_cold_prefix(prefix_tracker, ttl_seconds=_cc_ttl)
zone_start  = 0 if cold else max(frozen_message_count, tracker_frozen_count)
```

`tracker_frozen_count` is the provider-confirmed value the handler already
snapshots before any override (`anthropic.py` ~line 1554), and
`frozen_message_count` is the count the pipeline was actually told to freeze
this turn after `prepare_turn`'s confirmed-clamp (`min(tracker_frozen,
comp_cache.compute_frozen_count(...))`). The `max` is load-bearing: the clamp
can pull `frozen_message_count` *below* the confirmed prefix whenever the
session's compression cache lacks entries for it, and on those turns
`finalize_turn` replays the confirmed prefix from `_last_forwarded_messages`
**unconditionally** — undoing whatever the pipeline did there. Track E runs
after that replay, so it must never start its zone below the confirmed count,
or it would rewrite bytes the provider is holding in a live cache. Two cases
fall out:

- **Cold turn** (`zone_start = 0`): the whole history outside the recent tail
  is eligible. The provider cache for this lineage is dead or never existed;
  this request re-writes the prefix at the cache-write rate whatever Headroom
  forwards, so forwarding a *smaller* prefix is strictly cheaper, and every
  later warm turn reads the smaller form at the cache-read discount. This is
  the boundary the design exists for, and it is a real, recurring event in
  Claude Code use: a user who reads, thinks and types for more than five
  minutes between prompts returns to a cold cache; a `claude --resume` or a
  proxy restart starts one.
- **Warm turn** (`zone_start = max(frozen_message_count, tracker_frozen_count)`):
  the zone is the region beyond the provider-confirmed prefix that is also
  older than the recent tail. On a healthy Claude Code session the client keeps
  a breakpoint on its newest message, so the confirmed prefix covers everything
  but the current turn, and the zone is **empty** — Track E is a no-op,
  recorded as `anthropic_boundary_warm_empty`. When it is not empty (a client
  that stopped placing breakpoints, a partial cache write, a tracker estimate
  that undercounted), it is a region the provider has not confirmed cached and
  that Headroom's own pipeline was already free to rewrite this turn, so Jev
  rewriting there introduces no cache exposure the turn did not already have.

### Why this is safe without anyone declaring it

Three separate properties, each argued on its own:

**Cache safety.** Track E never rewrites a byte inside the provider-confirmed
cached prefix while that cache is alive. On warm turns the zone starts at the
confirmed prefix; on cold turns there is no live cache to protect. The one way
to get this wrong is to judge a cache cold when it is warm, and the exposure is
bounded and already accepted elsewhere in the handler: the 60 s margin in
`is_cold_prefix` makes a wrong-TTL misjudgement unlikely, and the worst case —
a proxy restarted within the TTL of a session whose provider cache is still
warm, so `tracker_frozen_count == 0` while the provider still holds the prefix
— costs **one** prefix re-write at the cache-write rate, with no content lost.
Headroom's own pipeline already does the identical thing on that turn (it
compresses the whole history when it has no confirmed prefix), so Track E adds
no new exposure there either.

**Content safety.** Nothing is deleted. Every rewrite is preceded by the Track
B/C CCR sequence in `retention_ccr.stage_retention`: write the original, read
it back byte-equal, take the 24-hour lease, and only then rewrite; any failure
keeps the original. The message envelope is preserved — an Anthropic
`tool_result` block keeps its `type`, `tool_use_id`, `is_error` and
`cache_control`; only its `content` becomes a marker — so a `tool_use` is never
orphaned (`retention_apply.py`, already proven for this shape by
`tests/test_jev_active_anthropic_shape.py`).

**Client transcript ownership — why Track C's claim-once store is not
needed.** Claude Code holds its own transcript and re-sends the *original*
bytes on every turn; it never sees Headroom's marker. Three consequences:

- A client retry of the same request re-sends the original tool result. The
  marker is re-applied not by re-deciding but by *replay*: the forwarded list
  is what `prefix_tracker.update_from_response` records as
  `_last_forwarded_messages`, and `finalize_turn` → `overlay_cached_prefix`
  replays that exact form on every later turn where the client's originals
  still match positionally. Replay is idempotent, so there is no "decided
  twice" hazard and no revision to claim.
- A proxy restart loses the replay state, so the next turn forwards the
  client's originals again — a cold cache write of the full prefix, not a
  content loss. Track C's docs make the same statement about its in-memory
  revision store.
- Because the HTTP request body is the handler's own list and is forwarded
  exactly once, there is no window in which the conversation "moves on" during
  the Jev call. Track A's staleness check exists for a measurement that
  outlives its request; an active decision applied to the same request it was
  made for has nothing to be stale against. Concurrent same-lineage requests
  (Claude Code parallel subagents share a session id) are serialised by the
  Track A in-flight guard, which Track E reuses.

### Why not bust a warm cache

The alternative trigger — fire on pressure alone, even inside a warm cached
prefix — was evaluated and rejected. With a cached prefix of `P` tokens and a
retention saving of `S` tokens, a warm-cache rewrite of the oldest candidates
re-writes nearly all of `P` at the cache-write rate this turn (≈1.25× input
price) instead of reading it at ≈0.1×, and then saves ≈0.1×`S` per subsequent
turn. For representative Claude Code sizes (`P` ≈ 150 k, `S` ≈ 30 k from a
dozen dropped tool results) the break-even is on the order of fifty warm turns,
which almost no session reaches before the next cold gap resets the economics
for free. The only regime where a warm bust is rational is when Claude Code's
own auto-compaction is imminent — the cache is about to be rebuilt anyway, and
freeing context might postpone a lossy summary. Headroom cannot observe Claude
Code's compaction threshold, and the dozen-candidate saving is unlikely to move
it, so that regime is **deferred as a named future item, not designed here**.
Track E therefore has no warm-bust path and no knob for one.

## Architecture

### Where Track E sits

Track E runs at exactly the position Track A's shadow hook occupies in
`AnthropicHandlerMixin.handle_anthropic_messages` (`anthropic.py` ~lines
2357–2379): **after** the compression pipeline, `finalize_turn`'s cached-prefix
replay, the inflation guard, `normalize_message_cache_control`, and read
maturation; **before** the CCR marker scan / sticky tool injection block that
follows (~line 2431 onward). The ordering reasons are in "Ordering relative to
Headroom's pipeline" below.

```
client (Claude Code)
   │  originals
   ▼
security scan / hooks / session id / prefix tracker fetch
   │
   ▼
T0 ─► deterministic compression (frozen prefix respected) ─► finalize_turn (replay prev_fwd)
   │                                                            normalize_message_cache_control
   │                                                            read_maturation
   ▼  TH  = optimized_messages
[Track E]  classify boundary ─► gates 1–7 ─► run_jev_active_retention(...)
   │         │ declined / no candidates / call failed / no lease / fail-open
   │         └────────────────────────────────────────────────► TH forwarded unchanged
   │  applied: optimized_messages := retained; optimized_tokens := TF
   ▼
CCR marker scan + verify_ownership ─► sticky headroom_retrieve injection
   │
   ▼
body["messages"] = optimized_messages ─► Anthropic
   │
   ▼  response usage
prefix_tracker.update_from_response(messages=forwarded (retained) form)
comp_cache.update_from_result(originals, retained)
```

### Sequence on a boundary turn

1. **Classify** (pure, never raises): `classify_retention_boundary(...)` takes
   the handler's already-computed values — `is_cache_mode`, `_decision`,
   `_bypass`, backpressure flag, `_compression_failed`, `tracker_frozen_count`,
   `frozen_message_count`, `_cc_ttl`, the prefix tracker (for `is_cold_prefix`),
   `optimized_tokens`, `context_limit`, `len(optimized_messages)` — and returns
   a `RetentionBoundary(zone_start, zone_end, cold, reason)` where `reason`
   names the first failed gate or `"cold"` / `"warm_zone"` when the turn
   qualifies. The handler calls the adapter in both `shadow` and `active`
   modes; under `shadow` the adapter stops here, records only the
   classification event, and returns the list unchanged (see "Shadow-mode
   measurement").
2. **Rate gates**: in-flight and cooldown on `(session_id, branch_id)`, with
   Track A's semantics (first eligible turn on a branch always runs; only
   turns that reach the gate count against the cooldown; the cooldown restarts
   when a call is spent, whatever it returned).
3. **Recovery-tool precondition** (below).
4. **Decide, stage, apply** by calling the existing Track B orchestrator
   `run_jev_active_retention(proxy, messages=optimized_messages, model,
   session_id, branch_id, frozen_prefix=zone_start, message_shape="anthropic",
   provider="anthropic", candidate_filter=...)`. It selects candidates
   (`select_candidates` with `frozen_prefix=zone_start`, which is what makes
   the zone the eligibility floor), builds the measured request, makes one
   bounded Jev call, stages every removable candidate through
   `stage_retention`, rewrites only leased slots through `apply_retention`, and
   returns a deep copy on the applied path or the caller's own list on every
   other path. It never raises.
5. **Splice** on `applied > 0`: `optimized_messages = result.messages`,
   `optimized_tokens = result.tokens_after`, `tokens_saved = max(0,
   original_tokens - optimized_tokens)`, `transforms_applied.append(
   f"jev_retention:{result.applied}")`. Then
   `self._get_compression_cache(session_id).update_from_result(messages,
   optimized_messages)` — the same index-aligned call the token-mode branch
   already makes after compression, reached through the same accessor rather
   than a local that may be unbound if the compression block raised — so the
   session's Zone-1 cache maps each retained original to its marker form and
   `prepare_turn` swaps the marker in on the next turn rather than Headroom's
   earlier compressed form.
6. Everything downstream is unchanged: the CCR scanner finds the new markers,
   `verify_ownership` confirms their hashes exist in the store, the sticky
   helper injects `headroom_retrieve`, the body is assembled, and the response
   path records the forwarded (retained) form as the replay source for the
   next turn.

### Candidate eligibility

`select_candidates` already enforces: Anthropic `tool_result` blocks only,
outside the caller's frozen prefix (here: the zone start), outside the last
`RECENT_TAIL_EXCLUSION = 6` messages, oldest first, at most
`HEADROOM_JEV_MAX_CANDIDATES`. Track E adds a `candidate_filter` applied after
selection and before the request budget:

- **Size floor.** Skip candidates with `est_tokens < 128`. A `drop` of a body
  that small saves less than the marker costs to read, and a `truncate` of a
  body under `TRUNCATE_CHARS = 400` is already refused by `apply_retention`.
  This is also what keeps Headroom's already-tiny `compressed to 0` markers and
  Track E's own earlier markers from being re-asked about: a bare marker is far
  below the floor. A Headroom-compressed tool result that still carries a
  substantial preview *is* eligible — dropping it stores the preview-plus-marker
  in CCR and replaces it with a second marker, a two-hop but fully retrievable
  chain, which is precisely the additive posture Track B already takes ("a
  marker resolves to exactly the bytes that turn would otherwise have
  forwarded").
- **Held Reads.** When read maturation is on, skip candidates whose
  `message_index` is in `maturation.holding_msg_indices`. Mechanism B is
  deliberately keeping those verbatim and uncached because the file is active;
  Jev should not overrule that on quality grounds. (Cache-wise a held Read is
  uncached and would be safe to touch; this is a decision-quality exclusion,
  not a safety one.)

Because the filter runs after `select_candidates` has applied
`max_candidates`, a filtered-out candidate consumes a selection slot. On a cold
boundary the oldest twelve tool results are asked about; an operator who wants
a bigger sweep per boundary raises `HEADROOM_JEV_MAX_CANDIDATES` together with
`HEADROOM_JEV_MAX_STATE_TOKENS`, exactly as the Track B note in `active.py`
says an episodic boundary may justify.

### Identity

- `session_id`: the handler's own, from
  `SessionTrackerStore.compute_session_id` (for Claude Code, a hash of model +
  system prompt; parallel subagents share it and are separated per lineage by
  `resolve_tracker`). Used for CCR key binding exactly as Track B uses its
  `config.session_id`.
- `branch_id`: `branch_id_for(session_id, messages[:1])` — the conversation
  root. Stable for ordinary turn growth; forks when Claude Code re-roots the
  history after `/compact` (the summary becomes message 0). This is the
  cooldown/in-flight key and the CCR binding's branch component. Track A keys
  on the frozen-prefix root instead; Track E does not, because its effective
  frozen count flips between 0 and the confirmed prefix across cold and warm
  turns, which would fork the branch on every cold gap and defeat the cooldown.
- No revision claim (see "Client transcript ownership").

### Recovery tool

A marker the model cannot redeem is data loss, so Track E refuses to run unless
`headroom_retrieve` will be in the request's `tools` this turn. On the Anthropic
path that is decidable before the fact:

- `self.config.ccr_inject_tool` is on and `_bypass` is false, in which case the
  sticky helper (`apply_session_sticky_ccr_tool`) will inject the tool when the
  marker scan finds Track E's markers and `verify_ownership` confirms them — the
  hashes are already in the store by then, because staging precedes rewriting;
  **or**
- the client already declares the tool itself (`body["tools"]` contains a
  `headroom_retrieve` or a namespaced `…__headroom_retrieve` entry, the same
  match `_is_recovery_tool_name` in `compaction.py` applies for Track C).

On a **warm** turn with a non-empty zone there is one further condition:
injecting the tool for the first time changes the `tools` segment, which is the
head of Anthropic's cache key and busts the whole prefix. So on warm turns Track
E additionally requires that the tool is already established —
`SessionCcrTracker.has_done_ccr("anthropic", session_id)`, or
`history_references_ccr_tool(optimized_messages)`, or a client declaration. On a
cold turn the injection is free and no such condition applies. When the
precondition fails the turn declines with `anthropic_boundary_no_recovery_tool`
and forwards Headroom's output unchanged.

## Configuration and the Mode Enum

### The choice

Three options were weighed for how Track E relates to
`HEADROOM_JEV_MODE=off|shadow|active`:

1. *Make `active` mean "all active tracks", so upgrading a proxy already on
   `active` starts mutating Claude Code traffic.* Rejected: it silently changes
   behaviour on both deployed machines, and it makes "active" mean something
   different before and after one release.
2. *Add a fourth mode value.* Rejected: it would have to encode a cross-product
   ("active-codex-only", "active-all", …) as the tracks grow, and Track C's
   gate is a literal `mode == "active"` string comparison that every new value
   would have to be threaded through.
3. **An orthogonal opt-in, meaningful only under `active`.** Chosen.

### `HEADROOM_JEV_ANTHROPIC_ACTIVE`

| Variable | Description | Default |
|---|---|---|
| `HEADROOM_JEV_ANTHROPIC_ACTIVE` | `0`/`1` (`false`/`true`, `off`/`on`, `no`/`yes` accepted; anything else fails startup). Arms Track E: active, CCR-backed retention on the Anthropic Messages path at a Claude Code retention boundary. Requires `HEADROOM_JEV_MODE=active` — with `shadow` it is a startup `ValueError` (a projection-only mode that also mutates would contradict what `shadow` promises), and with `off` it is not read at all, like every other `HEADROOM_JEV_*` knob. | `0` |

`JevConfig` gains `anthropic_active: bool = False`, parsed by a new strict
`_env_bool` (same posture as `_env_int`: a value that is not one of the
accepted spellings raises rather than being coerced), validated in
`JevConfig.validate()` (`anthropic_active and mode != "active"` → `ValueError`),
included in `redacted()` and in the hand-written `__repr__`. The default-off
contract is untouched: `from_env` returns before reading it when the mode is
`off`.

### The truth table

| `HEADROOM_JEV_MODE` | `…_ANTHROPIC_ACTIVE` | Track A (shadow) | Track B | Track C | Track E |
|---|---|---|---|---|---|
| `off` | ignored | — | — | — | — |
| `shadow` | `0` | Anthropic + OpenAI projection | — | — | boundary **classification events only**, no Jev call, no mutation |
| `shadow` | `1` | startup error | | | |
| `active` | `0` | — | as today | as today | — (**identical to today's behaviour**) |
| `active` | `1` | — | as today | as today | armed |

Two things this table makes unambiguous. First, `active` continues to exclude
the shadow projection, exactly as `wiki/configuration.md` states; Track E does
not smuggle Track A back in — it *reuses Track A's gate semantics* without
running Track A's projection. Second, Track C is gated on `mode == "active"`
alone and never reads the new field, so its behaviour under every row is
byte-for-byte what it is today; the wiring test asserts that
`compaction_hook.py` does not reference `anthropic_active`.

### Shadow-mode measurement

Under `mode=shadow`, the same adapter call the handler makes in active mode
runs only the classification step, on the same turn Track A's shadow hook
runs, and records exactly one event:
`anthropic_boundary_shadow_cold`, `anthropic_boundary_shadow_warm_zone`, or
`anthropic_boundary_shadow_declined`. No Jev call is made by Track E in shadow
mode (Track A's own call proceeds as it does today). This is how an operator
answers "how often would Track E have fired on my traffic" before setting the
flag, and it costs a handful of integer comparisons per turn.

## Ordering Relative to Headroom's Pipeline

Track C runs **before** Headroom compresses the compaction frame, so Jev is
shown — and CCR stores — the original tool output rather than an
already-compressed marker. Track E deliberately **diverges** and runs
**after** Headroom's pipeline, matching Track B. The reasoning:

- **On the Anthropic path there is no "original" left to protect by going
  first.** Track E's candidates are *historical* tool results. Headroom
  compressed them on the turn they arrived, and on every turn since, the bytes
  forwarded for them are the replayed compressed form (`overlay_cached_prefix`
  / Zone-1 swap). Running Jev before this turn's pipeline would not show it the
  client's raw bytes for those messages either — the pipeline does not touch
  frozen or replayed history — it would only show it a list that has not yet
  had the cached prefix replayed onto it, i.e. bytes that are *not* what the
  model has been seeing. What the model has been seeing is the post-`finalize_turn`
  list, and that is the correct "original" for a retention marker to resolve
  to: retrieving it hands the model exactly what it had before the drop.
- **Track C's ordering answers a different hazard.** On the WS frame the
  candidate is the *current* turn's tool output; compressing first would make
  Jev decide about, and CCR store, a marker instead of content. Track E's
  candidates are outside the recent tail by construction (`RECENT_TAIL_EXCLUSION
  = 6`), so the current turn's output is never a candidate and that hazard does
  not arise.
- **Double-mutation.** Headroom's pipeline never rewrites bytes it froze or
  replayed, and after Track E applies, the retained form is what
  `update_from_response` records as `_last_forwarded_messages` and what
  `comp_cache.update_from_result` records for Zone 1. On the next turn the
  pipeline is fed the marker form for those slots (Zone-1 swap) and the overlay
  replays the same marker form inside the confirmed prefix. Neither ever
  "compresses a marker": the marker is tiny and already-stable content. The
  inflation guard, the read-maturation pass and `normalize_message_cache_control`
  have all run before Track E, so nothing downstream re-touches the rewritten
  slots on this turn either.
- **Jev's view is honest.** The retention view already tells Jev the
  conversation "has already been deterministically compressed"
  (`request.TASK_DESCRIPTION`); each candidate carries its true byte length
  and SHA-256 next to a possibly truncated view. That is the same posture Track
  A and Track B take, and it is the truthful description of the post-pipeline
  list.
- **Two-hop markers are the additive contract, not a bug.** A candidate that
  is a Headroom compressed preview plus marker A is stored whole under a new
  hash B and replaced with marker B. `headroom_retrieve(B)` returns the preview
  and marker A; `headroom_retrieve(A)` returns the full original. Nothing on
  either hop is deleted, and the size floor keeps a *bare* marker from being
  asked about at all.

## Fail-Open Behaviour

Track E forwards Headroom's ordinary compressed output — `optimized_messages`
exactly as it stood before Track E ran — in **every** one of these cases. Each
is a named exit with exactly one event on
`headroom_jev_events_total{event}`; the cheap pre-boundary exits that fire on
the overwhelming majority of turns (mode off, cache mode, passthrough,
below-threshold, warm-empty) are counted under `anthropic_boundary_*` so the
signal "Track E is configured but never reaches a boundary" is visible as an
absence, exactly as Track C's docs describe for `compaction_boundary_detected`.

| Condition | Event | What is forwarded |
|---|---|---|
| `HEADROOM_JEV_MODE` is not `active`, or `HEADROOM_JEV_ANTHROPIC_ACTIVE` is off | (none — default path) | TH unchanged |
| `HEADROOM_MODE=cache` | `anthropic_boundary_cache_mode` | TH unchanged |
| Compression bypassed, skipped for backpressure, passthrough, or failed open this turn | `anthropic_boundary_passthrough` | TH unchanged |
| No usable context limit for the model | `anthropic_boundary_no_context_limit` | TH unchanged |
| Post-Headroom tokens below `HEADROOM_JEV_THRESHOLD_PERCENT` | `anthropic_boundary_below_threshold` | TH unchanged |
| Warm turn with an empty retention zone | `anthropic_boundary_warm_empty` | TH unchanged |
| A call already in flight for this `(session, branch)` | `anthropic_boundary_inflight` | TH unchanged |
| Cooldown not yet elapsed | `anthropic_boundary_cooldown` | TH unchanged |
| `headroom_retrieve` cannot be guaranteed this turn (injection disabled and no client declaration; or a warm turn where the tool is not yet established) | `anthropic_boundary_no_recovery_tool` | TH unchanged |
| Boundary reached | `anthropic_boundary_detected` (+ `_cold` or `_warm_zone`) | continues |
| No eligible candidate after the zone, tail, floor and held-Read filters; or the measured request budget admitted none | `active_no_candidates` (shared orchestrator event) | TH unchanged |
| Jev timeout (`HEADROOM_JEV_TIMEOUT_MS`), transport/TLS error, 4xx/5xx, non-JSON, missing `answers` | `active_call_failed`; `calls_timed_out` / `calls_rejected` accounting | TH unchanged; every candidate keeps |
| Ambiguous answer for a candidate (unrecognised word, missing id) | (answer is read as `keep`, no event of its own) | that candidate keeps |
| Jev keeps everything | `active_no_candidates` (`all_keep`) | TH unchanged |
| CCR write, read-back or lease fails for a candidate | `ccr_failed` accounting; `active_no_lease` when it fails for all | that candidate keeps its original; the turn is untouched only when no candidate could be staged (Track B's per-candidate rule) |
| A leased slot no longer matches its selected content, or a `truncate` of a body already ≤ 400 chars | `active_no_lease` (`not_applied`) when nothing applied | that slot keeps its original; the lease expires harmlessly |
| Any unexpected exception anywhere in the orchestrator | `active_fail_open` | TH unchanged |
| Any unexpected exception in Track E's own gates or splice | `anthropic_boundary_fail_open` | TH unchanged |
| `asyncio.CancelledError` | (propagates — the client hung up) | nothing forwarded |

Two cases that sound like fail-open but are not:

- **A misjudged cold cache is a cost, not a failure.** If the cache was in fact
  warm, the turn re-writes the prefix at the cache-write rate once. Content is
  untouched, the marker is retrievable, and the retained form is what gets
  cached for the rest of the session.
- **A request the provider rejects.** There is no retry-with-originals path in
  the Anthropic handler, so Track E does not rely on one. The one rejection a
  retention marker could cause — `Tool reference 'headroom_retrieve' not
  found` — is prevented by the recovery-tool precondition, which is why that
  gate refuses rather than hopes.

Nothing credential-bearing reaches a log line on any path: every exception
text goes through `scrub_secrets` against the live `JevConfig`, `exc_info` is
never used, and the store's own failure logs carry identifiers only — all
inherited unchanged from `active_hook.py` and `retention_ccr.py`.

## Invariants Protected

| Invariant (fresh design) | How Track E protects it |
|---|---|
| **Cache safety** — never bust a live provider prefix cache | The retention zone starts at the confirmed frozen prefix on warm turns and at 0 only when the cache is confidently dead (no confirmed prefix, caching disabled, or idle > TTL + 60 s). The recovery tool is never first-injected on a warm turn. The one bounded exposure (restart within TTL) is one re-write, shared with Headroom's own cold-start compression. |
| **Prefix preservation** — the next turn replays byte-identical what the provider cached | Track E's output *is* the forwarded list and therefore what `update_from_response` records as the replay source and what `finalize_turn` replays inside the confirmed floor next turn. `comp_cache.update_from_result` keeps Zone 1 in agreement. `apply_retention` never changes message count or order, so the overlay's positional guards keep holding. |
| **Additive, never a substitute** | Runs strictly after TH; every decline forwards TH; a `truncate`/`drop` only ever replaces content that already survived Headroom's compression; realized savings are reported as `TH − TF` into the same tokenizer-measured `realized_savings` Track B fills, never into `projected_savings`. |
| **Nothing is deleted** | Stage-before-rewrite via the single shared `stage_retention`; envelope preserved; leases 24 h; marker satisfies both scanners so the tool is injected. |
| **Default off; strict when on** | New field unread under `off`; invalid spelling or `shadow` + flag fails startup. |
| **Track C unchanged** | Reads `mode` only; asserted by test. |
| **No secrets in logs** | Inherited scrubbing; Track E's own module logs identifiers and reason strings only. |

## Components: Reuse vs. New

### Reused as-is

| Module | What Track E takes from it |
|---|---|
| `headroom/proxy/jev/active_hook.py` — `run_jev_active_retention`, `JevActiveResult` | The entire decide → stage → apply → account sequence, including per-call `JevClient` lifetime, per-candidate CCR staging, the `active_*` event vocabulary and the `tokens_active_baseline` / `tokens_final` accounting. This is the whole of Track B's mutation machinery, and it is provider-agnostic. |
| `headroom/proxy/jev/active.py` — `decide_active_retention` | Selection + measured request budget + bounded call, with the fail-open-to-keep rule enforced locally. Already accepts `provider` and `message_shape`. |
| `headroom/proxy/jev/candidates.py` — `select_candidates`, `count_messages_corrected`, `JevCandidate` | Anthropic `tool_result` block extraction, tail exclusion, frozen-prefix floor (fed the zone start). |
| `headroom/proxy/jev/request.py`, `client.py` | Retention view, questions, measured budget, bounded fail-open HTTP client, `scrub_secrets`. |
| `headroom/proxy/jev/retention_ccr.py` — `stage_retention`, `retention_marker`, `RetentionLease` | The one copy of the CCR safety ordering. |
| `headroom/proxy/jev/retention_apply.py` — `apply_retention` | Content-bound rewrite of `tool_result.content`, envelope preserved. |
| `headroom/proxy/jev/identity.py` — `branch_id_for` | Branch id from session + root. |
| `headroom/proxy/jev/accounting.py` | `record_jev_accounting`, `classify_call_error`. |
| `headroom/transforms/cold_prefix.py` — `is_cold_prefix`, `anthropic_cache_ttl_seconds` | Cold-cache judgement; the handler already computes `_cc_ttl`. |
| `headroom/proxy/helpers.py` — `get_session_ccr_tracker().has_done_ccr`, `history_references_ccr_tool` | Warm-turn recovery-tool precondition. |
| `headroom/proxy/jev/compaction.py` — `_is_recovery_tool_name` | Client-declared tool match (lift to a public name, no behaviour change). |

### Small modifications to existing code

| File | Change |
|---|---|
| `headroom/proxy/jev/config.py` | `anthropic_active: bool` field; `_env_bool`; `validate()` rule; `redacted()` and `__repr__` entries. |
| `headroom/proxy/jev/active_hook.py` | Two keyword-only pass-throughs on `run_jev_active_retention`: `provider: str = "compress"` (forwarded to `decide_active_retention`, so the state tells Jev `provider="anthropic"`) and `candidate_filter: Callable[[JevCandidate], bool] | None = None`. |
| `headroom/proxy/jev/active.py` | Apply `candidate_filter` after `select_candidates`, before the request budget. |
| `headroom/proxy/handlers/anthropic.py` | One call at the shadow-hook position (both modes), one splice on the applied path, one `comp_cache.update_from_result` call. The shadow hook call stays exactly as it is. |
| `headroom/proxy/server.py` | Construct `self.jev_anthropic_active = JevAnthropicActiveRunner(config.jev, metrics=self.metrics)` beside `self.jev_shadow`. Nothing to close: the orchestrator opens and closes its own client per boundary, as Track B does. |
| `headroom/proxy/prometheus_metrics.py` | Docstring only — the event counter is free-form keyed. No new accounting fields: Track E reports through the existing tokenizer-measured pair. |

### New code

| File | Contents |
|---|---|
| `headroom/proxy/jev/anthropic_boundary.py` | `RetentionBoundary` dataclass and `classify_retention_boundary(...)` — pure, stdlib-plus-`cold_prefix`, never raises, no `headroom.proxy.jev` imports beyond the constant `RECENT_TAIL_EXCLUSION`. Also `recovery_tool_guaranteed(...)`. |
| `headroom/proxy/jev/anthropic_active.py` | `JevAnthropicActiveRunner`: `enabled` property (`mode == "active" and anthropic_active`), bounded per-`(session, branch)` in-flight set and cooldown map (same `OrderedDict` shape and bound as `JevShadowRunner`, kept as its own instance so `shadow.py` is not touched), the size-floor / held-Read `candidate_filter`, and `maybe_run(...)` which orders the gates, records `anthropic_boundary_*` events, and delegates to `run_jev_active_retention`. Plus the never-raising handler adapter `run_jev_anthropic_active_hook(proxy, ...) -> JevAnthropicActiveOutcome` (fields: `messages`, `tokens_after`, `applied`, `reason`, `hashes`), mirroring `hook.py`'s contract: the handler gets one `await` and no error handling of its own. |
| `tests/test_jev_anthropic_boundary.py` | Classification matrix (see Testing). |
| `tests/test_jev_anthropic_active.py` | Runner gates, events, cooldown/in-flight semantics, filter behaviour, fail-open. |
| `tests/test_jev_anthropic_active_e2e.py` | Real `CompressionStore` over `InMemoryBackend`, `JevClient` over `httpx.MockTransport`, Anthropic-shaped transcript: select → stage → rewrite → retrieve round trip, envelope preserved, two-hop marker resolves. |
| `tests/test_jev_anthropic_active_wiring.py` | Source-level assertions on the handler, modelled on `tests/test_jev_shadow_wiring.py`. |
| `tests/test_jev_anthropic_replay.py` | Two-turn simulation through `overlay_cached_prefix` / `finalize_turn`: the retained form persists when the client re-sends originals, message count unchanged, confirmed-floor replay unconditional. |

The cooldown/in-flight bookkeeping is the one thing this design knowingly
duplicates rather than extracting from `JevShadowRunner`. It is policy, not a
safety sequence; the safety sequence (`stage_retention`) stays in exactly one
copy. Extracting a shared gate class is a reasonable later refactor once both
runners are stable, and is out of scope here to avoid touching a shipped track.

## Metrics

- Events (`headroom_jev_events_total{event}`): the `anthropic_boundary_*`
  vocabulary in the fail-open table, plus the orchestrator's existing
  `active_attempted` / `active_no_candidates` / `active_call_failed` /
  `active_no_lease` / `active_applied` / `active_fail_open`, plus
  `anthropic_active_applied` on the applied path so Track B and Track E
  outcomes can be told apart on the dashboard.
- Accounting (`/stats` → `jev`): Track E contributes to `calls_*`,
  `candidates*`, `keep`/`truncate`/`drop`, `applied`, `ccr_*`, and the
  tokenizer-measured `tokens_active_baseline` / `tokens_final` pair, so its
  savings appear in `realized_savings` beside Track B's — **not** in the
  `_estimated` twins Track C fills (Track E has a real tokenizer in reach), and
  never in `projected_savings`.
- `config` on `/stats` shows `anthropic_active` through `JevConfig.redacted()`.
- The dashboard's Jev block (commit `d4e46331`) needs one new row for the
  boundary-detected count; the call/decision counts it already shows are
  shared totals and need no change.

## Testing

Unit, at the module seam:

- **Classification matrix** (`test_jev_anthropic_boundary.py`): every gate in
  the fail-open table has a case that fails only that gate and asserts the
  reason; the cold definition is exercised on all three legs (`tracker_frozen
  == 0`, `_cc_ttl is None`, `is_cold_prefix` true) and on the margin boundary
  (idle = TTL + 59 s is warm, TTL + 61 s is cold); warm-empty vs warm-zone on
  the zone start relative to `len - 6`; on a warm turn the zone start is
  `max(frozen_message_count, tracker_frozen_count)`, so a clamped
  `frozen_message_count` below the confirmed prefix never opens the confirmed
  region; the function never raises on a tracker missing
  `_idle_seconds_at_fetch` or on a non-int frozen count.
- **Config** (`test_jev_config.py`, extended): default off; each accepted
  spelling; a rejected spelling fails startup; `shadow` + flag fails startup
  with a message naming the variable; `off` never reads the variable (an
  invalid value under `off` still boots); `redacted()` and `repr` carry the
  field and still withhold the key and endpoint credentials.
- **Runner** (`test_jev_anthropic_active.py`): the first eligible turn on a
  branch runs; cooldown counts only gate-reaching turns and restarts when a
  call is spent, including on a failed call; the in-flight guard declines a
  concurrent same-branch turn and admits a different-branch one; the size
  floor and held-Read exclusions filter as specified and a filtered slot is
  visible as `candidates - candidates_sent`; the warm-turn recovery-tool
  precondition declines when the tool is not established and admits when the
  session `has_done_ccr`, when history references the tool, or when the client
  declares it (namespaced included); the adapter returns the caller's own
  list object on every non-applied path and records `anthropic_boundary_fail_open`
  exactly once when the runner raises.
- **Track C unchanged** (`test_jev_compaction_wiring.py`, one assertion added):
  `compaction_hook.py` does not reference `anthropic_active`, and its
  `mode != "active"` gate is intact.

End-to-end at the orchestrator seam (`test_jev_anthropic_active_e2e.py`),
following `tests/test_jev_active_anthropic_shape.py`: a real
`CompressionStore` on `InMemoryBackend`, production `JevClient` over an
`httpx.MockTransport` answering `drop` for the oldest candidate and `truncate`
for the next; assert the `tool_result` blocks' envelopes survive, the marker's
hash resolves through `store.peek` to the exact pre-drop bytes, a second pass
over the already-retained list selects nothing (size floor), and a
Headroom-marker-bearing candidate resolves through both hops.

Replay (`test_jev_anthropic_replay.py`): build turn N's forwarded list with a
Track E rewrite at index 3, record it as `prev_forwarded`, present turn N+1 as
the client's originals plus one appended user message, and assert
`overlay_cached_prefix(..., confirmed_frozen_count=len(prev))` replays the
marker at index 3 and that the length guards hold. A second case drops the
confirmed floor to 0 and asserts the size bound still prefers the smaller
retained form.

Wiring (`test_jev_anthropic_active_wiring.py`), source-level like the shadow
wiring test: `run_jev_anthropic_active_hook(` appears in
`handle_anthropic_messages` after `run_jev_shadow_hook(` and before
`injector.scan_for_markers(`; its result is spliced into `optimized_messages`
and `optimized_tokens` under an `applied` check; `comp_cache.update_from_result`
is called on that path; the call passes `frozen_prefix=` from the boundary's
`zone_start`, `message_shape="anthropic"`, `provider="anthropic"`.

Docs consistency (`tests/test_jev_docs.py`): the table-equals-fields test will
fail until `wiki/configuration.md` gains the new row — that is the intended
forcing function; the `HEADROOM_JEV_THRESHOLD_PERCENT` / `COOLDOWN_TURNS` rows
must stop saying "Shadow mode only".

Manual acceptance on this machine, recorded as a results doc rather than
claimed as a test: arm the flag, run a real Claude Code session past the
threshold, idle past `TTL + 60 s`, send one more prompt, and confirm on `/stats`
one `anthropic_boundary_detected_cold`, `applied >= 1`, `realized_savings`
moved; then ask the model for something inside a dropped tool result and
observe the `headroom_retrieve` round trip return the exact bytes; then confirm
the following warm turn shows `cache_read_input_tokens` covering the retained
prefix (the marker form was cached) and `anthropic_boundary_warm_empty`.

## Documentation

- `wiki/configuration.md`: new `HEADROOM_JEV_ANTHROPIC_ACTIVE` row; amend the
  `HEADROOM_JEV_MODE` row ("`active`: … Track C, or — when
  `HEADROOM_JEV_ANTHROPIC_ACTIVE` is on — a Claude Code retention boundary
  (Track E)"); drop "Shadow mode only" from the threshold and cooldown rows;
  extend "What leaves this machine" with Track E's identifiers (the proxy's
  session id and a branch id hashed from the conversation root; the same
  revision hash Track B sends); extend "It fails open, everywhere" and
  "Reading the numbers".
- `wiki/proxy.md`: a new section "Jev Retention Boundary (Claude Code /
  Anthropic Messages)" in the same shape as "Jev Compaction Boundary (Codex
  WebSocket)": what the boundary is, the ordering, the full fail-open list
  above, and the three plainly-stated limits (cooldown/in-flight state is
  in-process; a misjudged cold cache costs one re-write; there is no warm-bust
  path).
- `wiki/ccr.md`: one paragraph under the Jev section noting that the proxy
  path can now stage retention too, and that two-hop markers resolve hop by
  hop.
- `docs/jev-claude-code-plugin.md`: replace "there is no Headroom-side
  equivalent planned for Claude Code" with the complementary-boundaries
  framing from "Reconciling with Track D".
- `docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`: add a
  one-line pointer to this document in the architecture table. No other edit.

## Rollout

1. Approve this design.
2. Write the implementation plan (`superpowers:writing-plans`), ordered:
   config → boundary classification → runner + adapter → orchestrator
   pass-throughs → handler wiring (shadow-mode classification first, mutation
   second) → replay test → docs.
3. Ship with the flag off. Run `mode=shadow` on one machine for a day to read
   `anthropic_boundary_shadow_*` counts and confirm the boundary fires at the
   expected rate before arming.
4. Arm on one machine; run the manual acceptance; then the second.

## Accepted Costs and Deferred Items

Stated as decisions, not open questions:

- **CPU work on the event loop on boundary turns.** `run_jev_active_retention`
  deep-copies the message list and recounts tokens inline, as it does for
  Track B. On a boundary turn with a 150 k-token transcript that is tens of
  milliseconds, bounded to turns that pass every gate (rare by construction).
  Accepted for v1; offloading via `asyncio.to_thread` — explicitly not the
  compression executor, for the quarantine reason `hook.py` documents — is the
  follow-up if measured latency warrants it.
- **Per-boundary sweep is bounded by `HEADROOM_JEV_MAX_CANDIDATES` (12,
  oldest first).** Operators raise it with `HEADROOM_JEV_MAX_STATE_TOKENS`.
- **Warm-cache bust** is out of scope with the economics stated above; the one
  regime worth revisiting is "native auto-compaction imminent", which Headroom
  cannot currently observe.
- **OpenAI Chat / Responses HTTP paths** get the same design in a follow-up if
  wanted; the boundary classification is provider-agnostic except for
  `anthropic_cache_ttl_seconds`.
- **Cooldown state is per process.** A restart reopens every branch's cooldown,
  which at worst allows one extra Jev call per branch — never an extra drop of
  live content, since a restart also forwards the client's originals again and
  the next boundary decides them afresh under the same CCR rules.
