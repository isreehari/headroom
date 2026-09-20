#!/usr/bin/env python3
"""One-off, read-only, single-machine research probe (Jev retention Phase 0b).

Question this answers, on THIS machine only: does a real ``headroom wrap codex``
style session ever send a request *through the Headroom proxy* that (a) is
distinguishable as a compaction-related call and (b) still carries prior
tool-call / tool-result content that Headroom could recognize and eventually
rewrite at a native Codex compaction boundary?

Phase 0b of ``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md``.

What this script does NOT do:

* No Jev API calls of any kind.
* No mutation of any request or response *payload* -- the logging shim forwards
  body bytes through untouched. It is a payload-faithful, not byte-identical,
  reverse proxy: like any reverse proxy it rewrites ``Host``, drops hop-by-hop
  headers, recomputes ``Content-Length`` on the way up and re-frames responses
  as chunked on the way down. Headroom itself runs with its normal behaviour.
* No SSH tunnels, no second machine, no allowlisting/aggregation infrastructure.
  This is deliberately much simpler than the abandoned two-Mac
  ``benchmarks/codex_compaction_probe.py``.
* No full message or tool content is recorded anywhere: only HTTP method, path,
  top-level JSON keys, the ordered list of ``input``/``messages`` item *type*
  values, and a handful of boolean compaction signals. Everything stays local
  and in-memory; nothing is transmitted.
* It does not touch the user's Headroom configuration/state: the proxy runs with
  a private throwaway ``HEADROOM_WORKSPACE_DIR`` on an unused loopback port.

The only real network traffic is whatever Codex CLI itself makes for the short
scripted session (via this machine's existing ChatGPT/Codex login), forwarded
upstream by Headroom exactly as it normally would be.

Caveats worth stating up front:

* Driving the session needs ``codex exec resume``, so Codex writes its usual
  session/rollout files under the real ``CODEX_HOME`` (``~/.codex``). The probe
  does not delete the user's Codex state.
* ``codex exec`` has no scriptable ``/compact`` command -- that slash command
  exists only in the interactive TUI. The only scriptable way to provoke native
  compaction is the ``model_auto_compact_token_limit`` config override, which
  this probe sets low and then fills with large shell-output tool results.
* The logging shim speaks HTTP only. Codex may try a WebSocket upgrade on
  ``/v1/responses`` (Headroom's real proxy serves that route over WebSocket as
  well); the shim refuses the upgrade so Codex falls back to HTTP, and the
  report says how often that happened.

Usage::

    python benchmarks/jev_codex_boundary_probe.py [--turns 3] [--auto-compact-limit 20000]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Wire-shape item types that matter for a retention boundary. Carried over from
# the abandoned probe's ALLOWED_TYPES purely as a vocabulary -- none of its
# machinery is reused.
ALLOWED_TYPES = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "reasoning",
        "compaction",
        "compaction_trigger",
        "tool_search_call",
        "tool_search_output",
    }
)

# Item types that represent prior tool-call / tool-result content.
TOOL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "tool_search_call",
        "tool_search_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "tool_call",
        "tool_result",
    }
)

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

MAX_OBSERVATIONS = 2000
MAX_BODY_BYTES = 64 * 1024 * 1024
# Cap on the *decompressed* size we are willing to materialize, so a small
# compressed body cannot expand without bound (zip-bomb shape). Only the local
# Codex CLI ever talks to this shim, but the cap costs nothing.
MAX_DECODED_BYTES = 256 * 1024 * 1024
# Inbound client socket timeout: a wedged Codex process must not hang the probe
# forever in `_read_body()`.
SHIM_CLIENT_TIMEOUT = 300.0
# Upper bounds on operator-supplied CLI values, so a typo cannot fan out into a
# long run of real provider turns.
MAX_TURNS = 10
MAX_TURN_TIMEOUT = 1800.0


# ---------------------------------------------------------------------------
# Observation model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """Structural shape of one request the Headroom proxy received."""

    seq: int
    method: str
    path: str
    body_bytes: int
    top_keys: tuple[str, ...] = ()
    array_field: str | None = None
    item_types: tuple[str, ...] = ()
    compaction_signals: tuple[str, ...] = ()
    note: str | None = None

    @property
    def is_compaction_related(self) -> bool:
        return bool(self.compaction_signals)

    @property
    def tool_item_types(self) -> tuple[str, ...]:
        return tuple(t for t in self.item_types if t in TOOL_ITEM_TYPES)

    @property
    def has_prior_tool_items(self) -> bool:
        return bool(self.tool_item_types)

    def shape_key(self) -> tuple[Any, ...]:
        return (
            self.method,
            self.path,
            self.top_keys,
            self.array_field,
            self.item_types,
            self.compaction_signals,
            self.note,
        )


# Patterns for anything token-shaped that could ride along in a Codex or
# Headroom error line. This probe never prints request content, but a provider
# auth error could echo part of a credential, so every diagnostic tail goes
# through `redact()` before it reaches stderr.
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\.?[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|authorization)\b\s*[:=]?\s*[A-Za-z0-9_\-./+]{8,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def redact(text: str) -> str:
    """Mask credential-shaped substrings in a diagnostic line."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def _collapse(types: tuple[str, ...]) -> str:
    """Render an ordered item-type list compactly: a*3, b, a*2."""
    if not types:
        return "[]"
    parts: list[str] = []
    current = types[0]
    count = 1
    for item in types[1:]:
        if item == current:
            count += 1
            continue
        parts.append(current if count == 1 else f"{current}*{count}")
        current, count = item, 1
    parts.append(current if count == 1 else f"{current}*{count}")
    return "[" + ", ".join(parts) + "]"


