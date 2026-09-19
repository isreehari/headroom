# Jev Proactive Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in Jev (Jev) retention-planning layer that proactively reduces eligible historical context before Headroom reaches the provider's hard context limit, while preserving Headroom's local compression, cache-safety, CCR, privacy, and analytics contracts.

**Architecture:** Headroom remains the primary request processor. The shipped `headroom proxy` command currently enters through the Python runtime (`headroom/cli/proxy.py` -> `headroom/proxy/server.py`), so the Jev adapter and active boundary target that path; the Rust proxy and `headroom-core` remain the cache/compression contract and later parity target. Headroom performs its existing deterministic work, measures context occupancy, and only at a configurable soft threshold sends a bounded retention view to Jev. Shadow mode records a projection without changing the request. Active mode is implemented only for the explicit `/v1/compress` CCR path with an acknowledged store write, protected frozen prefix, and caller-owned compaction boundary; unsupported routes remain fail-open.

**Tech Stack:** Python proxy/runtime for PR1, existing Python dashboard and savings tracker, existing CCR backends, Rust proxy/`headroom-core` for contract parity, Prometheus/OTEL metrics, and the Jev HTTPS API.

**Spec:** This document is the revised draft for the optional Jev feature. It must be reviewed before implementation.

**TypeSafe guidance:** See the [official-docs review and readiness gaps](2026-09-19-jev-typesafe-guidance.md)
for recommended question design, evidence, uncertainty handling, usage accounting,
and quality evaluation. Requirements below are not evidence that each protection
is already implemented.

## Current Status and Evidence

**Hardening update:** `metadata-keep-v2` batches explicit-path questions, labels
omitted result status as unknown, and preserves all unseen results. Process-local
cooldown/retry protection, serialized-request budgeting, usage accounting and
failed-commit metric guards are implemented. The historical projections below
predate this policy and do not demonstrate quality-preserving savings. Durable
CCR and cross-worker lineage remain production gates, not completed guarantees.

PR1 is implemented as an opt-in shadow path plus a reproducible benchmark. The
proof corpus uses seed `20260902` and the `gpt-5.6` provider tokenizer. The
live Jev run on 2026-09-19 made four successful shadow calls and returned four
`drop_result` decisions. Headroom saved 73,763 tokens on the corpus; applying
the decisions to an isolated in-memory copy saved another 59,390 tokens,
leaving 44,871 tokens in the simulation. These figures are evidence of Jev
decisions and a controlled projection, not production active savings; the
isolated benchmark copy is never forwarded to a model.

The active benchmark command is:

```bash
uv run python benchmarks/jev_proof_table.py \
  --seed 20260902 \
  --live \
  --apply-projection
```

`--apply-projection` never changes the running proxy. It applies Jev's
`drop_result`/`drop_call` semantics to a private benchmark copy so actual
post-decision token counts can be measured without changing the running proxy.

### Current implementation status

The current checkout also implements an experimental active slice:
`HEADROOM_JEV_MODE=active` is accepted when an API key is present, but active
mutation is gated to `/v1/compress` requests that use `config.mode="ccr"`, a
stable `config.session_id`, and `config.jev_compaction_boundary=true`. It
applies only `drop_result` decisions, preserves the assistant tool call,
acknowledges CCR storage before mutation, leases the entry against eviction,
and replays the active result into the session cache state. Missing gates,
storage failures, token recount failures, and session races fail open. The
regular provider proxy paths do not currently invoke the Jev planner. These
guards do not yet establish production readiness; the linked TypeSafe review
identifies additional evidence, accounting and safety validation requirements.

## Global Constraints

- `HEADROOM_JEV_MODE` defaults to `off`; enabling `shadow` or `active` requires an API key.
- Proactive calls use a soft threshold and cooldown; they do not run on every request.
- The existing live-zone, append-only, byte-fidelity, deterministic, and cache-hot-zone invariants remain unchanged.
- System prompts, tool definitions, encrypted reasoning, signatures, redacted thinking, and cache-frozen bytes are protected by default.
- Shadow mode never mutates the forwarded request.
- Active mode must fail open to the normal Headroom path on timeout, invalid output, stale output, CCR uncertainty, or unsupported session ownership.
- API keys, session bodies, and raw candidate content must not be written to logs, traces, telemetry, or dashboard payloads.
- Dashboard savings must use one token baseline and must not add overlapping compression and retention savings.
- Jev is an optional remote decision service, not a PII anonymizer and not a replacement for Headroom compression.

