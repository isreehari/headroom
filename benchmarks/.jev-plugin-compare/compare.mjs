/**
 * Phase 0 one-off comparison spike: fast-jev-compaction with NO Headroom.
 *
 * ONE-OFF RESEARCH SPIKE -- NOT PRODUCTION CODE. Part of Phase 0 of
 * `docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md`, and a
 * companion to `benchmarks/jev_savings_spike.py`.
 *
 * **WARNING: this makes REAL, BILLED Jev calls** -- through
 * fast-jev-compaction's own `JevClient`, i.e. the library's real compaction
 * entry point (`compactMessages`), not a re-implementation. The key is read by
 * the library from `TYPESAFE_API_KEY`; this script never reads, prints or logs
 * its value. Calls are hard-capped (see MAX_REQUESTS_PER_SCENARIO) and run once
 * per scenario over the same 6-scenario corpus the Python spike uses. Each call
 * is also bounded in wall-clock time (see DEFAULT_REQUEST_TIMEOUT_MS), because a
 * call cap bounds spend but not a hang; run.sh adds an outer timeout on top.
 *
 * What it does
 * ------------
 * 1. Loads the RAW (pre-Headroom, T0) message lists exported by
 *    `benchmarks/jev_plugin_compare_export.py export`.
 * 2. Adapts them into fast-jev-compaction's `Message` shape (see ADAPTATION
 *    NOTES below -- the mapping is near-lossless but not entirely).
 * 3. Calls `compactMessages` for real, once per scenario.
 * 4. Maps the library's ACTUAL output message list back into the original
 *    OpenAI / Anthropic wire shape, so the result can be token-counted by the
 *    same Python tokenizer that produced T0/TH/TP. `result.stats` reports
 *    characters and a tokenizer-free estimate only, which would not be
 *    comparable.
 * 5. Shells out to `jev_plugin_compare_export.py count` for TC and writes
 *    `{TC, meta, notes}` JSON for the Python report step.
 *
 * ADAPTATION NOTES (lossy assumptions, stated plainly)
 * ----------------------------------------------------
 * fast-jev-compaction's `Message` is `{role: 'user'|'assistant', text,
 * toolUses, toolResults?}` -- a subset of Claude Code's `SessionMessage`.
 * Mapping our corpus into it needed exactly four assumptions:
 *
 * (a) ROLE `system` HAS NO HOME. `Role` is only 'user' | 'assistant', so each
 *     scenario's leading system prompt is carried as a `user` message with its
 *     text verbatim. It is message index 0, which the library always pins, so
 *     this never changes a decision -- but it does mean Jev sees the system
 *     prompt labelled `user` in the state. Text is preserved byte for byte and
 *     the system role is restored on the way back out, so T0 and TC count the
 *     exact same bytes for it.
 * (b) OPENAI TOOL ARGUMENTS ARE A JSON *STRING*; the library's `ToolUse.input`
 *     is a `Record<string, unknown>`. We `JSON.parse` the arguments (falling
 *     back to `{__raw: <string>}` if it does not parse). This only affects what
 *     Jev is shown in the state; the original string is what gets restored and
 *     counted.
 * (c) TOOL RESULT CONTENT IS COERCED TO A STRING. `ToolResult.text` is a
 *     string; our corpus already stores tool results as JSON strings, so this
 *     is an identity mapping for every scenario here. A non-string would be
 *     `JSON.stringify`d (and the note recorded).
 * (d) OPENAI ROLE `tool` IS ALSO CARRIED AS `user`. Same root cause as (a) --
 *     the `Role` union is only 'user' | 'assistant' -- but worth stating
 *     separately because it is not a one-off on message 0: in the OpenAI-shaped
 *     scenarios *every* `role:"tool"` message becomes a `role:"user"` message
 *     whose only payload is a `toolResults` entry (text is ''). This is the
 *     shape the library itself expects (Claude Code threads a tool result back
 *     as a user-turn `toolResults` entry, which is why `ToolResult` hangs off
 *     `Message` rather than being its own role), so it is the honest mapping
 *     rather than a distortion -- but it does mean Jev is shown these turns
 *     labelled `user`, and that is a real change in message provenance. It does
 *     not touch TC: the reverse mapping restores `role:"tool"` with the original
 *     `tool_call_id` and body, and the keep-everything `--dry-run` proves the
 *     round trip is byte-exact on all six scenarios. What it can influence is
 *     the *decision* Jev makes, so treat it as a comparison limitation.
 *
 * Nothing else is lossy: message order, message count, all user/assistant text,
 * every tool-call id and every tool-result body round-trip unchanged. The
 * reverse mapping is driven by the library's real output (which tool ids
 * survived, and the exact truncated bodies it produced), not by re-deriving
 * decisions.
 */

