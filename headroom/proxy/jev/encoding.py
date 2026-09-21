"""The ONE way text becomes bytes for a Jev identity, hash or comparison.

Every place in this package that hashes caller-controlled text, measures it for
a ceiling, or compares two encodings of it goes through
:func:`encode_identity_text`. There is deliberately one function rather than one
per track: the property it carries is *injectivity*, and two copies of an
encoding choice drift, at which point the property is silently gone from one of
them.

``errors="surrogatepass"``, never ``"replace"`` or ``"ignore"``. Both of those
are lossy and NOT injective -- ``replace`` collapses every unencodable scalar
onto the single byte ``b"?"``, so a body of ``"\\ud800"`` and a body of ``"?"``
encode identically and therefore hash identically. That is client-reachable:
``json.loads`` accepts a lone surrogate escape happily, so a request can carry
one into any of the fields hashed here.

The consequence is not a cosmetic one. Track B stages every candidate of a
boundary turn under the single literal branch id ``compress``, so two colliding
candidates share one CCR entry: the second write overwrites the first original
and both retrieval markers then resolve to the second candidate's content. A
caller asking ``/v1/retrieve`` for one gets plausible but WRONG content back,
silently -- exactly the failure the write -> acknowledge -> lease sequence in
``retention_ccr`` exists to prevent.

``surrogatepass`` gives each surrogate its own three-byte sequence, so distinct
strings stay distinct. It is total over ``str`` -- the surrogate range is the
only thing UTF-8 cannot encode strictly, and this handler covers precisely that
range -- so it never raises and never costs a candidate its retention.
Rejecting lone surrogates instead would also be safe, but it would trade the
savings away to buy a property this handler gives for free.

Where the text is a structure this package serializes ITSELF, ``json.dumps``
with ``ensure_ascii=True`` meets the same family earlier and leaves nothing for
this function to do (see ``identity._canonical_root``). This function is for the
other case: a raw ``str`` off the wire, which no ``json.dumps`` flag can reach.
"""

from __future__ import annotations


def encode_identity_text(text: str) -> bytes:
    """Encode ``text`` to bytes injectively. Never raises for any ``str``."""
    return text.encode("utf-8", "surrogatepass")
