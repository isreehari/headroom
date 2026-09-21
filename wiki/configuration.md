# Configuration

Headroom can be configured via the SDK, proxy command line, or per-request overrides.

## Runtime Rollout Channels

Rollout channels control behaviors in an already-installed artifact. They do
not install or select a Headroom release/version.

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEADROOM_ROLLOUT_CHANNEL` | `stable` | Selects `stable`, `beta`, `canary`, or `dev`. |
| `HEADROOM_FEATURES` | unset | Comma-separated feature names to request explicitly. |
| `HEADROOM_DISABLE_FEATURES` | unset | Comma-separated feature names to force off. Disable wins over every enable path. |
| `HEADROOM_UNSAFE_ALLOW_UNSTABLE_FEATURES` | unset | Break-glass override for emergency mitigation only. |

Example:

```bash
export HEADROOM_ROLLOUT_CHANNEL=canary
export HEADROOM_FEATURES=tool_result_interceptors
headroom proxy --intercept-tool-results
```

## SDK Configuration

```python
from headroom import HeadroomClient, OpenAIProvider
from openai import OpenAI

client = HeadroomClient(
    original_client=OpenAI(),
    provider=OpenAIProvider(),
    # Mode: "audit" (observe only) or "optimize" (apply transforms)
    default_mode="optimize",
    # Enable provider-specific cache optimization
    enable_cache_optimizer=True,
    # Enable query-level semantic caching
    enable_semantic_cache=False,
    # Override default context limits per model
    model_context_limits={
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
    },
    # Database location (defaults to temp directory)
    # store_url="sqlite:////absolute/path/to/headroom.db",
)
```

## Proxy Configuration

### Command Line Options

```bash
headroom proxy \
  --port 8787 \              # Port to listen on
  --host 0.0.0.0 \           # Host to bind to
  --budget 10.00 \           # Daily budget limit in USD
  --log-file headroom.jsonl  # Log file path
```

### Feature Flags

```bash
# Disable optimization (passthrough mode)
headroom proxy --no-optimize

# Disable semantic caching
headroom proxy --no-cache

# Disable CCR entirely (no retrieval markers and no injected retrieve tool)
headroom proxy --no-ccr

# Disable proactive CCR expansion
headroom proxy --no-ccr-proactive-expansion

# (The earlier --llmlingua flag was retired in 0.9.x and replaced by
# Kompress (ModernBERT). See `wiki/transforms.md` for the current
# opt-in path via the `[ml]` extra.)
```

### All Options

```bash
headroom proxy --help
```

### Kompress backend selection

Kompress (the model-based compressor) can run on two engines:

- **ONNX Runtime** — lightweight, CPU-first. Installed with
  `pip install headroom-ai[proxy]`. Optionally uses the CoreML execution
  provider on macOS.
- **PyTorch** — heavier, supports CUDA and Apple-Silicon MPS
  acceleration. Installed with `pip install headroom-ai[ml]`. With
  `device=auto` it selects `cuda`, then `mps`, then `cpu`.

Select the backend via the `HEADROOM_KOMPRESS_BACKEND` environment
variable:

| Value               | Behavior                                                               |
|---------------------|------------------------------------------------------------------------|
| `auto`              | Default. ONNX CPU first (stable, lightweight), PyTorch as fallback.    |
| `onnx` / `onnx_cpu` | Force ONNX Runtime on CPU.                                             |
| `onnx_coreml`       | Force ONNX Runtime with the CoreML provider (CPU fallback).            |
| `pytorch`           | Force PyTorch with automatic device selection (CUDA → MPS → CPU).      |
| `pytorch_mps`       | Force PyTorch on Apple-Silicon MPS; falls back to ONNX CPU on failure. |

Values are case-insensitive and hyphens are accepted (`onnx-cpu` ==
`onnx_cpu`). Shorthand aliases: `cpu` → `onnx_cpu`, `coreml` →
`onnx_coreml`, `mps` / `torch_mps` → `pytorch_mps`, `torch` →
`pytorch`. Unrecognized values log a warning and fall back to `auto`.

Example — opt in to MPS on an Apple-Silicon machine:

```bash
export HEADROOM_KOMPRESS_BACKEND=mps
headroom proxy ...
```

The default deliberately stays on ONNX CPU so existing installs keep
their compression quality and performance characteristics; accelerator
backends are opt-in.

## Per-Request Overrides

Override configuration for specific requests:

```python
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    # Override mode for this request
    headroom_mode="audit",
    # Reserve more tokens for output
    headroom_output_buffer_tokens=8000,
    # Keep last N turns (don't compress)
    headroom_keep_turns=5,
    # Skip compression for specific tools
    headroom_tool_profiles={"important_tool": {"skip_compression": True}},
)
```

## Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `audit` | Observes and logs, no modifications | Production monitoring, baseline measurement |
| `optimize` | Applies safe, deterministic transforms | Production optimization |
| `simulate` | Returns plan without API call | Testing, cost estimation |

### Simulate Mode

Preview what would happen without making an API call:

```python
plan = client.chat.completions.simulate(
    model="gpt-4o",
    messages=large_conversation,
)

