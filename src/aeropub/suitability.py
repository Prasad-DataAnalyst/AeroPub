"""Does this aeroplane fit this aerodrome — and what did we not check?

The second question is the one that makes this module worth having. Anyone can
compare two numbers. What an operator needs is an answer that says, in the same
breath, which comparisons rested on published data, which rested on nothing,
and what a NOTAM might already have overtaken.

Scope, stated plainly
---------------------
This is **fit**, not **performance**. It answers whether an aeroplane's
geometry and pavement loading are compatible with what the State publishes
about an aerodrome, from ICAO Annex 14 Volume I:

- Table 1-1, the aerodrome reference code — :mod:`aeropub.aircraft`
- Table 3-1, minimum runway width for that code
- Table 9-1, the rescue and fire fighting category the aeroplane requires
- the ACN/PCN pavement method

It does **not** compute take-off or landing performance, and it never will.
That is certified computation against the manufacturer's own documentation, it
belongs in the operator's own tool, and plan decision D settled it. Declared
distances are reported here as published figures beside the aeroplane's
reference field length, which is a sea-level ISA still-air number and is not a
performance answer for any actual day.

Nothing here is a dispatch decision, and the rendered output says so.

Three rules the assessment keeps
--------------------------------
**Unknown never becomes suitable.** A check with nothing behind it is
:attr:`Assessment.UNKNOWN`, it is listed, and it makes :attr:`Suitability.is_conclusive`
false. The alternative — quietly dropping checks we could not make and printing
"suitable" from the ones we could — is the failure this whole project exists to
avoid, and it is more dangerous here than anywhere else in the platform because
the output looks like a clearance.

**Every verdict carries both sides of its evidence.** The aerodrome value with
its ``SourceRef``, and the aircraft characteristic with its own. A reader can
resolve any line to the document it came from, on both sides.

**NOTAM are surfaced, not interpreted.** These checks run on the AIP's
published values. A NOTAM that has closed the runway or downgraded the fire
category is exactly what would invalidate them, and this module does not read
NOTAM text into values — so it reports how many are in force against what each
check rested on, and says the checks do not account for them. A confident
"suitable" computed over a closed runway is the artefact this guard exists to
prevent.

Layer two, still
----------------
No fleet, no tenant, no severity. One aeroplane type against one aerodrome is
an operator-agnostic question with an operator-agnostic answer: the same pair
gives the same result for every airline. What a *particular* operator does
about a `RESTRICTED` result — accept it, seek the aerodrome's consent, plan an
alternate — is layer three.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from aeropub.aircraft import (
    AircraftType,
    Characteristic,
    Pcn,
    PavementVerdict,
    accommodates,
    code_letter,
    code_number,
    compare_pavement,
    rffs_category,
)
from aeropub.dossier import AerodromeDossier, ValueLine
from aeropub.entities import covers, scope_of
from aeropub.notam_register import ForceState, RegisteredNotam

__all__ = [
    "RUNWAY_WIDTH_M",
    "Assessment",
    "Check",
    "Note",
    "Suitability",
    "assess_suitability",
    "minimum_runway_width_m",
]


class Assessment(str, Enum):
    """What one check concluded."""

    SUITABLE = "suitable"
    """The comparison was made on held, cited data and it passed."""

    RESTRICTED = "restricted"
    """It passed, but with a condition the operator must observe — an overload
    needing the aerodrome's consent, a runway narrower than the design standard
    for this code. Never a quiet pass."""

    NOT_SUITABLE = "not_suitable"
    """The comparison was made on held, cited data and it failed."""

    UNKNOWN = "unknown"
    """One side was not held, so no comparison was possible. Not a pass. An
    unknown check may be hiding a failure, and it is reported as loudly as
    one."""

    @property
    def is_conclusive(self) -> bool:
        return self is not Assessment.UNKNOWN


#: How the assessments rank when several are rolled into one. A definite
#: failure dominates; an unknown outranks any pass, because the check that was
#: not made is the one that could still turn out to be the failure.
_RANK = {
    Assessment.NOT_SUITABLE: 3,
    Assessment.UNKNOWN: 2,
    Assessment.RESTRICTED: 1,
    Assessment.SUITABLE: 0,
}

#: Annex 14 Volume I, Table 3-1 — the minimum runway width for a reference
#: code. Combinations absent from the mapping do not appear in the table: no
#: code 4 runway is built to Code A or B geometry, and no code 1 or 2 runway to
#: Code D, E or F.
#:
#: This is a **design** standard for what a runway of that code is built to. An
#: existing narrower runway is not thereby illegal — States approve them, and
#: the aerodrome's own published width is the operational figure. A shortfall
#: against this table is a condition to understand, not a prohibition, and the
#: assessment grades it :attr:`Assessment.RESTRICTED` for exactly that reason.
RUNWAY_WIDTH_M: dict[tuple[int, str], float] = {
    (1, "A"): 18.0, (1, "B"): 18.0, (1, "C"): 23.0,
    (2, "A"): 23.0, (2, "B"): 23.0, (2, "C"): 30.0,
    (3, "A"): 30.0, (3, "B"): 30.0, (3, "C"): 30.0, (3, "D"): 45.0,
    (4, "C"): 45.0, (4, "D"): 45.0, (4, "E"): 45.0, (4, "F"): 60.0,
}


def minimum_runway_width_m(number: int, letter: str) -> float | None:
    """Table 3-1, or ``None`` for a code combination the table does not carry."""
    return RUNWAY_WIDTH_M.get((number, letter.strip().upper()))


@dataclass(frozen=True, slots=True)
class Check:
    """One comparison, its verdict, and both sides of the evidence behind it."""

    name: str
    assessment: Assessment
    detail: str
    scope: str = "aerodrome"
    """The aerodrome as a whole, or the runway this check is about."""

    section: str = ""
    """The AIP section the aerodrome side came from, where there is one."""

    aerodrome_basis: tuple[ValueLine, ...] = ()
    aircraft_basis: tuple[Characteristic, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.assessment.is_conclusive

    @property
    def blocks(self) -> bool:
        return self.assessment is Assessment.NOT_SUITABLE

    def describe(self) -> str:
        where = f" · {self.scope}" if self.scope != "aerodrome" else ""
        section = f" [{self.section}]" if self.section else ""
        return f"[{self.assessment.value}] {self.name}{where}{section} — {self.detail}"

    def citations(self) -> tuple[str, ...]:
        """Every source behind this check, aerodrome side then aircraft side."""
        return tuple(v.fact.source.describe() for v in self.aerodrome_basis) + tuple(
            c.source.describe() for c in self.aircraft_basis
        )


@dataclass(frozen=True, slots=True)
class Note:
    """Something reported alongside the checks, from which nothing is concluded.

    Deliberately not a :class:`Check`, and deliberately carrying no
    :class:`Assessment`. A declared distance beside a reference field length is
    worth putting in front of a reader and is not a comparison anybody should
    draw a verdict from. Filing it as an ``UNKNOWN`` check would be worse than
    omitting it: it would make every assessment permanently inconclusive, and
    an inconclusive flag that is always on tells a reader nothing.
    """

    name: str
    detail: str
    scope: str = "aerodrome"
    section: str = ""
    aerodrome_basis: tuple[ValueLine, ...] = ()
    aircraft_basis: tuple[Characteristic, ...] = ()

    def describe(self) -> str:
        where = f" · {self.scope}" if self.scope != "aerodrome" else ""
        section = f" [{self.section}]" if self.section else ""
        return f"{self.name}{where}{section} — {self.detail}"

    def citations(self) -> tuple[str, ...]:
        return tuple(v.fact.source.describe() for v in self.aerodrome_basis) + tuple(
            c.source.describe() for c in self.aircraft_basis
        )


@dataclass(frozen=True, slots=True)
class Suitability:
    """One aeroplane against one aerodrome, with everything it could not check."""

    aerodrome: str
    designator: str
    as_at: datetime
    checks: tuple[Check, ...] = ()
    notes: tuple[Note, ...] = ()
    """Reported alongside. Never enters ``overall`` or ``is_conclusive``."""

    notams: tuple[tuple[RegisteredNotam, ForceState], ...] = ()
    """NOTAM in force against this aerodrome. Counted, not interpreted."""

    @property
    def overall(self) -> Assessment:
        """The least favourable assessment across every check.

        With no checks at all this is ``UNKNOWN``, never ``SUITABLE``. An empty
        assessment is the absence of evidence and must not read as a pass.
        """
        if not self.checks:
            return Assessment.UNKNOWN
        return max((c.assessment for c in self.checks), key=lambda a: _RANK[a])

    @property
    def is_conclusive(self) -> bool:
        """Whether this assessment can be acted on as it stands.

        False for three reasons, and they are different failures:

        - the assessment is empty, so there is nothing behind the verdict;
        - some check could not be made, so the verdict does not cover it;
        - a NOTAM in force overlays evidence a check rested on, so the verdict
          may be computed from a value the State has already overtaken.

        The third is the one that bites. A worked example that assessed a
        runway as *restricted* on its published pavement rating, while a NOTAM
        had closed that same runway, produced a verdict which was true about
        the AIP and useless about the day. ``overall`` alone is not enough to
        act on: "suitable on four checks with three unmade" and "suitable on
        seven" must not print the same, and neither must "suitable" with a
        closure in force over it.
        """
        return (
            bool(self.checks)
            and all(c.is_known for c in self.checks)
            and not self.overtaken
        )

    @property
    def overtaken(self) -> tuple[Check, ...]:
        """Checks resting on evidence a NOTAM in force may have overtaken.

        Containment runs one way, as everywhere else in the platform: a NOTAM
        filed against the aerodrome reaches its runways, and a NOTAM against
        one runway does not reach the aerodrome's fire category. This does not
        read the NOTAM — it says which checks a reader must go and read it for,
        which is the honest limit of a module that indexes NOTAM structurally
        rather than parsing their text into values.
        """
        affected = {
            entity
            for notam, _ in self.operative_notams
            for entity in notam.entities
        }
        if not affected:
            return ()
        return tuple(
            check
            for check in self.checks
            if any(
                covers(notam_entity, line.entity)
                for line in check.aerodrome_basis
                for notam_entity in affected
            )
        )

    @property
    def unknown(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.is_known)

    @property
    def blocking(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.blocks)

    @property
    def restricted(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.assessment is Assessment.RESTRICTED)

    @property
    def operative_notams(self) -> tuple[tuple[RegisteredNotam, ForceState], ...]:
        return tuple((n, s) for n, s in self.notams if s.is_operative)

    def render(self) -> str:
        """A printable assessment. Unknown checks print as loudly as failures."""
        lines = [
            f"AERODROME SUITABILITY — {self.designator} at {self.aerodrome}",
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}",
            "",
            f"Overall: {self.overall.value.upper().replace('_', ' ')}"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE"),
            "",
        ]
        if self.overtaken:
            lines += [
                f"!! {len(self.overtaken)} of these checks rest on values a NOTAM "
                "in force may have overtaken.",
                "   They are computed from the AIP. Read the NOTAM below before "
                "acting on any of them:",
                "   "
                + ", ".join(
                    sorted({f"{c.name} ({c.scope})" for c in self.overtaken})
                ),
                "",
            ]
        lines += [
            "A fit assessment against published aerodrome data.",
            "It is not a performance calculation and not a dispatch decision.",
            "",
            "Checks",
        ]
        for check in self.checks:
            lines.append(f"  {check.describe()}")
            for citation in check.citations():
                lines.append(f"      {citation}")

        if self.notes:
            lines += [
                "",
                "Reported alongside — no verdict is drawn from these",
            ]
            for note in self.notes:
                lines.append(f"  {note.describe()}")
                for citation in note.citations():
                    lines.append(f"      {citation}")

        if self.unknown:
            lines += [
                "",
                "NOT CHECKED — these comparisons could not be made, and nothing "
                "above should be read as covering them",
            ]
            lines += [f"  {c.name} — {c.detail}" for c in self.unknown]

        operative = self.operative_notams
        lines += ["", "NOTAM"]
        if not operative:
            lines.append(
                "  none in force against this aerodrome at this moment, in what "
                "we hold — which is not the same as none published"
            )
        else:
            lines.append(
                f"  {len(operative)} in force against this aerodrome. The checks "
                "above are computed from AIP values and do not account for them."
            )
            for notam, state in operative:
                mark = "" if state is ForceState.IN_FORCE else f"  [{state.value}]"
                lines.append(f"    {notam.identifier}{mark}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Assembling the assessment
# --------------------------------------------------------------------------


def _lines(dossier: AerodromeDossier, attribute: str) -> tuple[ValueLine, ...]:
    return tuple(v for v in dossier.values() if v.attribute == attribute)


def _runway_lines(dossier: AerodromeDossier, attribute: str) -> dict[str, ValueLine]:
    """Values of this attribute filed against a runway, keyed by that runway."""
    found: dict[str, ValueLine] = {}
    for line in _lines(dossier, attribute):
        scope = scope_of(line.entity)
        if scope:
            found[scope] = line
    return found


def _acn_for(aircraft: AircraftType, pcn: Pcn) -> Characteristic | None:
    """The aeroplane's ACN for the pavement and subgrade this PCN reports.

    ACAP publishes ACN as a table across pavement type, subgrade category and
    weight, so a held ACN is only meaningful with the cell it came from. The
    convention here is a characteristic named ``acn`` whose ``variant`` begins
    with the pavement and subgrade — ``"F/A"``, ``"R/B at MTOW"`` — which is
    the cell address of the ACAP table it was read from.

    Where several match, the highest wins. The variants that differ beyond the
    cell address differ by weight, and the heaviest is the one a suitability
    check must answer for.
    """
    prefix = f"{pcn.pavement}/{pcn.subgrade}".upper()
    matching = [
        c
        for c in aircraft.characteristics
        if c.attribute == "acn"
        and c.variant is not None
        and c.variant.strip().upper().startswith(prefix)
    ]
    if not matching:
        return None
    return max(matching, key=lambda c: float(c.value))


def _reference_code_check(
    dossier: AerodromeDossier, aircraft: AircraftType
) -> Check:
    span = aircraft.get("wingspan_m")
    omgws = aircraft.get("omgws_m")
    basis = tuple(c for c in (span, omgws) if c is not None)
    letter = code_letter(
        wingspan_m=span.value if span else None,
        omgws_m=omgws.value if omgws else None,
    )
    declared = _lines(dossier, "aerodrome_reference_code")
    if letter is None or not declared:
        missing = []
        if letter is None:
            missing.append(
                "the aeroplane's wingspan and outer main gear wheel span"
                if not basis
                else "a span that falls inside Annex 14 Table 1-1"
            )
        if not declared:
            missing.append("the aerodrome's declared reference code")
        return Check(
            name="Aerodrome reference code",
            assessment=Assessment.UNKNOWN,
            detail="not held: " + "; and ".join(missing),
            aerodrome_basis=declared,
            aircraft_basis=basis,
        )

    line = declared[0]
    aerodrome_letter = str(line.value).strip().upper()[-1:]
    try:
        fits = accommodates(aerodrome_letter, letter)
    except ValueError:
        return Check(
            name="Aerodrome reference code",
            assessment=Assessment.UNKNOWN,
            detail=(
                f"the declared code {line.value!r} does not end in a code letter "
                "A to F, so it cannot be compared. Recorded as published rather "
                "than corrected."
            ),
            aerodrome_basis=declared,
            aircraft_basis=basis,
        )
    return Check(
        name="Aerodrome reference code",
        assessment=Assessment.SUITABLE if fits else Assessment.NOT_SUITABLE,
        detail=(
            f"the aeroplane is Code {letter}; the aerodrome declares "
            f"{line.value}"
            + (
                "."
                if fits
                else f", which is built to Code {aerodrome_letter} geometry. "
                "Taxiway widths, clearances and shoulder provision are those of "
                "the smaller code."
            )
        ),
        aerodrome_basis=declared,
        aircraft_basis=basis,
    )


def _pavement_checks(
    dossier: AerodromeDossier, aircraft: AircraftType
) -> list[Check]:
    reported = _runway_lines(dossier, "pcn")
    if not reported:
        return [
            Check(
                name="Pavement strength",
                assessment=Assessment.UNKNOWN,
                detail=(
                    "no PCN held for any runway at this aerodrome. Pavement "
                    "suitability is unknown, not assumed."
                ),
                section="AD 2.12",
            )
        ]

    checks: list[Check] = []
    for runway, line in sorted(reported.items()):
        try:
            pcn = Pcn.parse(str(line.value))
        except ValueError as error:
            checks.append(
                Check(
                    name="Pavement strength",
                    assessment=Assessment.UNKNOWN,
                    detail=(
                        f"the published rating {line.value!r} could not be read: "
                        f"{error}"
                    ),
                    scope=runway,
                    section="AD 2.12",
                    aerodrome_basis=(line,),
                )
            )
            continue

        acn = _acn_for(aircraft, pcn)
        if acn is None:
            checks.append(
                Check(
                    name="Pavement strength",
                    assessment=Assessment.UNKNOWN,
                    detail=(
                        f"the aerodrome reports {pcn}, and no ACN is held for a "
                        f"{pcn.pavement} pavement on subgrade {pcn.subgrade}. The "
                        "figure for a different cell of the ACAP table is not a "
                        "substitute."
                    ),
                    scope=runway,
                    section="AD 2.12",
                    aerodrome_basis=(line,),
                )
            )
            continue

        result = compare_pavement(
            acn=float(acn.value),
            acn_pavement=pcn.pavement,
            acn_subgrade=pcn.subgrade,
            pcn=pcn,
        )
        assessment = {
            PavementVerdict.WITHIN: Assessment.SUITABLE,
            PavementVerdict.OVERLOAD: Assessment.RESTRICTED,
            PavementVerdict.NOT_COMPARABLE: Assessment.UNKNOWN,
            PavementVerdict.UNKNOWN: Assessment.UNKNOWN,
        }[result.verdict]
        checks.append(
            Check(
                name="Pavement strength",
                assessment=assessment,
                detail=result.detail,
                scope=runway,
                section="AD 2.12",
                aerodrome_basis=(line,),
                aircraft_basis=(acn,),
            )
        )
    return checks


def _width_checks(dossier: AerodromeDossier, aircraft: AircraftType) -> list[Check]:
    letter = aircraft.code_letter()
    length = aircraft.get("reference_field_length_m")
    widths = _runway_lines(dossier, "runway_width_m")
    basis = tuple(
        c
        for c in (
            aircraft.get("wingspan_m"),
            aircraft.get("omgws_m"),
            length,
        )
        if c is not None
    )
    if letter is None or length is None or not widths:
        missing = []
        if letter is None or length is None:
            missing.append("the aeroplane's reference code")
        if not widths:
            missing.append("a published runway width")
        return [
            Check(
                name="Runway width",
                assessment=Assessment.UNKNOWN,
                detail="not held: " + "; and ".join(missing),
                section="AD 2.12",
                aircraft_basis=basis,
            )
        ]

    number = code_number(float(length.value))
    required = minimum_runway_width_m(number, letter)
    if required is None:
        return [
            Check(
                name="Runway width",
                assessment=Assessment.UNKNOWN,
                detail=(
                    f"Annex 14 Table 3-1 carries no width for Code {number}{letter}; "
                    "the combination does not occur in the table."
                ),
                section="AD 2.12",
                aircraft_basis=basis,
            )
        ]

    checks: list[Check] = []
    for runway, line in sorted(widths.items()):
        held = float(line.value)
        meets = held >= required
        checks.append(
            Check(
                name="Runway width",
                assessment=Assessment.SUITABLE if meets else Assessment.RESTRICTED,
                detail=(
                    f"{held:g} m published; Annex 14 Table 3-1 builds a Code "
                    f"{number}{letter} runway to {required:g} m"
                    + (
                        "."
                        if meets
                        else ". Table 3-1 is a design standard and a narrower "
                        "runway is not thereby prohibited — the State approved "
                        "it — but the margin an aeroplane of this code was "
                        "assumed to have is not there."
                    )
                ),
                scope=runway,
                section="AD 2.12",
                aerodrome_basis=(line,),
                aircraft_basis=basis,
            )
        )
    return checks


def _rffs_check(dossier: AerodromeDossier, aircraft: AircraftType) -> Check:
    length = aircraft.get("overall_length_m")
    width = aircraft.get("fuselage_width_m")
    basis = tuple(c for c in (length, width) if c is not None)
    published = _lines(dossier, "rffs_category")
    required = rffs_category(
        overall_length_m=length.value if length else None,
        fuselage_width_m=width.value if width else None,
    )
    if required is None or not published:
        missing = []
        if required is None:
            missing.append(
                "the aeroplane's overall length"
                if length is None
                else "an overall length inside Annex 14 Table 9-1"
            )
        if not published:
            missing.append("the aerodrome's published RFFS category")
        return Check(
            name="Rescue and fire fighting",
            assessment=Assessment.UNKNOWN,
            detail="not held: " + "; and ".join(missing),
            section="AD 2.6",
            aerodrome_basis=published,
            aircraft_basis=basis,
        )

    line = published[0]
    try:
        provided = int(str(line.value).strip().lstrip("Cc").strip())
    except ValueError:
        return Check(
            name="Rescue and fire fighting",
            assessment=Assessment.UNKNOWN,
            detail=(
                f"the published category {line.value!r} is not a number and was "
                "not interpreted."
            ),
            section="AD 2.6",
            aerodrome_basis=published,
            aircraft_basis=basis,
        )

    # Without the fuselage width, Table 9-1 gives a floor rather than an
    # answer: 9.2.2 can still push the requirement one category higher, and an
    # aerodrome that just meets the floor would then be one short.
    provisional = width is None
    if provided >= required:
        detail = (
            f"the aeroplane requires Category {required}; the aerodrome publishes "
            f"Category {provided}."
        )
        if provisional:
            detail += (
                " No fuselage width is held, so Annex 14 9.2.2 could not be "
                "applied: the requirement is a floor and may be one category "
                "higher."
            )
        return Check(
            name="Rescue and fire fighting",
            assessment=Assessment.RESTRICTED if provisional else Assessment.SUITABLE,
            detail=detail,
            section="AD 2.6",
            aerodrome_basis=published,
            aircraft_basis=basis,
        )
    return Check(
        name="Rescue and fire fighting",
        assessment=Assessment.NOT_SUITABLE,
        detail=(
            f"the aeroplane requires Category {required}; the aerodrome publishes "
            f"Category {provided}. A State may permit operation at a lower "
            "category under its own remission provisions, which is the State's "
            "decision and is published, not inferred here."
        ),
        section="AD 2.6",
        aerodrome_basis=published,
        aircraft_basis=basis,
    )


def _declared_distance_notes(
    dossier: AerodromeDossier, aircraft: AircraftType
) -> list[Note]:
    """Declared distances reported beside the reference field length.

    Deliberately a :class:`Note` and never a verdict. The reference field
    length is take-off at maximum certificated mass, at sea level, in ISA,
    still air, on zero slope — an aeroplane classification figure, not a
    performance answer for any actual day. A runway longer than it is not
    thereby adequate, and a runway shorter than it is not thereby unusable.
    Reporting the two side by side is useful; concluding anything from the
    comparison is not.
    """
    length = aircraft.get("reference_field_length_m")
    tora = _runway_lines(dossier, "tora_m")
    if length is None or not tora:
        return []
    return [
        Note(
            name="Declared distances",
            detail=(
                f"TORA {float(line.value):g} m published; the aeroplane's "
                f"reference field length is {float(length.value):g} m. Reported "
                "side by side, and nothing is concluded from them: a reference "
                "field length is a sea-level ISA still-air classification "
                "figure, and take-off performance for an actual day is computed "
                "by the operator's own tool."
            ),
            scope=runway,
            section="AD 2.13",
            aerodrome_basis=(line,),
            aircraft_basis=(length,),
        )
        for runway, line in sorted(tora.items())
    ]


def assess_suitability(
    dossier: AerodromeDossier,
    aircraft: AircraftType,
    *,
    include_declared_distances: bool = True,
) -> Suitability:
    """Assess one aeroplane against one aerodrome dossier.

    Every check runs, including the ones that cannot be made — an assessment
    that silently omitted them would print a shorter, cleaner and far more
    dangerous document.
    """
    checks: list[Check] = [_reference_code_check(dossier, aircraft)]
    checks += _pavement_checks(dossier, aircraft)
    checks += _width_checks(dossier, aircraft)
    checks.append(_rffs_check(dossier, aircraft))
    notes = _declared_distance_notes(dossier, aircraft) if include_declared_distances else []
    return Suitability(
        aerodrome=dossier.aerodrome,
        designator=aircraft.designator,
        as_at=dossier.as_at,
        checks=tuple(checks),
        notes=tuple(notes),
        notams=dossier.notams,
    )
