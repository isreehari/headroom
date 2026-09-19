"""Measure Headroom compression and Jev retention projections on one corpus.

This benchmark reuses the seeded MCP-shaped scenarios from
``index_proof_table.py``. Local Headroom savings are deterministic. Jev is
optional and must be explicitly enabled with ``--live`` because it makes a
remote request and its answer can change over time.

Examples:

    uv run python benchmarks/jev_proof_table.py --seed 20260902
    uv run python benchmarks/jev_proof_table.py --seed 20260902 --live
    uv run python benchmarks/jev_proof_table.py --seed 20260902 --live --apply-projection

The report keeps these measurements separate:

* ``headroom_saved`` is the actual local compression delta.
* ``jev_projected_saved`` is Jev's estimate using its bounded token estimate.
* ``jev_projected_provider_saved`` normalizes Jev's actions against the
  already-compressed messages with the same provider tokenizer.
* ``jev_applied_saved`` is zero in the shipped shadow-only implementation.

Projected Jev savings are never added to actual savings. That prevents the
report from claiming savings that active mode did not apply.

``--apply-projection`` applies the live Jev decisions to an isolated copy of
the benchmark messages. It is an active benchmark simulation, not production
proxy active mode; the proxy's CCR safety gate remains enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from headroom import CompressConfig, compress
from headroom.providers.openai_compatible import OpenAICompatibleTokenCounter
from headroom.proxy.jev import (
    JEV_POLICY_VERSION,
    JevConfig,
    JevPlan,
    JevPlanner,
    extract_openai_candidates,
    revision_for_messages,
)

try:  # Support both ``python benchmarks/jev_proof_table.py`` and package imports.
    from benchmarks.real_world_agent_benchmark import (  # noqa: E402
        DEFAULT_SEED,
        create_codebase_exploration_scenario,
        create_issue_triage_scenario,
        create_sre_debugging_scenario,
        generate_github_code_search,
        seed_everything,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by standalone invocation.
    from real_world_agent_benchmark import (  # noqa: E402
        DEFAULT_SEED,
        create_codebase_exploration_scenario,
        create_issue_triage_scenario,
        create_sre_debugging_scenario,
        generate_github_code_search,
        seed_everything,
    )

MODEL = "gpt-5.6"
TRAILING_TURNS = 8


@dataclass(frozen=True)
class JevBenchmarkRow:
    scenario: str
    before_tokens: int
    after_tokens: int
    headroom_saved: int
    jev_projected_saved: int
    jev_projected_provider_saved: int
    jev_latency_ms: float | None
    jev_called: bool
    jev_fallback_reason: str | None
    decision_counts: dict[str, int]
    jev_applied_saved: int = 0
    jev_input_tokens: int | None = None
    jev_output_tokens: int | None = None
    jev_response_model: str | None = None
    jev_policy_version: str = JEV_POLICY_VERSION
    jev_abstentions: dict[str, int] = field(default_factory=dict)
    jev_planning_latency_ms: float = 0.0

    @property
    def actual_total_saved(self) -> int:
        """Savings actually applied to the forwarded context."""
        return self.headroom_saved + self.jev_applied_saved

    def as_report_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actual_total_saved"] = self.actual_total_saved
        return data


def build_benchmark_messages(
    label: str, tools: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Build OpenAI-shaped messages while keeping candidates outside recent pins."""
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        candidate_id = f"call_{index}"
        tool_name = str(tool.get("tool", "mcp_tool"))
        tool_calls.append(
            {
                "id": candidate_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps({"scenario": label, "tool": tool_name}),
                },
            }
        )
        tool_results.append(
            {
                "role": "tool",
                "tool_call_id": candidate_id,
                "content": json.dumps(tool.get("result", {}), sort_keys=True),
            }
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": f"Analyse the {label} tool output and answer."},
        {"role": "assistant", "tool_calls": tool_calls},
        *tool_results,
    ]
    for turn in range(TRAILING_TURNS):
        messages.extend(
            [
                {"role": "user", "content": f"Follow-up check {turn}: continue the analysis."},
                {
                    "role": "assistant",
                    "content": "The current state is consistent with the recorded results.",
                },
            ]
        )
    return messages


def build_headroom_messages(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build the exact message shape used by the published proof table."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Analyse the tool output and answer."},
        *[
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "content": json.dumps(tool.get("result", {})),
            }
            for index, tool in enumerate(tools)
        ],
    ]