print(f"Would save {plan.tokens_saved} tokens")
print(f"Transforms: {plan.transforms}")
print(f"Estimated savings: {plan.estimated_savings}")
```

## SmartCrusher Configuration

Fine-tune JSON compression behavior:

```python
from headroom.transforms import SmartCrusherConfig

config = SmartCrusherConfig(
    # Maximum items to keep after compression
    max_items_after_crush=15,
    # Minimum tokens before applying compression
    min_tokens_to_crush=200,
    # Guarantee rows matching these patterns survive compression verbatim
    # (requires audit_safe=True; matched against each row's canonical JSON)
    audit_safe=True,
    protected_patterns=["error", "warning", "failure"],
)
# Error items and statistical anomalies (>2 std from mean) are always kept
# automatically. Relevance-scoring tier ("bm25"/"embedding"/"hybrid") is a
# separate `relevance_config` argument to SmartCrusher(), not a field here.
```

## Cache Aligner Configuration

Control prefix stabilization:

```python
from headroom import CacheAlignerConfig

config = CacheAlignerConfig(
    # Enable/disable cache alignment (disabled by default: prefix-stability
    # gains are marginal in practice -- see headroom/config.py:61)
    enabled=True,
    # Legacy pattern list (only used when use_dynamic_detector=False;
    # the field is `date_patterns`, not `dynamic_patterns`). Default mode
    # (use_dynamic_detector=True) auto-detects dates, UUIDs, tokens, etc.
    # via detection_tiers instead -- see headroom/config.py:68-79.
    use_dynamic_detector=False,
    date_patterns=[
        r"Today is \w+ \d+, \d{4}",
        r"Current time: .*",
    ],
)
```

## Context Management

Context management is handled automatically inside the pipeline
(live-zone-only compression) — there is nothing to configure. Headroom
**never** drops messages from the conversation history and does not do
position-based or score-based context management. It compresses only the
newest content blocks (the latest user message and the latest tool result /
tool output), type-aware and reversible via CCR. The cache hot zone — system
prompt, tools, and older turns — is never mutated, which preserves provider
prompt caching.

> The earlier `RollingWindowConfig`, `IntelligentContextConfig`, and
> `ScoringWeights` configuration classes (and the position-/score-based
> context managers they configured) have been removed and are no longer part
> of Headroom.

## Environment Variables

Some settings can be configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `HEADROOM_MODEL_LIMITS` | Custom model config (JSON string or file path) | - |
| `HEADROOM_CONFIG_DIR` | Canonical config (read-mostly) root. Derives `models.json` and per-plugin config paths when set. | `~/.headroom/config` |
| `HEADROOM_WORKSPACE_DIR` | Canonical workspace (read-write state) root. Derives savings ledger, memory DB, logs, TOIN, subscription state, and more when set. | `~/.headroom` |
| `HEADROOM_SAVINGS_PATH` | Full path to the proxy savings JSON ledger. Always wins when set. | derived from `${HEADROOM_WORKSPACE_DIR}` |
| `HEADROOM_TOIN_PATH` | Full path to the TOIN telemetry JSON file. Always wins when set. | derived from `${HEADROOM_WORKSPACE_DIR}` |
| `HEADROOM_SUBSCRIPTION_STATE_PATH` | Full path to the subscription tracker state. Always wins when set. | derived from `${HEADROOM_WORKSPACE_DIR}` |
| `HEADROOM_EMBEDDER_RUNTIME` | Set to `pytorch_mps` to run the memory embedder via the torch sentence-transformers backend on the Apple GPU (MPS). Only engages when Apple MPS is actually available; otherwise it logs a warning and uses the existing default embedder selection path. `pytorch_mps` is the only accepted value. Requires the `[pytorch-mps]` extra. See [Memory](memory.md#embedding-runtime--gpu-offload-apple-silicon). | default embedder selection |
| `HEADROOM_BETA_HEADER_STICKY` | Controls per-session `anthropic-beta` / `OpenAI-Beta` re-echo. `enabled` (default): the proxy unions beta tokens across turns within a session — if the client sends a token in turn N and omits it in turn N+1, the proxy re-injects it to preserve prefix-cache stability. `disabled`: the client's value is forwarded verbatim with no accumulation. Any other value raises at request time. See [Session Beta Header Tracking](#session-beta-header-tracking). | `enabled` |
| `HEADROOM_BETA_TRACKER_MAX_SESSIONS` | LRU capacity of the in-memory session beta tracker. Once full, the oldest session entry is evicted. | `1000` |

## Jev Retention (default off)

Jev is a third-party retention-decision service (TypeSafe System One). When it is
enabled, Headroom asks it -- per historical **tool result** -- whether that result must
stay verbatim, can be truncated, or can be replaced by a retrievable CCR marker. It is
**additive** to Headroom's compression and never a substitute for it: the deterministic
pipeline runs on every turn whatever Jev answers, and Jev only ever decides what to do
with what is left.

Where Jev sits relative to that pipeline differs by track, and the difference matters:

- **Shadow mode and Track B (`POST /v1/compress`)** run *after* Headroom's own
  deterministic compression, on the output it produced.
- **Track C (the Codex WebSocket boundary)** runs *before* that frame is compressed,
  deliberately: Jev must see -- and CCR must store -- the **original** tool output, not
  an already-compressed marker, or the retained "original" would be unrecoverable. The
  frame is still compressed afterwards, so Jev remains additive here too.

**Default off.** With no `HEADROOM_JEV_*` variable exported, no Jev code runs on any
request: `JevConfig.from_env` returns the default configuration and stops, without
reading any other `HEADROOM_JEV_*` variable. A typo in, say,
`HEADROOM_JEV_TIMEOUT_MS` therefore cannot stop an unconfigured proxy booting -- the
one exception is `HEADROOM_JEV_MODE` itself, which is always parsed and rejected if it
is not one of the three modes. The only Jev-related behaviour an `off` proxy still has
is the `/v1/compress` boundary gate, which rejects a *malformed* opt-in request with a
400 (see [below](#it-fails-open-everywhere)); a request that does not send
`config.jev_compaction_boundary` is unaffected.

| Variable | Description | Default |
|----------|-------------|---------|
| `HEADROOM_JEV_MODE` | `off`, `shadow` or `active`. `off`: no other `HEADROOM_JEV_*` variable is read at all. `shadow`: Jev is called and a projection is recorded, but the forwarded request is never modified. `active`: decisions are applied, and only at a declared compaction boundary -- `POST /v1/compress` with `config.jev_compaction_boundary=true` (Track B), or Codex's native WebSocket compaction (Track C). The two are exclusive: `active` does **not** also run the shadow projection. | `off` |
| `HEADROOM_JEV_API_KEY` | API key. Required whenever the mode is not `off`: `JevConfig.validate()` raises from `ProxyConfig.__post_init__`, so the proxy refuses to start rather than silently no-opping. It travels only in the client's `Authorization` header; it is excluded from the `repr`, from `JevConfig.redacted()`, from the `/stats` payload and from the multi-worker config payload, and is **never logged**. | - |
| `HEADROOM_JEV_ENDPOINT` | Jev API endpoint. Only its **prefix** is validated (it must start `http://` or `https://`); a typo in the host or path is not caught until the first request fails, which then fails open. Everywhere it is surfaced -- log lines, scrubbed exception text, the `repr`, `/stats` -- it passes through `redact_endpoint()` and is shown as scheme + host + path only, so userinfo (`user:pw@`) and any query string are replaced with `<redacted>`. | `https://api.typesafe.ai/v1/systemone` |
| `HEADROOM_JEV_MODEL` | Jev model name, passed in the request payload. **Not validated** -- a misspelled name such as `jev-lattest` is accepted at startup and only shows up as a failed (fail-open) call at request time. An unset or empty value falls back to the default, but a value of nothing but whitespace becomes the empty string, which no model matches. | `jev-latest` |
| `HEADROOM_JEV_TIMEOUT_MS` | Hard bound (>= 1) on the whole Jev round trip, applied by all three tracks. This is added latency on the turns that actually call Jev, so keep it small. | `500` |
| `HEADROOM_JEV_THRESHOLD_PERCENT` | 1..100. **Shadow mode only** (it is read nowhere else in the codebase): skip the call until post-Headroom tokens reach this percentage of the model's context window. | `80` |
| `HEADROOM_JEV_COOLDOWN_TURNS` | >= 0. **Shadow mode only**: turns to wait before another call on the same `(session, branch)`. Only turns that reach the cooldown gate count against it. | `5` |
| `HEADROOM_JEV_MAX_CANDIDATE_TOKENS` | >= 1. Per-candidate ceiling on how much content is shown to Jev, but it behaves differently per track. Shadow and Track B **truncate** each candidate's view to `tokens x 4` characters and tell Jev the view was truncated. Track C instead **skips** the boundary entirely: the same `x 4` factor makes a UTF-8 byte ceiling, and a candidate over it is rejected before any Jev request is made, so the original is forwarded untouched. Track C additionally applies a fixed, non-configurable 20,000-character cap when building its request. | `20000` |
| `HEADROOM_JEV_MAX_CANDIDATES` | >= 1. Maximum candidates selected per call, oldest first. Applies in shadow mode and to Track B; Track C's boundary carries exactly one candidate, so it is not used there. | `12` |
| `HEADROOM_JEV_MAX_STATE_TOKENS` | >= 1. Ceiling on the **measured** serialized request, in shadow mode and Track B. Jev rejects an oversized request outright -- the whole call is lost, not just the overflow -- so the request is trimmed (candidates dropped, then views thinned) until it really fits. Raise it together with `HEADROOM_JEV_MAX_CANDIDATES` if you want a bigger request at a boundary. Not used by Track C. | `8000` |

