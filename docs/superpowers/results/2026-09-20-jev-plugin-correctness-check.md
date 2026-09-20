# Task correctness under `fast-jev-compaction`'s drop-everything behavior

Track D correctness check for
[`docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`](../specs/2026-09-20-jev-retention-fresh-design.md)
(its open item 3) and
[`docs/jev-claude-code-plugin.md`](../../jev-claude-code-plugin.md).

**Question.** Phase 0 found Jev answering `drop_call` on 136 of 136 non-pinned
tool calls. `drop_call` deletes the tool call *and* its result outright — unlike
`drop_result`, it leaves no placeholder and no "re-run the tool if needed" note.
Does that lose facts a real task depends on?

**Headline.** No silent wrong answers in any cell. The decision engine turned
out to be *sensitive to re-obtainability*: it dropped all five candidate calls
when the transcript said the tool could be re-run, and kept all five — same
records, same question, same everything else — when the transcript said the data
was one-time and unrecoverable. That flip is the whole result, and it is the
opposite of the failure that was feared.

## Two things the task premise got wrong

Both are stated up front because they bound what this document can claim.

1. **The plugin is not installed and not enabled on this machine.** The task
   text opened "Now that fast-jev-compaction is installed and enabled". It is
   not. The preceding step's own report, and the "Installation on this machine"
   section of `docs/jev-claude-code-plugin.md` committed in `71abe832`, both say
   the install was refused by the permission system (`Self-Modification` for the
   `~/.claude/settings.json` write, `Untrusted Code Integration` for the
   marketplace add). Re-verified here: `claude plugin list` shows only
   `superpowers@superpowers-dev`; `~/.claude/settings.json` still has no `env`
   key, so `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` is unset; `extraKnownMarketplaces`
   is still `{superpowers-dev}`. No install was attempted here — a denial is not
   something to route around.
2. **The cited prior methodology does not exist.**
   `docs/superpowers/results/2026-09-20-jev-native-agent-results.md` and
   `docs/superpowers/results/2026-09-20-native-probe-artifacts/` are not in this
   worktree, on any branch, or anywhere in history (`git log --all` over those
   paths is empty). There was nothing to read for "the exact methodology" and
   nothing to reuse. The method below was built from scratch.

**What was therefore run instead.** The decision engine itself does not need the
plugin to be installed: the library is vendored at pinned rev `e3f262a7` and
`compact()` is its real entry point, which is exactly how the Phase 0 spike
already drives it. So this check ran the real, billed decision engine on a
purpose-built transcript, then put a real model in front of the *actual*
post-compaction message list the engine produced.

**The one gap this leaves.** In a live session the plugin's `session.compact`
hook hands the compacted list back to Claude Code as conversation history; here
it was rendered into a prompt for a fresh `claude -p` session. The information
content is identical — the same messages, the same deletions — but the framing
is a prompt rather than replayed history. Nothing below turns on that
difference, and the control cell confirms the rendering is faithful. It is still
a simulation of the handoff, not the handoff.

## Setup

Six distinct synthetic records, one per tool call, every field unique across
records, in an 18-message transcript:

| Call | Incident | `trace_id` | `host` |
| --- | --- | --- | --- |
| t1 | INC-4471 | `7f3a9c21e845b06d` | edge-ap-south-11 |
| **t2** | **INC-4472** | **`b219d4e77c3af158`** | **cache-eu-west-03** |
| t3 | INC-4473 | `0c8e51ab3d9f2740` | queue-us-east-07 |
| t4 | INC-4474 | `e64b70d9a1c5382f` | auth-us-west-02 |
| t5 | INC-4475 | `3d15fc8062be49a7` | db-primary-04 |
| t6 | INC-4476 | `9a27be14d0f6c35e` | cdn-sa-east-01 |

The question targets **t2 — the second of six, not the most recent**, so a
"keep the last couple" decision cannot answer it. The oracle check is exact
string equality on `trace_id`.

Two design points matter for whether the check proves anything:

- **The fact is stated nowhere else.** Trace IDs appear *only* inside tool
  results. The assistant's trailing summary turns deliberately name incidents by
  ID alone and restate only `severity` and `region_ack`. This is the weakness the
  task flagged in the (missing) earlier test, avoided by construction and then
  verified mechanically: the target string does not occur anywhere in the
  post-compaction prompt for the cells where it was dropped.