def _item_type(item: Any) -> str:
    """Return a type label for one input/messages item (never its content)."""
    if not isinstance(item, dict):
        return f"<{type(item).__name__}>"
    raw = item.get("type")
    if isinstance(raw, str) and raw:
        return raw
    role = item.get("role")
    if isinstance(role, str) and role:
        return f"message:{role}"
    return "<untyped>"


class _TooLarge(Exception):
    """Decompressed payload exceeded MAX_DECODED_BYTES."""


def _inflate(body: bytes, wbits: int) -> bytes:
    """Incrementally inflate, refusing to materialize more than the cap."""
    obj = zlib.decompressobj(wbits)
    out = obj.decompress(body, MAX_DECODED_BYTES + 1)
    if len(out) > MAX_DECODED_BYTES or obj.unconsumed_tail:
        raise _TooLarge
    return out


def decode_body(body: bytes, content_encoding: str | None) -> tuple[bytes, str | None]:
    """Best-effort decompression of a request body. Returns (bytes, note).

    Bounded by ``MAX_DECODED_BYTES`` so a small compressed body cannot expand
    without limit. Decoding failures are never fatal: they degrade into a note.
    """
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return body, None
    try:
        if encoding in {"gzip", "x-gzip"}:
            return _inflate(body, 31), None
        if encoding == "deflate":
            # Some senders emit raw deflate rather than zlib-wrapped.
            try:
                return _inflate(body, 15), None
            except zlib.error:
                return _inflate(body, -15), None
        if encoding == "zstd":
            # Codex compresses Responses request bodies with zstd. Python 3.14
            # ships a stdlib decoder; fall back to the third-party module.
            try:
                from compression.zstd import (  # noqa: PLC0415
                    ZstdDecompressor as StdZstdDecompressor,
                )
            except ImportError:
                import zstandard  # noqa: PLC0415 - optional third-party fallback

                decoded = zstandard.ZstdDecompressor().decompress(
                    body, max_output_size=MAX_DECODED_BYTES + 1
                )
            else:
                decoded = StdZstdDecompressor().decompress(body, MAX_DECODED_BYTES + 1)
            if len(decoded) > MAX_DECODED_BYTES:
                raise _TooLarge
            return decoded, None
        if encoding == "br":
            import brotli  # noqa: PLC0415 - optional, only if the client used brotli

            decoded = brotli.decompress(body)
            if len(decoded) > MAX_DECODED_BYTES:
                raise _TooLarge
            return decoded, None
    except _TooLarge:
        return body, f"oversized {encoding} body (decodes past {MAX_DECODED_BYTES} bytes)"
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fatal
        return body, f"undecodable {encoding} body ({type(exc).__name__})"
    return body, f"unknown content-encoding {encoding!r}"


