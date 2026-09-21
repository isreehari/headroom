"""Single-worker Jev identity: stable branch ids, rolling revisions, bounded store."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.jev.identity import (
    JevIdentityStore,
    JevTurnIdentity,
    branch_id_for,
    revision_for,
)

ROOT = [{"role": "system", "content": "you are a helpful agent"}]


def test_branch_id_is_stable_for_the_same_root_and_session() -> None:
    assert branch_id_for("sess-1", ROOT) == branch_id_for("sess-1", ROOT)


def test_branch_id_changes_with_session_or_root() -> None:
    assert branch_id_for("sess-1", ROOT) != branch_id_for("sess-2", ROOT)
    assert branch_id_for("sess-1", ROOT) != branch_id_for(
        "sess-1", [{"role": "system", "content": "different"}]
    )


def test_revision_tracks_the_candidate_set() -> None:
    assert revision_for(["a", "b"]) == revision_for(["a", "b"])
    assert revision_for(["a", "b"]) != revision_for(["a", "c"])
    assert revision_for(["a", "b"]) != revision_for(["b", "a"])


def test_identify_is_stable_except_for_the_event_id() -> None:
    store = JevIdentityStore()
    first = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    second = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert first.session_id == second.session_id == "s"
    assert first.branch_id == second.branch_id
    assert first.revision == second.revision
    assert first.event_id != second.event_id


def test_a_newer_revision_makes_the_older_identity_stale() -> None:
    store = JevIdentityStore()
    old = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert store.is_current(old) is True
    store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a", "b"])
    assert store.is_current(old) is False


def test_latest_revision_reads_back_per_branch() -> None:
    store = JevIdentityStore()
    identity = store.identify(session_id="s", branch_root=ROOT, candidate_fingerprints=["a"])
    assert store.latest_revision("s", identity.branch_id) == identity.revision
    assert store.latest_revision("s", "nope") is None


def test_store_is_bounded_and_evicts_oldest_first() -> None:
    store = JevIdentityStore(max_branches=2)
    a = store.identify(session_id="a", branch_root=ROOT, candidate_fingerprints=["x"])
    store.identify(session_id="b", branch_root=ROOT, candidate_fingerprints=["x"])
    store.identify(session_id="c", branch_root=ROOT, candidate_fingerprints=["x"])
    assert store.tracked_branches == 2
    assert store.latest_revision("a", a.branch_id) is None


def test_max_branches_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_branches must be >= 1"):
        JevIdentityStore(max_branches=0)


def test_restamping_a_branch_keeps_it_from_being_evicted() -> None:
    store = JevIdentityStore(max_branches=2)
    a = store.identify(session_id="a", branch_root=ROOT, candidate_fingerprints=["x"])
    b = store.identify(session_id="b", branch_root=ROOT, candidate_fingerprints=["x"])
    refreshed = store.identify(session_id="a", branch_root=ROOT, candidate_fingerprints=["x", "y"])
    store.identify(session_id="c", branch_root=ROOT, candidate_fingerprints=["x"])
    assert store.latest_revision("a", a.branch_id) == refreshed.revision
    assert store.latest_revision("b", b.branch_id) is None


def test_an_evicted_branch_is_never_current() -> None:
    store = JevIdentityStore(max_branches=1)
    old = store.identify(session_id="a", branch_root=ROOT, candidate_fingerprints=["x"])
    store.identify(session_id="b", branch_root=ROOT, candidate_fingerprints=["x"])
    assert store.is_current(old) is False


def test_ids_are_fail_open_on_pathological_inputs() -> None:
    circular: list[dict[str, object]] = [{"role": "system"}]
    circular[0]["self"] = circular
    assert len(branch_id_for("s", circular)) == 24  # type: ignore[arg-type]

    uncomparable_keys = [{"a": 1, 2: "b"}]
    assert len(branch_id_for("s", uncomparable_keys)) == 24  # type: ignore[arg-type]

    assert len(revision_for([object()])) == 32  # type: ignore[list-item]


def test_ids_survive_a_lone_surrogate_in_the_request_body() -> None:
    # json.loads accepts an unpaired surrogate escape, so a client can put one
    # in a message; the resulting str is not UTF-8 encodable.
    lone_surrogate = json.loads('"\\ud800 lone surrogate"')
    root = [{"role": "system", "content": lone_surrogate}]

    branch_id = branch_id_for("s", root)
    assert len(branch_id) == 24
    assert branch_id == branch_id_for("s", root)

    revision = revision_for([lone_surrogate])
    assert len(revision) == 32
    assert revision == revision_for([lone_surrogate])


def test_branch_key_pairs_session_and_branch() -> None:
    identity = JevTurnIdentity(session_id="s", branch_id="b", revision="r", event_id="e")
    assert identity.branch_key == ("s", "b")
