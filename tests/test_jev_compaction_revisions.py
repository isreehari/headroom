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


def test_the_length_cap_is_on_the_raw_string_not_the_stripped_one() -> None:
    """The cap is deliberately measured before any trimming.

    A value that only fits the cap once its surrounding whitespace is removed is
    still rejected. Capping on the raw length is what lets the guard reject an
    oversized value *before* transforming it, and it is the conservative
    direction: rejection only ever costs a skipped retention opportunity.
    """
    store = JevCompactionRevisionStore()
    over_by_whitespace = "r" * JevCompactionRevisionStore.MAX_REVISION_LENGTH + " "
    assert len(over_by_whitespace.strip()) == JevCompactionRevisionStore.MAX_REVISION_LENGTH
    assert store.claim(over_by_whitespace) is False
    assert store.tracked_revisions == 0


class _StripSpy(str):
    """A ``str`` that records whether anything trimmed it."""

    __slots__ = ("calls",)

    def __new__(cls, value: str) -> _StripSpy:
        spy = super().__new__(cls, value)
        spy.calls = []
        return spy

    calls: list[str | None]

    def strip(self, chars: str | None = None, /) -> str:
        self.calls.append(chars)
        return super().strip(chars)


def test_an_oversized_revision_is_rejected_before_it_is_transformed() -> None:
    """Reject-before-transform on the untrusted-input path.

    ``strip`` allocates a second string proportional to its input. For a value
    already past the cap that copy buys nothing -- the rejection was going to
    happen either way -- so the length test must come first. The marginal cost
    is one copy of an already-resident wire string rather than an unbounded new
    exposure, but the ordering is free and it is the right shape for a guard on
    attacker-influenced input.
    """
    store = JevCompactionRevisionStore()
    oversized = _StripSpy("r" * (JevCompactionRevisionStore.MAX_REVISION_LENGTH + 1))
    assert store.claim(oversized) is False
    assert oversized.calls == []
    assert store.tracked_revisions == 0

    # A value within the cap is still trimmed-tested, so the guard is reordered,
    # not removed: a whitespace-only id of an acceptable length is still declined.
    blank = _StripSpy("   ")
    assert store.claim(blank) is False
    assert blank.calls != []
    assert store.tracked_revisions == 0


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


class _PausingStore(JevCompactionRevisionStore):
    """A store that parks the FIRST caller inside the critical section.

    The pause sits between the membership test and the insertion -- exactly the
    window the lock exists to close -- so a second caller arriving while the
    first is parked either blocks on the lock (correct) or observes a map that
    does not yet contain the revision and claims it too (the bug). No sleep,
    no thread-scheduling luck: the handover is driven by two events.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._parked = False

    def _insert_locked(self, revision: str) -> None:
        if not self._parked:
            self._parked = True
            self.entered.set()
            assert self.release.wait(timeout=10.0) is True
        super()._insert_locked(revision)


def test_claim_is_atomic_between_its_test_and_its_set() -> None:
    """The core safety property, pinned deterministically.

    ``claim`` is a test-and-set and a test-and-set whose halves can interleave
    is not a claim at all. This test fails reliably -- not probabilistically --
    against a variant with the lock removed, because the interleaving window is
    forced open by the store itself rather than hoped for from the scheduler.
    """
    store = _PausingStore()
    results: dict[str, bool] = {}

    def first() -> None:
        results["first"] = store.claim("resp_race")

    def second() -> None:
        results["second"] = store.claim("resp_race")

    parked = threading.Thread(target=first)
    contender = threading.Thread(target=second)
    parked.start()
    try:
        # The first caller is now inside the critical section, past the
        # membership test and before the insertion.
        assert store.entered.wait(timeout=10.0) is True

        contender.start()
        contender.join(timeout=0.5)
        # Mutual exclusion: the second caller cannot get past the lock, so it
        # cannot have reached -- let alone answered -- its own membership test.
        assert contender.is_alive() is True
        assert "second" not in results
    finally:
        store.release.set()
        parked.join(timeout=10.0)
        if contender.ident is not None:
            contender.join(timeout=10.0)

    assert parked.is_alive() is False
    assert contender.is_alive() is False
    assert results["first"] is True
    assert results["second"] is False
    assert store.tracked_revisions == 1


def test_concurrent_claims_authorise_exactly_one_caller() -> None:
    """A smoke test on the real (unpaused) store under genuine contention.

    This one is probabilistic by nature -- the natural window in an O(1) dict
    operation is too narrow for the scheduler to interleave reliably -- so it
    is a companion to ``test_claim_is_atomic_between_its_test_and_its_set``
    above, which is the test that actually defends the guarantee.

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
