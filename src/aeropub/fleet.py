"""The fleet library — who operates what, and on whose word.

Layer three (:mod:`aeropub.operator`) answers "what does this change mean for
*this* operator", and it needs a profile to do it: a fleet of cited aircraft
types and a network of aerodromes with roles. Someone has to write that
profile. Today that someone is the customer, on their first afternoon, from a
blank template — which is the moment most of them stop.

This module is the base that makes the first session a lookup instead. Give it
an ICAO operator designator, or a single tail number, and it returns the fleet
that layer three consumes.

Three layers, three authorities, never merged
---------------------------------------------
The library holds three different kinds of statement, and they come from three
different places that are wrong in three different ways:

============================  ==========================================
Statement                     Who is authoritative
============================  ==========================================
This type's wingspan is X     The manufacturer's planning manual
This tail is that type        The national civil aircraft register
This operator holds that tail The operator, or an observation of it flying
============================  ==========================================

A register says a tail *is* a GL7T; it does not say who operates it today. An
observation says a tail *flew* a sector; it does not say the operator holds it
on their AOC. An operator's own list says what they hold; it does not verify
the type. So every holding carries a :class:`Basis` recording which of the
three said so, and nothing here flattens them into an unattributed whole.

The bibliography, and why it ships before the figures
-----------------------------------------------------
Parsing two hundred planning manuals is not a prerequisite for being useful.
A :class:`TypeReference` is what you hold *before* you have read a document: which
document has this type's figures, at which revision, in which table. It is
deliberately not a :class:`~aeropub.facts.SourceRef`, because nothing has been
read yet and a citation that resolves to nobody's reading is worse than no
citation at all. Reading it — through the ACAP manifest path — is what turns a
:class:`TypeReference` into figures each carrying a real ``SourceRef``.

That distinction is the whole point of :class:`TypeCoverage`. A type the library
has never heard of and a type whose manual is sitting in the bibliography
unread are both "we cannot check the wingspan", and they are completely
different problems: one is research, the other is an afternoon's ingest.

Business aviation is not a smaller case of the airline case
------------------------------------------------------------
An airline's profile can be discovered from what it flies, because it flies a
published network. A management company operating three Global 7500s has no
network to discover — the same tail serves a different city pair every week.
For those operators the fleet *is* the profile, which is why
:func:`route_profile` builds the network from a stated city pair rather than
expecting one to already exist. They are also the population where tails change
hands most often, so every record here carries the date it was recorded rather
than presenting a current owner as a timeless fact.

What this module will not do
----------------------------
It will not invent a fleet. Every operator record, every registration and every
figure arrives from a document that was read and hashed, exactly as
:mod:`aeropub.acap` requires for aircraft. A library assembled from aggregator
sites would be quick, plausible on screen, and precisely the failure the
no-mock rule exists to prevent: a wrong wingspan is a wrong stand allocation
and a wrong ACR is a wrong pavement verdict. **Absent is a usable answer here.
Approximately right is not.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from aeropub.acap import load_aircraft, merge
from aeropub.aircraft import AircraftType
from aeropub.dossier import AerodromeDossier
from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import ManifestError, document_source, read_manifest, sub_source, to_date
from aeropub.operator import Fleet, Network, NetworkEntry, OperatorProfile, Role
from aeropub.suitability import Assessment, Suitability, assess_suitability

__all__ = [
    "Basis",
    "fleet_of",
    "FleetLibrary",
    "FleetScreen",
    "Holding",
    "library_template",
    "load_library",
    "merge_libraries",
    "OperatorFleet",
    "OperatorRecord",
    "Registration",
    "route_profile",
    "screen",
    "Segment",
    "TypeCoverage",
    "TypeGap",
    "TypeReference",
    "TypeScreen",
]

#: The parser identity written into citations read from a library document.
LIBRARY_PARSER_ID = "aeropub.fleet"


def _count(number: int, singular: str, plural: str | None = None) -> str:
    """A count and its noun, agreeing. Output nobody has to forgive."""
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _mark(text: str) -> str:
    """Canonical registration mark: trimmed, upper case, internal space removed.

    ``"a7-baa"`` and ``"A7-BAA"`` are one aeroplane. Hyphenation is left alone
    because States differ on it and normalising it away would merge ``N123AB``
    with a mark that is genuinely different.
    """
    return "".join(str(text).split()).upper()


def _designator(text: str) -> str:
    return "".join(str(text).split()).upper()


class Basis(str, Enum):
    """On whose word an operator holds a registration.

    Kept separate from :class:`~aeropub.facts.SourceRef` because it answers a
    different question. The ``SourceRef`` says *which document* — this says
    what kind of claim that document was making.
    """

    REGISTER = "register"
    """A national civil aircraft register. Authoritative for the aeroplane —
    its type, serial and registered owner — and silent on who operates it. The
    registered owner of a leased airliner is very often a lessor that has never
    flown it."""

    ATTESTED = "attested"
    """The operator's own statement that they hold this tail. The only basis
    that speaks directly to operation, and the only one that can say a tail is
    on their AOC."""

    OBSERVED = "observed"
    """Seen operating, from flight data. Evidence that a tail flew, not that an
    operator holds it: a wet lease, a ferry or a one-off charter all look
    identical from a track."""

    @property
    def states_operation(self) -> bool:
        """Whether this basis speaks to who *operates* the aeroplane.

        Only attestation does. A register states ownership and an observation
        states an event; treating either as a statement of operation is how a
        lessor ends up in a fleet list.
        """
        return self is Basis.ATTESTED


class Segment(str, Enum):
    """What kind of operation this is. Changes how the profile is built."""

    COMMERCIAL = "commercial"
    """Scheduled or charter passenger airline."""

    CARGO = "cargo"
    """Freight. Same aerodrome checks, different network shape — night
    curfews and stand loading bite where passenger schedules do not."""

    BUSINESS = "business"
    """Charter, fractional or management company operating executive aircraft
    for others. Business, luxury and executive jets sit here."""

    PRIVATE = "private"
    """Owner-operated or a corporate flight department. Often a single tail,
    and often the registered owner is the only name there is."""

    @property
    def has_discoverable_network(self) -> bool:
        """Whether "where do they fly" can be answered from what they flew.

        An airline repeats city pairs, so observation converges on a network.
        A business or private operator flies a different pairing every week —
        past sectors describe where the aeroplane *has been*, and reading them
        as a network would produce a profile that is out of date the first time
        the customer files a plan.
        """
        return self in (Segment.COMMERCIAL, Segment.CARGO)


class TypeCoverage(str, Enum):
    """What the library holds about one aircraft type.

    The same three states the publication watcher uses, for the same reason:
    "we cannot check this" collapses two completely different problems into one
    word, and only one of them is anybody's afternoon.
    """

    VERIFIED = "verified"
    """Figures held, each carrying the citation it was read with."""

    REGISTERED = "registered"
    """The document that holds the figures is named in the bibliography, and
    nobody has read it yet. Known work, not unknown."""

    ABSENT = "absent"
    """Neither figures nor a document to get them from. Research."""

    @property
    def is_usable(self) -> bool:
        return self is TypeCoverage.VERIFIED


@dataclass(frozen=True, slots=True)
class TypeReference:
    """Where a type's figures are to be read from. Not a citation.

    A :class:`~aeropub.facts.SourceRef` says a document *was* read, by which
    parser, at which hash. A ``TypeReference`` says a document *exists* and has not
    been. Keeping them different types is what stops a bibliography entry from
    ever being presentable as provenance.
    """

    designator: str
    publisher: str
    document: str
    revision: str = ""
    locator: str = ""
    url: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "designator", _designator(self.designator))
        if not self.designator:
            raise ValueError("TypeReference.designator must be a non-empty string")
        if not self.publisher.strip():
            raise ValueError(
                "TypeReference.publisher must be a non-empty string — a document "
                "with no publisher cannot be gone and fetched by anybody else."
            )
        if not self.document.strip():
            raise ValueError("TypeReference.document must be a non-empty string")


@dataclass(frozen=True, slots=True)
class Registration:
    """One tail, as one document recorded it.

    ``source`` is mandatory for the same reason it is on a
    :class:`~aeropub.aircraft.Characteristic`: a tail-to-type mapping with
    nothing behind it is indistinguishable from one somebody remembered, and it
    decides which aeroplane every subsequent check is about.
    """

    mark: str
    designator: str
    source: SourceRef
    basis: Basis
    serial: str = ""
    model: str = ""
    owner: str = ""
    recorded_on: date | None = None
    """When the document stated this. Tails change hands, most often in exactly
    the executive and private population this library exists to cover, so the
    date is part of the record rather than an afterthought."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "mark", _mark(self.mark))
        object.__setattr__(self, "designator", _designator(self.designator))
        if not self.mark:
            raise ValueError("Registration.mark must be a non-empty string")
        if not self.designator:
            raise ValueError("Registration.designator must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Registration.source must be a SourceRef")
        if not isinstance(self.basis, Basis):
            raise TypeError("Registration.basis must be a Basis")


