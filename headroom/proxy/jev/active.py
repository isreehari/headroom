"""Track B's decision step: one bounded Jev call for a real drop/truncate answer.

Everything about *what* a candidate is, *how* the state is built and *how* the
request is kept inside Jev's input limit lives in Track A (``candidates.py`` /
``identity.py`` / ``request.py`` / ``client.py``). This module only sequences
those calls for the active path and guarantees that every candidate it selected
comes back with a decision -- ``keep`` unless Jev said otherwise.

Track B does NOT go through ``JevShadowRunner``. That runner's threshold,
cooldown, in-flight and staleness gates exist to decide *when* to speak up on
ordinary proxied traffic; here the caller has already decided, by reaching a
compaction boundary, that this turn asks. Everything below the gates -- the
selection, the identity, the measured request budget, the fail-open client --
is the same code shadow mode runs.

Two error policies, deliberately different:

* **A Jev answer that failed** is not an error of ours. ``JevClient.decide``
  never raises; an HTTP failure, a timeout or an unparseable body arrives as
  ``JevAnswer.error``. Such an answer is reported on
  :class:`JevActiveDecision.error`, every candidate stays ``keep``, and the
  turn proceeds having moved nothing.
* **A broken tokenizer, a failed tokenizer lookup or a selection failure
  propagates.** Task 16's orchestrator is the single fail-open guard for the
  active path and records ``active_fail_open``; swallowing the exception here
  would hide it and -- worse -- feed a fabricated token count into the request
  budget, which is exactly the failure mode ``candidates.py`` and
  ``request.py`` refuse for the same reason.

Nothing credential-bearing can reach a result field: the retention state is
built from caller-supplied identity strings only (the API key and endpoint live
on :class:`JevConfig` and are used solely by the client's transport), and the
client scrubs every error string it produces before returning it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from headroom.proxy.jev.candidates import (
    RECENT_TAIL_EXCLUSION,
    JevCandidate,
    select_candidates,
)
from headroom.proxy.jev.client import JEV_DECISIONS, build_request_payload
from headroom.proxy.jev.config import JevConfig
from headroom.proxy.jev.identity import revision_for
from headroom.proxy.jev.request import (
    build_questions,
    build_retention_state,
    enforce_state_budget,
)
from headroom.tokenizers import get_tokenizer

# Request bounds are operator configuration, not constants: ``max_candidates``
# (HEADROOM_JEV_MAX_CANDIDATES), ``max_state_tokens``
# (HEADROOM_JEV_MAX_STATE_TOKENS) and ``max_candidate_tokens``
# (HEADROOM_JEV_MAX_CANDIDATE_TOKENS) are read off ``config`` here exactly as
# the shadow runner reads them. A compaction boundary is episodic and can
# justify a larger request than an ordinary shadow turn -- but that is a
# decision the operator makes by raising those knobs, not one this module makes
# by ignoring them. ``max_state_tokens`` still bounds the MEASURED serialized
# request (Phase 0a found an estimated budget could re-trigger the exact
# ``max_tokens_exceeded`` error it was meant to prevent, which is why
# ``enforce_state_budget`` measures).


@dataclass(frozen=True)
class JevActiveDecision:
    """One turn's retention answer.

    ``candidates`` is every candidate selection produced, including any the
    measured request budget trimmed before the call -- a trimmed candidate is
    still honestly a candidate, it was simply never asked about.
    ``decisions`` has one entry per candidate in ``candidates``; a candidate
    Jev was not asked about, or did not answer for, is ``keep``.

    ``sent`` is the subset the request budget admitted -- exactly the
    candidates Jev was asked about, in order, and always a prefix of
    ``candidates``. It exists because ``decisions`` alone cannot tell a
    Jev-answered ``keep`` from a budget-trimmed one: both read ``"keep"``, and
    reporting the second as the first overstates both how many candidates were
    sent and how often Jev chose to keep. ``len(candidates) - len(sent)`` is
    the trimmed count, which is its own signal: a high trim rate means the
    request budget is the binding constraint, not Jev's judgement.
    """

    candidates: list[JevCandidate]
    decisions: dict[str, str]
    called: bool
    error: str | None
    latency_ms: float
    sent: list[JevCandidate] = field(default_factory=list)


async def decide_active_retention(
    *,
    config: JevConfig,
    client: Any,
    messages: list[dict[str, Any]],
    frozen_prefix: int,
    model: str,
    session_id: str,
    branch_id: str,
    provider: str = "compress",
    message_shape: str = "openai",
) -> JevActiveDecision:
    """Select candidates and ask Jev what may go. Never mutates ``messages``.

    ``client`` is supplied by the caller (Task 16 owns its lifetime): this
    function neither constructs nor closes one.
    """
    # Track A's selection takes the tokenizer's ``count_text`` callable, not
    # the tokenizer: the eligibility rules live in one place and this path
    # passes the tokenizer the request already resolved for this model.
    tokenizer = get_tokenizer(model)
    count_text = tokenizer.count_text

    candidates = select_candidates(
        messages,
        frozen_prefix=frozen_prefix,
        count_text=count_text,
        max_candidates=config.max_candidates,
    )
    if not candidates:
        return JevActiveDecision(
            candidates=[], decisions={}, called=False, error=None, latency_ms=0.0, sent=[]
        )

    # Every selected candidate gets an answer, and the default is always the
    # one that moves no tokens.
    decisions = {cand.candidate_id: "keep" for cand in candidates}

    # The revision names the candidate set this turn asked about, exactly as
    # Track A's shadow path computes it, and travels in the state so a Jev-side
    # log can be correlated with a Headroom-side one.
    revision = revision_for([cand.fingerprint for cand in candidates])

    def make_payload(sel: list[JevCandidate], view_tokens: int) -> dict[str, Any]:
        # The budget is measured on the payload that is actually POSTed --
        # state AND questions AND model -- because that whole body is what Jev
        # rejects with ``max_tokens_exceeded`` when it is too large.
        state = build_retention_state(
            provider=provider,
            model=model,
            jev_model=config.model,
            session_id=session_id,
            branch_id=branch_id,
            revision=revision,
            message_shape=message_shape,
            total_messages=len(messages),
            frozen_prefix=frozen_prefix,
            recent_tail=RECENT_TAIL_EXCLUSION,
            candidates=sel,
            max_candidate_tokens=view_tokens,
        )
        return build_request_payload(config, state, build_questions(sel, len(messages)))

    # An empty ``sent`` means the third value is the fixed overhead, not a
    # sendable payload: there is no call to make, and every candidate keeps.
    sent, view_tokens, _serialized = enforce_state_budget(
        candidates,
        count_text=count_text,
        make_payload=make_payload,
        max_candidate_tokens=config.max_candidate_tokens,
        max_state_tokens=config.max_state_tokens,
    )
    if not sent:
        # Nothing fit the measured request budget: every candidate was
        # trimmed, so `sent` is empty and none of these keeps is Jev's.
        return JevActiveDecision(
            candidates=candidates,
            decisions=decisions,
            called=False,
            error=None,
            latency_ms=0.0,
            sent=[],
        )

    payload = make_payload(sent, view_tokens)
    candidate_ids = [cand.candidate_id for cand in sent]

    answer = await client.decide(
        state=payload["state"],
        questions=payload["questions"],
        candidate_ids=candidate_ids,
    )

    # Fail open to keep, enforced here rather than trusted from the client:
    #
    # * A failed call moves nothing at all. ``JevAnswer.ok`` is ``error is
    #   None``, so that is the test used; a call that reports an error has no
    #   usable answer, whatever it also put in ``decisions``.
    # * Otherwise an id Jev did not answer for, answered unparseably, or
    #   answered with a word outside the decision vocabulary stays ``keep``.
    #
    # ``JevClient`` already guarantees both, but ``client`` is injected and
    # these decisions are what later steps act on to remove content, so the
    # guarantee is a property of this function, not of the client it was given.
    if answer.error is None:
        for cid in candidate_ids:
            choice = answer.decisions.get(cid)
            decisions[cid] = choice if choice in JEV_DECISIONS else "keep"

    return JevActiveDecision(
        candidates=candidates,
        decisions=decisions,
        called=True,
        error=answer.error,
        latency_ms=answer.latency_ms,
        sent=sent,
    )
