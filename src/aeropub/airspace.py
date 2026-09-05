"""ENR 2 — the airspace you fly *in*, and where it changes under you.

ENR 2 is the ATS airspace structure: flight information regions and upper
regions, terminal control areas, control areas, control zones, aerodrome
traffic zones. It is not a list of things to avoid — that is ENR 5, and
:mod:`aeropub.hazards` holds it. This is the airspace a flight is *inside* for
every minute of the sector, and what it publishes decides three things a
planner cannot get anywhere else:

**What service you get.** The class settles whether ATC separates you from
other IFR traffic, whether VFR traffic can be there at all, and whether you
need a clearance to enter. Class A and Class G are the same sky and opposite
answers.

**Who to call, and on what.** A boundary is a handover. The unit and frequency
on each side are published here, and a sector crossing six regions crosses six
handovers.

**What you must carry.** A radio mandatory zone, a transponder mandatory zone,
an RVSM band — these are carriage requirements attached to the airspace, not to
the route, and an aeroplane that does not meet one cannot be there whatever its
flight plan says.

The finding is the boundary, not the table
-------------------------------------------
The same insight the altimetry section rests on. A crew given the class of six
consecutive regions has to work out where each one starts;
:class:`ClassTransition` gives them the three places it actually changes. A
class that stays the same across a boundary is not news; a Class C terminal
area beneath a Class A upper region is.

What this does not claim
------------------------
The same limit as everywhere else in the en-route work: **no geometry**. This
platform does not hold the lateral boundary of a terminal area and cannot say
whether a track enters one. What it says is which airspace the crossed regions
publish, which of it the planned level reaches, and what each one demands — a
screening list a planner works from, and never a containment verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import named, normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)

__all__ = [
    "AIRSPACE",
    "Airspace",
    "AirspaceClass",
    "AirspaceStructure",
    "AirspaceType",
    "AirspaceView",
    "CarriageRequirement",
    "ClassTransition",
    "UNLIMITED",
    "airspace_template",
    "load_airspace",
    "read_limit",
    "view_airspace",
]

#: The parser identity written into citations read from an ENR 2 manifest.
AIRSPACE_PARSER_ID = "aeropub.airspace"

#: The entity kind an airspace volume is keyed under. Free-standing: airspace
#: belongs to no aerodrome, and rolling a terminal area up under one would
#: attach an airspace restriction to a runway.
AIRSPACE = "AIRSPACE"

#: What an upper limit of ``UNL`` means as a number, so a comparison against a
#: real level resolves the way the publication intends.
UNLIMITED = float("inf")


class AirspaceType(str, Enum):
    """The ENR 2 volumes, in the order they nest."""

    FIR = "fir"
    """Flight information region. The outermost container, and the unit of
    State responsibility — every other volume here sits inside one."""

    UIR = "uir"
    """Upper flight information region. A separate volume above a stated
    level, often with a different unit and a different class from the FIR
    beneath it, which is exactly the boundary a level change crosses."""

    CTA = "cta"
    """Control area. Controlled airspace from a stated level upwards."""

    TMA = "tma"
    """Terminal control area — the control area around one or more busy
    aerodromes, and usually the busiest class change on a sector."""

    CTR = "ctr"
    """Control zone. Controlled airspace from the surface upwards."""

    ATZ = "atz"
    """Aerodrome traffic zone."""

    FIZ = "fiz"
    """Flight information zone — information rather than control."""

    OCA = "oca"
    """Oceanic control area. Procedural rather than radar, which changes the
    separation and therefore the planning."""

    OTHER = "other"
    """Regulated airspace of a kind ENR 2.2 names and this list does not.
    Kept rather than forced into a neighbour: a volume filed under the wrong
    type answers the wrong question about clearances."""

    @property
    def is_region(self) -> bool:
        """Whether this is a region a route is described as crossing."""
        return self in (AirspaceType.FIR, AirspaceType.UIR, AirspaceType.OCA)

    @property
    def is_terminal(self) -> bool:
        return self in (AirspaceType.TMA, AirspaceType.CTR, AirspaceType.ATZ)


class AirspaceClass(str, Enum):
    """ICAO Annex 11 airspace classification.

    The letters are not a severity scale. They are seven different answers to
    three separate questions — is a clearance required, is IFR separated from
    IFR, is VFR permitted — and a planner needs the answers, not the letter.
    """

    A = "a"
    B = "b"
    C = "c"
    D = "d"
    E = "e"
    F = "f"
    G = "g"
    UNCLASSIFIED = "unclassified"
    """Published without a class, or published as a class this list does not
    carry. Reported as unclassified rather than defaulted: guessing G
    understates the clearance requirement and guessing A overstates it, and
    both are wrong in ways somebody acts on."""

    @property
    def ifr_clearance_required(self) -> bool | None:
        """Whether an IFR flight needs an ATC clearance to be here.

        ``None`` for unclassified, because the honest answer is that nobody
        read it. Classes A to E are controlled airspace and an IFR flight is
        cleared into all of them; F is advisory and G is uncontrolled.
        """
        if self is AirspaceClass.UNCLASSIFIED:
            return None
        return self in (
            AirspaceClass.A,
            AirspaceClass.B,
            AirspaceClass.C,
            AirspaceClass.D,
            AirspaceClass.E,
        )

    @property
    def ifr_separated_from_ifr(self) -> bool | None:
        """Whether ATC separates IFR traffic from other IFR traffic here.

        True through class E; advisory in F, where separation is *offered* and
        not guaranteed; nothing in G. The distinction between F and E is the
        one crews most often lose, and it is the difference between a service
        and a promise.
        """
        if self is AirspaceClass.UNCLASSIFIED:
            return None
        return self in (
            AirspaceClass.A,
            AirspaceClass.B,
            AirspaceClass.C,
            AirspaceClass.D,
            AirspaceClass.E,
        )

    @property
    def vfr_permitted(self) -> bool | None:
        """Whether VFR flight may be here at all. Only class A forbids it."""
        if self is AirspaceClass.UNCLASSIFIED:
            return None
        return self is not AirspaceClass.A

    def describe(self) -> str:
        if self is AirspaceClass.UNCLASSIFIED:
            return "class not held"
        return f"class {self.value.upper()}"


class CarriageRequirement(str, Enum):
    """Something the airspace requires the aeroplane to have.

    Attached to the airspace rather than the route, which is why it belongs
    here: an aeroplane that does not meet one cannot be there whatever its
    flight plan says, and no amount of route checking finds it.
    """

    RMZ = "rmz"
    """Radio mandatory zone — continuous listening watch and a report."""

    TMZ = "tmz"
    """Transponder mandatory zone."""

    RVSM = "rvsm"
    """Reduced vertical separation minimum airspace. Approval, not just
    equipment."""

    ADS_B = "ads_b"
    ADS_C = "ads_c"
    CPDLC = "cpdlc"
    PBN = "pbn"
    """A navigation specification the airspace requires. The specification
    itself is published per segment in ENR 3; this records that the volume
    demands one."""


@dataclass(frozen=True, slots=True)
class Airspace:
    """One ENR 2 volume, as the State publishes it."""

    designator: str
    kind: AirspaceType
    source: SourceRef
    name: str = ""
    airspace_class: AirspaceClass = AirspaceClass.UNCLASSIFIED
    region: str = ""
    """The flight information region this sits in. Empty on an FIR itself,
    which is its own region — and that is how a route crossing a region
    surfaces the terminal areas published inside it."""

    lower_ft: float | None = None
    upper_ft: float | None = None
    unit: str = ""
    """The ATS unit providing the service. On a boundary this is the handover,
    and it is the single most useful field here."""

    frequency_mhz: float | None = None
    hours: str = ""
    requirements: tuple[CarriageRequirement, ...] = ()
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "designator", normalise(self.designator))
        object.__setattr__(self, "region", normalise(self.region))
        if not self.designator:
            raise ValueError("Airspace.designator must be a non-empty string")
        if not isinstance(self.kind, AirspaceType):
            raise TypeError("Airspace.kind must be an AirspaceType")
        if not isinstance(self.airspace_class, AirspaceClass):
            raise TypeError("Airspace.airspace_class must be an AirspaceClass")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Airspace.source must be a SourceRef")
        if (
            self.lower_ft is not None
            and self.upper_ft is not None
            and self.lower_ft > self.upper_ft
        ):
            raise ValueError(
                f"{self.designator}: lower limit {self.lower_ft} is above "
                f"upper limit {self.upper_ft}"
            )

    @property
    def key(self) -> str:
        return named(AIRSPACE, self.designator)

    @property
    def is_unlimited_upper(self) -> bool:
        """Whether the upper limit is UNL rather than a number.

        Asked because JSON has no infinity: a payload that emitted one would
        be unparseable, and emitting a very large number would be a limit
        nobody published.
        """
        return self.upper_ft == UNLIMITED

    @property
    def belongs_to(self) -> str:
        """The region this volume is inside. An FIR is its own."""
        return self.region or (self.designator if self.kind.is_region else "")

    def reaches(self, level_ft: float) -> bool | None:
        """Whether the published vertical limits could contain that level.

        **Elimination by altitude only. Lateral position is untested and this
        platform holds nothing to test it with.** True means "not ruled out",
        never "you are inside it".

        ``None`` where the limits are not held: a volume whose extent nobody
        has read cannot be eliminated, and reporting it as out of the way is
        the one false negative that matters.
        """
        if self.lower_ft is None and self.upper_ft is None:
            return None
        if self.lower_ft is not None and level_ft < self.lower_ft:
            return False
        if self.upper_ft is not None and level_ft > self.upper_ft:
            return False
        return True

    def describe(self) -> str:
        head = f"{self.designator} {self.kind.value.upper()}"
        parts = [head]
        if self.name:
            parts.append(self.name)
        parts.append(self.airspace_class.describe())
        limits = describe_limits(self.lower_ft, self.upper_ft)
        if limits:
            parts.append(limits)
        if self.unit:
            parts.append(self.unit)
        if self.frequency_mhz is not None:
            parts.append(f"{self.frequency_mhz:.3f}")
        if self.requirements:
            parts.append(
                "/".join(r.value.upper().replace("_", "-") for r in self.requirements)
            )
        return "  ·  ".join(parts)


def describe_limits(lower: float | None, upper: float | None) -> str:
    """Vertical limits the way a publication prints them."""
    if lower is None and upper is None:
        return "limits not held"
    low = (
        "SFC"
        if lower in (0, 0.0)
        else (f"{lower:.0f} ft" if lower is not None else "?")
    )
    if upper is None:
        high = "?"
    elif upper == UNLIMITED:
        high = "UNL"
    else:
        high = f"{upper:.0f} ft"
    return f"{low} to {high}"


def read_limit(value: object, *, where: str = "", field: str = "limit") -> float | None:
    """Read a vertical limit the way an AIP prints one.

    ``SFC`` and ``GND`` are the surface, ``UNL`` is unlimited, ``FL195`` is a
    flight level, a bare number is feet. Anything else is refused rather than
    guessed: a guessed limit can rule a volume out of a screen, and the reader
    would never know it had been guessed.

    Shared with :mod:`aeropub.hazards`, which reads the same forms out of
    ENR 5. One implementation, because two would drift and only one of them
    would be the one somebody tested.
    """
    if value is None or value == "":
        return None
    text = "".join(str(value).split()).upper()
    if text in ("SFC", "GND", "SURFACE"):
        return 0.0
    if text in ("UNL", "UNLIMITED"):
        return UNLIMITED
    if text.startswith("FL"):
        try:
            return float(text[2:]) * 100.0
        except ValueError:
            raise ManifestError(
                f"{where}: {field} {value!r} is not a readable flight level"
            ) from None
    try:
        return float(text.removesuffix("FT").removesuffix("AMSL"))
    except ValueError:
        raise ManifestError(
            f"{where}: {field} {value!r} could not be read. A limit that "
            "cannot be parsed is left unread rather than guessed — a guessed "
            "limit rules a volume out of a screen and nobody sees it happen."
        ) from None


# --------------------------------------------------------------------------
# The structure, and the view along a route
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AirspaceStructure:
    """Every ENR 2 volume read so far."""

    volumes: tuple[Airspace, ...] = ()

    def __len__(self) -> int:
        return len(self.volumes)

    def __iter__(self):
        return iter(self.volumes)

    @property
    def regions(self) -> tuple[str, ...]:
        """Every region the structure can say anything about."""
        return tuple(sorted({v.belongs_to for v in self.volumes if v.belongs_to}))

    def volume(self, designator: str) -> Airspace | None:
        wanted = normalise(designator)
        return next((v for v in self.volumes if v.designator == wanted), None)

    def in_region(self, region: str) -> tuple[Airspace, ...]:
        wanted = normalise(region)
        return tuple(v for v in self.volumes if v.belongs_to == wanted)

    def of_type(self, kind: AirspaceType) -> tuple[Airspace, ...]:
        return tuple(v for v in self.volumes if v.kind is kind)


@dataclass(frozen=True, slots=True)
class ClassTransition:
    """Where the class of airspace changes between two regions crossed.

    The finding, in the same way an altimetry boundary is the finding. A crew
    given six regions' classes has to work out where each starts; this gives
    them the places it actually changes.
    """

    leaving: str
    entering: str
    from_class: AirspaceClass
    to_class: AirspaceClass
    from_unit: str = ""
    to_unit: str = ""
    from_reason: str = ""
    to_reason: str = ""
    """Why a side is unclassified, where it is. Two completely different
    gaps hide behind one word: nobody read the region, or the region's own
    volume does not reach the planned level — which means the flight is in
    something above it that nobody has read either. Naming which is the
    difference between "go and read ENR 2" and "you are looking at the wrong
    volume"."""

    @property
    def is_known(self) -> bool:
        """Whether both sides were read.

        A boundary with one side unclassified is not a boundary where nothing
        changes. It is one nobody can speak for.
        """
        return (
            self.from_class is not AirspaceClass.UNCLASSIFIED
            and self.to_class is not AirspaceClass.UNCLASSIFIED
        )

    @property
    def loses_separation(self) -> bool:
        """Whether IFR separation stops being provided across this boundary.

        The transition that matters most, and the one a table of letters
        hides: entering airspace where ATC no longer separates you from other
        IFR traffic changes what the crew is responsible for.
        """
        before = self.from_class.ifr_separated_from_ifr
        after = self.to_class.ifr_separated_from_ifr
        return before is True and after is False

    def describe(self) -> str:
        boundary = f"{self.leaving} → {self.entering}"
        if not self.is_known:
            missing = [
                f"{name} ({reason})" if reason else name
                for name, klass, reason in (
                    (self.leaving, self.from_class, self.from_reason),
                    (self.entering, self.to_class, self.to_reason),
                )
                if klass is AirspaceClass.UNCLASSIFIED
            ]
            return f"{boundary}: class not held for {', '.join(missing)}"
        text = (
            f"{boundary}: {self.from_class.describe()} → "
            f"{self.to_class.describe()}"
        )
        if self.from_unit or self.to_unit:
            text += f"  ·  {self.from_unit or '?'} → {self.to_unit or '?'}"
        if self.loses_separation:
            text += "  ·  IFR separation no longer provided"
        return text


