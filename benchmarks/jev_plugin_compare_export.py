"""Phase 0 one-off comparison spike: fast-jev-compaction WITHOUT Headroom.

ONE-OFF RESEARCH SPIKE -- NOT PRODUCTION CODE.

Companion to ``benchmarks/jev_savings_spike.py`` and part of Phase 0 of
``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md``.

``jev_savings_spike.py`` answered "does a Jev retention call buy anything *on
top of* Headroom's deterministic compression" (T0 -> TH -> TP). This helper
answers the neighbouring question the user asked: what does the
`fast-jev-compaction <https://github.com/tamaratran/fast-jev-compaction>`_
library achieve on the *same* corpus with **no Headroom in the loop at all**
(T0 -> TC)?

This file is only the Python half. It does three jobs, as subcommands:

``export``
    Build the identical 6-scenario corpus (same generators, same
    ``DEFAULT_SEED``) by importing ``jev_savings_spike.build_corpus``, and dump
    each scenario's **raw, pre-Headroom** message list to JSON, together with
    T0 (raw token count) and TH (post-Headroom token count). T0/TH are measured
    here so that every number in the final table comes from one single
    instantiation of the corpus -- the generators mint tool-call ids with an
    unseeded ``uuid.uuid4()``, so a second process would wobble by a few tokens.

``count``
    Re-count tokens on message lists handed back by the Node side, using the
    **same** ``OpenAICompatibleTokenCounter`` / ``count_tokens`` path the
    original spike uses. This is the subprocess bridge that makes TC comparable
    with T0/TH/TP: ``result.stats`` only reports *characters* and a
    tokenizer-free estimate, neither of which is Headroom's tokenizer.

``report``
    Join the export, the Node side's TC values and (optionally) a captured
    ``jev_savings_spike.py`` stdout for TP, and print the final table + JSON
    lines.

**No Jev call is ever made from this file.** Every real, billed Jev call in
this comparison is made by fast-jev-compaction's own ``JevClient`` inside
``.jev-plugin-compare/compare.mjs``. No API key is read, printed or logged here.

Like the original spike this module redirects ``HEADROOM_WORKSPACE_DIR`` to a
throwaway temp directory *before* importing ``headroom`` (it imports
``jev_savings_spike``, which does exactly that at import time), so running
``compress()`` for TH never touches ``~/.headroom`` or a live proxy's CCR store.

Usage (normally driven by ``.jev-plugin-compare/run.sh``)::

    uv run python benchmarks/jev_plugin_compare_export.py export --out /tmp/x/corpus.json
    uv run python benchmarks/jev_plugin_compare_export.py count --in /tmp/x/tc-in.json
    uv run python benchmarks/jev_plugin_compare_export.py report \
        --export /tmp/x/corpus.json --tc /tmp/x/tc.json --spike-output /tmp/x/spike.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Importing the original spike is deliberate, not incidental: it is what
# guarantees the corpus, the seed, the frozen-prefix declarations and the
# tokenizer are *identical* to the already-measured T0/TH/TP numbers rather
# than a re-implementation that drifts. Its module body also performs the
# Headroom-state isolation before `headroom` is imported.
import jev_savings_spike as spike  # noqa: E402

from headroom import CompressConfig, compress  # noqa: E402
from headroom.providers.openai_compatible import OpenAICompatibleTokenCounter  # noqa: E402


def _tokenizer() -> OpenAICompatibleTokenCounter:
    return OpenAICompatibleTokenCounter(model=spike.TARGET_MODEL)


# --- export -----------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    tok = _tokenizer()
    corpus = spike.build_corpus(args.seed)[: max(0, args.limit)]

    out: dict[str, Any] = {
        "seed": args.seed,
        "target_model": spike.TARGET_MODEL,
        "recent_tail_exclusion": spike.RECENT_TAIL_EXCLUSION,
        "workspace": spike.SPIKE_WORKSPACE,
        "scenarios": [],
    }

    for scenario in corpus:
        t0 = spike.count_tokens(tok, scenario.messages)
        # Headroom's own deterministic compression, exactly as the original
        # spike runs it. Free -- no Jev call, no provider call.
        result = compress(
            scenario.messages,
            model=spike.TARGET_MODEL,
            config=CompressConfig(frozen_message_count=scenario.frozen_prefix),
        )
        th = spike.count_tokens(tok, result.messages)
        print(
            f"[{scenario.name}] {len(scenario.messages)} raw messages  "
            f"T0={t0:,}  TH={th:,}  ({len(result.messages)} after Headroom)",
            file=sys.stderr,
        )
        out["scenarios"].append(
            {
                "name": scenario.name,
                "shape": scenario.shape,
                "frozen_prefix": scenario.frozen_prefix,
                "raw_message_count": len(scenario.messages),
                "headroom_message_count": len(result.messages),
                "T0": t0,
                "TH": th,
                # RAW, pre-Headroom messages. The whole point of the comparison
                # is what the plugin does with no Headroom preprocessing.
                "messages": scenario.messages,
            }
        )

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, default=str), encoding="utf-8")
    print(f"wrote {path} ({path.stat().st_size:,} bytes)", file=sys.stderr)
    return 0


# --- count ------------------------------------------------------------------


def cmd_count(args: argparse.Namespace) -> int:
    """Token-count message lists produced by the Node side.

    Input: ``{"scenarios": {"<name>": [<message>, ...], ...}}``
    Output (stdout, JSON): ``{"<name>": <tokens>}``

    The counting path is ``jev_savings_spike.count_tokens`` with the same
    ``OpenAICompatibleTokenCounter`` -- not a re-implementation, so TC cannot
    quietly drift from T0/TH.
    """
    payload = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    tok = _tokenizer()
    counts = {
        name: spike.count_tokens(tok, messages) for name, messages in payload["scenarios"].items()
    }
    print(json.dumps(counts))
    return 0


# --- report -----------------------------------------------------------------

_T0_LINE = re.compile(r"^\[(?P<name>[^\]]+)\]")


def _parse_spike_output(path: Path) -> dict[str, dict[str, Any]]:
    """Pull per-scenario TH/TP out of a captured ``jev_savings_spike.py`` run.

    The spike prints one JSON object per scenario under ``=== JSON lines ===``.
    Anything unparseable is skipped rather than guessed at.
    """
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "scenario" in obj and "TP" in obj:
            rows[obj["scenario"]] = obj
    return rows


def _pct(before: int, after: int) -> float:
    return round((before - after) / before * 100, 2) if before else 0.0


def cmd_report(args: argparse.Namespace) -> int:
    export = json.loads(Path(args.export).read_text(encoding="utf-8"))
    tc_doc = json.loads(Path(args.tc).read_text(encoding="utf-8"))
    tc_counts: dict[str, int] = tc_doc["TC"]
    tc_meta: dict[str, Any] = tc_doc.get("meta", {})
    spike_rows = _parse_spike_output(Path(args.spike_output)) if args.spike_output else {}

    print("=" * 118)
    print("fast-jev-compaction vs Headroom vs Headroom+Jev -- same corpus, same tokenizer")
    print("ONE-OFF PHASE 0 COMPARISON SPIKE (2026-09-20-jev-retention-fresh-design.md)")
    print("=" * 118)
    print(
        "T0 = raw tokens | TH = after Headroom | TP = after Headroom + a Jev retention call "
        "| TC = after fast-jev-compaction alone (no Headroom)"
    )
    print()

    header = (
        f"{'scenario':<30} {'T0':>9} {'TH':>9} {'H %':>7} "
        f"{'TP':>9} {'H+Jev %':>8} {'TC':>9} {'FJC %':>7} {'calls':>6} {'req':>4} {'ms':>7}"
    )
    print(header)
    print("-" * len(header))

    rows: list[dict[str, Any]] = []
    for scen in export["scenarios"]:
        name = scen["name"]
        t0 = int(scen["T0"])
        th = int(scen["TH"])
        tc = tc_counts.get(name)
        meta = tc_meta.get(name, {})
        stats = meta.get("stats") or {}

        spike_row = spike_rows.get(name)
        tp: int | None = None
        if spike_row is not None:
            # The spike's TP is measured against its own TH from a separate
            # corpus instantiation (unseeded uuid4 tool-call ids wobble by a
            # few tokens). Carry its TH->TP *ratio* onto this run's TH so every
            # percentage in the table shares one T0 baseline.
            spike_th = int(spike_row["TH"])
            spike_tp = int(spike_row["TP"])
            tp = round(th * spike_tp / spike_th) if spike_th else th

        row = {
            "scenario": name,
            "shape": scen["shape"],
            "raw_messages": scen["raw_message_count"],
            "T0": t0,
            "TH": th,
            "headroom_pct": _pct(t0, th),
            "TP": tp,
            "headroom_plus_jev_pct": _pct(t0, tp) if tp is not None else None,
            "TC": tc,
            "fast_jev_compaction_pct": _pct(t0, tc) if tc is not None else None,
            "fjc_messages_after": stats.get("messagesAfter"),
            "fjc_calls": stats.get("calls"),
            "fjc_kept": stats.get("kept"),
            "fjc_results_dropped": stats.get("resultsDropped"),
            "fjc_calls_dropped": stats.get("callsDropped"),
            "fjc_pinned": stats.get("pinned"),
            "fjc_state_tokens": stats.get("stateTokens"),
            "fjc_state_stage": stats.get("stateStage"),
            "fjc_requests": stats.get("requests"),
            "fjc_ms": stats.get("ms"),
            "fjc_error": meta.get("error"),
            "tp_source": "jev_savings_spike.py (ratio-carried)" if tp is not None else None,
        }
        rows.append(row)

        tp_s = f"{tp:>9,}" if tp is not None else f"{'n/a':>9}"
        tpp_s = (
            f"{row['headroom_plus_jev_pct']:>7.2f}%"
            if row["headroom_plus_jev_pct"] is not None
            else f"{'n/a':>8}"
        )
        tc_s = f"{tc:>9,}" if tc is not None else f"{'ERR':>9}"
        tcp_s = (
            f"{row['fast_jev_compaction_pct']:>6.2f}%"
            if row["fast_jev_compaction_pct"] is not None
            else f"{'n/a':>7}"
        )
        print(
            f"{name:<30} {t0:>9,} {th:>9,} {row['headroom_pct']:>6.2f}% "
            f"{tp_s} {tpp_s} {tc_s} {tcp_s} "
            f"{(stats.get('calls') if stats.get('calls') is not None else '-'):>6} "
            f"{(stats.get('requests') if stats.get('requests') is not None else '-'):>4} "
            f"{(stats.get('ms') if stats.get('ms') is not None else '-'):>7}"
        )

    print("-" * len(header))
    t0_tot = sum(r["T0"] for r in rows)
    th_tot = sum(r["TH"] for r in rows)
    tp_tot = sum(r["TP"] for r in rows if r["TP"] is not None)
    tp_base = sum(r["T0"] for r in rows if r["TP"] is not None)
    tc_tot = sum(r["TC"] for r in rows if r["TC"] is not None)
    tc_base = sum(r["T0"] for r in rows if r["TC"] is not None)
    print(
        f"{'TOTAL':<30} {t0_tot:>9,} {th_tot:>9,} {_pct(t0_tot, th_tot):>6.2f}% "
        f"{tp_tot:>9,} {_pct(tp_base, tp_tot):>7.2f}% {tc_tot:>9,} {_pct(tc_base, tc_tot):>6.2f}% "
        f"{sum(r['fjc_calls'] or 0 for r in rows):>6} "
        f"{sum(r['fjc_requests'] or 0 for r in rows):>4}"
    )
    if tp_base != t0_tot or tc_base != t0_tot:
        print(
            "  (TP/TC total percentages are computed only over the scenarios that "
            "produced a value, so their baselines may differ from T0 TOTAL)"
        )

    print("\n=== JSON lines ===")
    for row in rows:
        print(json.dumps(row, default=str))

    notes = tc_doc.get("notes") or []
    if notes:
        print("\n=== Adaptation notes from the Node side ===")
        for note in notes:
            print(f"  - {note}")

    print("\n=== Read this before quoting the FJC % column ===")
    print(
        "  - The three columns do NOT preserve the same thing. Headroom's TH is\n"
        "    recoverable compression (CCR markers, retrievable via /v1/retrieve).\n"
        "    TC is permanent deletion: fast-jev-compaction removes the tool call and\n"
        "    its result outright, with only a re-run of the tool to fall back on.\n"
        "    A larger FJC % is therefore not automatically a better result."
    )
    dropped_all = [
        r for r in rows if r["fjc_calls"] and not r["fjc_kept"] and not r["fjc_results_dropped"]
    ]
    if dropped_all:
        print(
            f"  - [FLAG] {len(dropped_all)}/{len(rows)} scenario(s) came back with "
            "kept=0 and resultsDropped=0: Jev chose `drop_call` for EVERY\n"
            "    non-pinned tool call. That is the mirror image of the old "
            "'always keep' bias and deserves the same scepticism -- a\n"
            "    synthetic corpus of generated tool output is plausibly seen as "
            "uniformly disposable."
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase 0 comparison spike helper: corpus export, token "
        "re-count bridge and final report for fast-jev-compaction vs Headroom. "
        "Makes NO Jev calls itself.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="dump the raw (T0) corpus + T0/TH to JSON")
    e.add_argument("--out", required=True)
    e.add_argument("--seed", type=int, default=spike.DEFAULT_SEED)
    e.add_argument("--limit", type=int, default=6)
    e.set_defaults(fn=cmd_export)

    c = sub.add_parser("count", help="token-count Node-produced message lists (TC bridge)")
    c.add_argument("--in", dest="in_path", required=True)
    c.set_defaults(fn=cmd_count)

    r = sub.add_parser("report", help="print the final side-by-side comparison")
    r.add_argument("--export", required=True)
    r.add_argument("--tc", required=True)
    r.add_argument("--spike-output", default=None, help="captured jev_savings_spike.py stdout")
    r.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
