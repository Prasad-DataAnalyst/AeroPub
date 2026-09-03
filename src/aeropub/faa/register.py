"""Turning FAA AIXM into a NOTAM register.

The adapter between :mod:`aeropub.faa.aixm` and
:mod:`aeropub.notam_register`. This is where the FAA's linked features become
canonical entity keys, and it is the step that makes a NOTAM joinable to an
aerodrome dossier rather than a string to be searched.

Everything here is a mapping, not an interpretation. No rule reads the NOTAM
text; nothing infers what a message affects beyond what the source states.
Where the FAA links no feature, the subject records only the location the
message was filed against, marked as such.
"""

from __future__ import annotations

from typing import Iterable

from aeropub.archive import ArchiveEntry
from aeropub.entities import compose, normalise
from aeropub.faa.aixm import NmsNotam, NotamFeed
from aeropub.notam_register import NotamRegister, RegisteredNotam, Subject, SubjectKind

__all__ = ["FEATURE_KINDS", "register_feed", "registered", "subjects_of"]

#: AIXM feature type to subject kind. A type absent from this table produces no
#: subject: keying an object we have no convention for would put an
#: unrecognised entity into the index, and a dossier would then report a NOTAM
#: against something nobody can look up.
FEATURE_KINDS: dict[str, SubjectKind] = {
    "AirportHeliport": SubjectKind.AERODROME,
    "Runway": SubjectKind.RUNWAY,
    "RunwayDirection": SubjectKind.RUNWAY_DIRECTION,
    "Taxiway": SubjectKind.TAXIWAY,
    "TaxiwayElement": SubjectKind.TAXIWAY,
    "Apron": SubjectKind.APRON,
    "ApronElement": SubjectKind.APRON,
    "Navaid": SubjectKind.NAVAID,
    "Airspace": SubjectKind.AIRSPACE,
    "Route": SubjectKind.ROUTE,
    "RouteSegment": SubjectKind.ROUTE,
    "DesignatedPoint": SubjectKind.ROUTE,
    "VerticalStructure": SubjectKind.OBSTACLE,
    "Procedure": SubjectKind.PROCEDURE,
}

#: Deliberately not in the table. ``RunwayElement`` carries a runway's extent
#: geometry, not a separate operational object, and its ``designator`` is
#: always empty — indexing it would double-count every runway NOTAM under a
#: key made of a UUID.
_GEOMETRY_ONLY = frozenset({"RunwayElement"})

#: Prefixes for objects that hang off an aerodrome, matching the fact store's
#: ``OTHH/RWY34L`` convention.
_AT_AERODROME = {
    SubjectKind.RUNWAY: "RWY",
    SubjectKind.RUNWAY_DIRECTION: "RWY",
    SubjectKind.TAXIWAY: "TWY",
    SubjectKind.APRON: "APRON",
}


def _aerodrome_key(notam: NmsNotam) -> str | None:
    """The aerodrome every other key on this message hangs from.

    Prefers the linked ``AirportHeliport``, then the ICAO indicator the FAA
    supplied, then the location the NOTAM was filed against.

    A wrinkle worth naming rather than hiding: this follows what each message
    happens to carry, so the same aerodrome can key as ``8WC`` in a message
    with no ICAO indicator and as ``K8WC`` in one that has it. The fix is the
    ``/locationseries`` endpoint — which is precisely the FAA's mapping from
    local identifiers to ICAO ones, and which this connector already fetches.
    Until that is ingested, keys are not reconciled and this comment is the
    warning.
    """
    for feature in notam.aerodromes():
        if feature.designator:
            return normalise(feature.designator)
    for candidate in (notam.icao_location, notam.location):
        if candidate and candidate.strip():
            return normalise(candidate)
    return None


def _entity_for(kind: SubjectKind, designator: str, aerodrome: str | None) -> str | None:
    if kind is SubjectKind.AERODROME:
        return designator
    prefix = _AT_AERODROME.get(kind)
    if prefix is not None:
        if not aerodrome:
            # "RWY20" on its own names a runway at every aerodrome that has
            # one, so there is nothing to key it under.
            return None
        return compose(aerodrome, prefix, designator)
    return f"{kind.value.upper()}:{designator}"


def subjects_of(notam: NmsNotam) -> tuple[Subject, ...]:
    """Canonical subjects for one NOTAM, in document order.

    Falls back to a single :attr:`SubjectKind.FILED_LOCATION` subject when the
    FAA linked no feature — most airspace NOTAM, including the UAS notices that
    make up much of the domestic feed. Knowing a message concerns ZBW is real
    information; it is simply not the same information as knowing which runway
    it closes.
    """
    aerodrome = _aerodrome_key(notam)
    subjects: list[Subject] = []
    seen: set[str] = set()

    for feature in notam.features:
        if feature.kind in _GEOMETRY_ONLY:
            continue
        kind = FEATURE_KINDS.get(feature.kind)
        if kind is None:
            continue
        designator = (feature.designator or feature.name or "").strip().upper()
        if not designator:
            continue
        entity = _entity_for(kind, designator, aerodrome)
        if entity is None or entity in seen:
            continue
        seen.add(entity)
        subjects.append(
            Subject(
                entity=entity,
                kind=kind,
                designator=feature.designator,
                name=feature.name,
                icao=notam.icao_location if kind is SubjectKind.AERODROME else None,
                uuid=feature.uuid,
            )
        )

    if subjects:
        return tuple(subjects)

    filed = notam.icao_location or notam.location
    if not filed:
        return ()
    return (
        Subject(
            entity=filed.strip().upper(),
            kind=SubjectKind.FILED_LOCATION,
            designator=notam.location,
            name=notam.airport_name,
            icao=notam.icao_location,
        ),
    )


def registered(notam: NmsNotam, entry: ArchiveEntry) -> RegisteredNotam:
    """One NOTAM with its subjects and a citation resolving to archived bytes."""
    return RegisteredNotam(
        identifier=notam.identifier,
        subjects=subjects_of(notam),
        source=notam.source_ref(entry),
        text=notam.text,
        effective_start=notam.effective_start,
        effective_end=notam.effective_end,
        permanent=notam.permanent,
        estimated=notam.estimated,
        schedule=notam.schedule,
        printed=notam.simple_text,
        classification=notam.classification,
        kind=notam.kind,
    )


def register_feed(
    notams: Iterable[NmsNotam] | NotamFeed,
    entry: ArchiveEntry,
    *,
    into: NotamRegister | None = None,
) -> NotamRegister:
    """Index a whole feed.

    ``entry`` is the archived artefact the feed was read from, so every NOTAM
    in the register cites the exact bytes it came from. Pass ``into`` to
    accumulate several classifications into one register.

    A NOTAM the mapping could not key at all is skipped, and the count is the
    difference between the register's length and the feed's ``notams_read`` —
    which is why callers should check both rather than trusting the length.
    """
    register = into if into is not None else NotamRegister()
    for notam in notams:
        item = registered(notam, entry)
        if not item.subjects:
            # Nothing to index it under: no linked feature and no location.
            # Skipped rather than filed under a placeholder key, which would
            # make it findable only by someone who already knew it existed.
            continue
        register.add(item)
    return register