@dataclass(frozen=True, slots=True)
class AirspaceView:
    """The airspace a sector is inside, as far as it has been read."""

    regions: tuple[str, ...] = ()
    planned_ft: float | None = None
    volumes: tuple[Airspace, ...] = ()
    """Volumes in the crossed regions that altitude does not rule out."""

    eliminated: tuple[Airspace, ...] = ()
    unbounded: tuple[Airspace, ...] = ()
    """Vertical limits not held, so nothing could be ruled out. Our gap, not
    the airspace's, and kept apart for that reason."""

    unread_regions: tuple[str, ...] = ()
    transitions: tuple[ClassTransition, ...] = ()

    @property
    def units(self) -> tuple[str, ...]:
        """Every ATS unit that will be spoken to, in order of crossing."""
        found: list[str] = []
        for volume in self.volumes:
            if volume.unit and volume.unit not in found:
                found.append(volume.unit)
        return tuple(found)

    @property
    def requirements(self) -> tuple[CarriageRequirement, ...]:
        """Everything the crossed airspace requires the aeroplane to carry."""
        found: list[CarriageRequirement] = []
        for volume in self.volumes:
            for requirement in volume.requirements:
                if requirement not in found:
                    found.append(requirement)
        return tuple(found)

    @property
    def changes(self) -> tuple[ClassTransition, ...]:
        return tuple(
            t for t in self.transitions if t.is_known and t.from_class is not t.to_class
        )

    @property
    def unknown_boundaries(self) -> tuple[ClassTransition, ...]:
        return tuple(t for t in self.transitions if not t.is_known)

    @property
    def is_conclusive(self) -> bool:
        """Never true merely because nothing came back.

        A view over regions nobody has read produces an empty volume list, and
        an empty list is the same shape as a clear one.
        """
        return (
            bool(self.regions)
            and not self.unread_regions
            and not self.unbounded
            and not self.unknown_boundaries
        )

    def render(self) -> str:
        lines = [
            "AIRSPACE — what you are inside, and where it changes",
            f"{len(self.regions)} regions"
            + (
                f"  ·  at {self.planned_ft:.0f} ft"
                if self.planned_ft is not None
                else ""
            )
            + f"  ·  {len(self.volumes)} volumes not ruled out"
            + (
                f"  ·  {len(self.eliminated)} ruled out by altitude"
                if self.eliminated
                else ""
            ),
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 2 has been read for {', '.join(self.unread_regions)}. "
                "Nothing was read",
                "   in those regions, and nothing read is the same shape as "
                "nothing found.",
            ]
        if self.changes:
            lines += ["", "CLASS CHANGES — where the service you get changes"]
            for boundary in self.changes:
                lines.append(f"  {boundary.describe()}")
        if self.unknown_boundaries:
            lines += ["", "BOUNDARIES NOBODY CAN SPEAK FOR"]
            for boundary in self.unknown_boundaries:
                lines.append(f"  {boundary.describe()}")
        if self.volumes:
            lines += ["", "VOLUMES"]
            for volume in self.volumes:
                lines.append(f"  {volume.describe()}")
        if self.unbounded:
            lines += [
                "",
                "LIMITS NOT HELD — could not be ruled out because nobody read "
                "their vertical extent",
            ]
            for volume in self.unbounded:
                lines.append(f"  {volume.describe()}")
        if self.requirements:
            lines += [
                "",
                "CARRIAGE — required by the airspace, whatever the flight plan says",
                "  "
                + ", ".join(
                    r.value.upper().replace("_", "-") for r in self.requirements
                ),
            ]
        if self.units:
            lines += ["", "UNITS — in order of crossing", "  " + " → ".join(self.units)]
        lines += [
            "",
            "None of this says your track enters a terminal area. This platform "
            "holds no",
            "geometry, so the list is what the regions publish and what altitude "
            "could not rule out.",
        ]
        return "\n".join(lines)


