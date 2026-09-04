"""Aircraft characteristics, and what an aerodrome can take.

Where the figures come from, and where they do not
--------------------------------------------------
**FCOM and FPPM do not ship with this product, and never will.** They are the
manufacturer's proprietary documentation, licensed to the operator who bought
the aeroplane. An operator has every right to their own copy and to compute
against it; a platform has no right to redistribute it. Plan decision D settled
this: certified performance computation stays with the operator's own tool, and
what crosses into AeroPub is data the operator supplies under their own licence,
marked :attr:`Origin.OPERATOR`, and kept to their tenant.

**ACAP is the public equivalent, and it covers most of what aerodrome work
needs.** Boeing, Airbus, Embraer and COMAC publish *Airplane Characteristics
for Airport Planning* freely, to the NAS 3601 specification: dimensions, wheel
spans, turning radii, ground service arrangements, and the ACN pavement tables.
That is enough to answer whether an aeroplane fits an aerodrome, which is the
question this module exists for. It is not enough to answer what it can lift
off a wet runway at 42 °C, and this module does not pretend otherwise.

**No figures are shipped here.** :class:`Characteristic` cannot be constructed
without a :class:`~aeropub.provenance.SourceRef`, exactly as :class:`Fact`
cannot. An aircraft library begins empty and fills from documents that were
actually read. A wingspan recalled from memory is the failure the whole project
is built against, and it is worse here than elsewhere: a wrong wingspan by one
metre moves an aeroplane across a code letter boundary and changes which
taxiways it may use.

What *is* encoded is the standard
---------------------------------
The aerodrome reference code is ICAO Annex 14 Volume I, Table 1-1 — a published
rule, encoded here as the AIRAC calendar encodes the 28-day cycle and
:mod:`aeropub.aip` encodes the AIP index. The rule that catches people is in
1.6.3: where wingspan and outer main gear wheel span give different letters,
**the more demanding one applies**. Code D and Code E share the same wheel-span
band, so wingspan alone separates them.

The pavement comparison has a similar trap. A PCN is not a number; it is a
number *and* a pavement type *and* a subgrade category *and* a tyre pressure
limit. An ACN quoted against a rigid pavement on subgrade B says nothing about
a flexible pavement on subgrade C, and comparing the bare numbers across them
is how an aeroplane ends up on a pavement that will not carry it.

.. warning::
   The tables here follow ICAO Annex 14 Volume I. Confirm them against the
   current edition before anything operational depends on them: this is a
   standard read into code, not a document that was fetched and archived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from aeropub.provenance import SourceRef

__all__ = [
    "CODE_LETTERS",
    "CODE_NUMBERS",
    "AircraftType",
    "Characteristic",
    "CodeLetterBand",
    "Origin",
    "RFFS_CATEGORIES",
    "RffsCategory",
    "PavementCheck",
    "PavementVerdict",
    "RatingSystem",
    "TYRE_PRESSURE_MPA",
    "accommodates",
    "code_letter",
    "code_number",
    "compare_pavement",
    "reference_code",
    "rffs_category",
]


class Origin(str, Enum):
    """Where a characteristic came from, and what may be done with it."""

    ACAP = "acap"
    """The manufacturer's published Airplane Characteristics for Airport
    Planning document. Public, citable, and shippable."""

    OPERATOR = "operator"
    """Supplied by the operator from their own licensed documentation — FCOM,
    FPPM, or their performance tool. Stays with that tenant and is never
    redistributed."""

    STATE = "state"
    """Published by a State, usually in an AIP supplement about a specific
    type's operation at a specific aerodrome."""

    @property
    def is_redistributable(self) -> bool:
        return self is not Origin.OPERATOR


