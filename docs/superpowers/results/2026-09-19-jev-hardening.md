# Jev Hardening Run Record

Date: 2026-09-19. Branch: `codex/jev-proactive-retention`.
Base HEAD: `67eb910e38b9879bb0bcbacdc36db69e36901075`, plus uncommitted work.
This is not a released or deployed build. Existing staged changes were retained.

## Scope

- `metadata-keep-v2`: batched, field-specific questions with unknown output status;
  unseen output is preserved in shadow and active modes.
- Conservative serialized request budgeting and a planning deadline.
- Bounded process-local cooldown, retry deduplication and eligible-revision
  freshness checks. These do not survive restarts or coordinate workers.
- API usage/model preservation, nullable unknown usage, failure-safe savings,
  and separate skipped-call versus attempted-call latency reporting.
- Endpoint failure and usage reporting; benchmark accounting and documentation.

No API credentials, persistent environment or running proxy were intentionally
changed. No live Jev or target-model benchmark was run for this update. Unit tests
used mocked HTTP responses. This update does not establish production readiness.

## Verification

Focused verification command:

```bash
.venv/bin/python -m pytest \
  tests/test_jev.py tests/test_jev_hardening.py tests/test_jev_benchmark.py \
  tests/test_proxy_compress_endpoint.py tests/test_compress_session_mode.py \
  tests/test_compression_store.py tests/test_compression_cache.py \
  tests/test_compression_cache_registry.py -m 'not live' -q --tb=short
```

The initial 19 hardening cases were observed failing before their implementation,
then passing. Four review regressions were also observed failing and passing:
stale decisions, endpoint usage on valid/invalid decisions, and skipped benchmark
latency. A further deadline regression confirmed that expiry before dispatch
does not count a remote attempt. Additional endpoint tests cover storage failure
reporting and rejection of a growing active result.

Final focused verification: **251 passed, 1 live test deselected**, exit 0 in
30.14 seconds. One Starlette/httpx deprecation warning was emitted. Ruff checks
passed for the changed Python files, and `git diff --check` found no whitespace
errors. The focused test log is `/tmp/headroom-jev-hardening-focused.log`.

Full-suite attempt:

```bash
.venv/bin/python -m pytest -m 'not live and not real_llm and not slow' --maxfail=1
```

This was interrupted while
`tests/test_cli/test_wrap_codex.py::test_wrap_codex_prepare_only_creates_backup_and_config`
was waiting on Serena project pre-indexing. The interrupted command reported
one failure (exit 143 inside the wrapper), 2,102 passed, 111 skipped and 18
deselected after about 326 seconds. This is an incomplete full-suite run, not a
green suite and not evidence of a Jev functional regression. Its log is at
`/tmp/headroom-jev-hardening-suite.log` on the development machine.

## Offline Proof Table

```bash
HEADROOM_JEV_MODE=off HEADROOM_BEACON=off .venv/bin/python \
  benchmarks/jev_proof_table.py --seed 20260902 \
  --json-output docs/superpowers/results/2026-09-19-jev-hardening-offline.json
```

[Recorded output](2026-09-19-jev-hardening-offline.json) uses the `gpt-5.6`
provider tokenizer. Total input: 178,024 tokens; after Headroom: 104,261; local
savings: 73,763. Jev was disabled: zero calls and zero incremental savings;
API usage and latency are null. These numbers validate the local baseline and
report formatting, not Jev quality or billed savings.

The historical 59,390-token Jev projection used the earlier permissive policy.
It is not a result of the hardened policy or proof of preserved task accuracy.

## Review and Remaining Work

An independent read-only review identified dropped endpoint usage/model fields,
timing of skipped benchmark calls, and late decision accounting. Regression tests
and fixes were added for all three within the process-local boundary.

Durable CCR acknowledgement/recovery, branch-aware shared session ownership,
cross-worker lineage, live quality evaluation, connection reuse, pricing and
dashboard rendering remain outstanding. The model must receive adequate,
explicitly approved evidence before a removal policy can be evaluated. The
current keep-only policy is intentionally not a token-saving Jev implementation.