@dataclass(frozen=True, slots=True)
class Holding:
    """One operator's claim on one tail, and what kind of claim it is."""

    mark: str
    basis: Basis
    source: SourceRef
    recorded_on: date | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "mark", _mark(self.mark))
        if not self.mark:
            raise ValueError("Holding.mark must be a non-empty string")
        if not isinstance(self.basis, Basis):
            raise TypeError("Holding.basis must be a Basis")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Holding.source must be a SourceRef")


@dataclass(frozen=True, slots=True)
class OperatorRecord:
    """One operator in the library, and the tails attributed to them."""

    icao: str
    name: str
    iata: str = ""
    segment: Segment = Segment.COMMERCIAL
    holdings: tuple[Holding, ...] = ()
    bases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "icao", normalise(self.icao).replace(" ", ""))
        object.__setattr__(self, "iata", normalise(self.iata).replace(" ", ""))
        object.__setattr__(self, "bases", tuple(normalise(b) for b in self.bases))
        if not self.icao:
            raise ValueError("OperatorRecord.icao must be a non-empty string")
        if not self.name.strip():
            raise ValueError("OperatorRecord.name must be a non-empty string")
        if not isinstance(self.segment, Segment):
            raise TypeError("OperatorRecord.segment must be a Segment")
        marks = [h.mark for h in self.holdings]
        duplicated = {m for m in marks if marks.count(m) > 1}
        if duplicated:
            raise ValueError(
                f"{self.icao} holds {', '.join(sorted(duplicated))} more than "
                "once. Two documents naming the same tail are merged, not "
                "listed twice — otherwise one of them silently answers."
            )

    @property
    def fleet_size(self) -> int:
        """How many tails are attributed to this operator.

        Counted, not claimed. An operator whose library entry names four tails
        has a fleet size of four here even if it flies four hundred, and that
        is the honest number: it is what the library holds.
        """
        return len(self.holdings)

    def marks(self, *, basis: Basis | None = None) -> tuple[str, ...]:
        return tuple(
            sorted(h.mark for h in self.holdings if basis is None or h.basis is basis)
        )