## Review Focus

- Previously forwarded history: the current append-only cache contract makes it unsafe to delete historical bytes without an explicit compaction boundary; active mode must remain disabled for such sessions.
- CCR write uncertainty: a retention decision is not applied unless the original is durably acknowledged by the configured backend.
- Stale decisions: a Jev answer must be rejected when the session revision, branch, or candidate hashes no longer match.
- Privacy scope: shadow mode is still an outbound data transfer and its candidate fields must be explicit and documented.
- Accounting replay: retries, duplicate callbacks, worker restarts, and shadow projections must not inflate savings.

## 1. User-facing behavior

### Modes

| Mode | Jev call | Changes context | Intended use |
|---|---:|---:|---|
| `off` | No | No | Default and compatibility path |
| `shadow` | At soft threshold | No | Measure projected savings and latency |
| `active` | At soft threshold | Eligible tool results on the explicit CCR boundary | Experimental opt-in for `/v1/compress` only |

The feature flag is opt-in. Existing users receive exactly the current Headroom behavior when the flag is absent or set to `off`.

### Proposed configuration

```text
HEADROOM_JEV_MODE=off|shadow|active
HEADROOM_JEV_API_KEY=<secret>
HEADROOM_JEV_ENDPOINT=https://api.typesafe.ai/v1/systemone
HEADROOM_JEV_MODEL=jev-latest
HEADROOM_JEV_TIMEOUT_MS=500
HEADROOM_JEV_TRIGGER=soft_threshold
HEADROOM_JEV_THRESHOLD_PERCENT=80
HEADROOM_JEV_COOLDOWN_TURNS=5
HEADROOM_JEV_MAX_CANDIDATE_TOKENS=20000
```

Configuration rules:

- `off` ignores the endpoint and never requires the API key.
- `shadow` and `active` fail startup validation when the API key is absent.
- The API key is loaded from process configuration or an existing secret mechanism and is never returned by settings, `/stats`, logs, or error messages.
- The endpoint and model are configurable so the integration remains provider-neutral even though the first adapter targets the Jev API.
- The timeout covers serialization, network, response validation, and commit preparation. It does not permit unbounded retries.
- Active mode does not silently downgrade to shadow. The request-level active gate also requires `config.mode="ccr"`, a stable `config.session_id`, and `config.jev_compaction_boundary=true`.

### PR1 identity and revision contract

Shadow mode still needs stable identity for cooldowns, stale-response rejection, and replay-safe metrics. PR1 uses the existing Headroom identity machinery:

- `session_id`: the existing `SessionTrackerStore.compute_session_id` result, preferring `x-headroom-session-id` and otherwise using the stable model plus leading system-prompt identity;
- `branch_id`: an explicit branch header when supplied, otherwise a stable resolved conversation-lineage key. The current in-memory lineage counter is not sufficient by itself; if the runtime cannot recover a durable lineage fingerprint across the request's worker boundary, the request is not eligible for Jev;
- `revision`: a SHA-256 digest of the provider-normalized canonical transcript used for the current request, excluding per-call transport annotations;
- `event_id`: a deterministic digest of provider, `session_id`, `branch_id`, `revision`, and the Jev policy/model version. Request IDs are not part of `event_id`, so transport retries remain deduplicable.

PR1 behavior is conservative across lifecycle boundaries:

- identical retries reuse the same revision and event ID;
- a new transcript revision invalidates all older proposals;
- a fork receives a different branch ID or is skipped when identity cannot be proven;
- a process restart or worker handoff without recoverable branch identity skips the remote call and records `identity_unavailable`;
- no Jev call occurs when the canonical session identity, branch identity, or revision cannot be constructed without guessing.

The latest-revision state is keyed by `(session_id, branch_id)` and is shared by every worker that can handle that session. After local compression reaches the soft threshold, the Jev-trigger admission transaction reuses the existing admission sequence for an exact retry of the same `(session_id, branch_id, revision, event_id)`; otherwise it allocates the next per-branch admission sequence and replaces the current revision before identity and candidate eligibility exits. A proposal carries that sequence in addition to the revision digest. Before a response is used or a shadow projection is counted, the same shared store atomically verifies that the proposal's sequence and digest are still current and inserts the event-ledger record. A newer admission therefore invalidates older proposals, while an exact replay cannot become a second counted event. The admission, freshness check, and projection ledger require the same shared-worker support described in the analytics section; without it, Jev calls are disabled rather than run as an uncoordinated `per_worker` feature. A revision sequence A -> B -> retry A after another worker or a restart must remain superseded and must not restore A as current or count its projection.