def apply_jev_actions(
    messages: Sequence[Mapping[str, Any]],
    actions: Mapping[str, str],
    *,
    truncate_head_chars: int = 300,
) -> list[dict[str, Any]]:
    """Apply Jev retention decisions to a private benchmark copy.

    This mirrors the reference Jev compaction semantics: ``drop_result`` keeps
    the call and a deterministic result head, while ``drop_call`` removes both
    sides of the pair. The function is intentionally benchmark-only; the live
    proxy still rejects active mode until CCR durability is implemented.
    """
    applied: list[dict[str, Any]] = []
    for original in messages:
        message = dict(original)
        tool_calls = message.get("tool_calls")
        if message.get("role") == "assistant" and isinstance(tool_calls, list):
            kept_calls = [
                call
                for call in tool_calls
                if not isinstance(call, Mapping) or actions.get(str(call.get("id"))) != "drop_call"
            ]
            if len(kept_calls) != len(tool_calls):
                if kept_calls:
                    message["tool_calls"] = kept_calls
                elif not message.get("content"):
                    continue
                else:
                    message.pop("tool_calls", None)
            applied.append(message)
            continue

        if message.get("role") == "tool":
            candidate_id = message.get("tool_call_id")
            action = actions.get(str(candidate_id))
            if action == "drop_call":
                continue
            if action == "drop_result":
                content = str(message.get("content", ""))
                head = content[: max(0, truncate_head_chars)]
                message["content"] = f"{head}\n[truncated by Jev]"
        applied.append(message)
    return applied


def _tool_result_tokens(
    messages: Sequence[Mapping[str, Any]], tokenizer: OpenAICompatibleTokenCounter
) -> dict[str, int]:
    return {
        str(message["tool_call_id"]): tokenizer.count_text(str(message.get("content", "")))
        for message in messages
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
    }


def calculate_provider_projection(
    messages: Sequence[Mapping[str, Any]],
    tokenizer: OpenAICompatibleTokenCounter,
    actions: Mapping[str, str],
) -> int:
    """Normalize Jev actions to incremental provider-token savings.

    The production Jev planner uses a bounded character-based estimate. This
    separate figure uses the benchmark's provider tokenizer on the messages
    after Headroom compression, so it does not count the same savings twice.
    It still remains a projection because shadow mode does not mutate the
    request.
    """
    result_tokens = _tool_result_tokens(messages, tokenizer)
    return sum(
        result_tokens.get(candidate_id, 0)
        for candidate_id, action in actions.items()
        if action in {"drop_result", "drop_call"}
    )


def _decision_actions(plan: JevPlan) -> dict[str, str]:
    return {decision.candidate_id: decision.action for decision in plan.decisions}


