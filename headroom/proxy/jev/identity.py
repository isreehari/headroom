"""Session / branch / revision / event identity for Jev retention (Track A).

Single-machine, single-worker scope. The fresh design
(``docs/superpowers/specs/2026-09-20-jev-retention-fresh-design.md``, "Track A")
keeps the original plan's four-part identity but drops the shared cross-worker
admission-sequence machinery: one worker means an in-process latest-revision
store is sufficient, so there is no SQLite ledger here.

Nothing in this module derives a session id. ``session_id`` is the one the
proxy already computes with
``headroom.cache.prefix_tracker.SessionTrackerStore.compute_session_id`` --
reusing the existing identity machinery rather than inventing a parallel one.

* ``branch_id`` -- the conversation lineage root within a session id. Derived
  from the session id plus the frozen/protected prefix, so a new system prompt
  or a re-rooted conversation forks a branch while ordinary turn growth does
  not. That is the scope for "one bounded call per session/branch".
* ``revision`` -- the candidate set as it stands this turn. Rolls whenever the
  eligible candidates change, which is exactly the staleness test a shadow call
  needs when it returns after the conversation has moved on.
* ``event_id`` -- unique per shadow attempt, so a duplicate or late response can
  be told apart from a fresh one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_BRANCHES = 512


def _canonical_root(branch_root: list[dict[str, Any]] | None) -> str:
    """Deterministic text for a branch root, without ever raising.

    ``branch_root`` is request-shaped data, so the happy path is a JSON-decoded
    list of message dicts and ``json.dumps`` handles it. The fallback exists
    because the project gate is fail-open: a pathological root (uncomparable
    dict keys, a self-referential structure) must degrade to a still-
    deterministic id rather than take the caller down.
    """
    try:
        return json.dumps(
            branch_root or [],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, RecursionError):
        return repr(branch_root)


def branch_id_for(session_id: str, branch_root: list[dict[str, Any]] | None) -> str:
    """Stable id for a conversation lineage root within ``session_id``."""
    canonical = _canonical_root(branch_root)
    return hashlib.sha256(f"{session_id}\x00{canonical}".encode()).hexdigest()[:24]


def revision_for(candidate_fingerprints: Sequence[str]) -> str:
    """Revision of the current candidate set. Order-sensitive on purpose."""
    canonical = json.dumps(list(candidate_fingerprints), separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class JevTurnIdentity:
    """Identity of one shadow attempt."""

    session_id: str
    branch_id: str
    revision: str
    event_id: str

    @property
    def branch_key(self) -> tuple[str, str]:
        return (self.session_id, self.branch_id)


class JevIdentityStore:
    """In-process latest-revision store, one entry per (session, branch).

    Bounded: the id space is caller-controlled (a client can rotate
    ``x-headroom-session-id`` freely), so the map evicts least-recently-stamped
    branches instead of growing without limit.
    """

    def __init__(self, *, max_branches: int = DEFAULT_MAX_BRANCHES) -> None:
        if max_branches < 1:
            raise ValueError("max_branches must be >= 1")
        self._max_branches = max_branches
        self._latest: OrderedDict[tuple[str, str], str] = OrderedDict()

    def identify(
        self,
        *,
        session_id: str,
        branch_root: list[dict[str, Any]] | None,
        candidate_fingerprints: Sequence[str],
    ) -> JevTurnIdentity:
        """Mint and stamp the identity for this turn's candidate set."""
        identity = JevTurnIdentity(
            session_id=session_id,
            branch_id=branch_id_for(session_id, branch_root),
            revision=revision_for(candidate_fingerprints),
            event_id=uuid.uuid4().hex,
        )
        self.record(identity)
        return identity

    def record(self, identity: JevTurnIdentity) -> None:
        """Make ``identity.revision`` the branch's latest."""
        key = identity.branch_key
        self._latest[key] = identity.revision
        self._latest.move_to_end(key)
        while len(self._latest) > self._max_branches:
            self._latest.popitem(last=False)

    def latest_revision(self, session_id: str, branch_id: str) -> str | None:
        return self._latest.get((session_id, branch_id))

    def is_current(self, identity: JevTurnIdentity) -> bool:
        """False once a newer revision has been stamped for the same branch.

        Also false for a branch that has been evicted: an identity the store no
        longer vouches for is treated as stale, never as current.
        """
        return self._latest.get(identity.branch_key) == identity.revision

    @property
    def tracked_branches(self) -> int:
        return len(self._latest)