def view_airspace(
    structure: AirspaceStructure,
    *,
    regions: Iterable[str],
    planned_ft: float | None = None,
) -> AirspaceView:
    """What the crossed regions publish, minus what altitude rules out.

    ``regions`` is in order of overflight, and the order matters: a class
    transition only exists between two consecutive regions, which is the same
    reason the altimetry section takes them in order.
    """
    crossed = tuple(normalise(r) for r in regions if str(r).strip())
    read = set(structure.regions)
    unread = tuple(r for r in crossed if r not in read)

    volumes: list[Airspace] = []
    eliminated: list[Airspace] = []
    unbounded: list[Airspace] = []
    for region in crossed:
        for volume in structure.in_region(region):
            if planned_ft is None:
                volumes.append(volume)
                continue
            verdict = volume.reaches(planned_ft)
            if verdict is None:
                unbounded.append(volume)
            elif verdict:
                volumes.append(volume)
            else:
                eliminated.append(volume)

    # The class of a region at a level is the class of the region volume that
    # actually contains that level, not of the one that shares its name. An
    # FIR topping at FL195 says nothing about a flight at FL350: the flight is
    # in the upper region above it, and reporting the FIR's class there would
    # be a confident answer about the wrong volume.
    def region_volume(region: str) -> tuple[Airspace | None, str]:
        named_volumes = [
            v for v in structure.in_region(region) if v.kind.is_region
        ]
        if not named_volumes:
            return (None, "not read")
        if planned_ft is None:
            exact = next(
                (v for v in named_volumes if v.designator == region), named_volumes[0]
            )
            return (exact, "")
        reaching = [v for v in named_volumes if v.reaches(planned_ft) is not False]
        if not reaching:
            return (
                None,
                f"no region volume reaches {planned_ft:.0f} ft — the flight is "
                "above what is held",
            )
        # Prefer the one whose designator is the region itself; where an upper
        # region covers the level, it is the one that applies.
        exact = next(
            (v for v in reaching if v.designator == region), reaching[0]
        )
        return (exact, "")

    transitions: list[ClassTransition] = []
    for before, after in zip(crossed, crossed[1:]):
        left, left_why = region_volume(before)
        right, right_why = region_volume(after)
        transitions.append(
            ClassTransition(
                leaving=before,
                entering=after,
                from_class=left.airspace_class if left else AirspaceClass.UNCLASSIFIED,
                to_class=right.airspace_class if right else AirspaceClass.UNCLASSIFIED,
                from_unit=left.unit if left else "",
                to_unit=right.unit if right else "",
                from_reason=left_why,
                to_reason=right_why,
            )
        )

    return AirspaceView(
        regions=crossed,
        planned_ft=planned_ft,
        volumes=tuple(volumes),
        eliminated=tuple(eliminated),
        unbounded=tuple(unbounded),
        unread_regions=unread,
        transitions=tuple(transitions),
    )


