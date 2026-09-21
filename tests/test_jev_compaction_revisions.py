"""Track C: a compaction revision may be decided exactly once."""

from __future__ import annotations

import threading
from typing import Any, cast

import pytest

from headroom.proxy.jev.compaction_state import JevCompactionRevisionStore


def test_first_claim_wins_and_the_replay_is_stale() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True
    assert store.seen("resp_abc") is True
    assert store.claim("resp_abc") is False


def test_a_reconnect_cannot_reclaim_the_same_revision() -> None:
    # The WS handler mints a fresh uuid4 session id per socket, so a reconnect
    # replay carries a NEW session id and the SAME previous_response_id. The
    # store must still call it stale -- that is the whole point of this gate.
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True  # first connection
    assert store.claim("resp_abc") is False  # reconnect replays the boundary


def test_distinct_revisions_are_independent() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True
    assert store.claim("resp_def") is True


def test_empty_identity_never_claims() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("") is False


def test_store_is_bounded_and_evicts_oldest_first() -> None:
    store = JevCompactionRevisionStore(max_entries=2)
    assert store.claim("r1") is True
    assert store.claim("r2") is True
    assert store.claim("r3") is True
    assert store.seen("r1") is False
    assert store.seen("r2") is True
    assert store.seen("r3") is True


def test_max_entries_must_be_positive() -> None:
    with pytest.raises(ValueError):
        JevCompactionRevisionStore(max_entries=0)


# --- seen() must not claim ------------------------------------------------


def test_seen_does_not_claim() -> None:
    """``seen`` is a pure read: it must never consume the first claim."""
    store = JevCompactionRevisionStore()
    assert store.seen("resp_abc") is False
    assert store.seen("resp_abc") is False
    # The revision was only ever looked at, so the claim is still available.
    assert store.claim("resp_abc") is True
    assert store.tracked_revisions == 1


def test_seen_on_bad_input_never_raises_and_never_stores() -> None:
    store = JevCompactionRevisionStore()
    assert store.seen("") is False
    assert store.seen(cast(Any, None)) is False
    assert store.seen(cast(Any, 1234)) is False
    assert store.tracked_revisions == 0


# --- the bound and what eviction costs ------------------------------------


def test_default_bound_is_512() -> None:
    store = JevCompactionRevisionStore()
    for index in range(600):
        assert store.claim(f"resp_{index}") is True
    assert store.tracked_revisions == 512
    # The 88 oldest are gone; the newest 512 remain.
    assert store.seen("resp_0") is False
    assert store.seen("resp_87") is False
    assert store.seen("resp_88") is True
    assert store.seen("resp_599") is True


def test_eviction_reopens_the_claim_on_the_evicted_revision() -> None:
    """The sharp edge, pinned deliberately.

    Eviction is not a no-op: once ``r1`` is evicted the replay guard has
    forgotten it, so a replayed boundary carrying ``r1`` claims afresh. This is
    the accepted cost of the bound -- see the module docstring -- and the test
    exists so that a future change to the eviction policy has to face it.
    """
    store = JevCompactionRevisionStore(max_entries=2)
    assert store.claim("r1") is True
    assert store.claim("r2") is True
    assert store.claim("r3") is True  # evicts r1
    assert store.seen("r1") is False
    assert store.claim("r1") is True  # the guard has reopened


def test_a_reclaimed_revision_re_enters_at_the_young_end() -> None:
    store = JevCompactionRevisionStore(max_entries=2)
    assert store.claim("r1") is True
    assert store.claim("r2") is True
    assert store.claim("r3") is True  # evicts r1
    assert store.claim("r1") is True  # evicts r2, r1 is youngest again
    assert store.seen("r2") is False
    assert store.seen("r3") is True
    assert store.seen("r1") is True


def test_a_rejected_replay_refreshes_the_entry() -> None:
    """A repeatedly replayed revision must not age out while it is replaying.

    A rejected claim moves the entry to the young end, so the revisions most
    likely to be replayed again are the last ones evicted.
    """
    store = JevCompactionRevisionStore(max_entries=2)
    assert store.claim("r1") is True
    assert store.claim("r2") is True
    assert store.claim("r1") is False  # rejected, but r1 is now the youngest
    assert store.claim("r3") is True  # so r2 is evicted, not r1
    assert store.seen("r1") is True
    assert store.seen("r2") is False