import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { compact, JevClient } from './vendor/fast-jev-compaction/dist/index.js';

/**
 * Hard cap on real Jev HTTP requests per scenario. `compact()` splits questions
 * into as many requests as needed to stay under `maxRequestTokens`; this
 * wrapper refuses to let a scenario quietly fan out into a dozen billed calls.
 */
const MAX_REQUESTS_PER_SCENARIO = 1;

/** Global cap across the whole run, matching the Python spike's 6-scenario cap. */
const MAX_REQUESTS_TOTAL = 6;

/**
 * Wall-clock bound on a single real Jev HTTP request. The call caps above bound
 * how much can be billed, not how long one request may hang: Node's `fetch` has
 * no default timeout, so a connection Jev accepts but never answers would wedge
 * the whole run. Overridable with `--request-timeout-ms`. run.sh layers an outer
 * timeout over the process as a backstop.
 */
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000;

const notes = [];
let totalRequests = 0;

function note(message) {
  if (!notes.includes(message)) notes.push(message);
}

// --- forward adapter: our corpus -> fast-jev-compaction Message[] ------------

function parseArgs(raw) {
  if (typeof raw !== 'string') return raw && typeof raw === 'object' ? raw : {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : { __raw: raw };
  } catch {
    note('adaptation (b): a tool-call `arguments` string did not parse as a JSON object; carried as {__raw}');
    return { __raw: raw };
  }
}

function asText(value) {
  if (typeof value === 'string') return value;
  note('adaptation (c): a tool result body was not a string and was JSON.stringify`d');
  return JSON.stringify(value);
}

/**
 * Adapt one scenario. Returns `{ messages, index }` where `messages[k].__i` is
 * the originating index in the raw list. `__i` survives untouched messages
 * (the library returns those as the very same object); rebuilt messages lose it
 * but always carry a tool id, which `index` maps back.
 */
function adapt(scenario) {
  const out = [];
  const callOwner = new Map(); // tool_use_id -> raw index of the message holding the call
  const resultOwner = new Map(); // tool_use_id -> raw index of the message holding the result

  scenario.messages.forEach((msg, i) => {
    const role = msg.role;
    const content = msg.content;

    // ---- Anthropic shape: content is a list of blocks ----
    if (Array.isArray(content)) {
      const texts = [];
      const toolUses = [];
      const toolResults = [];
      for (const block of content) {
        if (!block || typeof block !== 'object') continue;
        if (block.type === 'text') texts.push(block.text ?? '');
        else if (block.type === 'tool_use') {
          toolUses.push({ tool_use_id: block.id, tool: block.name, input: block.input ?? {} });
          callOwner.set(block.id, i);
        } else if (block.type === 'tool_result') {
          toolResults.push({ tool_use_id: block.tool_use_id, text: asText(block.content ?? '') });
          resultOwner.set(block.tool_use_id, i);
        }
      }
      const m = {
        role: role === 'assistant' ? 'assistant' : 'user',
        text: texts.join('\n'),
        toolUses,
        __i: i,
      };
      if (toolResults.length > 0) m.toolResults = toolResults;
      out.push(m);
      return;
    }

    // ---- OpenAI chat-completions shape ----
    if (role === 'tool') {
      const id = msg.tool_call_id;
      resultOwner.set(id, i);
      note(
        'adaptation (d): OpenAI role "tool" messages are carried as role "user" messages holding only a toolResults entry (the library\'s Role union has no "tool"); role and body are restored exactly on the way out, so TC is unaffected, but Jev sees these turns labelled "user"',
      );
      out.push({
        role: 'user',
        text: '',
        toolUses: [],
        toolResults: [{ tool_use_id: id, text: asText(content ?? '') }],
        __i: i,
      });
      return;
    }

    const toolUses = (msg.tool_calls ?? []).map((tc) => {
      callOwner.set(tc.id, i);
      return {
        tool_use_id: tc.id,
        tool: tc.function?.name ?? 'unknown',
        input: parseArgs(tc.function?.arguments),
      };
    });

    if (role === 'system') {
      note('adaptation (a): role "system" has no slot in fast-jev-compaction\'s Role union; carried as "user" (always message 0, always pinned)');
    }

    out.push({
      role: role === 'assistant' ? 'assistant' : 'user',
      text: typeof content === 'string' ? content : content == null ? '' : JSON.stringify(content),
      toolUses,
      __i: i,
    });
  });

  return { messages: out, callOwner, resultOwner };
}