`HEADROOM_JEV_MODE` is always validated. The numeric knobs and the endpoint *prefix*
are validated at startup only when the mode is not `off`, and an out-of-range number
fails the proxy's configuration check rather than being clamped. Nothing validates
`HEADROOM_JEV_MODEL`, and nothing validates the endpoint beyond its scheme -- a wrong
value in either is a fail-open call at request time, not a startup error.

### What leaves this machine

When the mode is not `off`, Headroom sends the Jev endpoint a request containing:

- the **content of the selected tool results** themselves -- an OpenAI Chat
  `role: "tool"` message, a Responses `function_call_output` /
  `custom_tool_call_output` item, or an Anthropic `tool_result` block. How much of
  each one is sent depends on the track: shadow and Track B send a leading slice
  bounded by `HEADROOM_JEV_MAX_CANDIDATE_TOKENS`, thinned further if the request
  would otherwise exceed `HEADROOM_JEV_MAX_STATE_TOKENS`, and mark it
  `content_truncated_for_view`. Track C sends the candidate whole, up to a fixed
  20,000 characters -- and if the tool output is larger than
  `HEADROOM_JEV_MAX_CANDIDATE_TOKENS x 4` bytes, **nothing is sent at all**: the
  candidate is rejected before the request is built, so no part of an oversized
  Track C tool result leaves the machine;
