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
* No full message or tool content is recorded anywhere: only the transport
  (HTTP method, or the literal ``WS``), path, top-level JSON keys, the ordered
  list of ``input``/``messages`` item *type* values, and a handful of boolean
  compaction signals. Everything stays local and in-memory; nothing is
  transmitted. WebSocket frames go through the same ``summarize_body`` path as
  HTTP bodies, so the same no-content guarantee covers both transports.
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
  this probe sets low and then fills with large shell-output tool results. An
  earlier run left this at the 20000 default and never crossed it, so native
  auto-compaction never had a chance to fire; pass a low ``--auto-compact-limit``
  (a few thousand) to actually exercise the boundary. That gap is closed.
* The shim now relays WebSocket as well as HTTP. An earlier run refused Codex's
  ``/v1/responses`` upgrade with 501 and only ever saw the HTTP fallback; the
  shim now forwards the handshake to Headroom's real WebSocket route and pipes
  frames in both directions, summarizing each client->proxy JSON frame through
  the same recorder the HTTP side uses. That gap is closed. Frame reads carry
  an absolute per-frame deadline (``WS_FRAME_TIMEOUT``) on top of the idle
  socket clock, so a stalled or byte-trickling peer cannot pin a relay thread
  or its sockets open for an unbounded time.
* Still open: only the *client to proxy* direction is summarized, on both
  transports. Server-sent event frames are relayed untouched and unrecorded, so
  a compaction signal that exists only in a provider *response* would not be
  seen here. Likewise this observes one Codex version, one model and one short
  scripted session on one machine -- a negative result is evidence, not proof.
* Still open: ``permessage-deflate`` WebSocket payloads are inflated with a
  best-effort persistent inflater for logging only. If that decode ever fails
  the frame is still relayed byte-for-byte, but it is reported as an
  undecodable-frame note rather than a shape.

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

# WebSocket relay tuning. The handshake is a normal HTTP round trip; the relay
# that follows can idle for a whole model turn, so it gets a much longer clock
# than SHIM_CLIENT_TIMEOUT allows for plain HTTP.
WS_HANDSHAKE_TIMEOUT = 60.0
WS_IDLE_TIMEOUT = 900.0
# Absolute wall-clock budget for assembling ONE frame once its first header byte
# has arrived. WS_IDLE_TIMEOUT is a per-recv idle clock and therefore resets on
# every partial read, so on its own it lets a peer that trickles bytes hold a
# frame read (and its socket) open indefinitely. This is a hard ceiling that
# does not reset: a frame whose payload is still incomplete after this many
# seconds aborts the relay. It only starts once the peer has begun a frame, so
# a legitimately idle connection between turns is still governed by
# WS_IDLE_TIMEOUT alone.
WS_FRAME_TIMEOUT = 120.0
WS_OPCODE_CONTINUATION = 0x0
WS_OPCODE_TEXT = 0x1
WS_OPCODE_BINARY = 0x2
WS_OPCODE_CLOSE = 0x8


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
# WebSocket frame plumbing (RFC 6455) -- relay only, never rewrite
# ---------------------------------------------------------------------------