// --- reverse adapter: library output -> original wire shape ------------------

/**
 * Read the library's real output list and derive, per raw message index,
 * whether it survived; plus, per tool id, whether the call survived and what
 * the (possibly truncated) result body now is.
 */
function readOutput(result, adapted) {
  const keptRawIndices = new Set();
  const survivingCalls = new Set();
  const survivingResults = new Map(); // tool_use_id -> text
  let unmatched = 0;

  for (const m of result.messages) {
    let raw = typeof m.__i === 'number' ? m.__i : undefined;
    for (const tool of m.toolUses ?? []) {
      survivingCalls.add(tool.tool_use_id);
      if (raw === undefined) raw = adapted.callOwner.get(tool.tool_use_id);
    }
    for (const res of m.toolResults ?? []) {
      survivingResults.set(res.tool_use_id, res.text);
      if (raw === undefined) raw = adapted.resultOwner.get(res.tool_use_id);
    }
    if (raw === undefined) unmatched += 1;
    else keptRawIndices.add(raw);
  }
  if (unmatched > 0) {
    note(`reverse mapping: ${unmatched} output message(s) carried neither the original index nor a tool id and were dropped from the recount`);
  }
  return { keptRawIndices, survivingCalls, survivingResults };
}

/** Rebuild the original OpenAI/Anthropic-shaped list from the library's output. */
function restore(scenario, view) {
  const { keptRawIndices, survivingCalls, survivingResults } = view;
  const out = [];

  scenario.messages.forEach((msg, i) => {
    const content = msg.content;

    if (Array.isArray(content)) {
      const blocks = [];
      let hadTool = false;
      for (const block of content) {
        if (!block || typeof block !== 'object') {
          blocks.push(block);
          continue;
        }
        if (block.type === 'tool_use') {
          hadTool = true;
          if (survivingCalls.has(block.id)) blocks.push(block);
        } else if (block.type === 'tool_result') {
          hadTool = true;
          if (survivingResults.has(block.tool_use_id)) {
            const text = survivingResults.get(block.tool_use_id);
            blocks.push(text === block.content ? block : { ...block, content: text });
          }
        } else {
          blocks.push(block);
        }
      }
      if (!hadTool) {
        if (keptRawIndices.has(i)) out.push(msg);
      } else if (blocks.length > 0) {
        out.push(blocks.length === content.length ? msg : { ...msg, content: blocks });
      }
      return;
    }

    if (msg.role === 'tool') {
      if (!survivingResults.has(msg.tool_call_id)) return;
      const text = survivingResults.get(msg.tool_call_id);
      out.push(text === content ? msg : { ...msg, content: text });
      return;
    }

    const calls = msg.tool_calls;
    if (Array.isArray(calls) && calls.length > 0) {
      const keptCalls = calls.filter((tc) => survivingCalls.has(tc.id));
      if (keptCalls.length === 0) {
        // Mirrors the library: a message left with no text and no calls is gone.
        if (typeof content === 'string' && content.trim().length > 0) {
          out.push({ ...msg, tool_calls: [] });
        }
        return;
      }
      out.push(keptCalls.length === calls.length ? msg : { ...msg, tool_calls: keptCalls });
      return;
    }

    if (keptRawIndices.has(i)) out.push(msg);
  });

  return out;
}