- per-candidate metadata: candidate type, tool call id, estimated token count and the
  SHA-256 of the *full* content, plus -- in shadow and Track B -- the role,
  message/block index, distance from the end of the conversation and byte length;
- conversation identifiers, the provider and upstream model names, and the Jev model
  name. Headroom does not anonymize the identifiers it forwards, but which identifier
  each track forwards differs, and Track C's is Headroom's own:
    - **shadow**: your session id as Headroom tracks it, a branch id that is a SHA-256
      of that session id and the protected prefix, and a revision hash over the
      candidate set,
    - **Track B** (`/v1/compress`): the `config.session_id` **the caller sent**,
      forwarded unchanged, the literal branch id `compress` (the sidecar route has no
      branch concept, so this keeps its turns in their own lane), and the same
      revision hash,
    - **Track C** (Codex WebSocket): a `uuid4` **minted by Headroom for that
      WebSocket connection** -- not your conversation id, and not derived from
      anything the client sent -- plus the raw Codex `previous_response_id` as the
      branch id and the boundary's item count. Track C sends **no revision hash**.
      Because that id is per connection, a reconnect produces a new one and Jev
      cannot correlate the two; the boundary's own replay protection is keyed on
      `previous_response_id` for exactly that reason;
- fixed English instructions and the keep/truncate/drop criteria.

