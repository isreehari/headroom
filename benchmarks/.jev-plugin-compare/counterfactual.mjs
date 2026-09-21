/**
 * Stage 1b: the cell Jev's own decisions did not produce.
 *
 * Jev KEPT every call in the one-time trial, so "a one-time fact was dropped"
 * never occurred naturally. This builds it deterministically -- NO Jev call --
 * by feeding the library's own `applyDecisions` a forced drop_call for every
 * non-pinned call of the one-time transcript. It answers the conditional:
 * IF drop_call fires on an unrecoverable fact, is the answer wrong?
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { applyDecisions, collectToolCalls } from './vendor/fast-jev-compaction/dist/index.js';

const OUT = process.argv[2];
const oracle = JSON.parse(readFileSync(join(OUT, 'oracle.json'), 'utf8'));

// Rebuild the one-time transcript exactly as correctness.mjs did, by reading
// the compacted one-time output (nothing was dropped there, so it IS the input).
const messages = JSON.parse(readFileSync(join(OUT, 'compacted-onetime.json'), 'utf8'));
const calls = collectToolCalls(messages, 6);
const forced = calls.filter((c) => !c.pinned).map((c) => ({
  id: c.id, tool: c.tool, keepCall: 0, keepResult: 0,
  action: 'drop_call', reason: 'call_dropped',
}));
const out = applyDecisions(messages, forced, calls, 300);

function render(ms) {
  const o = [];
  ms.forEach((m, i) => {
    const parts = [];
    if (m.text) parts.push(m.text);
    for (const t of m.toolUses ?? []) {
      parts.push(`[tool_use ${t.tool_use_id} ${t.tool}(${JSON.stringify(t.input)})]`);
      if (t.text) parts.push(`[tool_use result inline] ${t.text}`);
    }
    for (const r of m.toolResults ?? []) parts.push(`[tool_result ${r.tool_use_id}]\n${r.text}`);
    if (parts.length) o.push(`<${m.role} index="${i}">\n${parts.join('\n')}\n</${m.role}>`);
  });
  return o.join('\n\n');
}

const rendered = render(out);
const note =
  '\n\nTOOLS AVAILABLE TO YOU NOW: none. The `incident_lookup` stream was ' +
  'one-time and has been purged; it cannot be re-run, and no other source for ' +
  'this data exists. Do not attempt to look it up.';
writeFileSync(join(OUT, 'prompt-onetime-forcedrop.txt'),
  'Below is the full conversation history you have. It is everything you know.\n\n' +
  rendered + note + '\n\n' + oracle.question + '\n');

console.log(JSON.stringify({
  forcedDrops: forced.length,
  messagesBefore: messages.length,
  messagesAfter: out.length,
  targetTracePresent: rendered.includes(oracle.target.trace_id),
  anyTracePresent: oracle.records.filter((r) => rendered.includes(r.trace_id)).map((r) => r.id),
}, null, 2));
