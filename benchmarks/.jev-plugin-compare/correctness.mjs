/**
 * Track D correctness check: does fast-jev-compaction's `drop_call` decision
 * lose facts a real task depends on?
 *
 * ONE-OFF RESEARCH SPIKE -- NOT PRODUCTION CODE.
 *
 * WARNING: makes REAL, BILLED Jev calls through the vendored library's own
 * `JevClient` + `compact()` (its real entry point, not a re-implementation).
 * Hard-capped at 1 request per trial, 2 total. The key is read by the library
 * from TYPESAFE_API_KEY; this script never reads, prints or logs its value.
 *
 * Stage 1 (this script): build a transcript holding 6 DISTINCT synthetic
 * records, run the real compaction, record every decision, and render the
 * ACTUAL post-compaction transcript into a prompt for stage 2.
 * Stage 2 (run.sh / by hand): put a real model in front of that prompt and
 * check its answer against the oracle by exact string equality.
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { compact, JevClient } from './vendor/fast-jev-compaction/dist/index.js';

const OUT = process.argv[2];
if (!OUT) { console.error('usage: correctness.mjs <outdir>'); process.exit(2); }
mkdirSync(OUT, { recursive: true });

const MAX_REQUESTS_PER_TRIAL = 1;
const MAX_REQUESTS_TOTAL = 2;
let totalRequests = 0;

/** The oracle. Six distinct records; every field is unique across records. */
const RECORDS = [
  { id: 'INC-4471', trace_id: '7f3a9c21e845b06d', host: 'edge-ap-south-11',  message: 'TLS handshake aborted by peer' },
  { id: 'INC-4472', trace_id: 'b219d4e77c3af158', host: 'cache-eu-west-03',  message: 'eviction storm: 94% of keys purged' },
  { id: 'INC-4473', trace_id: '0c8e51ab3d9f2740', host: 'queue-us-east-07',  message: 'consumer lag reached 1.2M messages' },
  { id: 'INC-4474', trace_id: 'e64b70d9a1c5382f', host: 'auth-us-west-02',   message: 'token signing key rotated mid-flight' },
  { id: 'INC-4475', trace_id: '3d15fc8062be49a7', host: 'db-primary-04',     message: 'replication slot dropped' },
  { id: 'INC-4476', trace_id: '9a27be14d0f6c35e', host: 'cdn-sa-east-01',    message: 'origin shield returned 502' },
];

/** The question targets an OLD record (the 2nd of 6), not the most recent. */
const TARGET = RECORDS[1];

const CONTEXT = {
  repeatable:
    'You are triaging the overnight incident batch. The `incident_lookup` tool ' +
    'queries the live incident database: it is always available and can be ' +
    're-run at any time for any incident id, returning the same record.',
  onetime:
    'You are triaging the overnight incident batch. The records below were ' +
    'drained from a one-time ephemeral export stream that has since been purged ' +
    'upstream. `incident_lookup` CANNOT be re-run -- the stream is gone and there ' +
    'is no other copy of these records anywhere. The tool results in this ' +
    'conversation are the ONLY copy of this data.',
};

/** Builds the 18-message transcript. Trace IDs appear ONLY inside tool results. */
function buildTranscript(mode) {
  const messages = [];
  // index 0 -- always pinned by the library.
  messages.push({ role: 'user', text: CONTEXT[mode], toolUses: [] });

  RECORDS.forEach((rec, i) => {
    const tuid = `toolu_${String(i + 1).padStart(2, '0')}`;
    messages.push({
      role: 'assistant',
      text: `Pulling the record for ${rec.id}.`,
      toolUses: [{ tool_use_id: tuid, tool: 'incident_lookup', input: { incident_id: rec.id } }],
    });
    messages.push({
      role: 'user',
      text: '',
      toolUses: [],
      toolResults: [{
        tool_use_id: tuid,
        text: JSON.stringify({
          incident_id: rec.id, trace_id: rec.trace_id, host: rec.host,
          message: rec.message, severity: 'sev2', region_ack: true,
        }, null, 2),
      }],
    });
  });

  // Trailing turns. These deliberately name incidents by ID ONLY -- no trace_id,
  // host or message text is restated, so a dropped result is unrecoverable from
  // the surrounding prose.
  messages.push({ role: 'assistant', text: 'All six records are drained: INC-4471 through INC-4476, each sev2 with a region ack.', toolUses: [] });
  messages.push({ role: 'user', text: 'How many of them are sev2?', toolUses: [] });
  messages.push({ role: 'assistant', text: 'All six are sev2.', toolUses: [] });
  messages.push({ role: 'user', text: 'Did every one of them get a region ack?', toolUses: [] });
  messages.push({ role: 'assistant', text: 'Yes -- region_ack is true on all six.', toolUses: [] });
  return messages;
}