## 2. Request lifecycle

The normal and proactive paths are:

```text
Developer request
      |
      v
Headroom parses and identifies protected/cache-frozen ranges
      |
      v
Existing deterministic live-zone compression
      |
      v
Measure post-compression occupancy and predicted growth
      |
      +--> below soft threshold: forward normal final context
      |
      +--> at/above soft threshold:
              build bounded Jev retention view
              if no eligible candidates: record skip and forward normally
              call Jev once, with session revision and candidate hashes
              validate decision locally
              |
              +--> shadow: record projection, keep original context
              |
              +--> active: acknowledge CCR storage, then apply eligible result retention
      |
      v
Final context sent to the target model
```

The soft-threshold call occurs before the hard provider limit, but after the cheap local Headroom analysis and live-zone transformation. Jev is not placed in the path for every request. A cooldown prevents repeated calls while the session remains near the threshold. A growth prediction may schedule an evaluation on the next request, but PR1 still sends to Jev only when measured post-compression occupancy is at or above the configured soft threshold. When no eligible candidates remain, Headroom records `no_eligible_candidates`, records zero projected Jev savings, and makes no remote call.

If Jev is unavailable, slow, malformed, or stale, Headroom discards the proposal and forwards the normal request. A late response must not mutate a newer request.

## 3. Jev retention view

The adapter sends a decision input, not the exact final provider payload. The view contains:

- provider and model identifiers;
- opaque session and branch identifiers;
- canonical transcript revision digest;
- current and projected token counts;
- bounded eligible historical candidates;
- candidate type, role, tool/call identifier, ordering, byte/token estimates, and content hash;
- bounded candidate content only when required for the decision;
- explicit protected-range and candidate-range metadata.

The default view excludes:

- API credentials, authorization headers, cookies, and local environment secrets;
- system prompts and tool definitions;
- cache-frozen messages and previously forwarded bytes;
- encrypted reasoning, thinking signatures, and redacted thinking data;
- CCR payloads that are not eligible for this retention operation.

Tool results, source paths, source code, identifiers, and instructions may still contain sensitive information. Documentation must disclose that shadow mode sends the configured candidate fields to the configured endpoint. A separate local redaction policy may be added later; this feature must not claim general PII anonymization.

## 4. Decision contract

Jev returns a versioned proposal containing:

- session identifier, branch identifier, and transcript revision;
- provider/model and policy/model version;
- candidate content hashes;
- one action per candidate: `keep`, `truncate`, or `drop`;
- truncation boundaries where applicable;
- optional confidence and reason codes for analytics only.

Headroom rejects the proposal when it references an unknown candidate, a protected range, a different revision or branch, an invalid truncation boundary, duplicate candidates, an unsupported action, or a response outside the configured size limit. Uncertainty means `keep`.

Decisions are advisory. Headroom remains responsible for enforcing provider shape, ordering, tool-call/result linkage, cache boundaries, and token validation.

The revision digest is compared with the session's latest accepted revision before a response is used. A newer request invalidates all earlier pending proposals; the digest is the equality key, while the per-session latest-revision record establishes ordering without pretending that a hash is numerically monotonic.

PR1 provider coverage is explicit: Anthropic Messages and OpenAI Chat/Responses handler paths participate first. Gemini, Bedrock-native, and passthrough-only routes return `unsupported_provider` and continue normally until their provider-specific candidate walkers are added.

## 5. Cache and CCR safety

The current Headroom architecture is live-zone and append-only: once bytes have been forwarded to an upstream provider, they are frozen for cache stability. Therefore:

- active Jev retention cannot delete previously forwarded history in the existing default proxy path;
- active retention requires an explicit caller-owned compaction boundary and a session owner that can establish a new provider cache lineage;
- a session without that boundary is eligible for shadow measurement only;
- cache-frozen content, protected protocol items, unresolved tool calls, and their required results remain present.

Before applying an active `drop` or `truncate` decision, Headroom must:

1. write the original payload to the configured CCR backend;
2. receive an acknowledged success result;
3. bind the stored item to the session, branch, candidate hash, and supported retrieval lifetime;
4. make the surviving marker addressable through the existing retrieval path;
5. atomically commit the new request body.