def test_max_entries_of_one_still_claims_once() -> None:
    store = JevCompactionRevisionStore(max_entries=1)
    assert store.claim("r1") is True
    assert store.claim("r1") is False
    assert store.claim("r2") is True
    assert store.tracked_revisions == 1


def test_negative_max_entries_is_rejected() -> None:
    with pytest.raises(ValueError):
        JevCompactionRevisionStore(max_entries=-1)


# --- adversarial input: never raise, never poison the store ---------------


@pytest.mark.parametrize(
    "revision",
    [None, 0, 1234, 3.5, True, b"resp_abc", ["resp_abc"], {"id": "resp_abc"}, object()],
)
def test_non_string_revisions_never_claim_and_never_raise(revision: object) -> None:
    """``previous_response_id`` arrives over the wire, so it may be anything.

    A non-string is declined deterministically -- ``claim`` returns False, the
    caller keeps the original frame -- and nothing is stored, so a hostile type
    cannot occupy an entry or evict a real one.
    """
    store = JevCompactionRevisionStore()
    assert store.claim(cast(Any, revision)) is False
    assert store.seen(cast(Any, revision)) is False
    assert store.tracked_revisions == 0


def test_whitespace_only_revision_never_claims() -> None:
    store = JevCompactionRevisionStore()
    assert store.claim("   ") is False
    assert store.claim("\n\t") is False
    assert store.tracked_revisions == 0


def test_an_absurdly_long_revision_never_claims() -> None:
    """A megabyte-long id is not a provider response id; decline it unstored."""
    store = JevCompactionRevisionStore()
    huge = "r" * (JevCompactionRevisionStore.MAX_REVISION_LENGTH + 1)
    assert store.claim(huge) is False
    assert store.seen(huge) is False
    assert store.tracked_revisions == 0


def test_a_revision_at_the_length_limit_is_accepted() -> None:
    store = JevCompactionRevisionStore()
    at_limit = "r" * JevCompactionRevisionStore.MAX_REVISION_LENGTH
    assert store.claim(at_limit) is True
    assert store.claim(at_limit) is False


def test_revisions_are_compared_exactly() -> None:
    """No trimming, casefolding or normalisation: the id is an opaque token."""
    store = JevCompactionRevisionStore()
    assert store.claim("resp_abc") is True
    assert store.claim("resp_ABC") is True
    assert store.claim(" resp_abc") is True
    assert store.claim("resp_abc ") is True


def test_a_surrogate_bearing_revision_is_handled_without_raising() -> None:
    """Wire JSON can carry a lone surrogate; the store never encodes, so it holds."""
    store = JevCompactionRevisionStore()
    lonely = "resp_\ud800"
    assert store.claim(lonely) is True
    assert store.claim(lonely) is False
    assert store.seen(lonely) is True


# --- the safety asymmetry -------------------------------------------------


def test_a_false_already_claimed_only_ever_skips_a_decision() -> None:
    """The asymmetry that makes this gate safe, stated as a test.

    ``claim`` has exactly two answers and only one of them authorises a change.
    Whatever the store gets wrong -- an eviction, a bad input, a collision --
    it can only ever emit False more often than ideal, which makes the caller
    forward the original frame untouched. There is no input for which a
    revision authorises a second drop, because a claimed revision is never
    claimable again while it is remembered.
    """
    store = JevCompactionRevisionStore()
    authorised = [store.claim("resp_abc") for _ in range(50)]
    assert authorised.count(True) == 1
    assert authorised[0] is True
    assert all(answer is False for answer in authorised[1:])


# --- test-and-set atomicity ------------------------------------------------


def test_concurrent_claims_authorise_exactly_one_caller() -> None:
    """``claim`` is a test-and-set, so it is locked, not merely ordered.

    The relay is asyncio and single-worker, so this cannot happen there today;
    the lock is what keeps that an implementation detail of the caller rather
    than a correctness requirement of the store.
    """
    store = JevCompactionRevisionStore()
    start = threading.Barrier(16)
    results: list[bool] = []
    guard = threading.Lock()

    def worker() -> None:
        start.wait()
        outcome = store.claim("resp_race")
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 16
    assert results.count(True) == 1