def summarize_body(
    body: bytes,
    method: str,
    path: str,
    seq: int,
    content_encoding: str | None = None,
) -> Observation:
    """Build a content-free structural summary of a request body."""
    if not body:
        return Observation(seq=seq, method=method, path=path, body_bytes=0, note="empty body")
    raw_len = len(body)
    body, decode_note = decode_body(body, content_encoding)
    if decode_note:
        return Observation(seq=seq, method=method, path=path, body_bytes=raw_len, note=decode_note)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return Observation(
            seq=seq, method=method, path=path, body_bytes=raw_len, note="non-JSON body"
        )
    if not isinstance(payload, dict):
        return Observation(
            seq=seq,
            method=method,
            path=path,
            body_bytes=raw_len,
            note=f"JSON {type(payload).__name__} body",
        )

    top_keys = tuple(sorted(str(k) for k in payload))
    array_field: str | None = None
    items: list[Any] = []
    for candidate in ("input", "messages"):
        value = payload.get(candidate)
        if isinstance(value, list):
            array_field = candidate
            items = value
            break

    item_types = tuple(_item_type(item) for item in items)

    signals: list[str] = []
    if any("compact" in t.lower() for t in item_types):
        signals.append("item-type-contains-compact")
    if any("compact" in key.lower() for key in top_keys):
        signals.append("top-level-key-contains-compact")
    if "previous_response_id" in payload and payload.get("previous_response_id") is not None:
        signals.append("previous_response_id")
    if any(
        isinstance(item, dict) and item.get("previous_response_id") is not None for item in items
    ):
        signals.append("item-previous_response_id")
    if "compact" in path.lower():
        signals.append("path-contains-compact")

    return Observation(
        seq=seq,
        method=method,
        path=path,
        body_bytes=raw_len,
        top_keys=top_keys,
        array_field=array_field,
        item_types=item_types,
        compaction_signals=tuple(signals),
    )


# ---------------------------------------------------------------------------
# Logging reverse-proxy shim (sits in front of the real Headroom proxy)
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    observations: list[Observation] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    dropped: int = 0
    next_seq: int = 1

    def record(self, make: Any) -> None:
        # The sequence number is claimed under the lock (not derived from the
        # list length after releasing it), so concurrent requests can never be
        # handed the same seq. Summarizing happens outside the lock because it
        # parses the body.
        with self.lock:
            if self.next_seq > MAX_OBSERVATIONS:
                self.dropped += 1
                return
            seq = self.next_seq
            self.next_seq += 1
        observation = make(seq)
        with self.lock:
            self.observations.append(observation)


class ShimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # socketserver.StreamRequestHandler applies this to the client socket, so a
    # stalled or wedged Codex process cannot block `_read_body()` forever.
    timeout = SHIM_CLIENT_TIMEOUT
    upstream_host = "127.0.0.1"
    upstream_port = 0
    recorder: Recorder = Recorder()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib hook
        return  # keep the probe's stdout clean

    # -- body helpers -----------------------------------------------------
    def _read_body(self) -> bytes:
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunk = self.rfile.read(size)
                self.rfile.readline()
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    raise ValueError("request body too large")
                chunks.append(chunk)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def _forward_headers(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in {"host", "content-length"}:
                continue
            out.append((key, value))
        return out

    # -- main proxy path --------------------------------------------------
    def _proxy(self) -> None:
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            # The shim is HTTP-only; record the attempt honestly and refuse so
            # Codex falls back to the HTTP transport.
            self.recorder.record(
                lambda seq: Observation(
                    seq=seq,
                    method=self.command,
                    path=self.path,
                    body_bytes=0,
                    note="websocket upgrade attempted (refused by probe shim)",
                )
            )
            self.send_response(501)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            body = self._read_body()
        except (ValueError, OSError) as exc:
            reason = f"body read failed: {exc}"
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.recorder.record(
                lambda seq: Observation(
                    seq=seq,
                    method=self.command,
                    path=self.path,
                    body_bytes=0,
                    note=reason,
                )
            )
            return

        method, path = self.command, self.path
        encoding = self.headers.get("Content-Encoding")
        self.recorder.record(lambda seq: summarize_body(body, method, path, seq, encoding))

        conn = HTTPConnection(self.upstream_host, self.upstream_port, timeout=900)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"{self.upstream_host}:{self.upstream_port}")
            for key, value in self._forward_headers():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            response = conn.getresponse()

            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            # Re-frame every response as chunked: streaming SSE bodies have no
            # Content-Length, and a fixed one would be wrong after decoding.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (OSError, ConnectionError) as exc:
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass
            print(f"  [shim] upstream error for {method} {path}: {exc}", file=sys.stderr)
        finally:
            conn.close()

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_HEAD = _proxy
    do_OPTIONS = _proxy


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that does not dump a traceback on client resets."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        print(f"  [shim] handler error from {client_address}: {exc!r}", file=sys.stderr)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Headroom proxy lifecycle
# ---------------------------------------------------------------------------