If any step is unavailable, the original candidate remains in the request. The CCR interface used by active retention must expose write success or failure rather than silently logging a failed `put`.

PR2 must also issue a retention lease for every active entry. The lease defines a minimum retrieval lifetime, prevents eviction while the corresponding marker can be sent upstream, survives supported worker restarts, and has a recovery path when the lease cannot be renewed. An acknowledged write without a retention lease is insufficient for active mode because the entry could expire or be evicted after the request is forwarded.

## 6. Analytics and dashboard

The existing `/stats`, `/stats-history`, and `/metrics` surfaces gain a `jev` block and preserve all existing fields. Metrics must distinguish logical decisions from network attempts and deduplicate by session, revision, candidate hash, and event ID.

PR1 uses an idempotent event ledger for Jev accounting. The ledger atomically records the event ID and its aggregate delta before the event is considered counted. Single-worker deployments may use the existing local persistence path; multi-worker deployments must use a shared SQLite or equivalent backend, otherwise Jev calls and Jev accounting are disabled for that deployment. JSON snapshot replacement alone is not a cross-worker deduplication guarantee.

Required Jev metrics:

- mode, trigger reason, and configured threshold;
- calls attempted, completed, timed out, rejected, and failed;
- request latency and response size;
- candidate count and candidate tokens;
- keep, truncate, and drop decisions;
- active applied tokens and shadow projected tokens;
- CCR entries staged, acknowledged, retrieved, expired, and failed;
- fallback count and reason;
- configured model, endpoint label, and estimated Jev API cost, without secrets;
- cooldown skips and stale-response rejections.

Jev-triggered CCR staged, acknowledged, and failed values are reported separately from baseline Headroom CCR metrics. Retrieval remains visible through the existing CCR store statistics; attribution of retrievals to Jev entries is a follow-up telemetry enhancement.

Token accounting uses three named points for every eligible event. All three values cover the same provider-normalized input context, including protected system/tools/messages, and use the same provider tokenizer. They exclude output-token allowance and Jev request tokens, which are reported separately:

- `T0`: full input context before any Headroom transform;
- `TH`: the same full input context after existing Headroom compression;
- `TF`: the same full input context as finally forwarded after any valid active retention and CCR marker overhead.

Candidate-only tokens, protected-range tokens, output allowance, Jev request tokens, and CCR storage volume are separate dimensions. A shadow projection uses `TP` with the same full-request scope as `TF` and is never added to measured savings.

The dashboard reports:

- Headroom compression savings: `T0 - TH`;
- incremental active Jev savings: `TH - TF`;
- combined savings: `T0 - TF`;
- shadow combined projected savings: `T0 - TP`, separately and never added to measured savings;
- shadow incremental Jev projected savings: `TH - TP`, separately from Headroom compression savings; skips and unchanged projections report zero;
- Jev latency, API cost, CCR storage/retrieval, and fallback rates.

The dashboard must never count CCR storage volume as token savings, and must not add Headroom savings to Jev savings when both use the same baseline. Replayed or retried events use the same event ID and do not increase totals.

## 7. Failure, concurrency, and privacy rules

- One in-flight Jev request per session/branch; additional triggers coalesce or skip.
- A request has a bounded candidate size, response size, queue wait, and total deadline.
- New transcript revisions cancel or invalidate older proposals.
- The baseline request remains immutable until an active proposal passes validation and CCR acknowledgement.
- No Jev content is written to application logs, traces, OTEL attributes, dashboard JSON, or persistent savings files.
- Endpoint TLS and redirect behavior must not forward the API key to a different host.
- Jev calls and projected savings are visible in local diagnostics only as aggregate counts and token values.

## 8. Rollout and PR boundaries

### PR 1: safe measurement path

- Add configuration parsing and startup validation.
- Add the provider-neutral retention-policy interface and Jev adapter.
- Add soft-threshold and cooldown evaluation.
- Add shadow mode with bounded decision views, stable identity/revision handling, no-candidate skips, and stale-response rejection.
- Extend stats, history, metrics, dashboard, and exports.
- Add privacy and configuration documentation.
- Wire the shared shadow hook into the Anthropic and OpenAI provider handlers; unsupported routes fail open with an explicit metric.

