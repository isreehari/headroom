"""Retention lease: one-way TTL extension on a CCR entry.

Jev active retention (Track B) removes a tool result from the forwarded
conversation and leaves a `Retrieve original: hash=` marker behind. That entry is
then the ONLY copy of the content, so it must outlive the 30-minute default TTL
an ordinary compression entry gets.

The store models TTL as *seconds from ``created_at``* (``CompressionEntry.is_expired``
is ``time.time() - created_at > ttl``), so a lease for N seconds has to raise the
stored ``ttl`` to ``age + N`` — writing N into the column would leave less than N
seconds of life for any entry that is not brand new. Every test here therefore
asserts on the *remaining* lifetime, not just on the stored number.

Both backends are exercised: the in-memory one hands back the live object (a
mutation alone would "work"), while ``SQLiteBackend`` deserializes a fresh copy
per ``get`` and must have the new TTL written back through ``set`` to persist it
into the ``ttl`` column the purge query reads.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from headroom.cache import compression_store
from headroom.cache.backends import InMemoryBackend, SQLiteBackend
from headroom.cache.compression_store import (
    LEASE_SLACK_SECONDS,
    CompressionEntry,
    CompressionStore,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ORIGINAL = "the original tool output"


def _close(store: CompressionStore) -> None:
    """Close a SQLite-backed store's connection (no-op for in-memory).

    The backend has no public close(); without this, the garbage collector
    raises ResourceWarning, which `-W error` turns into a test failure.
    """
    conn = getattr(store._backend, "_conn", None)  # noqa: SLF001 - no public close()
    if conn is not None:
        conn.close()


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CompressionStore]:
    """A store over each backend the proxy can run with.

    Track B runs on SQLite (the `get_compression_store()` default), so the lease
    has to hold there and not only over the in-memory dict.
    """
    backend: InMemoryBackend | SQLiteBackend
    if request.param == "memory":
        backend = InMemoryBackend()
    else:
        backend = SQLiteBackend(tmp_path / "ccr_lease.db")
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=backend)
    yield store
    _close(store)


def _remaining(store: CompressionStore, hash_key: str) -> float:
    """Seconds of life the entry has left, as the store itself computes it."""
    status = store.get_entry_status(hash_key)
    return float(status["expires_at"]) - time.time()


def _backdate(store: CompressionStore, hash_key: str, seconds: float) -> None:
    """Age an entry by ``seconds`` without sleeping (created_at is the clock)."""
    backend = store._backend  # noqa: SLF001 - test needs to forge entry age
    entry = backend.get(hash_key)
    assert entry is not None
    entry.created_at -= seconds
    backend.set(hash_key, entry)


def _set_created_at(store: CompressionStore, hash_key: str, created_at: float) -> None:
    """Pin an entry's creation time so its age is exact under a frozen clock."""
    backend = store._backend  # noqa: SLF001 - test needs to forge entry age
    entry = backend.get(hash_key)
    assert entry is not None
    entry.created_at = created_at
    backend.set(hash_key, entry)


def _stored_ttl(store: CompressionStore, hash_key: str) -> int:
    entry = store._backend.get(hash_key)  # noqa: SLF001 - bypasses TTL checks on purpose
    assert entry is not None
    return entry.ttl


def test_extend_ttl_lengthens_a_live_entry(store: CompressionStore) -> None:
    hash_key = store.store(ORIGINAL, "compressed")
    assert store.extend_ttl(hash_key, 86_400) is True
    assert store.get_entry_status(hash_key)["ttl_seconds"] >= 86_400
    assert _remaining(store, hash_key) >= 86_400


def test_extend_ttl_leases_from_now_not_from_creation(store: CompressionStore) -> None:
    """An aged entry must still get the full lease window from *now*."""
    hash_key = store.store(ORIGINAL, "compressed", ttl=3_600)
    _backdate(store, hash_key, 3_000)

    assert store.extend_ttl(hash_key, 86_400) is True
    # 3000s of age + an 86400s lease: writing a bare 86400 would leave only
    # 83400s, and the marker would outlive its content by most of a day.
    assert store.get_entry_status(hash_key)["ttl_seconds"] >= 3_000 + 86_400
    assert _remaining(store, hash_key) >= 86_400


