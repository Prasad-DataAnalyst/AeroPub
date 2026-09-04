"""The entity key grammar — how everything in this system is named.

A fact, a NOTAM subject, a dossier and a bulletin all have to agree on what
``"OTHH/RWY34L"`` means, or they cannot be joined. That grammar is small enough
to be tempting to reimplement at each call site, and it was: four copies of
"this key, or something beneath it", one of them subtly different from the
others. That is precisely how a runway NOTAM stops appearing on an aerodrome
dossier six months after everything worked.

The grammar
-----------
=============================  ==========================================
``OTHH``                       an aerodrome, by ICAO indicator where one
                               exists and by the State's own identifier
                               where it does not (``8WC`` has no ICAO form)
``OTHH/RWY34L``                an object on that aerodrome
``8WC/RWY02/20``               a full runway pair — the designator itself
                               contains a slash, which is why only the
                               *first* separator divides the key
``AIRSPACE:EGTT``              a free-standing object, belonging to no
                               aerodrome
=============================  ==========================================

Containment runs one way. ``covers("OTHH", "OTHH/RWY34L")`` is true, and
``covers("OTHH/RWY34L", "OTHH")`` is false: an apron closure filed against the
whole aerodrome must never surface as a finding about a runway.
"""

from __future__ import annotations

from typing import Iterable, Iterator

__all__ = [
    "APRON",
    "RUNWAY",
    "SEPARATOR",
    "under",
    "TAXIWAY",
    "aerodrome_of",
    "beneath",
    "compose",
    "covers",
    "designator_of",
    "is_free_standing",
    "kind_of",
    "named",
    "normalise",
    "scope_of",
]

#: What divides an aerodrome from an object on it. Only the first occurrence
#: divides: a runway pair designator legitimately contains one.
SEPARATOR = "/"

#: What divides a free-standing object's kind from its designator.
KIND_SEPARATOR = ":"

RUNWAY = "RWY"
TAXIWAY = "TWY"
APRON = "APRON"


def normalise(key: str) -> str:
    """Canonical form: trimmed, upper case, single-spaced.

    Applied at every boundary, so ``" othh "`` and ``"OTHH"`` are one entity
    rather than two that never join.
    """
    return " ".join(str(key).strip().upper().split())


def is_free_standing(key: str) -> bool:
    """Whether this names an object belonging to no aerodrome.

    Airspace, routes and navaids are not on an aerodrome, so they carry a kind
    prefix instead of an aerodrome one. Rolling one up under an aerodrome would
    attribute a danger area to a runway.
    """
    head, sep, _ = normalise(key).partition(KIND_SEPARATOR)
    return bool(sep) and SEPARATOR not in head


def aerodrome_of(key: str) -> str | None:
    """The aerodrome a key hangs from, or ``None`` for a free-standing object."""
    canonical = normalise(key)
    if is_free_standing(canonical):
        return None
    return canonical.partition(SEPARATOR)[0] or None


def scope_of(key: str) -> str | None:
    """What the key names *on* its aerodrome, or ``None`` for the aerodrome itself.

    ``"OTHH/RWY34L"`` gives ``"RWY34L"``; ``"OTHH"`` gives ``None``. Only the
    first separator divides, so ``"8WC/RWY02/20"`` gives ``"RWY02/20"`` rather
    than losing the second half of a runway pair.
    """
    canonical = normalise(key)
    if is_free_standing(canonical):
        return None
    rest = canonical.partition(SEPARATOR)[2]
    return rest or None


def compose(aerodrome: str, prefix: str, designator: str) -> str:
    """Build a key for an object on an aerodrome.

    Raises rather than producing a key with an empty half: ``"RWY20"`` with no
    aerodrome names a runway at every aerodrome that has one, and ``"OTHH/"``
    names nothing at all.
    """
    ad = normalise(aerodrome)
    thing = normalise(designator)
    if not ad:
        raise ValueError(
            f"cannot key {normalise(prefix)}{thing} with no aerodrome: on its own it names "
            "an object at every aerodrome that has one"
        )
    if not thing:
        raise ValueError(f"cannot key an object on {ad} with no designator")
    return f"{ad}{SEPARATOR}{normalise(prefix)}{thing}"