PR 1 must not remove historical context, call Jev without recoverable identity, accept `active`, or perform any Jev-triggered CCR staging or write. Existing baseline Headroom compression and CCR behavior may continue unchanged; it is not part of Jev's PR1 decision or savings accounting.

### PR 2: active retention prerequisites

- Add session/branch ownership and explicit compaction-boundary plumbing.
- Make CCR writes acknowledged and session-scoped for this path.
- Add CCR retention leases, eviction protection, minimum retrieval lifetime, and recovery behavior.
- Add atomic proposal commit and rollback behavior.
- Enable active `drop`/`truncate` only for sessions satisfying those prerequisites.
- Add retrieval, restart, expiry, multi-worker, and cache-lineage tests.

The remaining PR2 work is broader provider parity, event-ledger deduplication,
and attribution of Jev retrievals in dashboard analytics. The current active
slice is intentionally narrower than the full plan and does not claim those
capabilities.

Active mode remains experimental and disabled by default until the acceptance criteria below pass. All Jev-triggered CCR staging, writes, leases, and retrieval behavior belong to PR2; PR1 only records shadow projections.

## 9. Acceptance tests

- Missing API key rejects `shadow`, while `off` starts normally; `active` is governed by the next rule.
- Active mode validates its API key at startup, never downgrades to `shadow`, and rejects request-level use without the CCR boundary contract.
- Below-threshold requests make no Jev call.
- At-threshold requests with no eligible candidates make no Jev call and record `no_eligible_candidates`.
- Soft-threshold requests call Jev once and honor cooldowns.
- Requests without recoverable session/branch/revision identity make no Jev call and record `identity_unavailable`.
- Shadow mode records a projection but forwards byte-identical Headroom output.
- [PR2] Active mode rejects stale, malformed, protected, duplicate, and unknown-candidate decisions.
- Previously forwarded history is never modified without an explicit compaction boundary.
- [PR2] Jev-triggered CCR write failure, expiry, restart, capacity exhaustion, and retrieval failure preserve the original candidate.
- [PR2] Active-mode tests verify retention leases prevent eviction/expiry for the supported retrieval lifetime.
- Jev timeout and cancellation cannot mutate a later transcript revision.
- Tool-call/result pairs, parallel calls, ordering, signed items, and opaque provider items remain valid.
- API keys and candidate bodies are absent from logs, traces, metrics labels, dashboard responses, and savings files.
- `/stats`, `/stats-history`, `/metrics`, JSON export, and CSV export report the same deduplicated totals.
- `T0`, `TH`, `TF`, and shadow `TP` use the same full-request token scope and produce no double counting under retries and supported worker restarts.
- Dashboard displays measured active savings and shadow projections separately.
- Admission, freshness validation, projection recording, and replay behavior are tested across concurrent workers for each supported session/branch, including revision A -> B -> retry A after restart.
- Growth prediction alone never causes a Jev call below the measured post-compression soft threshold.

## 10. Acceptance gates before enabling active mode

Active mode may be enabled only when all of the following are demonstrated in a controlled evaluation:

- no cache-prefix regressions across multi-turn sessions;
- CCR recovery succeeds across the supported TTL, restart, and worker model;
- Jev timeout and provider failure preserve baseline behavior;
- incremental token savings exceed Jev token/cost overhead for the target workload;
- p95 added latency stays within the configured product budget;
- task-quality evaluation shows no unacceptable loss on tool-heavy and instruction-heavy sessions;
- rollback to `shadow` or `off` is immediate and documented.

## Proposed implementation map

- Modify `headroom/proxy/models.py` and `headroom/proxy/server.py` to parse the flags, compute the soft trigger, enforce the deadline, and expose active mode only behind the request-level CCR boundary.
- Add a provider-neutral retention-policy module plus a Jev adapter with schema validation and redacted diagnostics; add Rust parity only after the Python shadow path is validated.
- Extend the CCR trait/backend contract only as required to return acknowledged writes and bind entries to session metadata; add retention leases before PR2 active mode.
- Extend the savings tracker, stats endpoints, Prometheus/OTEL facade, and dashboard template with the named Jev metrics and `T0`/`TH`/`TF` accounting.
- Add Rust and Python tests beside the existing live-zone, CCR, proxy stats, and dashboard tests.
- Update configuration, CCR, privacy, and observability documentation after the behavior is implemented.

## Implementation sequence after approval

### Task 1: Configuration and retention-policy boundary

**Files:**