@dataclass(frozen=True, slots=True)
class TypeGap:
    """A type the operator holds that the library cannot describe.

    Reported rather than dropped. A fleet that quietly shrinks to the types
    with figures produces an assessment that looks complete and covers three
    aeroplanes out of five.
    """

    designator: str
    coverage: TypeCoverage
    references: tuple[TypeReference, ...] = ()
    marks: tuple[str, ...] = ()

    @property
    def is_actionable(self) -> bool:
        """Whether somebody could close this gap today.

        A registered gap names the document to read. An absent one does not,
        and needs research before it needs an ingest.
        """
        return self.coverage is TypeCoverage.REGISTERED and bool(self.references)


@dataclass(frozen=True, slots=True)
class OperatorFleet:
    """What the library can say about one operator's fleet, gaps included."""

    operator: OperatorRecord
    fleet: Fleet
    gaps: tuple[TypeGap, ...] = ()
    unidentified: tuple[str, ...] = ()
    """Tails held with no registration record, so no type is known for them.
    A different failure from a type gap: here we do not know what aeroplane it
    is at all."""

    @property
    def is_complete(self) -> bool:
        """Whether every held tail resolved to a type with cited figures."""
        return not self.gaps and not self.unidentified

    @property
    def designators(self) -> tuple[str, ...]:
        """Every type designator held, described or not."""
        described = {t.designator for t in self.fleet}
        return tuple(sorted(described | {g.designator for g in self.gaps}))

    @property
    def actionable_gaps(self) -> tuple[TypeGap, ...]:
        return tuple(g for g in self.gaps if g.is_actionable)

    def render(self) -> str:
        """What the library holds for this operator, gaps first."""
        record = self.operator
        lines = [
            f"FLEET — {record.name} ({record.icao}"
            + (f"/{record.iata}" if record.iata else "")
            + f")  ·  {record.segment.value}",
            f"{_count(record.fleet_size, 'tail')} held  ·  "
            f"{_count(len(self.fleet), 'type')} described  ·  "
            f"{_count(len(self.gaps), 'type')} not described"
            + (f"  ·  {_count(len(self.unidentified), 'tail')} unidentified"
               if self.unidentified else ""),
            "",
        ]
        if not record.holdings:
            lines.append(
                "No tails recorded against this operator. That is what the "
                "library holds, not a statement that they fly nothing."
            )
            return "\n".join(lines)

        for aircraft in self.fleet:
            letter = aircraft.code_letter() or "unknown"
            lines.append(
                f"  {aircraft.designator:<6} code {letter:<8} "
                f"{len(aircraft.characteristics)} figures held"
            )
        for gap in self.gaps:
            marks = ", ".join(gap.marks[:4]) + ("…" if len(gap.marks) > 4 else "")
            lines.append(f"  {gap.designator:<6} {gap.coverage.value.upper():<11} {marks}")
            for reference in gap.references:
                lines.append(
                    f"      read: {reference.publisher} {reference.document}"
                    + (f" ({reference.revision})" if reference.revision else "")
                )
        if self.unidentified:
            lines += [
                "",
                "TAILS WITH NO TYPE — held, but no register entry says what they are",
                "  " + ", ".join(self.unidentified),
            ]
        if not self.is_complete:
            lines += [
                "",
                "This fleet is incomplete. Every line above is held and cited; "
                "the gaps are",
                "listed rather than dropped, because a fleet that silently "
                "shrinks to the types",
                "with figures produces an assessment that looks complete and "
                "covers half the aeroplanes.",
            ]
        return "\n".join(lines)

    def as_profile(self, network: Network | None = None) -> OperatorProfile:
        """Hand this to layer three.

        The network stays empty unless one is given: where the operator flies
        is not something the library knows, and an empty network is a truthful
        answer that layer three already renders as ``NOT_IN_NETWORK`` rather
        than as no exposure.
        """
        return OperatorProfile(
            name=self.operator.name,
            fleet=self.fleet,
            network=network or Network(),
        )


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FleetLibrary:
    """Operators, registrations, aircraft figures and the bibliography.

    One instance normally comes from one document, in the same way one aircraft
    manifest describes one document. Several are combined with
    :func:`merge_libraries`, which is how a national register, an operator's
    own fleet list and a set of planning manuals become one library while each
    statement keeps the citation it arrived with.
    """

    operators: tuple[OperatorRecord, ...] = ()
    registrations: tuple[Registration, ...] = ()
    references: tuple[TypeReference, ...] = ()
    types: tuple[AircraftType, ...] = ()

    def __post_init__(self) -> None:
        codes = [o.icao for o in self.operators]
        duplicated = {c for c in codes if codes.count(c) > 1}
        if duplicated:
            raise ValueError(
                f"the library lists {', '.join(sorted(duplicated))} more than "
                "once. Merge the records so one operator has one fleet."
            )
        marks = [r.mark for r in self.registrations]
        repeated = {m for m in marks if marks.count(m) > 1}
        if repeated:
            raise ValueError(
                f"the library lists {', '.join(sorted(repeated))} more than "
                "once. A tail with two type records answers differently "
                "depending on which one is read first."
            )
        designators = [t.designator for t in self.types]
        twice = {d for d in designators if designators.count(d) > 1}
        if twice:
            raise ValueError(
                f"the library holds figures for {', '.join(sorted(twice))} "
                "twice. Manifests for one type are merged, not listed twice."
            )

    # -- lookups ----------------------------------------------------------

    def operator(self, code: str) -> OperatorRecord | None:
        """Find an operator by ICAO designator, IATA code or name.

        ICAO first. The two code spaces overlap — an IATA two-letter code is
        never an ICAO three-letter one, but names collide with both — so the
        order is fixed rather than best-effort.
        """
        wanted = normalise(code).replace(" ", "")
        for record in self.operators:
            if record.icao == wanted:
                return record
        for record in self.operators:
            if record.iata and record.iata == wanted:
                return record
        named = normalise(code)
        for record in self.operators:
            if normalise(record.name) == named:
                return record
        return None

    def registration(self, mark: str) -> Registration | None:
        wanted = _mark(mark)
        return next((r for r in self.registrations if r.mark == wanted), None)

    def type(self, designator: str) -> AircraftType | None:
        wanted = _designator(designator)
        return next((t for t in self.types if t.designator.upper() == wanted), None)

    def references_for(self, designator: str) -> tuple[TypeReference, ...]:
        wanted = _designator(designator)
        return tuple(r for r in self.references if r.designator == wanted)

    def coverage(self, designator: str) -> TypeCoverage:
        """What is held about this type: figures, a document, or nothing."""
        if self.type(designator) is not None:
            return TypeCoverage.VERIFIED
        if self.references_for(designator):
            return TypeCoverage.REGISTERED
        return TypeCoverage.ABSENT

    def designators_of(self, code: str) -> tuple[str, ...]:
        """The types an operator holds, resolved through the register."""
        record = self.operator(code)
        if record is None:
            return ()
        found = set()
        for holding in record.holdings:
            registration = self.registration(holding.mark)
            if registration is not None:
                found.add(registration.designator)
        return tuple(sorted(found))

    def operators_of(self, mark: str) -> tuple[OperatorRecord, ...]:
        """Every operator claiming this tail.

        More than one is not a contradiction: a lessor's register entry and a
        lessee's attestation describe the same aeroplane truthfully. It is the
        reader's job to see both, so both are returned.
        """
        wanted = _mark(mark)
        return tuple(
            o for o in self.operators if any(h.mark == wanted for h in o.holdings)
        )

    @property
    def known_designators(self) -> tuple[str, ...]:
        """Every type designator the library mentions anywhere."""
        found = {r.designator for r in self.registrations}
        found |= {r.designator for r in self.references}
        found |= {t.designator.upper() for t in self.types}
        return tuple(sorted(found))

    def coverage_report(self) -> tuple[tuple[str, TypeCoverage], ...]:
        """Every known type and what is held about it, worst first.

        Ordered so the report opens on the research, not on the wins.
        """
        rank = {TypeCoverage.ABSENT: 0, TypeCoverage.REGISTERED: 1, TypeCoverage.VERIFIED: 2}
        rows = [(d, self.coverage(d)) for d in self.known_designators]
        return tuple(sorted(rows, key=lambda row: (rank[row[1]], row[0])))

    def ranked_by_fleet_size(self, limit: int | None = None) -> tuple[OperatorRecord, ...]:
        """Operators ordered by how many tails the library holds for them.

        This is how "the top 50 operators" is answered — counted from held
        records, not from a list of fifty names written into source. The
        ranking is therefore about the library's own coverage, and it moves as
        the library fills, which is the honest behaviour: a ranking baked into
        code would claim a completeness nobody verified.
        """
        ordered = sorted(
            self.operators, key=lambda o: (-o.fleet_size, o.icao)
        )
        return tuple(ordered if limit is None else ordered[:limit])

    def segment(self, segment: Segment) -> tuple[OperatorRecord, ...]:
        return tuple(
            sorted(
                (o for o in self.operators if o.segment is segment),
                key=lambda o: o.icao,
            )
        )

    def __len__(self) -> int:
        return len(self.operators)

    def __iter__(self) -> Iterator[OperatorRecord]:
        return iter(self.operators)