def under(aerodrome: str, scope: str) -> str:
    """Build a key from an aerodrome and a whole scope. The inverse of :func:`scope_of`.

    ``under("OTHH", "RWY34L")`` gives ``"OTHH/RWY34L"``, and ``scope_of`` of
    that gives ``"RWY34L"`` back. Use it wherever the scope arrives as one
    string — from a profile, a manifest, a foreign identifier — rather than as
    a kind and a designator, which is what :func:`compose` is for.

    It exists so that nothing outside this module ever joins the two halves
    itself. That rule is not fussiness: the separator was previously written
    out at four call sites, two of them normalised case and two did not, and an
    aerodrome with a live runway NOTAM reported as a coverage gap.
    """
    ad = normalise(aerodrome)
    thing = normalise(scope)
    if not ad:
        raise ValueError(
            f"cannot key {thing} with no aerodrome: on its own it names an "
            "object at every aerodrome that has one"
        )
    if not thing:
        raise ValueError(f"cannot key an object on {ad} with no scope")
    return f"{ad}{SEPARATOR}{thing}"


def named(kind: str, designator: str) -> str:
    """Build a key for an object belonging to no aerodrome.

    ``named("FIR", "OTDF")`` gives ``"FIR:OTDF"``. The counterpart of
    :func:`compose` for the free-standing half of the grammar, and it exists
    for the same reason: the separator was never written out anywhere before
    because nothing built these keys, and the first module that needed one
    would have joined the halves itself.

    A kind containing the aerodrome separator is refused. ``"A/B:C"`` parses as
    an object on an aerodrome, not as a free-standing thing, so a key built
    that way would be silently rolled up under an aerodrome called ``A``.
    """
    prefix = normalise(kind)
    thing = normalise(designator)
    if not prefix:
        raise ValueError(
            f"cannot name {thing} with no kind: a bare designator does not say "
            "whether it is an airspace, a route or a navaid"
        )
    if not thing:
        raise ValueError(f"cannot name a {prefix} with no designator")
    if SEPARATOR in prefix:
        raise ValueError(
            f"kind {prefix!r} contains {SEPARATOR!r}, which divides an "
            "aerodrome from an object on it. A key built this way would be "
            "rolled up under an aerodrome that does not exist."
        )
    return f"{prefix}{KIND_SEPARATOR}{thing}"


def kind_of(key: str) -> str | None:
    """The kind of a free-standing key, or ``None`` where it hangs from an aerodrome."""
    canonical = normalise(key)
    if not is_free_standing(canonical):
        return None
    return canonical.partition(KIND_SEPARATOR)[0] or None


def designator_of(key: str) -> str | None:
    """The designator of a free-standing key. The inverse of :func:`named`."""
    canonical = normalise(key)
    if not is_free_standing(canonical):
        return None
    return canonical.partition(KIND_SEPARATOR)[2] or None


def covers(parent: str, key: str) -> bool:
    """Whether ``key`` is ``parent`` or sits beneath it.

    The one implementation of this rule. Containment is one-directional by
    design: asking about an aerodrome reaches its runways, asking about a
    runway does not reach the aerodrome.

    Matching is on a whole path segment, so ``"OTHH"`` does not cover
    ``"OTHHX"`` — a prefix that is not a segment boundary is a different
    aerodrome, not a child.
    """
    top = normalise(parent)
    candidate = normalise(key)
    return candidate == top or candidate.startswith(f"{top}{SEPARATOR}")


def beneath(parent: str, keys: Iterable[str]) -> Iterator[str]:
    """Every key covered by ``parent``, in the order given."""
    for key in keys:
        if covers(parent, key):
            yield key