/** Wraps the real JevClient with a hard request cap (real HTTP, real spend). */
class CappedAsker {
  constructor(trial) {
    this.trial = trial; this.used = 0;
    this.inner = new JevClient(); // reads TYPESAFE_API_KEY itself
  }
  async ask(state, questions) {
    if (this.used >= MAX_REQUESTS_PER_TRIAL) throw new Error(`[${this.trial}] per-trial request cap reached`);
    if (totalRequests >= MAX_REQUESTS_TOTAL) throw new Error('global request cap reached');
    this.used++; totalRequests++;
    this.lastState = state; this.lastQuestions = questions;
    return this.inner.ask(state, questions);
  }
}

/** Renders a message list the way a post-compaction context would read. */
function render(messages) {
  const out = [];
  messages.forEach((m, i) => {
    const parts = [];
    if (m.text) parts.push(m.text);
    for (const t of m.toolUses ?? []) {
      parts.push(`[tool_use ${t.tool_use_id} ${t.tool}(${JSON.stringify(t.input)})]`);
      if (t.text) parts.push(`[tool_use result inline] ${t.text}`);
    }
    for (const r of m.toolResults ?? []) parts.push(`[tool_result ${r.tool_use_id}]\n${r.text}`);
    if (parts.length) out.push(`<${m.role} index="${i}">\n${parts.join('\n')}\n</${m.role}>`);
  });
  return out.join('\n\n');
}

const QUESTION =
  `What is the exact trace_id of ${TARGET.id}? ` +
  `Answer with a single line of the form TRACE=<value>. ` +
  `If you do not have the value and cannot obtain it, answer TRACE=UNKNOWN instead of guessing.`;

const TOOL_NOTE = {
  repeatable:
    '\n\nTOOLS AVAILABLE TO YOU NOW: you may run the shell command\n' +
    `  bash ${join(OUT, 'incident_lookup.sh')} <incident_id>\n` +
    'which is the same live `incident_lookup` service and returns the full record.',
  onetime:
    '\n\nTOOLS AVAILABLE TO YOU NOW: none. The `incident_lookup` stream was ' +
    'one-time and has been purged; it cannot be re-run, and no other source for ' +
    'this data exists. Do not attempt to look it up.',
};

const results = [];
for (const mode of ['repeatable', 'onetime']) {
  const messages = buildTranscript(mode);
  const asker = new CappedAsker(mode);
  const t0 = Date.now();
  let result, error = null;
  try {
    result = await compact(messages, asker, {
      goal: 'Triage the overnight incident batch and answer follow-up questions about the individual incidents.',
    });
  } catch (e) { error = String(e?.message ?? e); }
  const ms = Date.now() - t0;

  const entry = { trial: mode, ms, error, inputMessages: messages.length };
  if (result) {
    entry.stats = result.stats;
    entry.decisions = result.decisions;
    entry.outputMessages = result.messages.length;
    // Ground truth: is the target's trace_id still anywhere in the output?
    const rendered = render(result.messages);
    entry.targetTracePresentAfterCompaction = rendered.includes(TARGET.trace_id);
    entry.anyTracePresent = RECORDS.filter((r) => rendered.includes(r.trace_id)).map((r) => r.id);
    writeFileSync(join(OUT, `compacted-${mode}.json`), JSON.stringify(result.messages, null, 2));
    writeFileSync(join(OUT, `prompt-${mode}.txt`),
      'Below is the full conversation history you have. It is everything you know.\n\n' +
      rendered + TOOL_NOTE[mode] + '\n\n' + QUESTION + '\n');
    writeFileSync(join(OUT, `state-${mode}.json`), JSON.stringify({ state: asker.lastState, questions: asker.lastQuestions }, null, 2));
  }
  // Also render the UNCOMPACTED transcript as a control.
  writeFileSync(join(OUT, `prompt-${mode}-control.txt`),
    'Below is the full conversation history you have. It is everything you know.\n\n' +
    render(messages) + TOOL_NOTE[mode] + '\n\n' + QUESTION + '\n');
  results.push(entry);
}

writeFileSync(join(OUT, 'incident_lookup.sh'),
  '#!/usr/bin/env bash\n# stand-in for the live incident_lookup service\ncase "$1" in\n' +
  RECORDS.map((r) => `  ${r.id}) echo '${JSON.stringify(r)}' ;;`).join('\n') +
  '\n  *) echo "no such incident" >&2; exit 1 ;;\nesac\n');

writeFileSync(join(OUT, 'oracle.json'), JSON.stringify({ target: TARGET, records: RECORDS, question: QUESTION }, null, 2));
writeFileSync(join(OUT, 'decisions.json'), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
console.log(`\nbilled Jev requests used: ${totalRequests}`);
