/**
 * Phase 0 one-off comparison spike -- vendored-library pin check.
 *
 * ONE-OFF RESEARCH SPIKE -- NOT PRODUCTION CODE. See compare.mjs.
 *
 * `npm run setup` clones fast-jev-compaction at the exact revision pinned in
 * vendor-pin.json. This script proves the checkout sitting in vendor/ is still
 * that revision, so re-running the comparison later cannot silently measure a
 * different library implementation and produce non-reproducible numbers.
 *
 * Two independent checks, both offline:
 *   1. vendor/fast-jev-compaction/.pinned-rev (written by `npm run setup`)
 *      equals vendor-pin.json's `rev`.
 *   2. a sha256 over the vendored `src/` tree (sorted relative paths + bytes)
 *      equals vendor-pin.json's `srcSha256`. This catches a local edit or a
 *      checkout that predates the pin, which (1) alone cannot.
 *
 * `--write` recomputes and stores `srcSha256` instead of checking it; that is
 * only for bootstrapping the pin against a checkout already verified against
 * upstream by other means.
 */

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const PIN_PATH = join(HERE, 'vendor-pin.json');
const VENDOR = join(HERE, 'vendor', 'fast-jev-compaction');
const SRC = join(VENDOR, 'src');

function walk(dir, acc = []) {
  for (const entry of readdirSync(dir).sort()) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
}

function hashSrc() {
  const h = createHash('sha256');
  for (const file of walk(SRC)) {
    h.update(relative(SRC, file).split('\\').join('/'));
    h.update('\0');
    h.update(readFileSync(file));
    h.update('\0');
  }
  return h.digest('hex');
}

const pin = JSON.parse(readFileSync(PIN_PATH, 'utf8'));

if (!existsSync(SRC)) {
  console.error(`vendor check FAILED: ${SRC} does not exist -- run: npm run setup`);
  process.exit(1);
}

const actualSrc = hashSrc();

if (process.argv.includes('--write')) {
  pin.srcSha256 = actualSrc;
  writeFileSync(PIN_PATH, `${JSON.stringify(pin, null, 2)}\n`);
  console.error(`vendor pin updated: srcSha256=${actualSrc}`);
  process.exit(0);
}

const problems = [];

const revFile = join(VENDOR, '.pinned-rev');
if (!existsSync(revFile)) {
  problems.push(
    `.pinned-rev is missing -- this checkout was made before the pin existed, or by hand. ` +
      `Re-run \`npm run setup\` to clone ${pin.rev} exactly.`,
  );
} else {
  const recorded = readFileSync(revFile, 'utf8').trim();
  if (recorded !== pin.rev) {
    problems.push(`vendored rev ${recorded} != pinned rev ${pin.rev} -- re-run \`npm run setup\``);
  }
}

if (pin.srcSha256 === 'REPLACE_ME') {
  problems.push('vendor-pin.json has no srcSha256 yet (bootstrap with: node verify-vendor.mjs --write)');
} else if (actualSrc !== pin.srcSha256) {
  problems.push(
    `vendored src/ sha256 ${actualSrc} != pinned ${pin.srcSha256} -- the library source has ` +
      `changed under the harness; comparison numbers would not be reproducible.`,
  );
}

if (problems.length > 0) {
  console.error('vendor check FAILED:');
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}

console.error(`vendor check ok: fast-jev-compaction @ ${pin.rev} (src sha256 ${actualSrc.slice(0, 12)}...)`);