async def run_scenario(
    label: str,
    tools: Sequence[Mapping[str, Any]],
    tokenizer: OpenAICompatibleTokenCounter,
    planner: JevPlanner,
    *,
    apply_projection: bool = False,
) -> JevBenchmarkRow:
    headroom_messages = build_headroom_messages(tools)
    before_tokens = sum(
        tokenizer.count_text(str(message.get("content", "")))
        for message in headroom_messages
        if message.get("role") == "tool"
    )
    result = compress(
        headroom_messages,
        model=MODEL,
        config=CompressConfig(protect_recent=0),
    )
    after_tokens = sum(
        tokenizer.count_text(str(message.get("content", "")))
        for message in result.messages
        if message.get("role") == "tool" and isinstance(message.get("content"), str)
    )

    jev_messages = build_benchmark_messages(label, tools)
    candidates = extract_openai_candidates(jev_messages, preserve_recent_messages=6)
    started = time.perf_counter()
    plan = await planner.plan(
        provider="headroom-benchmark",
        model=MODEL,
        session_id=f"benchmark-{label.lower().replace(' ', '-')}",
        branch_id="main",
        revision=revision_for_messages(jev_messages),
        goal=f"Evaluate retention for the {label} scenario",
        candidates=candidates,
        current_tokens=after_tokens,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    actions = _decision_actions(plan)
    decision_counts = {
        action: sum(decision.action == action for decision in plan.decisions)
        for action in ("keep", "drop_result", "drop_call")
    }
    jev_applied_saved = 0
    if apply_projection:
        jev_before = sum(
            tokenizer.count_text(str(message.get("content", "")))
            for message in result.messages
            if message.get("role") == "tool"
        )
        applied_messages = apply_jev_actions(result.messages, actions)
        jev_after = sum(
            tokenizer.count_text(str(message.get("content", "")))
            for message in applied_messages
            if message.get("role") == "tool"
        )
        jev_applied_saved = max(0, jev_before - jev_after)
    return JevBenchmarkRow(
        scenario=label,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        headroom_saved=max(0, before_tokens - after_tokens),
        jev_projected_saved=plan.projected_tokens_saved,
        jev_projected_provider_saved=calculate_provider_projection(
            result.messages, tokenizer, actions
        ),
        jev_latency_ms=latency_ms if plan.called else None,
        jev_planning_latency_ms=latency_ms,
        jev_called=plan.called,
        jev_fallback_reason=plan.fallback_reason,
        decision_counts=decision_counts,
        jev_applied_saved=jev_applied_saved,
        jev_input_tokens=plan.input_tokens,
        jev_output_tokens=plan.output_tokens,
        jev_response_model=plan.response_model,
        jev_abstentions={
            reason: sum(decision.reason == reason for decision in plan.decisions)
            for reason in sorted(
                {decision.reason for decision in plan.decisions if decision.reason}
            )
        },
    )


def scenario_inputs(seed: int) -> list[tuple[str, Sequence[Mapping[str, Any]]]]:
    """Return the proof-table scenarios in the same seeded order."""
    seed_everything(seed)
    return [
        (
            "Code search (100 results)",
            [generate_github_code_search("JWT authentication middleware", num_results=100)],
        ),
        ("SRE incident debugging", create_sre_debugging_scenario().tools),
        ("Codebase exploration", create_codebase_exploration_scenario().tools),
        ("GitHub issue triage", create_issue_triage_scenario().tools),
    ]


async def run_benchmark(
    seed: int, planner: JevPlanner, *, apply_projection: bool = False
) -> list[JevBenchmarkRow]:
    tokenizer = OpenAICompatibleTokenCounter(model=MODEL)
    return [
        await run_scenario(
            label,
            tools,
            tokenizer,
            planner,
            apply_projection=apply_projection,
        )
        for label, tools in scenario_inputs(seed)
    ]


def _print_report(seed: int, planner: JevPlanner, rows: Sequence[JevBenchmarkRow]) -> None:
    print(
        f"seed={seed}  model={MODEL}  tokenizer={type(OpenAICompatibleTokenCounter(model=MODEL)._tokenizer).__name__}"
    )
    print(f"jev_mode={planner.config.mode}  endpoint={planner.config.endpoint}")
    print(
        "benchmark_application=isolated_copy"
        if any(row.jev_applied_saved for row in rows)
        else "benchmark_application=shadow_only"
    )
    print()
    print(
        f"{'Scenario':<30} {'Before':>9} {'After':>9} {'Headroom':>9} "
        f"{'Jev est.':>9} {'Jev incr.':>9} {'Latency':>9} {'Applied':>9} {'Decision':>18}"
    )
    print("-" * 125)
    for row in rows:
        decisions = (
            ",".join(f"{name}:{count}" for name, count in row.decision_counts.items() if count)
            or row.jev_fallback_reason
            or "none"
        )
        latency = f"{row.jev_latency_ms:.1f}ms" if row.jev_latency_ms is not None else "-"
        print(
            f"{row.scenario:<30} {row.before_tokens:>9,} {row.after_tokens:>9,} "
            f"{row.headroom_saved:>9,} {row.jev_projected_saved:>9,} "
            f"{row.jev_projected_provider_saved:>9,} {latency:>9} "
            f"{row.jev_applied_saved:>9,} {decisions:>18}"
        )
    print("-" * 125)
    print(
        f"{'TOTAL':<30} {sum(r.before_tokens for r in rows):>9,} "
        f"{sum(r.after_tokens for r in rows):>9,} {sum(r.headroom_saved for r in rows):>9,} "
        f"{sum(r.jev_projected_saved for r in rows):>9,} "
        f"{sum(r.jev_projected_provider_saved for r in rows):>9,} "
        f"{sum(r.jev_applied_saved for r in rows):>9,}"
    )
    print("\nProjected Jev savings are reported separately and are not added to actual savings.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the configured Jev endpoint; without this flag no network request is made.",
    )
    parser.add_argument(
        "--apply-projection",
        action="store_true",
        help="Apply Jev decisions to an isolated benchmark copy; never changes the proxy.",
    )
    parser.add_argument(
        "--json-output", type=str, help="Write row-level metrics to this JSON file."
    )
    args = parser.parse_args()

    if args.apply_projection and not args.live:
        parser.error("--apply-projection requires --live")

    config = JevConfig.from_env()
    if args.live:
        if not config.api_key:
            parser.error("--live requires HEADROOM_JEV_API_KEY or TYPESAFE_API_KEY")
        config = replace(config, mode="shadow")
    else:
        config = replace(config, mode="off", api_key=None)
    planner = JevPlanner(config)
    rows = asyncio.run(run_benchmark(args.seed, planner, apply_projection=args.apply_projection))
    _print_report(args.seed, planner, rows)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as output:
            json.dump([row.as_report_dict() for row in rows], output, indent=2)
            output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
