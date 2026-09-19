# Jev Fork and Local Development Pilot

## Status

Requested by the user on 2026-09-19. Proposed duration: 21 days of actual
instrumented use, starting after deployment, not from the date of this document.
Fork: [isreehari/headroom](https://github.com/isreehari/headroom), verified as a
fork of `headroomlabs-ai/headroom`. Local GitHub CLI authentication is working.
The running proxy has not been switched and the observation period has not begun.

The feature branch is `codex/jev-proactive-retention`; `origin` is
`https://github.com/isreehari/headroom.git`, and `upstream` is
`https://github.com/headroomlabs-ai/headroom.git`. Default pushes target the fork.
HTTPS uses the existing GitHub CLI credential helper in this checkout only; no
token is stored in git configuration. SSH was not used after host-key validation
failed. No reset, rebase or replacement of the checkout was needed.

A daily 09:00 local-time follow-up named `Headroom Jev pilot review` is configured
but paused. It will not start collecting evidence until the deployment and
instrumentation gates below are met and its start time is recorded.

Pre-publication checks on 2026-09-19: 251 focused tests passed, one live test was
deselected, and lint/whitespace checks passed. A local `detect-secrets` scan with
network verification disabled found four keyword matches, all reviewed as
documentation placeholders or test keys. No environment files or runtime stores
are included. The earlier full-suite interruption remains documented in the
[hardening run record](../results/2026-09-19-jev-hardening.md).

## Fork Setup

1. Authenticate the local GitHub CLI through its normal login flow. Never put a
   token in chat, repository files or command arguments.
2. Check for an existing user-owned fork. Reuse it if its parent is the Headroom
   repository; do not overwrite an unrelated repository with the same name.
3. Create a fork if necessary. Once verified, retain the original repository as
   `upstream` and make the user's fork `origin`. Default pushes target the fork.
4. Review the complete implementation diff and scan for secrets before publishing
   the feature branch. Include source, tests and sanitized documentation only;
   exclude environment files, credentials, real conversations and runtime stores.
5. Keep the fork's main branch aligned with upstream. Keep experimental work on
   the feature branch, using reviewed commits and a recorded upstream base.
6. Use an isolated local environment and a separate trial port/state directory
   first. Record the exact executable, commit and configuration. Switch the
   development harness only after health and rollback checks succeed.

## Implementation Gates

The existing `metadata-keep-v2` policy preserves unseen tool results and cannot
demonstrate incremental Jev savings. A fork does not remove that limitation.

- Define an evidence-backed policy and test it with synthetic transcripts first.
  Tool-result content must not be sent remotely without explicit data-scope
  approval. Filtering is not a guarantee that all secrets or PII are removed.
- Implement persistent, content-free measurements before the observation clock
  starts. Current counters are process-local and do not survive restarts.
- Define and enforce a user-selected daily Jev cost/call budget. Cooldowns alone
  are not a spending cap. Unknown pricing/usage must not become zero cost.
- Preserve ordinary Headroom behavior when Jev is off, unavailable or uncertain.
- Before active use, verify durable CCR recovery, leases, session/branch ownership,
  worker/restart behavior, protocol integrity and cache-frozen history protections.
- Prove that the chosen harness can retrieve retained originals. Merely returning
  a CCR marker does not establish recoverability.
- Add dashboard views for API usage, skips, abstentions, latency, errors, projected
  versus committed savings and quality checks. Do not imply unrendered metrics
  are already visible in the dashboard.

## Observation Stages

Stages advance on evidence, not automatically on calendar dates. If a gate is
not met, remain in the previous stage and report the limitation.

| Stage | Traffic | What it establishes |
| --- | --- | --- |
| Baseline | Headroom with Jev off | Normal token use, cache billing, latency and task outcomes |
| Shadow | Same workflow with approved Jev evidence, no mutation | Decision quality, projected deltas, API cost, reliability and added latency |
| Isolated active | Selected disposable development sessions after safety gates | Actual forwarded context, recovery behavior and task outcomes |
| Broader local trial | Opt-in new sessions only after isolated validation | Sustained effectiveness and operational failures |

Use stable session cohorts or matched repeatable tasks. Do not toggle policies
randomly between turns in a session: that confounds history and cache effects.
Keep target model, task fixtures and configuration comparable. Evaluate diverse
tasks and report failures as well as successes. Natural development sessions
complement controlled tasks; they are not a randomized benchmark by themselves.

## Measurement Record

Persist numeric aggregates and pseudonymous event identifiers only. Record:

- Trial start/end, upstream base, feature commit, policy version, target model,
  Jev response model where reported, and non-secret configuration.
- Tokens before Headroom, after Headroom, and after committed Jev actions using
  the same tokenizer. Report incremental and total savings without double counting.
- Jev input/output usage and price assumptions, target-provider cached/uncached
  billing, CCR retrieval costs, and unknown portions of the bill.
- Latency distributions, attempted calls, skips, abstentions, timeouts, invalid
  decisions, application failures, restarts and deduplicated events.
- Task success, tests passed/failed, lost necessary context, retrieval success,
  human corrections and rollback events. No raw prompts or tool results in reports.

Scheduled checks should inspect only local sanitized trial artifacts and health
signals. They must not generate extra paid model calls, change mode, restart the
proxy, pull/rebase code or publish anything automatically. Notify only on a
meaningful regression, blocker, stage milestone or the end-of-trial report.
Unavailable evidence is a collection gap, not a zero-savings or healthy result.

## Rollback and Submission

On lost context, broken retrieval, cache-lineage violations or cost-limit breaches,
stop new Jev mutations and notify the user. Keep existing CCR data and retrieval
available for sessions that already contain markers; turning Jev off must not
erase their recovery path. Rollback must be tested before active deployment.

At the end of 21 instrumented days, prepare a sanitized report with sample sizes,
quality outcomes, net costs, latency distributions, failure cases and reproducible
commands. Separate synthetic, shadow, simulated and actual active measurements.
The upstream PR remains a draft until readiness gates and maintainer expectations
are met. Ask the user to review the public report and PR before submission; do
not automatically publish private local-development evidence.

## Next Action

Develop the remaining evidence and measurement pieces on the fork's feature
branch. Monitoring remains paused until an instrumented trial is deployed and
its start is recorded. Do not label normal harness traffic as Jev evaluation:
the current planner is wired only into `/v1/compress`, not the normal provider
proxy routes. Route coverage must be implemented and verified before daily
Codex or Claude Code usage can serve as a meaningful Jev trial.