Candidates are only ever tool results. In shadow mode and Track B they must also lie
outside the protected prefix and outside the last 6 messages; Track C's boundary
carries exactly the one tool output Codex is compacting. User messages, assistant
messages, system prompts and tool *call* arguments are not part of the payload.

Nothing is scrubbed on the way out. Per the design doc: *"No PII anonymization claim;
Jev is a retention-decision service only."* If your tool output would contain secrets,
customer data or anything else you would not hand to a third-party API, do not enable
this feature on that traffic.

### It fails open, everywhere

Once a turn reaches the Jev hooks, every gate that can go wrong forwards Headroom's
ordinary output unchanged: a timeout, a transport or TLS failure, a 4xx/5xx, a
malformed or unreadable answer, an answer for a candidate that was not asked about, a
`truncate` verdict at a boundary that only offers keep/drop, a stale (already-decided)
revision, a candidate that does not fit the request budget or exceeds Track C's byte
ceiling, a frame that does not advertise the `headroom_retrieve` recovery tool, a
missing session identity on Track C, and a failed CCR write, read-back or lease.
Anything ambiguous is read as `keep`, the direction that changes nothing. Shadow mode
additionally cannot affect a request at all: it measures its projection on a private
deep copy and never mutates the message list it is given.

Two things that sound like the above but are not:

- **A malformed boundary request is a 400, not a fail-open.** On `POST /v1/compress`,
  `jev_compaction_boundary=true` with no non-empty `config.session_id` -- or without
  `config.mode="ccr"` -- is rejected with HTTP 400 *before* any compression runs, on
  every proxy including one with `HEADROOM_JEV_MODE=off`. A request that asks for
  retention it cannot get is told so rather than silently served. This is the one
  Jev-related path that changes the response an operator sees, and because it is
  refused before the Jev hooks are reached it emits **no**
  `headroom_jev_events_total` event. Simply *omitting* the flag is a no-op, not an
  error.
