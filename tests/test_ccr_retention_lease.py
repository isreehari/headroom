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
from typing import TYPE_CHECKING

import pytest

from headroom.cache.backends import InMemoryBackend, SQLiteBackend
from headroom.cache.compression_store import CompressionStore

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