def test_extend_ttl_grants_slack_beyond_the_requested_window(
    store: CompressionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease clears `ttl` with margin, so a slow write cannot eat into it.

    The deadline is computed from the clock read *before* the write-back; with
    no slack, a write that lands in the next second leaves marginally less than
    the leased window. The clock is frozen here so the arithmetic is exact
    rather than "somewhere in a one-second band".
    """
    hash_key = store.store(ORIGINAL, "compressed", ttl=3_600)
    frozen = time.time()
    monkeypatch.setattr(compression_store.time, "time", lambda: frozen)
    _set_created_at(store, hash_key, frozen - 100.0)  # age is exactly 100s

    assert store.extend_ttl(hash_key, 86_400) is True
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 100 + 86_400 + LEASE_SLACK_SECONDS
    assert _remaining(store, hash_key) == pytest.approx(86_400 + LEASE_SLACK_SECONDS)


def test_extend_ttl_never_shortens(store: CompressionStore) -> None:
    hash_key = store.store(ORIGINAL, "compressed", ttl=86_400)
    assert store.extend_ttl(hash_key, 60) is True
    # A later ordinary re-store must not be able to shrink a lease that
    # retention already took, or the marker outlives its content.
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 86_400
    assert _remaining(store, hash_key) >= 86_000


def test_extend_ttl_of_zero_never_shortens(store: CompressionStore) -> None:
    """``ttl=0`` is a valid (no-op) lease, not a request to expire the entry."""
    hash_key = store.store(ORIGINAL, "compressed", ttl=3_600)
    assert store.extend_ttl(hash_key, 0) is True
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 3_600
    assert store.retrieve(hash_key) is not None


def test_extend_ttl_reports_missing_entry(store: CompressionStore) -> None:
    assert store.extend_ttl("deadbeefdeadbeefdeadbeef", 86_400) is False


def test_extend_ttl_reports_expired_entry(store: CompressionStore) -> None:
    hash_key = store.store(ORIGINAL, "compressed", ttl=0)
    # ttl=0 in this store means "expired on the next read" (is_expired is
    # `age > ttl`), NOT "never expires": the lease must fail so the caller
    # keeps the original content instead of dropping it.
    _backdate(store, hash_key, 1)
    assert store.extend_ttl(hash_key, 86_400) is False


def test_extend_ttl_does_not_resurrect_an_expired_entry(store: CompressionStore) -> None:
    hash_key = store.store(ORIGINAL, "compressed", ttl=60)
    _backdate(store, hash_key, 120)

    assert store.extend_ttl(hash_key, 86_400) is False
    assert store.get_entry_status(hash_key)["status"] == "expired"
    assert store.retrieve(hash_key) is None


def test_extend_ttl_rejects_negative(store: CompressionStore) -> None:
    hash_key = store.store(ORIGINAL, "compressed")
    with pytest.raises(ValueError, match="ttl"):
        store.extend_ttl(hash_key, -1)
    assert store.get_entry_status(hash_key)["ttl_seconds"] == 60


def test_extend_ttl_preserves_the_rest_of_the_entry(store: CompressionStore) -> None:
    """The write-back must not drop content or feedback counters."""
    hash_key = store.store(ORIGINAL, "compressed", tool_name="Read")
    assert store.retrieve(hash_key, query="q") is not None

    assert store.extend_ttl(hash_key, 86_400) is True

    entry = store.retrieve(hash_key)
    assert entry is not None
    assert entry.original_content == ORIGINAL
    assert entry.tool_name == "Read"
    assert entry.retrieval_count == 2  # one before the lease, one just now


class _BusyOnInsertConnection:
    """Wraps a live connection and fails writes with a transient error."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if sql.lstrip().upper().startswith("INSERT"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_extend_ttl_reports_a_transient_sqlite_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swallowed backend write must NOT be reported as a held lease.

    ``SQLiteBackend.set`` catches transient ``sqlite3.DatabaseError`` (busy /
    locked under multi-worker contention), logs it and returns normally. Track
    B binds a marker and drops the original only on an acknowledged lease, so
    a silently lost write has to surface as False, not True.
    """
    backend = SQLiteBackend(tmp_path / "ccr_lease_busy.db")
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=backend)
    try:
        hash_key = store.store(ORIGINAL, "compressed", ttl=60)
        with monkeypatch.context() as m:
            # sqlite3.Connection.execute is read-only, so swap the whole
            # connection for a proxy that fails only the INSERT.
            m.setattr(backend, "_conn", _BusyOnInsertConnection(backend._conn))  # noqa: SLF001
            assert store.extend_ttl(hash_key, 86_400) is False

        # The row is untouched and still has its original TTL: nothing was
        # half-applied, and the caller is free to retry.
        assert _stored_ttl(store, hash_key) == 60
        assert store.retrieve(hash_key) is not None
    finally:
        _close(store)


class _DroppingBackend(InMemoryBackend):
    """A backend that silently discards writes and never aliases its entries.

    Stands in for any backend whose ``set`` can fail quietly (SQLite already
    does, by design). Copying on ``get`` is what makes the read-back a real
    check rather than an inspection of the object the caller just mutated.
    """

    def get(self, hash_key: str) -> CompressionEntry | None:
        entry = super().get(hash_key)
        return None if entry is None else replace(entry)

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        return None


def test_extend_ttl_reports_a_write_that_vanishes() -> None:
    """Any backend that drops the write (not just SQLite) must fail the lease."""
    backend = _DroppingBackend()
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=backend)
    # store() goes through the same dropped set(), so seed the entry directly.
    InMemoryBackend.set(
        backend,
        "h",
        CompressionEntry(
            hash="h",
            original_content=ORIGINAL,
            compressed_content="compressed",
            original_tokens=10,
            compressed_tokens=2,
            original_item_count=1,
            compressed_item_count=1,
            tool_name="Read",
            tool_call_id=None,
            query_context=None,
            created_at=time.time(),
            ttl=60,
        ),
    )

    assert store.extend_ttl("h", 86_400) is False
    assert _stored_ttl(store, "h") == 60


def test_extend_ttl_persists_to_the_sqlite_ttl_column(tmp_path: Path) -> None:
    """The lease must survive a restart and the purge query, not just live in RAM.

    ``SQLiteBackend`` purges with ``created_at + ttl < now`` against the columns,
    so the extension is only real if ``set`` rewrote the ``ttl`` column.
    """
    db_path = tmp_path / "ccr_lease_persist.db"
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=SQLiteBackend(db_path))
    hash_key = store.store(ORIGINAL, "compressed", ttl=60)
    assert store.extend_ttl(hash_key, 86_400) is True
    _backdate(store, hash_key, 3_600)  # well past the original 60s TTL
    _close(store)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT ttl FROM ccr_entries WHERE hash = ?", (hash_key,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] >= 86_400

    # Restart: a fresh backend on the same file sweeps expired rows on open.
    reopened = CompressionStore(
        default_ttl=60, enable_feedback=False, backend=SQLiteBackend(db_path)
    )
    try:
        entry = reopened.retrieve(hash_key)
        assert entry is not None
        assert entry.original_content == ORIGINAL
    finally:
        _close(reopened)


def test_re_store_does_not_shorten_a_leased_entry(store: CompressionStore) -> None:
    """A re-store must never cut an unexpired entry's life short.

    `store()` overwrites an existing key in place with a fresh
    `created_at`/`ttl`, and the CCR mirror bridge re-stores the same
    `explicit_hash` on every turn a marker is re-encountered. Without a one-way
    deadline floor, the retention lease taken in turn N is wiped in turn N+1 and
    the entry expires while its marker is still in the conversation -- a
    guaranteed 404 on `/v1/retrieve` with no copy left anywhere.
    """
    hash_key = store.store(ORIGINAL, "compressed")
    assert store.extend_ttl(hash_key, 86_400) is True
    leased_deadline = float(store.get_entry_status(hash_key)["expires_at"])

    # Turn N+1: same content, same key, the store's (short) default TTL.
    assert store.store(ORIGINAL, "compressed", explicit_hash=hash_key) == hash_key

    status = store.get_entry_status(hash_key)
    assert status["status"] == "available"
    assert float(status["expires_at"]) >= leased_deadline - 1
    assert _remaining(store, hash_key) >= 86_400


def test_re_store_may_lengthen_but_never_shortens(store: CompressionStore) -> None:
    """The floor is one-way: a longer TTL on a re-store still wins."""
    hash_key = store.store(ORIGINAL, "compressed", ttl=3_600)
    assert store.store(ORIGINAL, "compressed", explicit_hash=hash_key, ttl=7_200) == hash_key
    assert _remaining(store, hash_key) >= 7_200 - 1

    assert store.store(ORIGINAL, "compressed", explicit_hash=hash_key, ttl=60) == hash_key
    assert _remaining(store, hash_key) >= 7_200 - 1


def test_re_store_floor_survives_an_aged_entry(store: CompressionStore) -> None:
    """The floor carries the DEADLINE forward, not the raw ttl number."""
    hash_key = store.store(ORIGINAL, "compressed", ttl=86_400)
    _backdate(store, hash_key, 3_600)  # one hour old: 85_800s of life left
    assert store.store(ORIGINAL, "compressed", explicit_hash=hash_key) == hash_key

    remaining = _remaining(store, hash_key)
    assert 82_000 <= remaining <= 86_400 + 1


def test_re_store_of_an_expired_entry_starts_a_fresh_window(store: CompressionStore) -> None:
    """An expired entry has no life to preserve; the new TTL applies as written."""
    hash_key = store.store(ORIGINAL, "compressed", ttl=86_400)
    _backdate(store, hash_key, 90_000)
    assert store.get_entry_status(hash_key)["status"] == "expired"

    assert store.store(ORIGINAL, "compressed", explicit_hash=hash_key) == hash_key
    assert _stored_ttl(store, hash_key) == 60
    assert store.retrieve(hash_key) is not None


def test_re_store_floor_does_not_leak_across_different_keys(store: CompressionStore) -> None:
    """Only the SAME key's deadline is carried forward."""
    leased = store.store(ORIGINAL, "compressed", ttl=86_400)
    other = store.store("different content", "compressed")
    assert other != leased
    assert _stored_ttl(store, other) == 60