- **CCR failure is per candidate on Track B, not all-or-nothing.** Each candidate is
  staged independently, and only candidates with an acknowledged, leased entry are
  rewritten. If one candidate's CCR write fails, that candidate keeps its original
  content while the others are still rewritten; the turn is only left entirely
  untouched when *no* candidate could be staged. The `ccr_staged` / `ccr_acknowledged`
  / `ccr_failed` counters on `/stats` are what make a partial failure visible. Track C
  carries exactly one candidate, so there the distinction does not arise.

Each of the gates above records an event on `headroom_jev_events_total{event}` once a
compaction boundary has actually been recognised, and the aggregate is on `/stats`
under `jev` (with `config` reported through `JevConfig.redacted()`). The cheap
pre-boundary exits are deliberately **not** counted: on Track C a frame that is
disabled, not JSON, not a `response.create`, or simply not a compaction event returns
silently, because those are the overwhelming majority of frames on a live connection
and counting them would drown the signal. So the counters answer "what happened at the
boundaries we saw", not "how many frames went past" -- a deployment where Jev is
configured but never reaches a boundary shows up as an *absence* of
`compaction_boundary_detected`, which is itself the diagnostic.

### Nothing is deleted in active mode

Before a tool result is truncated or replaced, the original is written to the CCR
store under a hash bound to `(session, branch, content)`, read back and compared byte
for byte, and given a **24-hour** retention lease; only then is the slot rewritten. A
candidate without an acknowledged, leased entry keeps its original content whatever
Jev answered, and the message envelope is always preserved -- a `drop` replaces the
*content* with a `[N tokens compressed to 0. ... Retrieve more: hash=...]` marker that
the model redeems with the `headroom_retrieve` tool or `POST /v1/retrieve`, so a tool
result is never removed and never orphans its tool call.