def _ws_unmask(payload: bytes, key: bytes) -> bytes:
    """XOR-unmask a client frame payload. Returns a *copy*; the wire bytes that
    get forwarded upstream are always the original masked ones."""
    if not payload or not key:
        return payload
    repeated = (key * (len(payload) // len(key) + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
        len(payload), "big"
    )


def _read_exact(
    reader: Any,
    count: int,
    *,
    deadline: float | None = None,
    sock: socket.socket | None = None,
) -> bytes:
    """Read exactly ``count`` bytes from a buffered reader, or raise.

    ``deadline`` is an absolute ``time.monotonic()`` instant. Unlike a socket
    timeout -- which is an *idle* clock and resets on every partial recv -- this
    ceiling does not move, so a peer trickling one byte at a time cannot keep a
    single frame read (and its socket) open beyond the budget. When ``sock`` is
    given its timeout is also clamped to the time remaining, so no individual
    blocking read can overshoot the deadline either.
    """
    if count == 0:
        return b""
    # ``read1`` returns as soon as *any* buffered/available bytes exist, at most
    # one underlying recv. Plain ``read(n)`` would block inside the buffered
    # reader until all n bytes arrived, which would hide the deadline check
    # below entirely -- a peer trickling one byte at a time satisfies every
    # individual recv, so neither the socket timeout nor an outer-loop check
    # would ever fire. Looping over short reads is what makes the deadline real.
    read_some = getattr(reader, "read1", None) or reader.read
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ConnectionError(
                    f"websocket frame incomplete after {WS_FRAME_TIMEOUT:.0f}s "
                    f"({remaining} of {count} bytes still outstanding)"
                )
            if sock is not None:
                try:
                    sock.settimeout(left)
                except OSError:
                    pass
        chunk = read_some(remaining)
        if not chunk:
            raise ConnectionError("websocket peer closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_http_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read a raw HTTP message head off a socket. Returns (head, leftover)."""
    buffer = b""
    # Same reasoning as _read_exact: WS_HANDSHAKE_TIMEOUT is a per-recv idle
    # clock, so cap the whole head read with an absolute deadline too.
    deadline = time.monotonic() + WS_HANDSHAKE_TIMEOUT
    try:
        while b"\r\n\r\n" not in buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ConnectionError("upstream websocket handshake response timed out")
            try:
                sock.settimeout(left)
            except OSError:
                pass
            chunk = sock.recv(8192)
            if not chunk:
                raise ConnectionError("upstream closed during websocket handshake")
            buffer += chunk
            if len(buffer) > 128 * 1024:
                raise ConnectionError("upstream handshake response head too large")
    finally:
        # Hand the socket back with the plain handshake timeout, not whatever
        # sliver of the deadline was left; the caller re-arms WS_IDLE_TIMEOUT
        # itself once an upgrade is confirmed.
        try:
            sock.settimeout(WS_HANDSHAKE_TIMEOUT)
        except OSError:
            pass
    head, _, leftover = buffer.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", leftover


def _status_code(head: bytes) -> int:
    try:
        return int(head.split(b"\r\n", 1)[0].split(b" ")[1])
    except (IndexError, ValueError):
        return 0


def _looks_like_json(payload: bytes) -> bool:
    stripped = payload.lstrip()
    return bool(stripped) and stripped[:1] in (b"{", b"[")


@dataclass
class WSFrameRelay:
    """Frame-aware reader for the client->proxy half of a WebSocket relay.

    Yields the *raw* frame bytes (to be forwarded upstream byte-for-byte) plus,
    when a data message completes, a decoded copy used only to build a
    structural summary. Nothing here ever mutates what is forwarded.
    """

    inflater: Any = None
    inflate_broken: bool = False
    frag_opcode: int | None = None
    frag_rsv1: bool = False
    frag_buffer: bytearray = field(default_factory=bytearray)
    frag_oversized: bool = False
    closing: bool = False

    def _inflate(self, payload: bytes) -> tuple[bytes | None, str | None]:
        """Best-effort permessage-deflate inflate of one complete message."""
        if self.inflate_broken:
            return None, None
        if self.inflater is None:
            self.inflater = zlib.decompressobj(-15)
        try:
            out = self.inflater.decompress(payload + b"\x00\x00\xff\xff", MAX_DECODED_BYTES + 1)
        except zlib.error as exc:
            # Context takeover means one failure poisons the stream; stop
            # decoding rather than emit misleading shapes.
            self.inflate_broken = True
            return None, f"undecodable permessage-deflate frame ({type(exc).__name__})"
        if len(out) > MAX_DECODED_BYTES or self.inflater.unconsumed_tail:
            self.inflate_broken = True
            return None, "oversized permessage-deflate frame"
        return out, None

    def read_frame(
        self, reader: Any, sock: socket.socket | None = None
    ) -> tuple[bytes, bytes | None, str | None]:
        """Read one frame. Returns (raw_bytes, complete_message_or_None, note).

        Waiting for the *start* of a frame is unbounded here on purpose -- a
        WebSocket can legitimately sit idle for a whole model turn, and that
        wait is governed by the socket's WS_IDLE_TIMEOUT. Once the first header
        byte lands, the rest of the frame is on a fixed WS_FRAME_TIMEOUT budget
        that does not reset on partial reads.
        """
        header = _read_exact(reader, 2)
        deadline = time.monotonic() + WS_FRAME_TIMEOUT
        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        rsv1 = bool(first & 0x40)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        extension = b""
        try:
            if length == 126:
                extension = _read_exact(reader, 2, deadline=deadline, sock=sock)
                length = int.from_bytes(extension, "big")
            elif length == 127:
                extension = _read_exact(reader, 8, deadline=deadline, sock=sock)
                length = int.from_bytes(extension, "big")
            if length > MAX_BODY_BYTES:
                raise ConnectionError(f"websocket frame payload too large ({length} bytes)")

            mask_key = _read_exact(reader, 4, deadline=deadline, sock=sock) if masked else b""
            payload = _read_exact(reader, length, deadline=deadline, sock=sock)
        finally:
            # Restore the idle clock for the next frame's (legitimately long)
            # header wait, whether this frame completed or aborted.
            if sock is not None:
                try:
                    sock.settimeout(WS_IDLE_TIMEOUT)
                except OSError:
                    pass
        raw = header + extension + mask_key + payload

        if opcode >= 0x8:  # control frame: relay, never summarize
            if opcode == WS_OPCODE_CLOSE:
                self.closing = True
            return raw, None, None

        plain = _ws_unmask(payload, mask_key) if masked else payload

        if opcode != WS_OPCODE_CONTINUATION:
            self.frag_opcode = opcode
            self.frag_rsv1 = rsv1
            self.frag_buffer = bytearray()
            self.frag_oversized = False
        if self.frag_opcode is None:
            return raw, None, "orphan websocket continuation frame"
        if len(self.frag_buffer) + len(plain) > MAX_BODY_BYTES:
            self.frag_oversized = True
            self.frag_buffer = bytearray()
        else:
            self.frag_buffer.extend(plain)

        if not fin:
            return raw, None, None

        opcode_done, rsv1_done = self.frag_opcode, self.frag_rsv1
        message = bytes(self.frag_buffer)
        oversized = self.frag_oversized
        self.frag_opcode = None
        self.frag_buffer = bytearray()
        self.frag_oversized = False

        if oversized:
            return raw, None, "oversized websocket message (not summarized)"
        if rsv1_done:
            inflated, note = self._inflate(message)
            if inflated is None:
                return raw, None, note
            message = inflated
        if opcode_done not in (WS_OPCODE_TEXT, WS_OPCODE_BINARY):
            return raw, None, None
        if not _looks_like_json(message):
            return raw, None, None
        return raw, message, None


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

    # -- websocket relay --------------------------------------------------
    def _handshake_bytes(self) -> bytes:
        """Rebuild the client's upgrade request with only ``Host`` rewritten."""
        lines = [f"{self.command} {self.path} {self.request_version}"]
        for key, value in self.headers.items():
            if key.lower() == "host":
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {self.upstream_host}:{self.upstream_port}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _note(self, path: str, note: str) -> None:
        self.recorder.record(
            lambda seq: Observation(seq=seq, method="WS", path=path, body_bytes=0, note=note)
        )

    def _pump_to_client(self, upstream: socket.socket) -> None:
        """Relay proxy->client frames verbatim. Responses are never summarized."""
        try:
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            try:
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def _relay_client_frames(self, upstream: socket.socket, path: str) -> None:
        relay = WSFrameRelay()
        seen_notes: set[str] = set()
        while True:
            raw, message, note = relay.read_frame(self.rfile, self.connection)
            # Forward first, summarize second: logging never delays the relay
            # and never touches the bytes on the wire.
            upstream.sendall(raw)
            if note and note not in seen_notes:
                seen_notes.add(note)
                self._note(path, note)
            if message is not None:
                self.recorder.record(
                    lambda seq, body=message: summarize_body(body, "WS", path, seq, None)
                )
            if relay.closing:
                break

    def _proxy_websocket(self) -> None:
        path = self.path
        self.close_connection = True
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=WS_HANDSHAKE_TIMEOUT
            )
        except OSError as exc:
            self._note(path, f"websocket upstream connect failed ({type(exc).__name__})")
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass
            return

        pump: threading.Thread | None = None
        try:
            upstream.sendall(self._handshake_bytes())
            head, leftover = _read_http_head(upstream)
            status = _status_code(head)
            self.wfile.write(head)
            if leftover:
                self.wfile.write(leftover)
            self.wfile.flush()

            if status != 101:
                self._note(path, f"websocket upgrade relayed; upstream declined ({status})")
                self._pump_to_client(upstream)
                return

            self._note(path, "websocket upgrade relayed to headroom proxy (101)")
            try:
                self.connection.settimeout(WS_IDLE_TIMEOUT)
            except OSError:
                pass
            upstream.settimeout(WS_IDLE_TIMEOUT)
            pump = threading.Thread(target=self._pump_to_client, args=(upstream,), daemon=True)
            pump.start()
            self._relay_client_frames(upstream, path)
        except (OSError, ConnectionError, ValueError) as exc:
            self._note(path, f"websocket relay ended ({type(exc).__name__})")
        finally:
            for sock in (upstream, self.connection):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if pump is not None:
                pump.join(timeout=5.0)
            try:
                upstream.close()
            except OSError:
                pass

    # -- main proxy path --------------------------------------------------
    def _proxy(self) -> None:
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._proxy_websocket()
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
        # The shim now relays WebSocket as well as HTTP, so Codex's own
        # transport choice is left alone: whichever it picks is observable, and
        # whichever it picks is what a real wrapped session would use.
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


def _transport(observation: Observation) -> str:
    """Which wire the observation came off: the HTTP shim or the WS relay."""
    return "ws" if observation.method == "WS" else "http"


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
    by_transport = Counter(_transport(o) for o in observations)
    print("Observations by transport:")
    for transport in ("http", "ws"):
        print(f"  {by_transport.get(transport, 0):4d}  {transport}")
    print()

    print("Distinct request shapes (structural only, no content; HTTP and WS transports):")
    shapes: dict[tuple[Any, ...], list[Observation]] = {}
    for observation in observations:
        shapes.setdefault(observation.shape_key(), []).append(observation)
    for index, (_key, group) in enumerate(shapes.items(), start=1):
        head = group[0]
        print(f"  [{index}] x{len(group)}  [{_transport(head)}] {head.method} {head.path}")
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
    print(
        f"  (a) requests distinguishable as compaction-related: {len(compaction_hits)} "
        f"(http={sum(1 for o in compaction_hits if _transport(o) == 'http')}, "
        f"ws={sum(1 for o in compaction_hits if _transport(o) == 'ws')})"
    )
    for observation in compaction_hits[:10]:
        print(
            f"        #{observation.seq} [{_transport(observation)}] {observation.method} "
            f"{observation.path} signals={list(observation.compaction_signals)} "
            f"tool_items={len(observation.tool_item_types)}"
        )
    print(
        f"  (b) requests carrying prior tool-call/result items: {len(tool_hits)} "
        f"(http={sum(1 for o in tool_hits if _transport(o) == 'http')}, "
        f"ws={sum(1 for o in tool_hits if _transport(o) == 'ws')})"
    )
    if tool_hits:
        widest = max(tool_hits, key=lambda o: len(o.tool_item_types))
        print(
            f"        widest: #{widest.seq} [{_transport(widest)}] {widest.method} "
            f"{widest.path} {len(widest.tool_item_types)} tool items of "
            f"{len(widest.item_types)} total -> {_collapse(widest.item_types)}"
        )
    unknown = sorted(
        {t for o in observations for t in o.item_types if t not in ALLOWED_TYPES and o.item_types}
    )
    if unknown:
        print(f"  item types seen outside the known ALLOWED_TYPES vocabulary: {unknown}")
    print()

    ws_established = sum(1 for o in observations if o.note and "(101)" in o.note)
    ws_declined = sum(1 for o in observations if o.note and "declined" in o.note)
    ws_frames = sum(1 for o in observations if _transport(o) == "ws" and not o.note)
    print("Caveats for reading this result:")
    print(
        "  * `codex exec` exposes no scriptable `/compact` command (that is a TUI-only slash "
        "command). The only scriptable trigger is the "
        "`model_auto_compact_token_limit` config override used above; if the session never "
        "crossed that limit, native compaction simply never ran."
    )
    if ws_established or ws_declined or ws_frames:
        print(
            f"  * WebSocket transport: {ws_established} upgrade(s) relayed to Headroom's real "
            f"/v1/responses WS route, {ws_declined} declined upstream, {ws_frames} client->proxy "
            "JSON frame(s) summarized. WS shapes appear above alongside the HTTP ones."
        )
    else:
        print(
            "  * Codex never attempted a WebSocket upgrade in this run, so every shape above is "
            "from the HTTP Responses transport. The relay was available but unused."
        )
    print(
        "  * Only the client->proxy direction is summarized on either transport. A compaction "
        "signal carried solely in a provider *response* would not be visible here."
    )
    print()

    if both:
        transports = sorted({_transport(o) for o in both})
        print(f"VERDICT: boundary observed (transport(s): {', '.join(transports)})")
        print(
            f"  {len(both)} request(s) were both compaction-distinguishable and carried prior "
            "tool-call/result items:"
        )
        for observation in both[:10]:
            print(
                f"    #{observation.seq} [{_transport(observation)}] {observation.method} "
                f"{observation.path} signals={list(observation.compaction_signals)} "
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