def start_headroom_proxy(port: int, workspace: Path, log_path: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["HEADROOM_WORKSPACE_DIR"] = str(workspace)
    env["HEADROOM_TELEMETRY"] = "off"
    env.pop("HEADROOM_PORT", None)
    log = log_path.open("wb")
    return subprocess.Popen(
        ["headroom", "proxy", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_for_health(port: int, process: subprocess.Popen[bytes], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"headroom proxy exited early (rc={process.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - loopback
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    raise RuntimeError("headroom proxy did not become healthy in time")


# ---------------------------------------------------------------------------
# Scripted Codex session
# ---------------------------------------------------------------------------

PROMPTS = [
    (
        "Run exactly this shell command and then tell me only the very last line "
        "of its output: seq 1 6000 | awk '{print $1, $1*$1, \"filler-token-row\"}'"
    ),
    (
        "Run exactly this shell command and then tell me only how many lines it printed: "
        "seq 6001 12000 | awk '{print $1, $1*3, \"another-filler-row\"}'"
    ),
    (
        "Using only what you already have in this conversation, state the last line of the "
        "first command's output and the line count of the second. Do not run any new commands."
    ),
]


def codex_common_flags(shim_port: int, auto_compact_limit: int) -> list[str]:
    """Flags accepted by BOTH `codex exec` and `codex exec resume`.

    `codex exec resume` takes neither `-C/--cd` nor `-s/--sandbox`, so the
    working directory comes from the subprocess cwd and the sandbox from a
    `-c` override.
    """
    base_url = f"http://127.0.0.1:{shim_port}/v1"
    return [
        "--json",
        "--skip-git-repo-check",
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'model_provider="openai"',
        "-c",
        f'openai_base_url="{base_url}"',
        "-c",
        f"model_auto_compact_token_limit={auto_compact_limit}",
        "-c",
        "notify=[]",
        # The shim is HTTP-only; keep Codex on the HTTP Responses transport so
        # every request is observable.
        "--disable",
        "responses_websockets",
        "--disable",
        "responses_websockets_v2",
    ]


def extract_session_id(stdout: str) -> str | None:
    """Pull a session/thread id out of `codex exec --json` JSONL output."""
    keys = ("thread_id", "session_id", "conversation_id", "threadId", "sessionId")

    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and len(value) >= 8:
                    return value
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        found = walk(event)
        if found:
            return found
    return None


def run_codex_turn(
    args: list[str], env: dict[str, str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, local CLI
        args,
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def drive_session(
    *,
    shim_port: int,
    workdir: Path,
    turns: int,
    auto_compact_limit: int,
    turn_timeout: float,
) -> list[dict[str, Any]]:
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{shim_port}/v1"
    flags = codex_common_flags(shim_port, auto_compact_limit)
    results: list[dict[str, Any]] = []
    session_id: str | None = None

    for index in range(turns):
        prompt = PROMPTS[index % len(PROMPTS)]
        if index == 0:
            argv = ["codex", "exec", *flags, prompt]
        elif session_id:
            argv = ["codex", "exec", "resume", *flags, session_id, prompt]
        else:
            # Deliberately NOT falling back to `codex exec resume --last`: that
            # resumes whichever Codex session was most recently touched on this
            # machine, which may not be ours, and would silently corrupt the
            # observed turn sequence. Stop instead and report what we have.
            reason = (
                "no session id recovered from turn 1 output; refusing to "
                "`resume --last` (could attach to an unrelated session)"
            )
            print(f"  turn {index + 1}/{turns}: ABORTED -- {reason}")
            results.append(
                {
                    "turn": index + 1,
                    "returncode": None,
                    "seconds": 0.0,
                    "session_id": None,
                    "stderr_tail": [],
                    "note": reason,
                }
            )
            break
        print(f"  turn {index + 1}/{turns}: {' '.join(argv[:3])} ... (prompt {index + 1})")
        started = time.monotonic()
        try:
            completed = run_codex_turn(argv, env, workdir, turn_timeout)
            rc: int | None = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            rc = None
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        elapsed = time.monotonic() - started
        if session_id is None:
            session_id = extract_session_id(stdout)
        results.append(
            {
                "turn": index + 1,
                "returncode": rc,
                "seconds": round(elapsed, 1),
                "session_id": session_id,
                "stderr_tail": [redact(line) for line in stderr.strip().splitlines()[-3:]]
                if stderr.strip()
                else [],
            }
        )
        print(f"    rc={rc} in {elapsed:.1f}s session={session_id}")
        if rc not in (0, None):
            for line in results[-1]["stderr_tail"]:
                print(f"    stderr: {line}")
            # A failed turn usually means later turns fail identically; keep going
            # anyway so the report shows whatever the proxy did observe.
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(
    observations: list[Observation],
    turn_results: list[dict[str, Any]],
    dropped: int,
    auto_compact_limit: int,
) -> None:
    print()
    print("=" * 78)
    print("JEV PHASE 0b -- CODEX NATIVE COMPACTION BOUNDARY PROBE")
    print("=" * 78)
    print()
    print(f"Codex turns run:            {len(turn_results)}")
    for result in turn_results:
        print(
            f"  turn {result['turn']}: rc={result['returncode']} "
            f"{result['seconds']}s session={result['session_id']}"
        )
        if result.get("note"):
            print(f"        note: {result['note']}")
    print(f"model_auto_compact_token_limit: {auto_compact_limit}")
    print()
    print(
        f"Total requests observed:    {len(observations)}"
        + (f" (+{dropped} dropped)" if dropped else "")
    )

    by_path = Counter(f"{o.method} {o.path}" for o in observations)
    print("Requests by route:")
    for route, count in by_path.most_common():
        print(f"  {count:4d}  {route}")
    print()

    print("Distinct request shapes (structural only, no content):")
    shapes: dict[tuple[Any, ...], list[Observation]] = {}
    for observation in observations:
        shapes.setdefault(observation.shape_key(), []).append(observation)
    for index, (_key, group) in enumerate(shapes.items(), start=1):
        head = group[0]
        print(f"  [{index}] x{len(group)}  {head.method} {head.path}")
        if head.note:
            print(f"        note: {head.note}")
        if head.top_keys:
            print(f"        top-level keys: {list(head.top_keys)}")
        if head.array_field:
            print(
                f"        {head.array_field}[{len(head.item_types)}] types: "
                f"{_collapse(head.item_types)}"
            )
        if head.compaction_signals:
            print(f"        compaction signals: {list(head.compaction_signals)}")
        sizes = sorted(o.body_bytes for o in group)
        print(f"        body bytes: min={sizes[0]} max={sizes[-1]}")
    print()

    compaction_hits = [o for o in observations if o.is_compaction_related]
    tool_hits = [o for o in observations if o.has_prior_tool_items]
    both = [o for o in observations if o.is_compaction_related and o.has_prior_tool_items]

    print("Evidence:")
    print(f"  (a) requests distinguishable as compaction-related: {len(compaction_hits)}")
    for observation in compaction_hits[:10]:
        print(
            f"        #{observation.seq} {observation.method} {observation.path} "
            f"signals={list(observation.compaction_signals)} "
            f"tool_items={len(observation.tool_item_types)}"
        )
    print(f"  (b) requests carrying prior tool-call/result items: {len(tool_hits)}")
    if tool_hits:
        widest = max(tool_hits, key=lambda o: len(o.tool_item_types))
        print(
            f"        widest: #{widest.seq} {widest.method} {widest.path} "
            f"{len(widest.tool_item_types)} tool items of "
            f"{len(widest.item_types)} total -> {_collapse(widest.item_types)}"
        )
    unknown = sorted(
        {t for o in observations for t in o.item_types if t not in ALLOWED_TYPES and o.item_types}
    )
    if unknown:
        print(f"  item types seen outside the known ALLOWED_TYPES vocabulary: {unknown}")
    print()

    ws_attempts = sum(1 for o in observations if o.note and "websocket" in o.note)
    print("Caveats for reading this result:")
    print(
        "  * `codex exec` exposes no scriptable `/compact` command (that is a TUI-only slash "
        "command). The only scriptable trigger is the "
        "`model_auto_compact_token_limit` config override used above; if the session never "
        "crossed that limit, native compaction simply never ran."
    )
    if ws_attempts:
        print(
            f"  * Codex attempted a WebSocket upgrade on /v1/responses {ws_attempts} time(s). "
            "This HTTP-only shim refused them (501) and Codex fell back to the HTTP Responses "
            "transport. Headroom's real proxy does serve /v1/responses over WebSocket, so a "
            "wrapped session may use that transport instead; shapes seen here are the HTTP ones."
        )
    print()

    if both:
        print("VERDICT: boundary observed")
        print(
            f"  {len(both)} request(s) were both compaction-distinguishable and carried prior "
            "tool-call/result items:"
        )
        for observation in both[:10]:
            print(
                f"    #{observation.seq} {observation.method} {observation.path} "
                f"signals={list(observation.compaction_signals)} "
                f"items={_collapse(observation.item_types)}"
            )
    else:
        print("VERDICT: boundary not observed")
        if not observations:
            print("  No requests reached the proxy at all -- the session never got on the wire.")
        elif not compaction_hits:
            print(
                "  No observed request was distinguishable as compaction-related "
                "(no compaction-ish item type, no compaction-ish top-level key, "
                "no previous_response_id, no compaction path)."
            )
            if tool_hits:
                print(
                    "  Prior tool-call/result items WERE visible on ordinary turns, so the "
                    "content is on the wire; what is missing is a recognizable boundary marker."
                )
        else:
            print(
                "  Compaction-related requests were seen, but none of them carried prior "
                "tool-call/result items -- i.e. only a fresh window crossed the wire."
            )
    print("=" * 78)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def bounded_int(low: int, high: int) -> Any:
    """argparse type: an int in [low, high]. Guards against fat-fingered runs."""

    def parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {value}")
        return value

    return parse


def bounded_float(low: float, high: float) -> Any:
    """argparse type: a float in [low, high]."""

    def parse(raw: str) -> float:
        value = float(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {value}")
        return value

    return parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--turns",
        type=bounded_int(1, MAX_TURNS),
        default=3,
        help=f"Codex turns to run, 1-{MAX_TURNS} (default: 3)",
    )
    parser.add_argument(
        "--auto-compact-limit",
        type=bounded_int(1000, 1_000_000),
        default=20000,
        help="model_auto_compact_token_limit override used to provoke native compaction",
    )
    parser.add_argument(
        "--turn-timeout",
        type=bounded_float(10.0, MAX_TURN_TIMEOUT),
        default=300.0,
        help=f"Seconds per codex exec turn, 10-{MAX_TURN_TIMEOUT:.0f} (default: 300)",
    )
    args = parser.parse_args()

    # Turns are slow; keep progress visible when stdout is redirected to a file.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    if shutil.which("headroom") is None:
        print("headroom CLI not found on PATH", file=sys.stderr)
        return 2
    if shutil.which("codex") is None:
        print("codex CLI not found on PATH", file=sys.stderr)
        return 2

    proxy_port = free_port()
    shim_port = free_port()
    tmp_root = Path(tempfile.mkdtemp(prefix="jev-codex-boundary-"))
    workspace = tmp_root / "headroom-workspace"
    workdir = tmp_root / "session-cwd"
    workspace.mkdir(parents=True)
    workdir.mkdir(parents=True)
    log_path = tmp_root / "headroom-proxy.log"

    proxy: subprocess.Popen[bytes] | None = None
    server: QuietThreadingHTTPServer | None = None
    recorder = Recorder()

    print("Jev Phase 0b probe -- read-only, single machine, no Jev calls, no mutation.")
    print(f"  temp root:     {tmp_root}")
    print(f"  headroom port: {proxy_port}   logging shim port: {shim_port}")

    try:
        proxy = start_headroom_proxy(proxy_port, workspace, log_path)
        print("  starting headroom proxy ...")
        wait_for_health(proxy_port, proxy)
        print("  headroom proxy healthy")

        ShimHandler.upstream_port = proxy_port
        ShimHandler.recorder = recorder
        server = QuietThreadingHTTPServer(("127.0.0.1", shim_port), ShimHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print("  logging shim listening")

        turn_results = drive_session(
            shim_port=shim_port,
            workdir=workdir,
            turns=args.turns,
            auto_compact_limit=args.auto_compact_limit,
            turn_timeout=args.turn_timeout,
        )

        with recorder.lock:
            observations = list(recorder.observations)
            dropped = recorder.dropped
        print_report(observations, turn_results, dropped, args.auto_compact_limit)
        return 0
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass
        if proxy is not None and proxy.poll() is None:
            try:
                os.killpg(os.getpgid(proxy.pid), signal.SIGTERM)
                proxy.wait(timeout=15)
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(proxy.pid), signal.SIGKILL)
                    proxy.wait(timeout=5)
                except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                    pass
            if proxy.poll() is None:
                print(
                    f"  WARNING: headroom proxy pid {proxy.pid} may still be running",
                    file=sys.stderr,
                )
        if log_path.exists():
            tail = log_path.read_text(errors="replace").strip().splitlines()[-3:]
            if tail and any("Traceback" in line or "ERROR" in line for line in tail):
                print("  headroom proxy log tail (redacted):", file=sys.stderr)
                for line in tail:
                    print(f"    {redact(line)}", file=sys.stderr)
        shutil.rmtree(tmp_root, ignore_errors=True)
        if tmp_root.exists():
            print(f"  WARNING: temp root not fully removed, delete manually: {tmp_root}")
        else:
            print(f"  cleaned up {tmp_root}")


if __name__ == "__main__":
    raise SystemExit(main())