@dataclass(frozen=True, slots=True)
class Characteristic:
    """One published figure about one aircraft type.

    Cannot be constructed without a citation, for the same reason a
    :class:`~aeropub.facts.Fact` cannot: a wingspan with no source behind it is
    indistinguishable from one somebody remembered, and one metre of error
    moves an aeroplane across a code letter boundary.
    """

    attribute: str
    value: Any
    source: SourceRef
    origin: Origin
    unit: str | None = None
    variant: str | None = None
    """Which configuration this figure is for — "with winglets", "high gross
    weight", "777-300ER". A characteristic without its variant is a figure
    that is right for some aeroplanes with this designator and wrong for
    others."""

    def __post_init__(self) -> None:
        if not self.attribute.strip():
            raise ValueError("Characteristic.attribute must be a non-empty string")
        if self.value is None:
            raise ValueError(
                f"{self.attribute}: a characteristic with no value is a gap in what "
                "we hold, not a characteristic. Leave it out."
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError(
                f"{self.attribute}: a characteristic without provenance cannot exist. "
                "A figure with no source behind it is indistinguishable from one "
                "somebody remembered."
            )

    def describe(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        variant = f" [{self.variant}]" if self.variant else ""
        return f"{self.attribute} = {self.value}{unit}{variant} ({self.origin.value})"


# --------------------------------------------------------------------------
# ICAO Annex 14 Volume I, Table 1-1
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeLetterBand:
    """One row of the code letter table. Upper bounds are exclusive."""

    letter: str
    wingspan_from_m: float
    wingspan_to_m: float | None
    omgws_from_m: float
    omgws_to_m: float | None

    def admits_wingspan(self, metres: float) -> bool:
        return metres >= self.wingspan_from_m and (
            self.wingspan_to_m is None or metres < self.wingspan_to_m
        )

    def admits_omgws(self, metres: float) -> bool:
        return metres >= self.omgws_from_m and (
            self.omgws_to_m is None or metres < self.omgws_to_m
        )


#: Annex 14 Volume I, Table 1-1, code element 2. Note that Code D and Code E
#: share the same outer main gear wheel span band — wingspan alone separates
#: them, which is why the classifier must consider both criteria and take the
#: more demanding result rather than reading one column.
CODE_LETTERS: tuple[CodeLetterBand, ...] = (
    CodeLetterBand("A", 0.0, 15.0, 0.0, 4.5),
    CodeLetterBand("B", 15.0, 24.0, 4.5, 6.0),
    CodeLetterBand("C", 24.0, 36.0, 6.0, 9.0),
    CodeLetterBand("D", 36.0, 52.0, 9.0, 14.0),
    CodeLetterBand("E", 52.0, 65.0, 9.0, 14.0),
    CodeLetterBand("F", 65.0, 80.0, 14.0, 16.0),
)

#: Annex 14 Volume I, Table 1-1, code element 1 — aeroplane reference field
#: length, being the minimum field length for take-off at maximum certificated
#: take-off mass, at sea level in ISA, still air, zero slope.
CODE_NUMBERS: tuple[tuple[int, float, float | None], ...] = (
    (1, 0.0, 800.0),
    (2, 800.0, 1200.0),
    (3, 1200.0, 1800.0),
    (4, 1800.0, None),
)

_ORDER = {band.letter: index for index, band in enumerate(CODE_LETTERS)}


def code_letter(
    *, wingspan_m: float | None = None, omgws_m: float | None = None
) -> str | None:
    """The Annex 14 code letter, or ``None`` where neither figure is known.

    The table is read one column at a time — the first row a figure falls in is
    that criterion's letter — and then, per Annex 14 1.6.3, **the more demanding
    of the two letters applies**. Reading the wingspan column alone is the
    common mistake and it under-reports: an aeroplane with a Code C wingspan and
    a Code D wheel span needs Code D taxiway and pavement geometry.

    The order of those two steps matters, and getting it backwards is subtle
    enough to be worth stating. Code D and Code E share the 9-14 m wheel span
    band, so a wheel span of 10 m admits both rows. Taking the most demanding
    letter across *all* admitting rows at once would call a 34 m span aeroplane
    Code E — a letter its wingspan has already ruled out. Each criterion gets
    one vote, and the shared band votes D.

    Where only the wheel span is held and it falls in that shared band, D is the
    answer this can give: the wingspan is what separates D from E, and without
    it the distinction is not available. That is a floor, not a certainty.

    A figure that falls outside the table entirely — a span wider than Code F —
    yields ``None`` even when the other criterion is in range. An aeroplane
    larger than the code has no letter, and reporting the other column's letter
    would describe an aeroplane that does not exist.
    """
    letters = []
    for metres, admits in (
        (wingspan_m, "admits_wingspan"),
        (omgws_m, "admits_omgws"),
    ):
        if metres is None:
            continue
        row = next((b for b in CODE_LETTERS if getattr(b, admits)(metres)), None)
        if row is None:
            return None
        letters.append(row.letter)
    if not letters:
        return None
    return max(letters, key=lambda letter: _ORDER[letter])


def code_number(reference_field_length_m: float) -> int:
    """The Annex 14 code number for an aeroplane reference field length."""
    if reference_field_length_m < 0:
        raise ValueError("a reference field length cannot be negative")
    for number, lower, upper in CODE_NUMBERS:
        if reference_field_length_m >= lower and (upper is None or reference_field_length_m < upper):
            return number
    return 4  # pragma: no cover - the last band is open-ended


def reference_code(
    *,
    wingspan_m: float | None = None,
    omgws_m: float | None = None,
    reference_field_length_m: float | None = None,
) -> str | None:
    """The full reference code, e.g. ``"4E"``, where both halves are known."""
    letter = code_letter(wingspan_m=wingspan_m, omgws_m=omgws_m)
    if letter is None or reference_field_length_m is None:
        return None
    return f"{code_number(reference_field_length_m)}{letter}"


def accommodates(aerodrome_letter: str, aircraft_letter: str) -> bool:
    """Whether an aerodrome of this code letter takes an aircraft of that one."""
    try:
        return _ORDER[aerodrome_letter.strip().upper()] >= _ORDER[aircraft_letter.strip().upper()]
    except KeyError:
        raise ValueError(
            f"code letters are A to F; got {aerodrome_letter!r} and {aircraft_letter!r}"
        ) from None


# --------------------------------------------------------------------------
# Rescue and fire fighting — Annex 14 Volume I, Table 9-1
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RffsCategory:
    """One row of Table 9-1. The length band is closed below, open above."""

    category: int
    length_from_m: float
    length_to_m: float
    max_fuselage_width_m: float


#: Annex 14 Volume I, Table 9-1 — the aerodrome category an aeroplane requires.
#: Two dimensions, read in order: overall length selects the row, and then 9.2.2
#: applies. This is the aerodrome category the *aeroplane* needs, which is not
#: the same question as what the aerodrome provides; AD 2.6 answers that, and a
#: State may also reduce the published category during periods of lower traffic.
RFFS_CATEGORIES: tuple[RffsCategory, ...] = (
    RffsCategory(1, 0.0, 9.0, 2.0),
    RffsCategory(2, 9.0, 12.0, 2.0),
    RffsCategory(3, 12.0, 18.0, 3.0),
    RffsCategory(4, 18.0, 24.0, 4.0),
    RffsCategory(5, 24.0, 28.0, 4.0),
    RffsCategory(6, 28.0, 39.0, 5.0),
    RffsCategory(7, 39.0, 49.0, 5.0),
    RffsCategory(8, 49.0, 61.0, 7.0),
    RffsCategory(9, 61.0, 76.0, 7.0),
    RffsCategory(10, 76.0, 90.0, 8.0),
)


def rffs_category(
    *, overall_length_m: float | None, fuselage_width_m: float | None = None
) -> int | None:
    """The aerodrome fire category this aeroplane requires, per Table 9-1.

    Annex 14 9.2.2 is explicit about the order, and it is the half people
    forget: **overall length selects the category, and then, if the fuselage
    width exceeds the maximum in column 3 for that category, the category is
    one higher.** Length alone under-reports for wide aeroplanes — the case the
    rule exists for is a fuselage that is short for its girth.

    Without the fuselage width this returns the length-only category and cannot
    know whether the bump applies. That is a floor, not an answer, and callers
    must present it as one: an aerodrome that meets the floor may still be one
    category short.

    Returns ``None`` where the length is not held or falls outside the table.
    Annex 14 stops at 90 m; beyond it the State determines the category, and
    inventing a row would be a confident answer about an aeroplane the standard
    does not cover.
    """
    if overall_length_m is None:
        return None
    row = next(
        (
            r
            for r in RFFS_CATEGORIES
            if overall_length_m >= r.length_from_m and overall_length_m < r.length_to_m
        ),
        None,
    )
    if row is None:
        return None
    if fuselage_width_m is not None and fuselage_width_m > row.max_fuselage_width_m:
        # 9.2.2. Table 9-1 stops at category 10, so an aeroplane already in the
        # last row has nowhere to be promoted to; the State determines what it
        # needs, and reporting 10 is the most the table can say.
        return min(row.category + 1, RFFS_CATEGORIES[-1].category)
    return row.category



# --------------------------------------------------------------------------
# Pavement
# --------------------------------------------------------------------------

_RATING = re.compile(
    r"^\s*(?:PCN|PCR)?\s*(?P<number>\d+(?:\.\d+)?)\s*/\s*(?P<pavement>[RF])\s*/\s*"
    r"(?P<subgrade>[ABCD])\s*/\s*(?P<tyre>[WXYZ]|\d+(?:\.\d+)?)\s*/\s*"
    r"(?P<method>[TU])\s*$",
    re.IGNORECASE,
)


class RatingSystem(str, Enum):
    """Which pavement classification system a rating belongs to.

    ICAO replaced ACN/PCN with ACR/PCR in Annex 14 Volume I, mandatory from
    **28 November 2024** after a four-year transition. States are converting at
    different rates, so both will be published somewhere for years — which is
    the whole reason this enum exists rather than an assumption.
    """

    ACN_PCN = "acn_pcn"
    """The legacy method. Withdrawn from the standard, still published by many
    States and still on many charts."""

    ACR_PCR = "acr_pcr"
    """The current method. A more accurate model of the load each aeroplane
    puts on a pavement — and on a completely different numerical scale."""

    @property
    def pavement_label(self) -> str:
        return "PCN" if self is RatingSystem.ACN_PCN else "PCR"

    @property
    def aircraft_label(self) -> str:
        return "ACN" if self is RatingSystem.ACN_PCN else "ACR"


#: Tyre pressure category limits, in MPa, ``None`` meaning no limit. The
#: categories kept their letters across the change and moved their limits,
#: which is exactly the kind of silent difference that makes reading a rating
#: without knowing its system dangerous.
TYRE_PRESSURE_MPA: dict[RatingSystem, dict[str, float | None]] = {
    RatingSystem.ACN_PCN: {"W": None, "X": 1.50, "Y": 1.00, "Z": 0.50},
    RatingSystem.ACR_PCR: {"W": None, "X": 1.75, "Y": 1.25, "Z": 0.50},
}


@dataclass(frozen=True, slots=True)
class PavementRating:
    """A reported pavement rating, in all five of its parts and its system.

    A rating is not a number. ``80/F/A/W/T`` says the pavement rates 80 **for a
    flexible pavement on a high-strength subgrade with no tyre pressure limit,
    determined technically**. Comparing an aeroplane's figure for a rigid
    pavement on subgrade C against that number compares two different things.

    The sixth part is the one that does not appear in the string at all.
    ACN/PCN and ACR/PCR share the identical five-part format and share nothing
    else: a real PCR runs in the hundreds where the PCN for the same pavement
    runs in the tens. Reading ``560/F/B/W/T`` as a PCN, or comparing an ACR to
    a PCN, produces a confident answer that is wrong by an order of magnitude
    and wrong in the permissive direction. So the system is required at parse
    time and is never guessed from the number: a State that has converted says
    PCR in AD 2.12, and that is where it is read from.
    """

    number: float
    pavement: str
    """``R`` rigid or ``F`` flexible."""

    subgrade: str
    """``A`` high, ``B`` medium, ``C`` low, ``D`` ultra low."""

    tyre_pressure: str
    """``W`` unlimited, or ``X``/``Y``/``Z`` — whose MPa limits differ between
    the two systems — or a figure, where the State reports one."""

    method: str
    """``T`` technical evaluation, ``U`` using aircraft experience."""

    system: RatingSystem

    @classmethod
    def parse(cls, text: str, *, system: RatingSystem) -> "PavementRating":
        """Read a five-part rating, in the system the State publishes it in."""
        if not isinstance(system, RatingSystem):
            raise TypeError(
                "the rating system is required and is never inferred: ACN/PCN "
                "and ACR/PCR share a format and nothing else, and guessing "
                "from the number is wrong in the permissive direction."
            )
        match = _RATING.match(str(text))
        if match is None:
            raise ValueError(
                f"{text!r} is not a {system.pavement_label}. The reported form "
                "is number/pavement(R|F)/subgrade(A-D)/tyre(W|X|Y|Z or a "
                "figure)/method(T|U), for example 80/F/A/W/T."
            )
        return cls(
            number=float(match.group("number")),
            pavement=match.group("pavement").upper(),
            subgrade=match.group("subgrade").upper(),
            tyre_pressure=match.group("tyre").upper(),
            method=match.group("method").upper(),
            system=system,
        )

    @classmethod
    def pcn(cls, text: str) -> "PavementRating":
        """A rating published under the legacy ACN/PCN method."""
        return cls.parse(text, system=RatingSystem.ACN_PCN)

    @classmethod
    def pcr(cls, text: str) -> "PavementRating":
        """A rating published under the current ACR/PCR method."""
        return cls.parse(text, system=RatingSystem.ACR_PCR)

    def __str__(self) -> str:
        number = f"{self.number:g}"
        return (
            f"{number}/{self.pavement}/{self.subgrade}/"
            f"{self.tyre_pressure}/{self.method}"
        )

    def describe(self) -> str:
        """The rating with its system named, for anywhere a reader sees it."""
        return f"{self.system.pavement_label} {self}"

    @property
    def label(self) -> str:
        return self.system.pavement_label

    @property
    def is_technical(self) -> bool:
        """Whether the rating was determined technically rather than by experience.

        A ``U`` rating is what the pavement has been seen to carry, not what it
        was calculated to carry, and it carries less weight when the margin is
        thin."""
        return self.method == "T"

    @property
    def tyre_pressure_limit_mpa(self) -> float | None:
        """The MPa limit for this category, or ``None`` where unlimited.

        Returns ``None`` for a category letter only. Where the State reported a
        figure instead of a letter, that figure is the limit.
        """
        if self.tyre_pressure in TYRE_PRESSURE_MPA[self.system]:
            return TYRE_PRESSURE_MPA[self.system][self.tyre_pressure]
        return float(self.tyre_pressure)


class PavementVerdict(str, Enum):
    """What an aircraft rating against a pavement rating means."""

    WITHIN = "within"
    """The aircraft rating does not exceed the pavement's. Unrestricted
    operation."""

    OVERLOAD = "overload"
    """It exceeds. Not a prohibition — Annex 14 provides for overload
    operations — but it needs the aerodrome's own procedures and its consent,
    and it is never a dispatcher's decision alone."""

    NOT_COMPARABLE = "not_comparable"
    """The two describe different pavements, different subgrades, or different
    classification systems. Comparing the numbers would be meaningless, and is
    how an aeroplane ends up on a pavement that will not carry it."""

    UNKNOWN = "unknown"
    """One side is missing."""

    @property
    def permits_operation(self) -> bool:
        return self is PavementVerdict.WITHIN


@dataclass(frozen=True, slots=True)
class PavementCheck:
    """One aircraft-rating-against-pavement-rating comparison, and why."""

    verdict: PavementVerdict
    acn: float | None
    pcn: PavementRating | None
    detail: str

    @property
    def rating(self) -> PavementRating | None:
        """The pavement side. ``pcn`` is the older name and still works."""
        return self.pcn

    def describe(self) -> str:
        return f"[{self.verdict.value}] {self.detail}"


def compare_pavement(
    *,
    acn: float | None,
    acn_pavement: str | None = None,
    acn_subgrade: str | None = None,
    acn_system: RatingSystem | None = None,
    pcn: PavementRating | None,
) -> PavementCheck:
    """Compare an aeroplane's rating against a pavement's.

    The comparison is only valid when the aircraft figure was quoted in the
    same **system**, for the same **pavement type**, and on the same
    **subgrade** the pavement reports. Where any of the three differ this
    returns :attr:`PavementVerdict.NOT_COMPARABLE` rather than a number,
    because the alternative — comparing them anyway — produces a confident
    answer about the wrong pavement.

    The system check is the newest and the most dangerous to omit. During the
    ACN/PCN to ACR/PCR changeover an operator holds ACAP tables in both, and a
    State may publish one while a chart still carries the other. An ACR of 700
    against a PCN of 80 is not an overload by 620; it is two numbers that mean
    nothing to each other.
    """
    if acn is None or pcn is None:
        missing = "aircraft rating" if acn is None else "pavement rating"
        return PavementCheck(
            verdict=PavementVerdict.UNKNOWN, acn=acn, pcn=pcn,
            detail=f"no {missing} held; pavement suitability is unknown, not assumed",
        )

    if acn_system is not None and acn_system is not pcn.system:
        return PavementCheck(
            verdict=PavementVerdict.NOT_COMPARABLE, acn=acn, pcn=pcn,
            detail=(
                f"the aircraft figure is an {acn_system.aircraft_label} and the "
                f"aerodrome reports a {pcn.label}. These are different "
                "classification systems on different numerical scales — ICAO "
                "replaced ACN/PCN with ACR/PCR from 28 November 2024 and States "
                f"are converting at different rates. Read the "
                f"{pcn.system.aircraft_label} from the ACAP table; the numbers "
                "are not convertible."
            ),
        )
    if acn_pavement is not None and acn_pavement.upper() != pcn.pavement:
        return PavementCheck(
            verdict=PavementVerdict.NOT_COMPARABLE, acn=acn, pcn=pcn,
            detail=(
                f"the aircraft rating is quoted for a {acn_pavement.upper()} "
                f"pavement and the aerodrome reports {pcn.pavement}. Take the "
                f"{pcn.system.aircraft_label} for the reported pavement type from "
                "the ACAP table rather than comparing these."
            ),
        )
    if acn_subgrade is not None and acn_subgrade.upper() != pcn.subgrade:
        return PavementCheck(
            verdict=PavementVerdict.NOT_COMPARABLE, acn=acn, pcn=pcn,
            detail=(
                f"the aircraft rating is quoted on subgrade "
                f"{acn_subgrade.upper()} and the aerodrome reports subgrade "
                f"{pcn.subgrade}. Read the {pcn.system.aircraft_label} for the "
                "reported subgrade; the numbers are not interchangeable."
            ),
        )

    if acn <= pcn.number:
        return PavementCheck(
            verdict=PavementVerdict.WITHIN, acn=acn, pcn=pcn,
            detail=(
                f"{pcn.system.aircraft_label} {acn:g} does not exceed "
                f"{pcn.label} {pcn.number:g} ({pcn.pavement}/{pcn.subgrade}). "
                "Unrestricted operation."
            ),
        )
    margin = acn - pcn.number
    experience = "" if pcn.is_technical else (
        " The rating is by aircraft experience rather than technical evaluation, "
        "which carries less weight when the margin is thin."
    )
    return PavementCheck(
        verdict=PavementVerdict.OVERLOAD, acn=acn, pcn=pcn,
        detail=(
            f"{pcn.system.aircraft_label} {acn:g} exceeds {pcn.label} "
            f"{pcn.number:g} by {margin:g} ({pcn.pavement}/{pcn.subgrade}). "
            "Annex 14 provides for overload operations, but they need the "
            "aerodrome's own procedures and its consent — not a dispatch "
            f"decision alone.{experience}"
        ),
    )


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AircraftType:
    """One aircraft type, and every cited figure held about it.

    Begins empty. Characteristics arrive from documents that were read, each
    carrying the citation that produced it.
    """

    designator: str
    """ICAO type designator, e.g. ``"B77W"``. Not a marketing name: two
    marketing names can share a designator and one marketing name can span
    several."""

    manufacturer: str = ""
    model: str = ""
    characteristics: tuple[Characteristic, ...] = ()

    def __post_init__(self) -> None:
        if not self.designator.strip():
            raise ValueError("AircraftType.designator must be a non-empty string")

    def with_characteristics(self, items: Iterable[Characteristic]) -> "AircraftType":
        return AircraftType(
            designator=self.designator, manufacturer=self.manufacturer,
            model=self.model, characteristics=self.characteristics + tuple(items),
        )

    def get(self, attribute: str, *, variant: str | None = None) -> Characteristic | None:
        """The figure held for this attribute, or ``None``.

        ``None`` is a coverage gap and must be rendered as one. There is no
        default wingspan.
        """
        for item in self.characteristics:
            if item.attribute != attribute:
                continue
            if variant is not None and item.variant != variant:
                continue
            return item
        return None

    def value(self, attribute: str, *, variant: str | None = None) -> Any | None:
        found = self.get(attribute, variant=variant)
        return found.value if found else None

    @property
    def redistributable(self) -> tuple[Characteristic, ...]:
        """Everything that may leave this tenant. Operator data may not."""
        return tuple(c for c in self.characteristics if c.origin.is_redistributable)

    def code_letter(self, *, variant: str | None = None) -> str | None:
        """The Annex 14 code letter, from held figures only."""
        return code_letter(
            wingspan_m=self.value("wingspan_m", variant=variant),
            omgws_m=self.value("omgws_m", variant=variant),
        )

    def reference_code(self, *, variant: str | None = None) -> str | None:
        return reference_code(
            wingspan_m=self.value("wingspan_m", variant=variant),
            omgws_m=self.value("omgws_m", variant=variant),
            reference_field_length_m=self.value(
                "reference_field_length_m", variant=variant
            ),
        )

    def describe(self) -> str:
        name = " ".join(p for p in (self.manufacturer, self.model) if p)
        letter = self.code_letter()
        code = f", Code {letter}" if letter else ", code letter unknown"
        return f"{self.designator}{' — ' + name if name else ''}{code}"