# --------------------------------------------------------------------------
# Reading an ENR 2 manifest
# --------------------------------------------------------------------------


def load_airspace(path: Path | str) -> AirspaceStructure:
    """Read one ENR 2 extract, with every volume cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=AIRSPACE_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    rows = manifest.get("volumes", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: volumes must be a list")

    volumes: list[Airspace] = []
    for index, row in enumerate(rows):
        where = f"{path}: volumes[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which row of ENR 2 this came "
                "from."
            )
        try:
            kind = AirspaceType(str(row.get("kind", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in AirspaceType)}. A volume filed "
                "under the wrong type answers the wrong question about "
                "clearances, so there is no safe default."
            ) from None
        raw_class = str(row.get("class", "")).strip().lower()
        try:
            airspace_class = (
                AirspaceClass(raw_class) if raw_class else AirspaceClass.UNCLASSIFIED
            )
        except ValueError:
            raise ManifestError(
                f"{where}: class must be one of "
                f"{', '.join(c.value.upper() for c in AirspaceClass if c is not AirspaceClass.UNCLASSIFIED)}"
                ", or left out where the State does not publish one"
            ) from None

        requirements: list[CarriageRequirement] = []
        listed = row.get("requirements", [])
        if not isinstance(listed, list):
            raise ManifestError(f"{where}: requirements must be a list")
        for entry in listed:
            try:
                requirements.append(
                    CarriageRequirement(str(entry).strip().lower().replace("-", "_"))
                )
            except ValueError:
                raise ManifestError(
                    f"{where}: requirement {entry!r} must be one of "
                    f"{', '.join(r.value for r in CarriageRequirement)}"
                ) from None

        frequency = row.get("frequency_mhz")
        if frequency is not None and frequency != "":
            try:
                frequency = float(frequency)
            except (TypeError, ValueError):
                raise ManifestError(
                    f"{where}: frequency_mhz {frequency!r} is not a number"
                ) from None
        else:
            frequency = None

        try:
            volumes.append(
                Airspace(
                    designator=str(row.get("designator", "")),
                    kind=kind,
                    source=sub_source(document, locator),
                    name=str(row.get("name", "")).strip(),
                    airspace_class=airspace_class,
                    region=str(row.get("region", default_region)).strip(),
                    lower_ft=read_limit(row.get("lower"), where=where, field="lower"),
                    upper_ft=read_limit(row.get("upper"), where=where, field="upper"),
                    unit=str(row.get("unit", "")).strip(),
                    frequency_mhz=frequency,
                    hours=str(row.get("hours", "")).strip(),
                    requirements=tuple(requirements),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return AirspaceStructure(volumes=tuple(volumes))


_AIRSPACE_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "volumes": [
        {
            "designator": "",
            "kind": "fir",
            "name": "",
            "class": "",
            "region": "",
            "lower": "SFC",
            "upper": "UNL",
            "unit": "",
            "frequency_mhz": None,
            "hours": "",
            "requirements": [],
            "remarks": "",
            "locator": "",
        }
    ],
}


def airspace_template() -> str:
    """A blank ENR 2 extract.

    ``region`` at the top applies to every volume that does not name its own,
    so a State's whole table needs it written once. An FIR names itself as its
    region — it is its own container. ``class`` may be left out where the
    State publishes none: it comes back as unclassified, which is reported as
    a gap rather than defaulted, because guessing G understates the clearance
    requirement and guessing A overstates it.
    """
    return json.dumps(_AIRSPACE_TEMPLATE, indent=2)
