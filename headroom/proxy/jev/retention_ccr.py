"""The CCR safety sequence for Jev active retention (Tracks B and C).

The ORDER is the contract, not an implementation detail:

1. write the ORIGINAL content to the CCR store under a hash bound to
   ``(session_id, branch_id, content)``,
2. require an ACKNOWLEDGED success — read the entry back and compare bytes,
3. take a retention lease by extending that entry's TTL,
4. only then may the caller rewrite the conversation.

Any step that fails returns ``None`` and the caller keeps that candidate's
original content untouched. Nothing in this module mutates a conversation, and
nothing here raises: a retention failure is a missed saving, never a dropped
tool result.

There is deliberately exactly ONE copy of this sequence. Track B stages every
candidate of a ``/v1/compress`` boundary turn through :func:`stage_retention`,
and Track C's Codex WebSocket boundary stages the one candidate it carries
through the same function (with ``branch_id=boundary.previous_response_id``).
Two copies of a safety ordering drift; this one cannot.

Single-worker scope (design doc, "Non-Goals"): no cross-worker lease renewal, no
shared admission ledger. The lease is one TTL extension on the entry the marker
points at.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from headroom.proxy.jev.encoding import encode_identity_text

if TYPE_CHECKING:
    from headroom.cache.compression_store import CompressionStore

logger = logging.getLogger(__name__)

#: How long a retained original must stay retrievable. The store's default TTL
#: (30 minutes) assumes the caller still holds the content in its own
#: transcript; after active retention it does not, so the entry is the only
#: copy and has to outlive the agent session that produced it.
JEV_RETENTION_LEASE_SECONDS = 86_400

#: Namespaces the hash so a retention key can never collide with an ordinary
#: content-addressed compression key for the same bytes.
_RETENTION_HASH_VERSION = "jev-retention-v1"

#: Recorded on the entry so retention writes are distinguishable from ordinary
#: compression ones in the store's own telemetry.
_RETENTION_STRATEGY = "jev_retention"


def candidate_retention_hash(session_id: str, branch_id: str, content: str) -> str:
    """Storage key for one retained candidate, bound to its session and branch.

    Binding matters: two sessions can legitimately hold byte-identical tool
    output, and a bare content hash would let session B's entry — and its lease,
    and its TTL — stand in for session A's. 24 hex chars is the same width
    ``CompressionStore``'s own default key uses, satisfies ``store()``'s
    ``explicit_hash`` hex validation, and sits inside the 12–24 range both
    marker scanners accept.

    The fields are LENGTH-PREFIXED, not merely separated. Any separator byte —
    NUL included — can legally occur inside tool output, and a separated
    encoding then lets one field bleed into the next: ``("a\\0b", "c")`` and
    ``("a", "b\\0c")`` would hash identically, so two different branches of one
    session could share an entry, and therefore a lease and a TTL. A length
    prefix has no such collision: the parse is unambiguous for every input.

    The ENCODE step has to be injective too, or the length prefix buys nothing.
    :func:`~headroom.proxy.jev.encoding.encode_identity_text` is the package's
    single injective encoder and is shared with Track C's candidate binding, so
    the two cannot drift apart. It is deliberately not ``errors="replace"``,
    which collapses the whole lone-surrogate range onto ``b"?"`` and would let
    two distinct candidates of one ``compress`` branch share one entry -- the
    second overwriting the first's original, both markers then resolving to the
    second's content.
    """
    digest = hashlib.sha256()
    for part in (_RETENTION_HASH_VERSION, session_id, branch_id, content):
        encoded = encode_identity_text(part)
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return digest.hexdigest()[:24]


def retention_marker(hash_key: str, *, original_tokens: int = 0) -> str:
    """The text that replaces a retained tool result.

    ONE marker shape for both active tracks, and it has to satisfy two
    independent scanners:

    * the handler's ``_CCR_HASH_RE`` (``Retrieve more|original: hash=<hex>``
      followed by a non-hex character or end-of-string), which is what fills the
      response's ``ccr_hashes`` and what ``/v1/retrieve`` resolves;
    * ``CCRToolInjector.scan_for_markers``, which is what decides whether the
      ``headroom_retrieve`` tool gets injected at all. A marker that matches the
      first but not the second hands the model a pointer it has no tool to
      redeem — silent data loss, not compression (issue #1006).

    The bracketed ``[N tokens compressed to 0. … Retrieve more: hash=…]`` shape
    satisfies both (the trailing ``]`` is the non-hex terminator the handler's
    lookahead needs), and matches what every other Headroom compressor emits.
    It is deliberately not a bare ``<<ccr:…>>`` marker, which is the one shape
    ``CompressionStore.store()`` refuses to persist as an entry's original
    (#2694) and which this text is itself stored as on the ``compressed`` side.
    """
    measured = f"{original_tokens} tokens" if original_tokens > 0 else "Tool output"
    return (
        f"[{measured} compressed to 0. The original was withheld to free "
        "context; call headroom_retrieve to read it in full. "
        f"Retrieve more: hash={hash_key}]"
    )


@dataclass(frozen=True)
class RetentionLease:
    """A committed, acknowledged, leased retention of one candidate."""

    candidate_id: str
    hash_key: str
    marker: str
    original_tokens: int
    lease_seconds: int


def stage_retention(
    store: CompressionStore,
    *,
    candidate_id: str,
    session_id: str,
    branch_id: str,
    content: str,
    tool_name: str | None,
    tool_call_id: str | None,
    original_tokens: int,
    lease_seconds: int = JEV_RETENTION_LEASE_SECONDS,
) -> RetentionLease | None:
    """Run the full write → acknowledge → bind → lease sequence for one candidate.

    Returns the lease when every step succeeded, otherwise ``None`` — in which
    case the caller MUST leave that candidate's original content in the
    conversation.

    Failure logging carries identifiers only (candidate id, hash key, exception
    type). ``content`` is the store's credential-bearing payload in the general
    case, and an exception's message can quote what it was handed, so neither
    the content nor a stringified exception reaches the log.
    """
    hash_key = candidate_retention_hash(session_id, branch_id, content)
    marker = retention_marker(hash_key, original_tokens=original_tokens)

    # 1. Write the original. `ttl=lease_seconds` on the very first write so the
    #    entry is never live under the store's short default TTL, not even for
    #    the window between here and the explicit lease below.
    try:
        returned = store.store(
            content,
            marker,
            original_tokens=original_tokens,
            original_item_count=1,
            compressed_item_count=1,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            compression_strategy=_RETENTION_STRATEGY,
            ttl=lease_seconds,
            explicit_hash=hash_key,
        )
    except Exception as exc:  # noqa: BLE001 - any store failure keeps the original
        logger.warning(
            "jev retention: CCR write failed for candidate %s (hash=%s, %s); keeping original",
            candidate_id,
            hash_key,
            type(exc).__name__,
        )
        return None
    if returned != hash_key:
        logger.warning(
            "jev retention: CCR write for candidate %s returned an unexpected key "
            "(expected hash=%s); keeping original",
            candidate_id,
            hash_key,
        )
        return None

    # 2. Require an ACKNOWLEDGED success. The return value above is not one:
    #    CompressionStore.store() also returns the hash on the path where it
    #    refuses to persist (a bare CCR marker as `original`), and a transient
    #    sqlite3.DatabaseError is swallowed inside SQLiteBackend.set(). Only
    #    reading the bytes back proves the content is retrievable — and the
    #    bytes must MATCH, because a collision or a stale row under the same
    #    key would otherwise let us drop content nothing holds a copy of.
    #
    #    `peek`, not `retrieve`: retrieve() is the model-facing read and has two
    #    side effects this integrity check must not have — it logs a redacted
    #    preview of the payload (on by default), which would put the retained
    #    content in the log on every staged candidate and break this function's
    #    identifiers-only contract, and it records an access, which would score
    #    one phantom retrieval per candidate in the CCR feedback statistics.
    try:
        entry = store.peek(hash_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jev retention: CCR read-back failed for candidate %s (hash=%s, %s); keeping original",
            candidate_id,
            hash_key,
            type(exc).__name__,
        )
        return None
    if entry is None or entry.original_content != content:
        logger.warning(
            "jev retention: CCR write for candidate %s was not acknowledged "
            "(hash=%s present=%s content_match=%s); keeping original",
            candidate_id,
            hash_key,
            entry is not None,
            entry is not None and entry.original_content == content,
        )
        return None

    # 3. Take the lease. Belt and braces over the `ttl=` above: extend_ttl is
    #    the step that verifies the deadline through a read-back of its own, and
    #    it re-anchors the window on *now* rather than on the write.
    try:
        leased = store.extend_ttl(hash_key, lease_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jev retention: lease failed for candidate %s (hash=%s, %s); keeping original",
            candidate_id,
            hash_key,
            type(exc).__name__,
        )
        return None
    if not leased:
        logger.warning(
            "jev retention: lease refused for candidate %s (hash=%s); keeping original",
            candidate_id,
            hash_key,
        )
        return None

    return RetentionLease(
        candidate_id=candidate_id,
        hash_key=hash_key,
        marker=marker,
        original_tokens=original_tokens,
        lease_seconds=lease_seconds,
    )