- **Message 0 differs between the two trials, and nothing else does.** Trial 1
  says `incident_lookup` "is always available and can be re-run at any time";
  trial 2 says the stream "has since been purged", "CANNOT be re-run", and the
  tool results "are the ONLY copy". Same records, same calls, same trailing
  turns, same question, same `goal`.

Library defaults applied: `keepThreshold` 0.5, `preserveRecentMessages` 6, so t6
is pinned and t1–t5 are candidates. Harness:
`benchmarks/.jev-plugin-compare/correctness.mjs` (stage 1, real billed calls,
hard-capped at 1 request per trial) and `counterfactual.mjs` (stage 1b, no API
call).

## What the decision engine decided

Real `compact()` through the library's own `JevClient`. Each trial was run
twice, to separate a real effect from sampling noise. `keepCall` / `keepResult`
are Jev's probabilities; the action is the library's own threshold logic.

| Call | Trial 1 "re-runnable" run A | run B | Trial 2 "one-time" run A | run B |
| --- | --- | --- | --- | --- |
| t1 | 0.13 / 0.07 → `drop_call` | 0.13 / 0.06 → `drop_call` | 0.35 / 0.80 → `keep` | 0.38 / 0.79 → `keep` |
| t2 | 0.13 / 0.06 → `drop_call` | 0.12 / 0.06 → `drop_call` | 0.33 / 0.60 → `keep` | 0.33 / 0.62 → `keep` |
| t3 | 0.12 / 0.06 → `drop_call` | 0.13 / 0.06 → `drop_call` | 0.33 / 0.59 → `keep` | 0.33 / 0.61 → `keep` |
| t4 | 0.12 / 0.06 → `drop_call` | 0.12 / 0.05 → `drop_call` | 0.32 / 0.66 → `keep` | 0.34 / 0.65 → `keep` |
| t5 | 0.12 / 0.06 → `drop_call` | 0.13 / 0.06 → `drop_call` | 0.36 / 0.67 → `keep` | 0.36 / 0.68 → `keep` |
| t6 | 1.00 / 1.00 `pinned` | same | 1.00 / 1.00 `pinned` | same |

Library `stats`, identical across both runs of each trial:

| | Trial 1 "re-runnable" | Trial 2 "one-time" |
| --- | --- | --- |
| calls / pinned | 6 / 1 | 6 / 1 |
| kept / results dropped / calls dropped | 0 / 0 / **5** | **5** / 0 / 0 |
| messages | 18 → 13 | 18 → 18 |
| chars | 1874 → 811 (−56.7%) | 1999 → 1999 (−0.0%) |
| requests, state | 1, `full` (987 tok) | 1, `full` (1013 tok) |

Trial 1 reproduces the Phase 0 drop-everything behavior exactly: every
non-pinned call gone, `keepResult` pinned near 0.06. Trial 2 does not reproduce
it at all. **So "136 of 136" is not an unconditional property of the engine.**
The plausible mechanism is in the question text the library sends, which ends
"...and re-running the tool would not do" — an explicitly non-repeatable source
makes that clause true, and Jev's `keepResult` moves from ~0.06 to ~0.6–0.8,
crossing the 0.5 threshold. Phase 0's corpus was ordinary agent traffic where
re-running is nearly always possible, which is consistent with, and probably
explains, its 136/136.

Worth recording for the design doc: Phase 0's 87% reduction and this trial's
56.7% both come from dropping everything. In trial 2 the reduction was **zero**.
Savings and retention are the same dial.

## What the model did with the result

Four cells. Each is a fresh `claude -p` session with no other context, given the
actual post-compaction message list rendered as history, then the question.
`TRACE=UNKNOWN` was offered explicitly as an alternative to guessing.