// --- request-capping asker ---------------------------------------------------

/**
 * Wraps the library's real `JevClient` (real HTTP, real spend) and bounds it two
 * ways: it refuses to exceed the per-scenario / global call caps, and it injects
 * a `fetch` carrying an abort signal so no single request can hang forever.
 * `JevClientOptions.fetch` is the library's own injection point, so this is
 * still the library's real request path -- only the socket lifetime is ours.
 * Nothing about the request or the key is inspected or logged.
 */
class CappedClient {
  constructor(scenarioName, timeoutMs) {
    this.scenario = scenarioName;
    this.timeoutMs = timeoutMs;
    this.used = 0;
    this.inner = new JevClient({
      fetch: (input, init = {}) =>
        // `init` never carries a signal from the library, so nothing is dropped.
        fetch(input, { ...init, signal: AbortSignal.timeout(timeoutMs) }),
    });
  }
  async ask(state, questions) {
    if (this.used >= MAX_REQUESTS_PER_SCENARIO) {
      throw new Error(
        `[${this.scenario}] request cap reached (${MAX_REQUESTS_PER_SCENARIO}/scenario) -- refusing extra billed Jev calls`,
      );
    }
    if (totalRequests >= MAX_REQUESTS_TOTAL) {
      throw new Error(`global request cap reached (${MAX_REQUESTS_TOTAL}) -- refusing extra billed Jev calls`);
    }
    this.used += 1;
    totalRequests += 1;
    try {
      return await this.inner.ask(state, questions);
    } catch (err) {
      if (err && (err.name === 'TimeoutError' || err.name === 'AbortError')) {
        throw new Error(
          `[${this.scenario}] Jev request exceeded --request-timeout-ms=${this.timeoutMs} and was aborted`,
        );
      }
      throw err;
    }
  }
}

// --- main --------------------------------------------------------------------

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

