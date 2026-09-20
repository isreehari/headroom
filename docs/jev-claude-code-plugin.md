# Jev retention in Claude Code: the `fast-jev-compaction` plugin

Track D of
[`docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`](superpowers/specs/2026-09-20-jev-retention-fresh-design.md)
(see its "Track D: Claude Code — `fast-jev-compaction` Plugin" section).

Claude Code gets Jev-guided retention from a third-party Claude Code plugin,
[`tamaratran/fast-jev-compaction`](https://github.com/tamaratran/fast-jev-compaction),
used as-is and not forked. This is not Headroom code and there is no
Headroom-side equivalent planned for Claude Code.

## Why a plugin instead of a Headroom feature

The decision hinges on where the compaction boundary is visible.

**Claude Code owns a `/compact`-lifecycle function hook that Headroom cannot
reach.** Compaction in Claude Code is a client-side event: the client decides
to compact, assembles the transcript it is about to replace, and swaps in the
result. On the wire Headroom sees only the ordinary passthrough request that
comes *after* the swap — by then the discarded turns are already gone and the
substitution has been made locally. A proxy in front of ordinary passthrough
traffic has no way to intervene at that moment. A Claude Code plugin does: the
`session.compact` function hook runs inside the client, is handed the
transcript, and returns the replacement. That is a boundary only a plugin can
stand on, so building this in Headroom would not be a different implementation
of the same thing — it would be unreachable.

**Codex has no equivalent hook**, so this is genuinely Claude-Code-only rather
than a stopgap we would later generalize. Confirmed by reading this machine's
`~/.codex/hooks.json`, whose event list is exactly:

```
SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest,
PostToolUse, SubagentStart, SubagentStop, Stop
```

There is no compaction event. Codex's compaction boundary is reachable only at
the wire level, over WebSocket, which is why Track C of the design doc builds
that path in Headroom's own Codex provider instead of as a plugin. Forking
`fast-jev-compaction` to cover Codex was considered and rejected for the same
reason: a fork would still need Headroom's wire-level access to see anything.

**The plugin and Headroom are complementary, not competing.** Headroom
compresses every turn, continuously, with CCR-backed retrievable compression.
The plugin only acts at discrete compaction events. Running both is the
intended configuration.

## Installation on this machine

**Status: not yet applied.** The install is a machine-level Claude Code
settings change, and every step was refused by this session's permission
system — `Self-Modification` for the settings write, `Untrusted Code
Integration` for the marketplace registration. `~/.claude/settings.json` is
byte-for-byte unchanged (verified against a copy taken before the attempt), no
marketplace was added, and no plugin was installed or enabled. Only
`superpowers@superpowers-dev` remains installed, from the `superpowers-dev`
marketplace. Update this section once the steps below have actually been run.

The steps, verified against the plugin's own `README.md` and
`hooks/README.md` at the pinned revision `e3f262a7` (vendored under
`benchmarks/.jev-plugin-compare/vendor/fast-jev-compaction` by the Phase 0
comparison spike):

1. Add the function-hooks opt-in and the TypeSafe key to `~/.claude/settings.json`.
   Function hooks are an early-access Claude Code feature requiring 2.1.274 or
   newer; this machine runs 2.1.278.

   ```json
   {
     "env": {
       "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
       "TYPESAFE_API_KEY": "<the same key already exported as HEADROOM_JEV_API_KEY>"
     }
   }
   ```

   This is an additive edit — `settings.json` currently has no `env` block, and
   nothing else in it should change. The key is the same TypeSafe/Jev
   credential Headroom's own Jev tracks already use; never commit it or write
   it into a source file.

2. Register the marketplace and install the plugin:

   ```sh
   claude plugin marketplace add tamaratran/fast-jev-compaction
   claude plugin install fast-jev-compaction@fast-jev-compaction
   ```

   The install prompts for the plugin's `userConfig` options (`apiKey`,
   `keepThreshold`, `preserveRecentMessages`, `compactAtPercent`,
   `minReductionRatio`, `maxStateTokens`, `maxRequestTokens`,
   `truncateHeadChars`, `model`). Leaving `apiKey` unset makes the hook fall
   back to `TYPESAFE_API_KEY` from the environment, which is what step 1 sets.