def merge_libraries(*libraries: FleetLibrary) -> FleetLibrary:
    """Combine libraries, each statement keeping the citation it arrived with.

    Operators merge by ICAO designator and their holdings union by mark, so an
    operator's own fleet list and an observation set describe one operator with
    both bases visible. Registrations and aircraft figures merge by identity;
    a later document does not overwrite an earlier one, because "the newest
    file wins" silently discards the citation somebody is relying on.
    """
    if not libraries:
        raise ValueError("merge_libraries needs at least one library")

    operators: dict[str, OperatorRecord] = {}
    for library in libraries:
        for record in library.operators:
            existing = operators.get(record.icao)
            if existing is None:
                operators[record.icao] = record
                continue
            seen = {(h.mark, h.basis) for h in existing.holdings}
            combined = list(existing.holdings)
            by_mark = {h.mark for h in existing.holdings}
            for holding in record.holdings:
                if (holding.mark, holding.basis) in seen:
                    continue
                if holding.mark in by_mark:
                    # Same tail on a different basis. Keep the stronger claim:
                    # an attestation says the operator holds it, an observation
                    # only says it flew.
                    kept = next(h for h in combined if h.mark == holding.mark)
                    if holding.basis.states_operation and not kept.basis.states_operation:
                        combined[combined.index(kept)] = holding
                    continue
                combined.append(holding)
                by_mark.add(holding.mark)
            operators[record.icao] = OperatorRecord(
                icao=existing.icao,
                name=existing.name,
                iata=existing.iata or record.iata,
                segment=existing.segment,
                holdings=tuple(combined),
                bases=tuple(sorted(set(existing.bases) | set(record.bases))),
            )

    registrations: dict[str, Registration] = {}
    for library in libraries:
        for registration in library.registrations:
            registrations.setdefault(registration.mark, registration)

    references: list[TypeReference] = []
    for library in libraries:
        for reference in library.references:
            if reference not in references:
                references.append(reference)

    by_designator: dict[str, list[AircraftType]] = {}
    for library in libraries:
        for aircraft in library.types:
            by_designator.setdefault(aircraft.designator, []).append(aircraft)

    return FleetLibrary(
        operators=tuple(operators[k] for k in sorted(operators)),
        registrations=tuple(registrations[k] for k in sorted(registrations)),
        references=tuple(references),
        types=tuple(merge(*group) for group in by_designator.values()),
    )


