"""Track C's stale-revision gate for the Codex compaction boundary.

Stdlib only, by the same rule as :mod:`headroom.proxy.jev.compaction`: this
module consumes nothing from the rest of the package, so a proxy with
``HEADROOM_JEV_MODE`` unset pays nothing but an import for its existence.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

DEFAULT_MAX_ENTRIES = 512


class JevCompactionRevisionStore:
    """Remembers which compaction revisions this process already decided.

    A boundary's revision is its ``previous_response_id``: the provider-assigned
    id of the response the compaction hangs off. Codex retries a turn -- and
    replays it wholesale after a reconnect -- against the same anchor, so a
    second boundary carrying a revision already decided here is stale: the
    candidate bytes it carries need not be the bytes staged in CCR the first
    time. ``claim`` returns False for those and Track C keeps the original.

    **Why this is not Track A's** :class:`~headroom.proxy.jev.identity.JevIdentityStore`.
    The two answer different questions. Track A's store answers "is this still
    the latest revision on this branch" -- a newer revision *supersedes* an
    older one, which is the right rule for a shadow call that returns after the
    conversation has moved on. The WS boundary needs "has this
    ``previous_response_id`` already been decided in this process, ever". A
    supersede rule cannot express that: the replayed frame carries the *same*
    revision, so it would still compare equal to the latest and read as current.
    Claim-once is the only rule that makes a replay stale. The identity differs
    too -- the branch here is the boundary's ``previous_response_id``, which
    Track A's HTTP-path store never sees.

    **Why the key is the revision ALONE, with no session id in it.** The WS
    handler assigns ``session_id = uuid.uuid4().hex`` per accepted socket
    (``headroom/proxy/handlers/openai.py:6773``), so a reconnect *always*
    arrives under a new session id. A ``(session_id, revision)`` key would
    therefore never match a reconnect replay -- the single case this store
    exists to catch -- because the tuple differs in its first element every
    time. ``previous_response_id`` is provider-assigned and names one branch
    point, so it is both stable across reconnects and unique across unrelated
    conversations; keying on it alone is what makes the replay stale, and no
    session scoping is needed to keep unrelated conversations apart.

    **The asymmetry that makes both choices safe.** ``claim`` has two answers
    and only True authorises a change. A false "already claimed" -- from an
    eviction, a rejected input, or an id reused across conversations -- at worst
    *skips* a retention opportunity: the original frame is forwarded untouched
    and the turn behaves as it does with Jev off. It can never cause content to
    be dropped twice, because a remembered revision is never claimable again.
    Every degradation in this module is therefore pointed at False.

    **Bounded, and what the bound costs.** ``max_entries`` (default
    :data:`DEFAULT_MAX_ENTRIES`) caps the map, because the key is
    attacker-influenced input arriving over the wire and an unbounded dict keyed
    on such input is a memory leak (Task 8 in this plan replaced exactly such a
    plain dict with a bounded ``OrderedDict`` for the same reason). Eviction is
    least-recently-touched first: a rejected claim moves its entry back to the
    young end, so the revisions being actively replayed are the last to go and
    only boundaries untouched for 512 intervening compactions are forgotten.
    The sharp edge is that eviction is *not* free -- once a revision is evicted
    ``claim`` on it returns True again and the replay guard has silently
    reopened for it. That is accepted: to reach it an attacker must push 512
    distinct revisions through the boundary after the one they want to replay,
    and the reward is one extra keep/drop decision on a boundary that is by then
    far in the past. One extra decision costs at most one extra CCR stage of
    content the client itself just re-sent -- not a double drop of live content,
    since the replayed frame is replaced by a marker that retrieves the bytes
    that frame carried. Flooding the store is otherwise inert: entries are
    O(len(revision)) and capped in both count and length.

    **Scope: this process, this run.** Single-machine, single-worker, in-memory;
    there is no cross-worker or cross-restart ledger. The consequence, stated
    rather than left implicit: a proxy restart reopens *every* revision, exactly
    as an eviction does, and with the same bounded cost.

    **Thread safety.** ``claim`` is a test-and-set, and a test-and-set whose
    membership check and insert can interleave is not a claim at all. It is
    therefore taken under a :class:`threading.Lock`. The relay is single-worker
    asyncio and never calls this off the event loop today, so the lock is
    uncontended and costs an uncontended acquire per boundary -- but it means
    the claim-once guarantee is a property of this store rather than of its
    caller's threading model, and it keeps the store correct if it is ever
    reached from a worker thread (as :mod:`compaction_decision` uses for a
    blocking client). The lock is synchronous on purpose: ``claim`` must stay a
    plain method, and it holds the lock only for O(1) dict work, so it never
    blocks the event loop meaningfully.

    **Bad input never raises.** This runs inline in the relay path on
    provider-assigned values that nonetheless arrive over the wire, so
    ``revision`` is untrusted. A non-string, an empty or whitespace-only string,
    or one longer than :attr:`MAX_REVISION_LENGTH` is declined -- ``claim``
    returns False, ``seen`` returns False -- and is *not* stored, so a hostile
    value can neither occupy an entry nor evict a real one. Declining is the
    safe direction by the asymmetry above. Revisions are compared exactly, with
    no trimming, casefolding or Unicode normalisation: the id is an opaque token
    and any normalisation would merge two distinct branch points.
    """

    #: Upper bound on a revision's length. Provider response ids are tens of
    #: characters; this is generous by orders of magnitude and exists only so a
    #: single wire value cannot pin an arbitrary amount of memory in the map.
    MAX_REVISION_LENGTH = 512

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._claimed: OrderedDict[str, None] = OrderedDict()

    def _usable(self, revision: str) -> bool:
        """Whether ``revision`` is a value this store will ever remember."""
        if not isinstance(revision, str):
            return False
        if not revision.strip():
            return False
        return len(revision) <= self.MAX_REVISION_LENGTH

    def claim(self, revision: str) -> bool:
        """Claim ``revision``; True only the first time this process sees it.

        True authorises exactly one keep/drop decision for this boundary.
        Anything else -- a replay, a reconnect, an unusable id -- is False and
        the caller forwards the original frame untouched.
        """
        if not self._usable(revision):
            return False
        with self._lock:
            if revision in self._claimed:
                # Keep an actively replayed revision at the young end so it is
                # the last thing evicted, not the first.
                self._claimed.move_to_end(revision)
                return False
            self._claimed[revision] = None
            while len(self._claimed) > self._max_entries:
                self._claimed.popitem(last=False)
            return True

    def seen(self, revision: str) -> bool:
        """Whether this revision was already claimed (and not yet evicted).

        A pure read: it never claims, never stores and never reorders, so
        calling it cannot consume the one claim a boundary is entitled to.
        """
        if not isinstance(revision, str):
            return False
        with self._lock:
            return revision in self._claimed

    @property
    def tracked_revisions(self) -> int:
        """How many revisions are currently remembered. Diagnostics only."""
        with self._lock:
            return len(self._claimed)