async function main() {
  const exportPath = arg('export');
  const outPath = arg('out');
  const pythonBin = arg('python', 'python3');
  const exportHelper = arg('helper');
  const dryRun = process.argv.includes('--dry-run');
  // The library's own default. Verified by `--dry-run` on this corpus: every
  // scenario's state fits at the 'full' stage (max ~6.1k tokens) and every
  // scenario needs exactly one request, so no plugin default is bent to honour
  // the "at most 6 billed calls" cap -- the cap simply is not reached.
  const maxStateTokens = Number(arg('max-state-tokens', '25000'));
  const requestTimeoutMs = Number(arg('request-timeout-ms', String(DEFAULT_REQUEST_TIMEOUT_MS)));

  if (!exportPath || !outPath || !exportHelper) {
    console.error(
      'usage: node compare.mjs --export <corpus.json> --out <tc.json> --helper <jev_plugin_compare_export.py>\n' +
        '                       [--python uv-python] [--max-state-tokens N] [--request-timeout-ms N] [--dry-run]\n' +
        '\n' +
        'All three of --export/--out/--helper are required in --dry-run too: a dry run still\n' +
        'exercises the real adapt/compact/restore path and the real token-count bridge, it only\n' +
        'swaps the billed Jev call for a keep-everything asker. Use `npm run dry-run` (which\n' +
        'drives run.sh --dry-run) to get all three wired up for free.',
    );
    return 2;
  }
  if (!Number.isFinite(requestTimeoutMs) || requestTimeoutMs <= 0) {
    console.error(`ERROR: --request-timeout-ms must be a positive number, got ${arg('request-timeout-ms')}`);
    return 2;
  }
  if (!dryRun && !process.env.TYPESAFE_API_KEY) {
    console.error('ERROR: TYPESAFE_API_KEY is not set. This spike only measures anything by calling the real Jev API; refusing to run.');
    return 2;
  }

  const corpus = JSON.parse(readFileSync(exportPath, 'utf8'));
  console.error(
    `fast-jev-compaction comparison spike -- ONE-OFF, REAL BILLED JEV CALLS${dryRun ? ' (DRY RUN: no calls)' : ''}`,
  );
  console.error(
    `scenarios=${corpus.scenarios.length}  maxStateTokens=${maxStateTokens}  ` +
      `caps=${MAX_REQUESTS_PER_SCENARIO}/scenario, ${MAX_REQUESTS_TOTAL} total  ` +
      `requestTimeout=${requestTimeoutMs}ms`,
  );

  const restored = {};
  const meta = {};

  for (const scenario of corpus.scenarios) {
    const adapted = adapt(scenario);
    const input = adapted.messages;
    const asker = dryRun
      ? {
          // Dry run: keep everything. Exercises fitState/batchCalls/applyDecisions
          // and reports request counts without spending a cent.
          ask: async (_state, questions) => {
            totalRequests += 1;
            return { answers: Object.fromEntries(Object.keys(questions).map((k) => [k, { noul: 1 }])) };
          },
        }
      : new CappedClient(scenario.name, requestTimeoutMs);

    let result;
    try {
      // `compact(messages, asker, options)` is the library's real entry point;
      // `compactMessages` is only `compact` + a `JevClient` it builds itself.
      // We pass the asker explicitly so the real client sits behind the call
      // cap -- same library code path, same real HTTP, bounded spend.
      result = await compact(input, asker, { maxStateTokens });
    } catch (err) {
      console.error(`  [${scenario.name}] ERROR: ${String(err && err.message ? err.message : err)}`);
      meta[scenario.name] = { error: String(err && err.message ? err.message : err) };
      continue;
    }

    const view = readOutput(result, adapted);
    restored[scenario.name] = restore(scenario, view);
    meta[scenario.name] = {
      stats: result.stats,
      restored_message_count: restored[scenario.name].length,
      decisions: result.decisions.map((d) => ({
        id: d.id,
        tool: d.tool,
        action: d.action,
        reason: d.reason,
        keepCall: Number(d.keepCall.toFixed(4)),
        keepResult: Number(d.keepResult.toFixed(4)),
      })),
    };
    const s = result.stats;
    console.error(
      `  [${scenario.name}] msgs ${s.messagesBefore}->${s.messagesAfter}  chars ${s.charsBefore.toLocaleString()}->${s.charsAfter.toLocaleString()}  ` +
        `calls=${s.calls} kept=${s.kept} resultsDropped=${s.resultsDropped} callsDropped=${s.callsDropped} pinned=${s.pinned}  ` +
        `state=${s.stateTokens}tok/${s.stateStage} requests=${s.requests} ${s.ms}ms  restored=${restored[scenario.name].length} msgs`,
    );
  }

  // --- TC via the SAME Python tokenizer (subprocess bridge) ---
  const dir = mkdtempSync(join(tmpdir(), 'fjc-compare-'));
  const bridgeIn = join(dir, 'tc-in.json');
  writeFileSync(bridgeIn, JSON.stringify({ scenarios: restored }));
  const proc = spawnSync(pythonBin, [exportHelper, 'count', '--in', bridgeIn], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024 * 512,
  });
  if (proc.status !== 0) {
    console.error(`token-count bridge failed (exit ${proc.status}):\n${proc.stderr}`);
    return 1;
  }
  const TC = JSON.parse(proc.stdout.trim().split('\n').pop());

  writeFileSync(outPath, JSON.stringify({ TC, meta, notes }, null, 2));
  console.error(
    `\nwrote ${outPath}; ${totalRequests} ${dryRun ? 'SIMULATED (unbilled) request(s) made' : 'real Jev request(s) made'}`,
  );
  if (notes.length > 0) {
    console.error('adaptation notes:');
    for (const n of notes) console.error(`  - ${n}`);
  }
  return 0;
}

main().then((code) => process.exit(code));
