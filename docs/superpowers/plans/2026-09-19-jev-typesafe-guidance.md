# Jev: TypeSafe Guidance and Headroom Readiness

Reviewed 2026-09-19 against TypeSafe's official documentation and this working
checkout. This records implemented hardening and remaining recommendations, not
a claim that active mode is production-ready. It supplements the
[retention plan](2026-09-19-jev-proactive-retention.md).

## What TypeSafe Recommends

Jev returns structured judgments rather than generated summaries. Give it narrow
decisions; combine answers in application code. This supports a retention adviser,
not a replacement for Headroom's compression or safety checks.
[Introduction](https://docs.typesafe.ai/introduction)

Batch independent questions against shared state in one request. Put the complete
question in its instructions, because question IDs are not visible to the model.
Use explicit field paths, such as `history[0].tool_calls[0].input`. State and
questions share the documented approximately 32,000-token budget; do not budget
only the tool outputs. Choice selects categories, Score uses a defined ordered
rubric, and Noul evaluates a yes/no proposition.
[Primitives](https://docs.typesafe.ai/primitives)

Supply the evidence needed to answer the question in structured, descriptively
named state fields. Keep evidence separate from evaluation instructions.
[State](https://docs.typesafe.ai/concepts/state)

Noul returns the probability of yes, with no separate confidence field. A value
near 0.5 signals uncertainty, not medium relevance. Test precise questions with
and without explicit true/false criteria.
[Noul](https://docs.typesafe.ai/primitives/noul)

Choice and Score expose confidence derived from their answer distributions.
Use conservative, risk-dependent action thresholds and calibrate them on our own
data; documentation examples are not validated retention thresholds.
[Confidence](https://docs.typesafe.ai/confidence)

The documented API uses a bearer key with `POST /v1/systemone`. Its response
includes the model and input/output token usage as well as answers. Preserve that
usage for accounting rather than estimating it from candidate output sizes.
[Quickstart](https://docs.typesafe.ai/introduction/quickstart)

## Current Integration Versus Recommended Changes

These observations come from `headroom/proxy/jev.py` and the `/v1/compress`
handler in `headroom/proxy/handlers/openai.py`, not from vendor claims.

| Area | Current checkout | Recommended change |
| --- | --- | --- |
| Batching | Two independent Noul questions, with explicit field paths, in one call | Evaluate narrower diagnostic questions on labeled data |
| Evidence | Outputs omitted; status explicitly unknown; goal remains truncated to 500 characters | Preserve relevant constraints and actual provenance within an explicit data budget |
| Decision | `metadata-keep-v2` always preserves unseen results, with uncertainty/evidence reasons | Calibrate a removal policy only after evidence and recovery contracts are established |
| Request budget | Conservative serialized-byte guard includes model, state and questions | Use provider tokenization if made available; measure rejection rates |
| Trigger | Process-local, locked cooldown and revision deduplication | Durable shared-worker admission and lineage remain unimplemented |
| Network | HTTP timeout plus overall planning deadline | Connection reuse and a full commit deadline remain follow-up work |
| Accounting | Validated API usage and response model retained; unknown usage is nullable; failed commits do not add savings | Add cost pricing, dashboard presentation and durable event accounting |
| Route coverage | Planner call is wired into `/v1/compress` | Do not advertise Jev shadow/active coverage on normal provider routes yet |

### Hardening Semantics

The metadata-only policy returns `keep` in both shadow and active modes. It never
converts an answer about unseen contents into deletion. The 0.1--0.9 uncertainty
band labels diagnostics only; it is not a calibrated authorization threshold.
`HEADROOM_JEV_KEEP_THRESHOLD` remains accepted for compatibility but cannot bypass
the evidence gate. Historical nonzero Jev savings are not reproduced by this policy.

`HEADROOM_JEV_COOLDOWN_TURNS` now skips that many new eligible revisions after a
remote attempt, including failed attempts. Retries do not consume cooldown or
repeat API calls. Counters and admission state are process-local, not persistent.
Tracking is bounded to 1,024 session/provider/model/branch combinations and 256
revisions per combination. Capacity exhaustion skips Jev rather than evicting
retry protection; Headroom's ordinary compression continues.

When a newer eligible revision reaches admission, a late earlier response no
longer contributes decision counts; its billed usage is still counted. Requests
which bypass the planner and cross-process arrivals are not covered by this
limited freshness check. It is not a replacement for durable cache lineage.

For compatibility, `HEADROOM_JEV_MAX_CANDIDATE_TOKENS` also limits serialized
request bytes, capped at 30,000 bytes including JSON fields and questions. This
deliberately conservative bound does not claim to be the private Jev tokenizer.
Original omitted output size no longer determines remote admission.

`/stats.jev` exposes policy version, process scope, skip and abstention counts,
and `api_usage` input/output totals with known/unknown call counts. Totals cover
only known calls; all-unknown totals are null, not zero. Latency averages include
attempted calls, not skipped requests. Benchmark JSON records usage, response
model, policy version and abstentions. Skipped benchmark calls have null API
latency and a separate planning-overhead measurement. `/v1/compress` also returns
reported usage and response model, including when decisions are invalid.
The existing dashboard has not yet been
extended to render these new fields.

## Proposed Decision Flow

1. Run normal local compression and enforce protected-content exclusions locally.
2. Check occupancy, stable session identity, cooldown and remaining call budget.
3. Build an allowlisted evidence view. Do not automatically expand remote data
   disclosure beyond the existing opt-in scope.
4. Batch atomic judgments; validate every response locally.
5. Keep candidates when evidence is missing, answers are uncertain, or any gate
   fails. A model probability is not proof that deletion is safe.
6. Shadow records projections only. Active additionally requires verified CCR
   recovery and an atomic, current-session commit before modifying the response.
7. Recount the final payload and record committed savings separately from estimates.

Suggested evidence fields include the relevant task constraints, candidate ID,
artifact identity, actual success/error status, and references to newer results.
Only populate facts that can be established from the source. Unknown remains
unknown. Metadata alone cannot prove that an omitted output lacks unique facts.

Example future questions, only when their referenced fields exist:

- Does `task.latest_request` explicitly reference the artifact identified by
  `candidates[0].artifact`?
- Does `candidates[0].evidence` contain an unresolved failure?
- Does `candidates[0].replacement_evidence` cover the same task-relevant facts as
  `candidates[0].evidence`?

Missing or truncated evidence must veto removal in code, regardless of the
answer. Any future output excerpts require an explicit disclosure policy, local
secret filtering and their own evaluation. Existing key-name redaction is not
general PII or free-text secret protection. Shadow also sends data externally.

## Best-Fit Workloads

These are Headroom-specific hypotheses to test, not vendor benchmark claims.

| Workload | Expected fit and limits |
| --- | --- |
| Long sessions with repeated reads or searches | Potential incremental savings when older results are demonstrably superseded |
| Repeated status snapshots | Potentially useful for obsolete snapshots; preserve history needed for incident analysis |
| Large repetitive tool output | Compare against Headroom alone; local compression may already capture most savings |
| Short sessions or dense unique results | Usually poor candidates: remote cost and risk may outweigh savings |
| Unresolved errors, exact code details, constraints or protected history | Keep unless independently established safe; do not optimize solely for token count |

Tool selection and model routing are separate possible uses of structured
judgments, not part of this retention PR. Jev does not execute tools.

## Evaluation and Dashboard Requirements

Compare Headroom alone against Headroom plus Jev on the same tasks, target model,
tokenizer and settings. Separate threshold calibration data from held-out tests.
Record seed, commit, model response identifier where available, policy version,
configuration and artifact locations without credentials or sensitive transcripts.

- Measure task success and loss of necessary evidence, not just smaller payloads.
- Include repeated reads, unique facts, failed tools, changing user instructions,
  CCR retrieval, retries, session forks and concurrent requests.
- Report p50/p95 end-to-end latency, Jev latency, failures, abstentions and calls
  avoided by cooldown. Repeat trials; report sample sizes and uncertainty.
- Let B be pre-compression tokens, H post-Headroom tokens, and J post-committed-Jev
  tokens. Local savings are B-H, incremental Jev savings H-J, and combined savings
  B-J. Preserve negative deltas; reject transformations that grow context.
- Separate projected, simulated, committed and provider-billed figures. Include
  Jev API cost, target-model cache billing and extra CCR retrieval cost in net
  monetary savings. Missing prices or usage mean unknown, not zero.
- Count committed savings once per event. Failed commits and replays must not
  increase savings; expose application failures, not only planner failures.

The previously recorded four live Jev calls demonstrate API decisions and
isolated token reductions. The resulting contexts were not sent to a target
model, so those runs do not establish task accuracy or production net savings.

## Production Gates Still Required

A feature flag controls rollout, not correctness. Before promoting active mode,
validate exact-content CCR write acknowledgement and recovery, lease lifetime and
renewal, backend failure behavior, cross-worker session/branch lineage, atomic
mutation, frozen-prefix protection, and replay-safe metrics. Merely finding a
CCR hash after a write is not proof of durable recovery. A retrieval marker is
useful only if the consuming harness can actually retrieve its content.

Next priorities are evidence quality and a held-out task-quality benchmark,
then durable production gate tests. Hardening code does not expand the remote
data scope or change credentials. The running service has not been restarted
as part of this hardening update.

See the [hardening run record](../results/2026-09-19-jev-hardening.md) for test
commands, review findings, limitations and the offline proof-table artifact.