See [CCR](ccr.md#jev-active-retention-v1compress) for the `/v1/compress` boundary
request shape and lease semantics, and [Proxy](proxy.md#jev-compaction-boundary-codex-websocket)
for the Codex WebSocket boundary.

### Reading the numbers on `/stats`

Three savings figures under `jev`, and they are deliberately never summed:

- `projected_savings` -- shadow mode's projection (`TH - TP`): what active mode
  *would* have saved. It is reported here and nowhere else; it is never added to
  realized savings and never reaches the savings ledger or `/stats-history`.
- `realized_savings` -- `TH - TF` over the content active retention actually
  rewrote, measured with a real tokenizer. Only Track B contributes to it; shadow
  mode rewrites nothing, so it has no realized saving at all.
- `realized_savings_estimated` -- the same subtraction for Track C, which runs at a
  WebSocket frame boundary with no tokenizer in reach and prices its candidate at
  `bytes // 4`. The saving is real; its *size* is an estimate, which is why it sits
  beside `realized_savings` instead of inside it. An operator has to read both
  numbers to see the total effect.

### Multi-process and benchmark deployments

The proxy hands its configuration to worker processes through the
`HEADROOM_PROXY_CONFIG_JSON` environment variable, which is readable from the process
table on most platforms. The Jev block is deliberately **left out** of that payload so
the API key cannot leak there; each worker rebuilds it with `JevConfig.from_env()`
instead. Any multi-worker or benchmark deployment must therefore export the
`HEADROOM_JEV_*` variables into the process environment -- setting them only in the
parent's in-memory config will leave the workers with Jev off.

## Settings GUI

A web-based settings interface is available at `http://127.0.0.1:<port>/dashboard/settings` for configuring every safe `HEADROOM_*` proxy knob without hand-exporting environment variables, plus an **Endpoints** group for custom Anthropic/OpenAI upstream base URLs (`ANTHROPIC_TARGET_API_URL` / `OPENAI_TARGET_API_URL`) and extra headers merged into (and overriding) forwarded requests -- e.g. for a corporate gateway or Azure Foundry deployment that needs a different endpoint plus one extra auth header. Fields are split into a **Settings** tab (commonly-tuned: compression ratio, budget, rate limits, verbosity) and an **Advanced** tab (everything else, including Endpoints). Third-party credentials such as `OPENAI_API_KEY`/`AWS_*` are never exposed here; the two extra-headers fields are the only secret-typed fields in the panel and render masked once set, with a "Clear stored value" action to remove them -- resaving the page without touching a masked field never overwrites the real stored value.

- **Persistence**: Settings are saved to `~/.headroom/settings.json` (merged with existing values, not replaced) and loaded into the process environment at startup.
- **Precedence** (highest to lowest):
  - Explicit shell export (`export HEADROOM_FOO=bar`)
  - Settings from `~/.headroom/settings.json`
  - Code default
- **Activation**: Click "Save" to persist without restarting, or "Apply & Restart" to persist and take effect immediately. Apply & Restart behavior depends on how the proxy is running:
  - **Service** (supervised launchd/systemd install): self-restarts in one click.
  - **Docker**: cannot self-restart from inside the container; the GUI surfaces the host-side `headroom install restart --profile <p>` command to run instead.
  - **Task** (Windows Task Scheduler / cron-managed install): `headroom install` does not support lifecycle operations for task deployments; the GUI shows an instruction to restart via the OS task scheduler or by stopping the process so it relaunches on its next trigger.
  - **Foreground** (plain `headroom proxy`): shows a manual-restart instruction.
- **Provenance / locking**: a field currently shadowed by an explicit environment variable export is rendered read-only with a tooltip, since editing it here would have no effect until the env var is unset. Manifest-baked settings (`HEADROOM_PORT`, `HEADROOM_HOST`) are similarly locked on supervised (Docker/Service) installs — managed by the install manifest, not the settings interface.
- **CSRF protection**: `/settings` and `/settings/apply` reject requests whose `Origin` header (when present) doesn't resolve to a loopback host, in addition to the existing loopback-only + Host-header DNS-rebinding guard shared by all admin endpoints.

## Session Beta Header Tracking

When running as a proxy, Headroom maintains a per-session union of `anthropic-beta` (and `OpenAI-Beta`) tokens via `SessionBetaTracker`. The session key is derived from the `x-headroom-session-id` header if present, otherwise from `md5(model + system_prompt[:500])[:16]` — stable across turns of the same conversation.

**Why:** clients such as Claude Code and Codex CLI may drop a beta token between consecutive turns. Because `anthropic-beta` is part of the request bytes that determine the upstream prefix-cache key, a dropped token would bust the cache mid-conversation. The tracker re-injects any token seen earlier in the session so the cache key stays stable.

**Trade-off:** once the proxy has seen a beta token in a session it will continue re-sending it for the rest of that session, even if the client stops including it. Stopping the token on the client side alone is not sufficient — the proxy re-injects it. Set `HEADROOM_BETA_HEADER_STICKY=disabled` to pass the client's `anthropic-beta` value verbatim and bypass this accumulation.

```bash
# Disable sticky beta re-echo
export HEADROOM_BETA_HEADER_STICKY=disabled
headroom proxy ...
```

Note: disabling sticky mode may reduce prefix-cache hit rates for clients that legitimately drop-and-re-add beta tokens across turns.

## Filesystem Contract

Headroom resolves every on-disk resource through a two-root model:

- `HEADROOM_CONFIG_DIR` (default `~/.headroom/config`) — read-mostly
  configuration
- `HEADROOM_WORKSPACE_DIR` (default `~/.headroom`) — read-write state

Precedence for each resource is: explicit argument > per-resource env
var > derived from canonical root > default. Every legacy env var
continues to work unchanged.

See **[Filesystem Contract](filesystem-contract.md)** for the full
bucket table, plugin-author guidance, and the Docker naming overlap
note (`HEADROOM_WORKSPACE` is *not* the same as `HEADROOM_WORKSPACE_DIR`).

---

## Custom Model Configuration

Configure context limits and pricing for new or custom models. Useful when:
- A new model is released before Headroom is updated
- You're using fine-tuned or custom models
- You want to override built-in limits

### Configuration Methods

Settings are resolved in this order (later overrides earlier):
1. Built-in defaults
2. `${HEADROOM_CONFIG_DIR}/models.json` (defaults to
   `~/.headroom/config/models.json`); falls back to the legacy location
   `~/.headroom/models.json` when the canonical file is absent
3. `HEADROOM_MODEL_LIMITS` environment variable
4. SDK constructor arguments

### Config File Format

Create `~/.headroom/models.json`:

```json
{
  "anthropic": {
    "context_limits": {
      "claude-4-opus-20250301": 200000,
      "claude-custom-finetune": 128000
    },
    "pricing": {
      "claude-4-opus-20250301": {
        "input": 15.00,
        "output": 75.00,
        "cached_input": 1.50
      }
    }
  },
  "openai": {
    "context_limits": {
      "gpt-5": 256000,
      "ft:gpt-4o:my-org": 128000
    },
    "pricing": {
      "gpt-5": [5.00, 15.00]
    }
  }
}
```

### Environment Variable

Set `HEADROOM_MODEL_LIMITS` as a JSON string or file path:

```bash
# JSON string
export HEADROOM_MODEL_LIMITS='{"anthropic":{"context_limits":{"claude-new":200000}}}'

# File path
export HEADROOM_MODEL_LIMITS=/path/to/models.json
```

### Pattern-Based Inference

Unknown models are automatically inferred from naming patterns:

| Pattern | Inferred Settings |
|---------|-------------------|
| `*opus*` | 200K context, Opus-tier pricing |
| `*sonnet*` | 200K context, Sonnet-tier pricing |
| `*haiku*` | 200K context, Haiku-tier pricing |
| `gpt-4o*` | 128K context, GPT-4o pricing |
| `o1*`, `o3*` | 200K context, reasoning model pricing |

This means new models like `claude-4-sonnet-20251201` will work automatically with Sonnet-tier defaults.

### SDK Override

Override in code for specific models:

```python
from headroom import HeadroomClient, AnthropicProvider

client = HeadroomClient(
    original_client=Anthropic(),
    provider=AnthropicProvider(
        context_limits={
            "claude-new-model": 300000,
        }
    ),
)
```

## Provider-Specific Settings

### OpenAI

```python
from headroom import OpenAIProvider

provider = OpenAIProvider(
    # Enable automatic prefix caching
    enable_prefix_caching=True,
)
```

### Anthropic

```python
from headroom import AnthropicProvider

provider = AnthropicProvider(
    # Enable cache_control blocks
    enable_cache_control=True,
)
```

### Google

```python
from headroom import GoogleProvider

provider = GoogleProvider(
    # Enable context caching
    enable_context_caching=True,
)
```

## Configuration Precedence

Settings are applied in this order (later overrides earlier):

1. Default values
2. Environment variables
3. SDK constructor arguments
4. Per-request overrides

## Validation

Validate your configuration:

```python
result = client.validate_setup()

if not result["valid"]:
    print("Configuration issues:")
    for issue in result["issues"]:
        print(f"  - {issue}")
```

---

## TypeScript SDK Configuration

The TypeScript SDK is configured via environment variables or constructor options.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HEADROOM_BASE_URL` | Base URL of the Headroom proxy | `http://localhost:8787` |
| `HEADROOM_API_KEY` | Optional API key for authenticated Headroom endpoints | - |

### Usage

```bash
export HEADROOM_BASE_URL=http://localhost:8787
export HEADROOM_API_KEY=your-api-key
```

```typescript
import { HeadroomClient } from 'headroom-ai';

// Reads from HEADROOM_BASE_URL and HEADROOM_API_KEY automatically
const client = new HeadroomClient();

// Or configure explicitly
const client = new HeadroomClient({
  baseUrl: 'http://localhost:8787',
  apiKey: 'your-api-key',
});
```

See the [TypeScript SDK Guide](typescript-sdk.md) for full configuration options.