3. Restart Claude Code or run `/reload-plugins`, then verify:

   ```sh
   claude plugin list            # expect fast-jev-compaction@fast-jev-compaction, enabled
   claude plugin marketplace list
   ```

   In-session, a `/compact` should produce a toast. The text the hook itself
   passes to `$.ui.toast()` is `kept N/M messages, no summary (…)`, or
   `fallback to built-in summary (…)` when Jev could not remove enough —
   unprefixed. Whether Claude Code's plugin runtime prepends the plugin name
   when it renders the toast has not been checked, so expect the wording above
   with or without a `fast-jev-compaction:` prefix.

   "Could not remove enough" is the `minReductionRatio` option from step 2, and
   its default of `0.25` is worth understanding before relying on the plugin:
   when Jev's decisions reduce the transcript by less than 25%, the hook
   **discards them and calls Claude Code's built-in summarizer instead**
   (`return next(event)`). Since Jev keeping everything produces 0% reduction,
   a transcript Jev judges entirely worth keeping is compacted by the built-in
   path, not by this plugin. See
   [the Track D correctness check](superpowers/results/2026-09-20-jev-plugin-correctness-check.md#the-minreductionratio-gate),
   where that is exactly what happened to the one-time trial.

To try it without installing, from a checkout:
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir .`

## Known caveat: the 87% number is deletion, not compression

The Phase 0 comparison spike measured `fast-jev-compaction` alone against
Headroom on the same six-scenario corpus with the same tokenizer and the same
`T0` baseline (with the denominator caveat below):

| | Headroom alone | Headroom + Jev (track B) | fast-jev-compaction alone |
|---|---:|---:|---:|
| Reduction off `T0` | 29.85% | 40.45% | 87.27% |

The 87% is real but it is not the same kind of result as the other two
columns. It comes from Jev answering `drop_call` on **136 of 136** non-pinned
tool calls across all five agentic scenarios — permanent deletion, recoverable
only by re-running the tool — versus Headroom's CCR-backed, retrievable
compression. On a synthetic corpus, "everything looks equally disposable" is a
decision-quality question, not a free win; it is the mirror image of the
"always keep" bias the original plan warned about.

Two qualifications on the comparison itself, both from the harness source
(`benchmarks/.jev-plugin-compare/compare.mjs`,
`benchmarks/jev_plugin_compare_export.py`). The `fast-jev-compaction` percentage
is computed only over the scenarios that produced a value, so its baseline is
not necessarily the same `T0` total as the Headroom column's — the export step
prints a warning when those baselines differ. And the corpus is adapted before
Jev sees it: `system` and OpenAI `tool` messages are carried as `user` (the
library's `Role` union is only `user | assistant`) and OpenAI tool arguments are
`JSON.parse`d out of their string form. Token counts round-trip byte-exactly, so
the reduction figures are comparable; the *decisions* were made on input shaped
slightly differently from what native Claude Code would hand the hook.

This does not change the recommended architecture. Per the design doc's Track D
gate, a real task-correctness check has since been run —
[`superpowers/results/2026-09-20-jev-plugin-correctness-check.md`](superpowers/results/2026-09-20-jev-plugin-correctness-check.md).
Summary: the drop-everything behavior is **conditional on the transcript
presenting the data as re-obtainable**, and in the repeatable case (the one the
live hook actually runs, since it clears `minReductionRatio`) exact-field
recovery held with no wrong answers. The one-time case is *not* settled: it
reduces nothing, so the gate hands it to the built-in summarizer, which that
check did not test. Read its "Honest limits" before treating any of this as
closing the gate.

Two smaller caveats from the plugin's own docs: function hooks are early
access and may change between Claude Code releases (the plugin's type
declarations are generated from 2.1.274 and should be regenerated after an
upgrade), and its token sizes are character-count estimates rather than real
tokenizer counts.
