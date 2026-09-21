"""The CCR safety sequence for Jev active retention.

Contract (design doc, "Track B: Active Mode via /v1/compress CCR"): write the
original to CCR -> require an ACKNOWLEDGED success -> bind to
session/branch/candidate hash + retention lease -> commit. Any failed step keeps
the original, so every failure here must return None rather than raise.

This is the SHARED sequence: Track B's `/v1/compress` boundary and Track C's
Codex WebSocket boundary both stage through `stage_retention`, so the ordering
is exercised once, here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from headroom.cache.backends import InMemoryBackend, SQLiteBackend
from headroom.cache.compression_store import CompressionStore
from headroom.proxy.jev.retention_ccr import (
    JEV_RETENTION_LEASE_SECONDS,
    RetentionLease,
    candidate_retention_hash,
    retention_marker,
    stage_retention,
)

if TYPE_CHECKING:
    from pathlib import Path


def _store() -> CompressionStore:
    return CompressionStore(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())


def _stage(store: CompressionStore, content: str = "ORIGINAL TOOL OUTPUT") -> RetentionLease | None:
    return stage_retention(
        store,
        candidate_id="cand_0000",
        session_id="s1",
        branch_id="compress",
        content=content,
        tool_name="get_items",
        tool_call_id="call_1",
        original_tokens=4096,
    )


def test_hash_is_bound_to_session_and_branch() -> None:
    same = candidate_retention_hash("s1", "compress", "payload")
    assert same == candidate_retention_hash("s1", "compress", "payload")
    assert same != candidate_retention_hash("s2", "compress", "payload")
    assert same != candidate_retention_hash("s1", "other", "payload")
    assert same != candidate_retention_hash("s1", "compress", "other payload")
    assert len(same) == 24
    assert all(c in "0123456789abcdef" for c in same)


def test_hash_fields_cannot_be_smeared_into_each_other() -> None:
    """Concatenating the parts must not let one field bleed into the next.

    Without a separator, ("s1a", "b") and ("s1", "ab") would hash identically
    and two different branches of the same session would share one entry --
    and therefore one lease and one TTL.
    """
    assert candidate_retention_hash("s1a", "b", "p") != candidate_retention_hash("s1", "ab", "p")


def test_hash_is_accepted_as_an_explicit_store_key() -> None:
    """`store()` validates `explicit_hash` as hex and raises otherwise."""
    store = _store()
    hash_key = candidate_retention_hash("s1", "compress", "payload")
    assert store.store("payload", "marker", explicit_hash=hash_key) == hash_key


def test_marker_is_resolvable_by_both_marker_scanners() -> None:
    from headroom.ccr.tool_injection import CCRToolInjector
    from headroom.proxy.handlers.openai import _CCR_HASH_RE

    hash_key = candidate_retention_hash("s1", "compress", "payload")
    marker = retention_marker(hash_key, original_tokens=812)
    assert _CCR_HASH_RE.findall(marker) == [hash_key]
    # The same marker must also make the proxy inject `headroom_retrieve`: a
    # marker the model cannot redeem is silent data loss, not compression.
    injector = CCRToolInjector(inject_tool=False, inject_system_instructions=False)
    injector.scan_for_markers([{"role": "user", "content": marker}])
    assert injector.detected_hashes == [hash_key]


def test_marker_without_a_token_count_is_still_resolvable() -> None:
    """`original_tokens=0` (an unmeasured candidate) must not break the shape."""
    from headroom.ccr.tool_injection import CCRToolInjector
    from headroom.proxy.handlers.openai import _CCR_HASH_RE

    hash_key = candidate_retention_hash("s1", "compress", "payload")
    marker = retention_marker(hash_key)
    assert _CCR_HASH_RE.findall(marker) == [hash_key]
    injector = CCRToolInjector(inject_tool=False, inject_system_instructions=False)
    injector.scan_for_markers([{"role": "user", "content": marker}])
    assert injector.detected_hashes == [hash_key]


def test_marker_is_not_a_bare_ccr_marker() -> None:
    """A `<<ccr:...>>`-only marker is refused by `store()` as an original.

    The marker we emit is re-stored as the `compressed` side of the entry and
    can be re-fed to the store by the CCR mirror bridge, so it must not be the
    shape `store()` refuses to persist (#2694).
    """
    marker = retention_marker(candidate_retention_hash("s1", "compress", "p"), original_tokens=1)
    stripped = marker.strip()
    assert not (stripped.startswith("<<ccr:") and stripped.endswith(">>"))


def test_successful_stage_writes_acknowledges_and_leases() -> None:
    store = _store()
    lease = _stage(store)
    assert lease is not None
    assert lease.candidate_id == "cand_0000"
    assert lease.hash_key == candidate_retention_hash("s1", "compress", "ORIGINAL TOOL OUTPUT")
    assert lease.marker == retention_marker(lease.hash_key, original_tokens=4096)
    assert lease.original_tokens == 4096
    assert lease.lease_seconds == JEV_RETENTION_LEASE_SECONDS
    # Acknowledged: the bytes are readable back, under the bound hash.
    entry = store.retrieve(lease.hash_key)
    assert entry is not None
    assert entry.original_content == "ORIGINAL TOOL OUTPUT"
    # Leased: the entry outlives the store's 60s default. `extend_ttl` measures
    # the lease FROM NOW and adds slack, so the stored ttl is >= the lease, not
    # equal to it; what the caller is promised is the remaining lifetime.
    status = store.get_entry_status(lease.hash_key)
    assert status["ttl_seconds"] >= JEV_RETENTION_LEASE_SECONDS
    assert float(status["expires_at"]) - time.time() >= JEV_RETENTION_LEASE_SECONDS


def test_lease_is_frozen() -> None:
    lease = _stage(_store())
    assert lease is not None
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is a ValueError-ish
        lease.hash_key = "nope"  # type: ignore[misc]


def test_unacknowledged_write_returns_none() -> None:
    """A store that swallows the write must NOT yield a lease.

    CompressionStore.store() returns the hash even when it refuses to persist
    (e.g. a bare CCR marker as `original`), so the return value alone is not an
    acknowledgement -- only a read-back is.
    """
    store = _store()
    lease = _stage(store, content="<<ccr:abc123abc123>>")
    assert lease is None


def test_read_back_with_different_content_returns_none() -> None:
    """Presence is not acknowledgement: the bytes must match, or keep the original.

    A collision (or a stale row under the same key) would otherwise let the
    caller drop content whose only "copy" is somebody else's.
    """

    class Colliding(CompressionStore):
        def store(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
            hash_key = str(kwargs["explicit_hash"])
            super().store("SOMEBODY ELSE'S BYTES", "marker", explicit_hash=hash_key)
            return hash_key

    store = Colliding(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    assert _stage(store) is None


def test_missing_read_back_returns_none() -> None:
    class Amnesiac(CompressionStore):
        def retrieve(self, hash_key: str, query: str | None = None) -> None:  # type: ignore[override]
            return None

    store = Amnesiac(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    assert _stage(store) is None


def test_wrong_hash_from_store_returns_none() -> None:
    class Renaming(CompressionStore):
        def store(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
            super().store(*args, **kwargs)
            return "deadbeefdeadbeefdeadbeef"

    store = Renaming(default_ttl=60, enable_feedback=False, backend=InMemoryBackend())
    assert _stage(store) is None


def test_store_failure_returns_none_and_does_not_raise() -> None:
    class Exploding(CompressionStore):
        def store(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    lease = _stage(Exploding(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None


def test_read_back_failure_returns_none_and_does_not_raise() -> None:
    class Exploding(CompressionStore):
        def retrieve(self, hash_key: str, query: str | None = None) -> None:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    lease = _stage(Exploding(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None


def test_lease_failure_returns_none() -> None:
    class NoLease(CompressionStore):
        def extend_ttl(self, hash_key: str, ttl: int) -> bool:  # type: ignore[override]
            return False

    lease = _stage(NoLease(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None


def test_lease_exception_returns_none_and_does_not_raise() -> None:
    class Exploding(CompressionStore):
        def extend_ttl(self, hash_key: str, ttl: int) -> bool:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    lease = _stage(Exploding(default_ttl=60, enable_feedback=False, backend=InMemoryBackend()))
    assert lease is None


def test_failed_stage_logs_no_content(caplog: pytest.LogCaptureFixture) -> None:
    """Failure logs identify the entry, never the payload (it may hold secrets)."""
    secret = "<<ccr:abc123abc123>>"  # refused by store(), so this path logs
    with caplog.at_level("WARNING"):
        assert _stage(_store(), content=secret) is None
    jev_records = [r for r in caplog.records if r.name.endswith("retention_ccr")]
    assert jev_records
    for record in jev_records:
        assert secret not in record.getMessage()


def test_first_write_already_carries_the_lease() -> None:
    """Belt: the initial `store()` must not leave a default-TTL window open.

    Between `store()` and `extend_ttl()` the entry would otherwise be live with
    the store's short default TTL; a purge in that window loses the content.
    """
    store = _store()
    written: list[int | None] = []
    original_store = store.store

    def _record(*args: Any, **kwargs: Any) -> str:
        written.append(kwargs.get("ttl"))
        return original_store(*args, **kwargs)

    store.store = _record  # type: ignore[method-assign]
    lease = _stage(store)
    assert lease is not None
    assert written == [JEV_RETENTION_LEASE_SECONDS]


def test_lease_survives_the_ccr_mirror_re_store(tmp_path: Path) -> None:
    """The mirror bridge re-stores the same hash every turn a marker is seen.

    `store()` overwrites in place with a brand-new `created_at`/`ttl`, so
    without a one-way deadline floor a turn-N lease is wiped in turn N+1 and the
    entry expires while its marker is still in the conversation -- exactly the
    "marker outlives its content" failure Track B exists to prevent.
    """
    backend = SQLiteBackend(tmp_path / "ccr_mirror.db")
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=backend)
    try:
        lease = _stage(store)
        assert lease is not None
        # Turn N+1: the mirror re-stores the same content under the same key
        # with the store's default TTL.
        store.store(
            "ORIGINAL TOOL OUTPUT",
            lease.marker,
            explicit_hash=lease.hash_key,
        )
        status = store.get_entry_status(lease.hash_key)
        assert status["status"] == "available"
        assert float(status["expires_at"]) - time.time() >= JEV_RETENTION_LEASE_SECONDS
    finally:
        backend._conn.close()  # noqa: SLF001 - no public close(); -W error hates ResourceWarning


def test_stage_retention_round_trips_over_sqlite(tmp_path: Path) -> None:
    """Track B runs on the SQLite backend, so the sequence has to hold there."""
    backend = SQLiteBackend(tmp_path / "ccr_stage.db")
    store = CompressionStore(default_ttl=60, enable_feedback=False, backend=backend)
    try:
        lease = _stage(store)
        assert lease is not None
        entry = store.retrieve(lease.hash_key)
        assert entry is not None
        assert entry.original_content == "ORIGINAL TOOL OUTPUT"
        assert float(store.get_entry_status(lease.hash_key)["expires_at"]) - time.time() >= (
            JEV_RETENTION_LEASE_SECONDS
        )
    finally:
        backend._conn.close()  # noqa: SLF001 - no public close()


def test_custom_lease_seconds_are_honoured() -> None:
    store = _store()
    lease = stage_retention(
        store,
        candidate_id="c1",
        session_id="s1",
        branch_id="resp_42",
        content="body",
        tool_name=None,
        tool_call_id=None,
        original_tokens=10,
        lease_seconds=3_600,
    )
    assert lease is not None
    assert lease.lease_seconds == 3_600
    assert float(store.get_entry_status(lease.hash_key)["expires_at"]) - time.time() >= 3_600