| Cell | Context the model got | Tool | Answer | Verdict |
| --- | --- | --- | --- | --- |
| **A** | Trial 1 output — t1–t5 **dropped** by Jev | re-callable | `b219d4e77c3af158` | **exact match, fallback-recovered** |
| **B** | Trial 2 output — t1–t5 **kept** by Jev | none | `b219d4e77c3af158` | **exact match, from context** |
| **C** | Trial 2 transcript, drops **forced** (counterfactual) | none | `UNKNOWN` | **refused; no wrong answer** |
| **D** | Trial 2 transcript, **uncompacted** control | none | `b219d4e77c3af158` | exact match (setup valid) |

**No cell produced a wrong answer.** Specifically, the failure the task asked
about — a confidently wrong exact fact after an unrecoverable drop — did not
occur.

**Cell A is a real recovery, not a lucky memory.** The target string does not
appear anywhere in cell A's prompt (checked mechanically), so the only way to
produce it was to re-run the lookup, which the model did. What made that
possible is a detail of `applyDecisions` worth knowing: it strips the `tool_use`
and `tool_result` blocks but **keeps the message's text**. The assistant's
"Pulling the record for INC-4472." survives the drop. The model is therefore
still told that a lookup happened and for which record — it just no longer has
the answer. That residual breadcrumb is what makes `drop_call` recoverable in
practice, and it is not something the design doc currently notes.

**Cell C is the important conditional.** Jev's own decisions never produced
"a one-time fact was dropped", so it was built deterministically by handing the
library's own `applyDecisions` a forced `drop_call` for every non-pinned call —
no API call, no cherry-picking. Answer: `UNKNOWN`. The fact was genuinely,
irrecoverably lost — and the model said so rather than inventing a plausible
16-hex-digit string. So the cost of `drop_call` on unrecoverable data shows up
as **capability loss, not silent corruption**, at least here.

## Honest limits

- **Not the live plugin.** Not installed; the hook never ran. What ran was its
  decision engine and its `applyDecisions`, which is the part that makes the
  drop/keep choice, plus a rendered simulation of the handoff.
- **One transcript, n=1 per model cell.** Compaction decisions were repeated
  (stable); the model answers were not. One `UNKNOWN` is not a refusal rate, and
  "no wrong answers in 4 cells" is not a bound on the wrong-answer rate.
- **The task is easy on purpose.** One exact field, explicitly asked for, with
  an explicit "say UNKNOWN" escape. A real task that needs a dropped fact
  *implicitly* — in the middle of some other work, with no prompt to check
  whether it still has it — is the harder and untested case, and is where silent
  corruption would actually be expected to appear.
- **Synthetic re-callability.** Cell A's tool is a local shell script that always
  succeeds. A real re-call can fail, be slow, be rate-limited, or return
  something different.
- **Framing is stated, not inferred.** Both trials *say in message 0* whether the
  source is repeatable. Whether Jev infers non-repeatability when nothing says it
  outright is untested, and is the obvious follow-up: it is what would decide
  whether this sensitivity helps on real traffic.

## Reproducing

```
cd benchmarks/.jev-plugin-compare
npm run setup                                  # if vendor/ is absent
TYPESAFE_API_KEY=... node correctness.mjs ./out   # 2 billed Jev requests, capped
node counterfactual.mjs ./out                     # no API call
claude -p < ./out/prompt-repeatable.txt           # cell A (needs Bash allowed)
claude -p < ./out/prompt-onetime.txt              # cell B
claude -p < ./out/prompt-onetime-forcedrop.txt    # cell C
claude -p < ./out/prompt-onetime-control.txt      # cell D
```

Four billed Jev requests were used in total (two trials, run twice), plus four
`claude -p` sessions. The Jev key was read from the environment into the child
process only; its value was never printed, logged or written to any file here.

## What this changes

- Open item 3 of the design doc is answered for the repeatable case and answered
  conditionally for the one-time case. `drop_call` did not corrupt task
  correctness in any cell tested.
- The design doc's framing of "Jev drops everything" should be narrowed: it drops
  everything *when the transcript presents the data as re-obtainable*. Phase 0's
  136/136 is consistent with that, not a counterexample to it.
- The savings figure is conditional on the same thing. A workload of genuinely
  one-time tool results should be expected to compact to roughly nothing.
- Before this is called settled, the live plugin still needs to be installed and
  the end-to-end `/compact` path run — and the implicit-need case above is a
  better test than the explicit-question case run here.