# --------------------------------------------------------------------------
# Reading a library document
# --------------------------------------------------------------------------


def _basis(value: object, *, where: str) -> Basis:
    try:
        return Basis(str(value).strip().lower())
    except ValueError:
        raise ManifestError(
            f"{where}: basis must be one of "
            f"{', '.join(b.value for b in Basis)}. It records what kind of "
            "claim the document was making, and there is no safe default: a "
            "register entry read as an attestation puts a lessor in a fleet."
        ) from None


def _segment(value: object, *, where: str) -> Segment:
    try:
        return Segment(str(value).strip().lower())
    except ValueError:
        raise ManifestError(
            f"{where}: segment must be one of "
            f"{', '.join(s.value for s in Segment)}"
        ) from None


def load_library(path: Path | str) -> FleetLibrary:
    """Read one library document, with every statement cited to it.

    One document, one basis, one citation — the same rule
    :func:`aeropub.acap.load_aircraft` keeps for aircraft manifests, and for
    the same reason. A file mixing a national register with an operator's own
    fleet list would emit both cited to whichever document the header named,
    and one of those citations would resolve to a page that does not contain
    the statement. Load them separately and merge.

    ``aircraft`` lists aircraft manifest paths, resolved relative to the
    library, so the figure layer arrives through the existing ACAP path rather
    than through a second, weaker one here.
    """
    path = Path(path)
    manifest = read_manifest(path)
    base = path.parent

    document = document_source(
        manifest.get("source"),
        base=base,
        where=f"{path}: source",
        parser_id=LIBRARY_PARSER_ID,
    )

    has_claims = bool(manifest.get("operators") or manifest.get("registrations"))
    default_basis = None
    if "basis" in manifest:
        default_basis = _basis(manifest.get("basis"), where=f"{path}: basis")
    elif has_claims:
        raise ManifestError(
            f"{path}: basis is required — whether this document is a register, "
            "an operator's own statement, or an observation. The three are "
            "wrong in different ways and the library keeps them apart."
        )

    registrations: list[Registration] = []
    rows = manifest.get("registrations", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: registrations must be a list")
    for index, row in enumerate(rows):
        where = f"{path}: registrations[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        if "basis" in row:
            raise ManifestError(
                f"{where}: declares its own basis. One document makes one kind "
                "of claim, and every record in it is cited to that document — "
                "so a record on a different basis would come out cited to this "
                "one. Put it in its own library file and merge."
            )
        mark = str(row.get("mark", "")).strip()
        designator = str(row.get("designator", "")).strip()
        if not mark:
            raise ManifestError(f"{where}: mark is required")
        if not designator:
            raise ManifestError(
                f"{where}: {mark} has no designator. A tail with no type is not "
                "a smaller record; it identifies nothing that can be checked."
            )
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: {mark} needs a locator — where in the document this "
                "entry was read. Naming the document alone is not a citation a "
                "reviewer can resolve."
            )
        registrations.append(
            Registration(
                mark=mark,
                designator=designator,
                source=sub_source(document, locator),
                basis=default_basis,
                serial=str(row.get("serial", "")).strip(),
                model=str(row.get("model", "")).strip(),
                owner=str(row.get("owner", "")).strip(),
                recorded_on=to_date(
                    row.get("recorded_on"), where=where, field="recorded_on"
                ),
            )
        )

    operators: list[OperatorRecord] = []
    rows = manifest.get("operators", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: operators must be a list")
    for index, row in enumerate(rows):
        where = f"{path}: operators[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        icao = str(row.get("icao", "")).strip()
        if not icao:
            raise ManifestError(
                f"{where}: icao is required — the operator's ICAO designator. "
                "Airline names are not unique and change with rebranding; the "
                "designator is what a flight plan carries."
            )
        name = str(row.get("name", "")).strip()
        if not name:
            raise ManifestError(f"{where}: {icao} needs a name")
        segment = _segment(row.get("segment", Segment.COMMERCIAL.value), where=where)
        listed = row.get("holdings", [])
        if not isinstance(listed, list):
            raise ManifestError(f"{where}: holdings must be a list of tails")
        holdings: list[Holding] = []
        for position, entry in enumerate(listed):
            place = f"{where}: holdings[{position}]"
            if isinstance(entry, str):
                entry = {"mark": entry}
            if not isinstance(entry, Mapping):
                raise ManifestError(f"{place}: must be a tail or an object")
            if "basis" in entry:
                raise ManifestError(
                    f"{place}: declares its own basis. One document makes one "
                    "kind of claim — put a differently sourced holding in its "
                    "own library file and merge."
                )
            held = str(entry.get("mark", "")).strip()
            if not held:
                raise ManifestError(f"{place}: mark is required")
            holdings.append(
                Holding(
                    mark=held,
                    basis=default_basis,
                    source=sub_source(
                        document,
                        str(entry.get("locator", "")).strip() or f"{icao} fleet",
                    ),
                    recorded_on=to_date(
                        entry.get("recorded_on"), where=place, field="recorded_on"
                    ),
                    note=str(entry.get("note", "")),
                )
            )
        operators.append(
            OperatorRecord(
                icao=icao,
                name=name,
                iata=str(row.get("iata", "")).strip(),
                segment=segment,
                holdings=tuple(holdings),
                bases=tuple(str(b) for b in row.get("bases", [])),
            )
        )

    references: list[TypeReference] = []
    rows = manifest.get("references", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: references must be a list")
    for index, row in enumerate(rows):
        where = f"{path}: references[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        try:
            references.append(
                TypeReference(
                    designator=str(row.get("designator", "")),
                    publisher=str(row.get("publisher", "")),
                    document=str(row.get("document", "")),
                    revision=str(row.get("revision", "")).strip(),
                    locator=str(row.get("locator", "")).strip(),
                    url=str(row.get("url", "")).strip(),
                    note=str(row.get("note", "")),
                )
            )
        except ValueError as error:
            raise ManifestError(f"{where}: {error}") from None

    listed = manifest.get("aircraft", [])
    if not isinstance(listed, list):
        raise ManifestError(f"{path}: aircraft must be a list of manifest paths")
    by_designator: dict[str, list[AircraftType]] = {}
    for entry in listed:
        resolved = Path(str(entry))
        if not resolved.is_absolute():
            resolved = base / resolved
        aircraft = load_aircraft(resolved)
        by_designator.setdefault(aircraft.designator, []).append(aircraft)

    return FleetLibrary(
        operators=tuple(operators),
        registrations=tuple(registrations),
        references=tuple(references),
        types=tuple(merge(*group) for group in by_designator.values()),
    )


_LIBRARY_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "basis": "register",
    "operators": [
        {
            "icao": "",
            "iata": "",
            "name": "",
            "segment": "commercial",
            "bases": [],
            "holdings": [{"mark": "", "locator": "", "recorded_on": ""}],
        }
    ],
    "registrations": [
        {
            "mark": "",
            "designator": "",
            "serial": "",
            "model": "",
            "owner": "",
            "recorded_on": "",
            "locator": "",
        }
    ],
    "references": [
        {
            "designator": "",
            "publisher": "",
            "document": "",
            "revision": "",
            "locator": "",
            "url": "",
        }
    ],
    "aircraft": [],
}


def library_template() -> str:
    """A blank library document.

    ``basis`` applies to every operator and registration in the file, so one
    file holds one kind of claim: a national register, an operator's own fleet
    list, or an observation set. ``references`` is the bibliography — which
    document holds a type's figures — and costs nothing to fill in before
    anybody has read it, which is exactly its value. ``aircraft`` takes paths
    to ACAP manifests, and is what turns a registered type into a verified one.
    """
    return json.dumps(_LIBRARY_TEMPLATE, indent=2)


# --------------------------------------------------------------------------
# Using the library
# --------------------------------------------------------------------------


def fleet_of(library: FleetLibrary, code: str) -> OperatorFleet:
    """Resolve one operator into a fleet layer three can assess.

    Every held tail is looked up in the register to get a type, and every type
    is looked up in the figures. Both lookups can fail, and they fail
    differently: a tail with no register entry is an aeroplane we cannot
    identify, and a type with no figures is an aeroplane we can name and cannot
    check. Neither is dropped.
    """
    record = library.operator(code)
    if record is None:
        raise KeyError(
            f"{code!r} is not in this library. That is a coverage gap, not an "
            "operator with no aircraft — the difference matters, so it raises "
            "rather than returning an empty fleet."
        )

    marks_by_designator: dict[str, list[str]] = {}
    unidentified: list[str] = []
    for holding in record.holdings:
        registration = library.registration(holding.mark)
        if registration is None:
            unidentified.append(holding.mark)
            continue
        marks_by_designator.setdefault(registration.designator, []).append(holding.mark)

    described: list[AircraftType] = []
    gaps: list[TypeGap] = []
    for designator in sorted(marks_by_designator):
        aircraft = library.type(designator)
        if aircraft is not None:
            described.append(aircraft)
            continue
        gaps.append(
            TypeGap(
                designator=designator,
                coverage=library.coverage(designator),
                references=library.references_for(designator),
                marks=tuple(sorted(marks_by_designator[designator])),
            )
        )

    return OperatorFleet(
        operator=record,
        fleet=Fleet(tuple(described)),
        gaps=tuple(gaps),
        unidentified=tuple(sorted(unidentified)),
    )


@dataclass(frozen=True, slots=True)
class TypeScreen:
    """One of the operator's types against one aerodrome."""

    designator: str
    suitability: Suitability
    marks: tuple[str, ...] = ()

    @property
    def assessment(self) -> Assessment:
        return self.suitability.overall

    @property
    def is_conclusive(self) -> bool:
        return self.suitability.is_conclusive


@dataclass(frozen=True, slots=True)
class FleetScreen:
    """Which of an operator's types can use one aerodrome.

    The question a planner actually asks before a trip exists, and the reason
    the library is worth building: it is answerable for a whole fleet the
    moment the fleet is known, instead of one aeroplane at a time.
    """

    aerodrome: str
    operator: OperatorRecord
    screened: tuple[TypeScreen, ...] = ()
    gaps: tuple[TypeGap, ...] = ()
    unidentified: tuple[str, ...] = ()

    def by_assessment(self, assessment: Assessment) -> tuple[TypeScreen, ...]:
        return tuple(s for s in self.screened if s.assessment is assessment)

    @property
    def suitable(self) -> tuple[str, ...]:
        return tuple(
            s.designator for s in self.by_assessment(Assessment.SUITABLE)
        )

    @property
    def not_suitable(self) -> tuple[str, ...]:
        return tuple(
            s.designator for s in self.by_assessment(Assessment.NOT_SUITABLE)
        )

    @property
    def restricted(self) -> tuple[str, ...]:
        return tuple(
            s.designator for s in self.by_assessment(Assessment.RESTRICTED)
        )

    @property
    def unchecked(self) -> tuple[str, ...]:
        """Types the screen could not conclude on, and types it never saw.

        The two are combined deliberately. To a planner asking "can I send
        something here", a type whose wingspan is unknown and a type whose
        manual nobody has read are the same answer: not yet.
        """
        unknown = [s.designator for s in self.by_assessment(Assessment.UNKNOWN)]
        return tuple(sorted(set(unknown) | {g.designator for g in self.gaps}))

    @property
    def is_complete(self) -> bool:
        """Whether every type the operator holds got a conclusive answer."""
        return (
            bool(self.screened)
            and not self.gaps
            and not self.unidentified
            and all(s.is_conclusive for s in self.screened)
        )


    def render(self) -> str:
        """Which of this operator's types can use this aerodrome."""
        lines = [
            f"FLEET SCREEN — {self.operator.name} ({self.operator.icao}) "
            f"at {self.aerodrome}",
            f"{_count(len(self.screened), 'type')} screened  ·  "
            f"{len(self.suitable)} suitable  ·  "
            f"{len(self.restricted)} restricted  ·  "
            f"{len(self.not_suitable)} not suitable  ·  "
            f"{len(self.unchecked)} unchecked"
            + ("" if self.is_complete else "  ·  NOT COMPLETE"),
            "",
        ]
        if not self.screened and not self.gaps:
            lines.append(
                "Nothing to screen. This operator holds no type the library "
                "can describe, which is a coverage gap and not a clear result."
            )
            return "\n".join(lines)

        for entry in self.screened:
            marks = f"  ({_count(len(entry.marks), 'tail')})" if entry.marks else ""
            flag = "" if entry.is_conclusive else "  · not conclusive"
            lines.append(
                f"  {entry.designator:<6} "
                f"{entry.assessment.value.upper():<13}{marks}{flag}"
            )
            for check in entry.suitability.blocking:
                lines.append(f"      {check.name}: {check.detail}")
        for gap in self.gaps:
            lines.append(
                f"  {gap.designator:<6} {'NOT SCREENED':<13}  "
                f"({gap.coverage.value} — no figures to screen with)"
            )
        if self.unidentified:
            lines += [
                "",
                f"{_count(len(self.unidentified), 'tail')} could not be "
                "identified and went unscreened: "
                + ", ".join(self.unidentified),
            ]
        if self.unchecked:
            lines += [
                "",
                "Unchecked is not a pass. "
                + ", ".join(self.unchecked)
                + " reached no conclusion here,",
                "either because a figure is missing or because nobody has read "
                "the type's manual.",
            ]
        return "\n".join(lines)


def screen(
    library: FleetLibrary,
    code: str,
    dossier: AerodromeDossier,
    *,
    designators: Iterable[str] | None = None,
) -> FleetScreen:
    """Screen an operator's whole fleet against one aerodrome.

    ``designators`` narrows the screen to particular types — the sub-fleet a
    planner is actually considering. Narrowing does not hide the gaps for the
    types it excluded; those are only excluded from the screen, and the fleet's
    own coverage is reported by :func:`fleet_of`.
    """
    resolved = fleet_of(library, code)
    wanted = (
        None if designators is None else {_designator(d) for d in designators}
    )

    marks_by_designator: dict[str, list[str]] = {}
    for holding in resolved.operator.holdings:
        registration = library.registration(holding.mark)
        if registration is not None:
            marks_by_designator.setdefault(registration.designator, []).append(
                holding.mark
            )

    screened = tuple(
        TypeScreen(
            designator=aircraft.designator,
            suitability=assess_suitability(dossier, aircraft),
            marks=tuple(sorted(marks_by_designator.get(aircraft.designator.upper(), ()))),
        )
        for aircraft in resolved.fleet
        if wanted is None or aircraft.designator.upper() in wanted
    )
    gaps = tuple(
        gap for gap in resolved.gaps if wanted is None or gap.designator in wanted
    )
    return FleetScreen(
        aerodrome=dossier.aerodrome,
        operator=resolved.operator,
        screened=screened,
        gaps=gaps,
        unidentified=resolved.unidentified,
    )


def route_profile(
    library: FleetLibrary,
    code: str,
    *,
    departure: str,
    destination: str,
    alternates: Iterable[str] = (),
    takeoff_alternate: str = "",
    enroute_alternates: Iterable[str] = (),
    designators: Iterable[str] | None = None,
) -> OperatorProfile:
    """Build a layer-three profile for one city pair.

    This is the shape a business or private operator needs, and increasingly
    the shape an airline planner wants for a one-off: they have a fleet and a
    pairing, not a standing network. The fleet comes from the library and the
    network is exactly the aerodromes this sector uses, each at the role it
    actually serves.

    The departure aerodrome enters as a destination role rather than as an
    en-route one. It is a planned landing on the return, its pavement and fire
    category matter on the day, and giving it the en-route role — where
    pavement and RFFS deliberately do not count — would quietly drop the checks
    that keep an aeroplane out of trouble at the field it is sitting on.

    A single alternate is marked ``sole_suitable``. With one named alternate
    there is by definition nothing to swap to, so the condition is derived here
    rather than waiting for the operator to notice; where they name several,
    the judgement goes back to them, because only they know what their region,
    approvals and handling actually allow.
    """
    resolved = fleet_of(library, code)
    fleet = resolved.fleet
    if designators is not None:
        wanted = {_designator(d) for d in designators}
        fleet = Fleet(tuple(t for t in fleet if t.designator.upper() in wanted))

    listed = [normalise(a) for a in alternates if str(a).strip()]
    sole = len(set(listed)) == 1

    entries = [
        NetworkEntry(aerodrome=departure, role=Role.DESTINATION, group="departure"),
        NetworkEntry(aerodrome=destination, role=Role.DESTINATION, group="destination"),
    ]
    entries += [
        NetworkEntry(
            aerodrome=where,
            role=Role.ALTERNATE,
            sole_suitable=sole,
            group=f"{normalise(destination)} alternates",
        )
        for where in listed
    ]
    if str(takeoff_alternate).strip():
        entries.append(
            NetworkEntry(
                aerodrome=takeoff_alternate,
                role=Role.TAKEOFF_ALTERNATE,
                sole_suitable=True,
                group=f"{normalise(departure)} take-off alternates",
            )
        )
    listed_enroute = [normalise(a) for a in enroute_alternates if str(a).strip()]
    entries += [
        NetworkEntry(
            aerodrome=where,
            role=Role.EDTO_ALTERNATE,
            sole_suitable=len(set(listed_enroute)) == 1,
            group=f"{normalise(departure)}-{normalise(destination)} en-route alternates",
        )
        for where in listed_enroute
    ]

    return OperatorProfile(
        name=resolved.operator.name,
        fleet=fleet,
        network=Network(tuple(entries)),
    )