- Modify: `headroom/proxy/models.py`
- Modify: `headroom/proxy/server.py`
- Create: `headroom/proxy/retention_policy.py`
- Test: `tests/test_config.py`

- [ ] Add typed configuration for mode, endpoint, model, timeout, threshold, cooldown, and candidate budget.
- [ ] Reject a missing API key for `shadow`; reject reserved `active` first with the PR2 error, regardless of API-key presence.
- [ ] Add a provider-neutral `RetentionPolicy` boundary whose proposal includes session revision, branch, candidate hashes, and actions.
- [ ] Add tests for default-off behavior, missing-key validation, reserved-active rejection, threshold calculation, cooldown, stable identity/revision construction, and bounded candidate selection.

### Task 2: Jev adapter and shadow lifecycle

**Files:**

- Create: `headroom/proxy/jev.py`
- Modify: `headroom/proxy/server.py`
- Modify: `headroom/proxy/handlers/anthropic.py`
- Modify: `headroom/proxy/handlers/openai.py`
- Test: `tests/test_proxy_jev_shadow.py`

- [ ] Serialize only the documented retention view and never forward credentials or protected ranges.
- [ ] Skip the call for unsupported providers, missing identity, and empty candidate sets.
- [ ] Enforce one request per session/branch, one bounded attempt, total deadline, and cancellation on a newer revision.
- [ ] Validate response schema, candidate hashes, revision, branch, actions, and truncation boundaries locally.
- [ ] Record shadow projections while forwarding byte-identical output.
- [ ] Test timeout, malformed response, stale response, duplicate response, unknown candidate, and late-response races.
- [ ] Test retries, forks, worker handoff, process restart, and no-candidate behavior.

### Task 3 (PR2 only): Acknowledged CCR and active preconditions

**Files:**

- Modify: `headroom/cache/compression_store.py`
- Modify: `headroom/cache/backends/base.py`
- Modify: `headroom/cache/backends/sqlite.py`
- Test: `tests/test_proxy_ccr.py`
- Test: `tests/test_proxy_jev_active.py`
- Parity follow-up: `crates/headroom-core/src/ccr/mod.rs` and its backend tests

- [ ] Introduce an acknowledged write result for the active-retention path without changing existing callers until their behavior is migrated.
- [ ] Bind active entries to session, branch, candidate hash, and supported retrieval lifetime.
- [ ] Add a retention lease that prevents eviction or expiry while a marker is recoverable.
- [ ] Stage CCR before applying a drop or truncate and keep the candidate when acknowledgement fails.
- [ ] Gate active retention on an explicit compaction boundary and supported cache lineage.
- [ ] Test restart, expiry, capacity, authorization, multi-worker, retrieval, and rollback behavior.

### Task 4: Metrics, savings accounting, and dashboard

**Files:**

- Modify: `headroom/proxy/savings_tracker.py`
- Modify: `headroom/proxy/persistent_metrics.py`
- Modify: `headroom/proxy/prometheus_metrics.py`
- Modify: `headroom/dashboard/templates/dashboard.html`
- Create: `headroom/proxy/jev_event_ledger.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_persistent_metrics.py`

- [ ] Persist deduplicated Jev events keyed by event ID, session, revision, branch, and candidate hash with an atomic ledger update.
- [ ] Emit `T0`, `TH`, `TF`, and shadow `TP` using one full-request token scope plus separate measured, projected, cost, latency, CCR, and fallback fields.
- [ ] Add `/stats`, `/stats-history`, `/metrics`, JSON export, and CSV export coverage.
- [ ] Display active savings and shadow projections separately without counting CCR volume as savings.
- [ ] Test retries, duplicate callbacks, worker restart, and combined-savings arithmetic.

### Task 5: Documentation and rollout gate

**Files:**

- Modify: `wiki/configuration.md`
- Modify: `wiki/ccr.md`
- Modify: `wiki/proxy.md`
- Modify: `SECURITY.md`
- Test: `tests/test_config.py`

- [ ] Document the opt-in remote-data disclosure, candidate fields, API-key handling, and fail-open behavior.
- [ ] Document stable identity/revision requirements, no-candidate skips, unsupported-provider behavior, and PR1's reserved `active` error.
- [ ] Document that active mode cannot delete previously forwarded history without a compaction boundary.
- [ ] Document soft-threshold, cooldown, shadow, active, and rollback settings.
- [ ] Keep active mode disabled until the acceptance gates in Section 10 are verified.
