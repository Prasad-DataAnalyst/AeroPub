"""ATS routes — the filed route string, the published structure, and the gap.

A route is not a city pair. It is what Item 15 of the flight plan says: an
ordered walk through significant points, along published ATS routes, at levels
the State permits in that direction, under navigation specifications the tail
has to hold. Everything a planner actually worries about en route lives in that
structure, and none of it is visible from "OTHH to EGLL".

Three things meet here
----------------------
**The filed route** is the operator's. It arrives as a string, in the grammar
every flight plan in the world uses, and :func:`parse_route_string` reads it
without needing to know anything about the airspace it crosses.

**The published structure** is the State's — ENR 3, the ATS route catalogue:
which segments exist on which airway, between which points, with what minimum
en-route altitude, what direction of cruising levels, what navigation
specification. It arrives cited, through a manifest, like everything else.

**The expansion** is where they meet, and where the value is. Resolving a filed
route against held structure answers questions nobody can answer from the
string alone — is the planned level above the MEA on every segment, is it the
right parity for the direction of flight, does the tail hold the navigation
specification each airway demands — and, just as importantly, says which legs
could not be resolved at all.

That last part is the point. A route string parses perfectly whether or not we
hold a single fact about the airspace it crosses. A system that reported "no
findings" from a route it could not resolve would be stating the strongest
possible conclusion from the weakest possible evidence, so
:class:`RouteExpansion` counts resolved legs against *checkable* ones and every
screen carries that count with it.

Checkable, not filed. A direct leg has no published segment behind it, so there
is nothing to resolve and nothing to screen — that is a property of flying
direct, not a gap in the AIP, and counting it against coverage would report a
route filed entirely DCT as nought per cent covered while blaming the State for
a decision the operator made.

Why the grammar is worth implementing properly
-----------------------------------------------
Item 15 is the one interface every operator already has. A planner can paste
the route they are about to file and get an answer; nobody has to learn a new
way to describe a route, and nothing has to be re-keyed. The grammar is
published, small, and fully testable offline — which is why it is here rather
than behind an integration.

The one genuine ambiguity in it is that a SID or STAR designator and an airway
designator are the same shape. Nothing in the string distinguishes ``UM688``
from a departure procedure called ``BAYA1A``. This module does not guess from
position: it resolves against what is held, and a designator that matches
neither a known airway nor a known procedure is reported unresolved rather than
assumed to be either.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
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
from aeropub.notam_register import ForceState, NotamRegister, RegisteredNotam

__all__ = [
    "ATS_ROUTE",
    "AtsStructure",
    "CruisingLevels",
    "DIRECT",
    "Element",
    "ElementKind",
    "FIX",
    "FiledRoute",
    "Leg",
    "LevelFinding",
    "NAVAID",
    "PointKind",
    "Resolution",
    "RouteExpansion",
    "RouteSegment",
    "SignificantPoint",
    "expand",
    "load_ats_structure",
    "notams_on_route",
    "parse_route_string",
    "route_entities",
    "screen_levels",
    "structure_template",
]

#: The parser identity written into citations read from an ENR 3 manifest.
ATS_PARSER_ID = "aeropub.ats"

#: Entity kinds. Free-standing, because none of these belongs to an aerodrome:
#: an airway crosses many and a fix belongs to none. Rolling any of them up
#: under an aerodrome would attach an airspace restriction to a runway.
FIX = "FIX"
NAVAID = "NAVAID"
ATS_ROUTE = "ATS"

#: What the flight plan writes for a direct leg.
DIRECT = "DCT"

# --------------------------------------------------------------------------
# The filed route string
# --------------------------------------------------------------------------


class PointKind(str, Enum):
    """How a significant point was written. All four are legal in Item 15."""

    NAME_CODE = "name_code"
    """A five-letter pronounceable name-code — ``ALSEM``. The commonest form
    and the only one that is unambiguous on sight."""

    NAVAID = "navaid"
    """A two- to three-character navaid identifier — ``DOH``, ``KIA``."""

    BEARING_DISTANCE = "bearing_distance"
    """A navaid, a three-digit bearing and a three-digit distance —
    ``DOH180040``. Reads as a nine-character word and is not one."""

    LATLON = "latlon"
    """Degrees, or degrees and minutes — ``46N078W``, ``4620N07805W``."""


class ElementKind(str, Enum):
    """What one whitespace-separated element of Item 15 is."""

    SPEED_LEVEL = "speed_level"
    """``N0450F350`` — the cruising speed and level this part is flown at."""

    POINT = "point"
    DESIGNATOR = "designator"
    """An ATS route designator, or a SID or STAR. The grammar does not
    distinguish the two, and neither does this module: it resolves against
    held structure rather than guessing from position."""

    DIRECT = "direct"
    STAY = "stay"
    RULES = "rules"
    """``IFR`` or ``VFR`` — a change of flight rules at this point."""

    CRUISE_CLIMB = "cruise_climb"
    TRUNCATED = "truncated"
    """``T`` — the route as filed was cut short."""

    UNPARSED = "unparsed"
    """Read, not understood. Kept rather than dropped: a route with an element
    nobody could read is not a route that parsed."""


_SPEED_LEVEL = re.compile(
    r"^(?:[NMK]\d{3,4}|M\d{3})(?:[FSAM]\d{3,4}|VFR)$"
)
_NAME_CODE = re.compile(r"^[A-Z]{5}$")
_NAVAID = re.compile(r"^[A-Z]{2,3}$")
_BEARING_DISTANCE = re.compile(r"^([A-Z]{2,3})(\d{3})(\d{3})$")
_LATLON = re.compile(
    r"^(?:\d{2}[NS]\d{3}[EW]|\d{4}[NS]\d{5}[EW])$"
)
_DESIGNATOR = re.compile(r"^[A-Z]{1,2}\d{1,3}[A-Z]?$")
_PROCEDURE = re.compile(r"^[A-Z]{3,5}\d[A-Z]$")
_STAY = re.compile(r"^STAY\d?/\d{4}$")


@dataclass(frozen=True, slots=True)
class Element:
    """One element of a filed route, as written."""

    kind: ElementKind
    text: str
    point_kind: PointKind | None = None
    speed_level: str = ""
    """A change of speed and level attached to a point — the ``/N0450F370``
    half of ``ALSEM/N0450F370``. Carried on the point rather than split into a
    separate element, because it is a property of arriving there."""

    @property
    def is_point(self) -> bool:
        return self.kind is ElementKind.POINT

    def describe(self) -> str:
        if self.speed_level:
            return f"{self.text}/{self.speed_level}"
        return self.text


def _classify_point(text: str) -> PointKind | None:
    if _LATLON.match(text):
        return PointKind.LATLON
    if _BEARING_DISTANCE.match(text):
        return PointKind.BEARING_DISTANCE
    if _NAME_CODE.match(text):
        return PointKind.NAME_CODE
    if _NAVAID.match(text):
        return PointKind.NAVAID
    return None


def _classify(word: str) -> Element:
    text, _, attached = word.partition("/")
    if not text:
        return Element(kind=ElementKind.UNPARSED, text=word)

    if text == DIRECT:
        return Element(kind=ElementKind.DIRECT, text=text)
    if text in ("IFR", "VFR"):
        return Element(kind=ElementKind.RULES, text=text)
    if text == "T":
        return Element(kind=ElementKind.TRUNCATED, text=text)
    if text == "C" and attached:
        return Element(kind=ElementKind.CRUISE_CLIMB, text=word)
    if _STAY.match(word):
        return Element(kind=ElementKind.STAY, text=word)
    if _SPEED_LEVEL.match(text):
        return Element(kind=ElementKind.SPEED_LEVEL, text=text)

    # A point is tried before a designator. The two overlap only where a
    # designator is also a legal navaid identifier, and in that position the
    # flight plan means the point: an airway never follows an airway.
    point_kind = _classify_point(text)
    if point_kind is not None and not (
        point_kind is PointKind.NAVAID and _DESIGNATOR.match(text)
    ):
        return Element(
            kind=ElementKind.POINT,
            text=text,
            point_kind=point_kind,
            speed_level=attached,
        )
    if _DESIGNATOR.match(text) or _PROCEDURE.match(text):
        return Element(kind=ElementKind.DESIGNATOR, text=text)
    if point_kind is not None:
        return Element(
            kind=ElementKind.POINT,
            text=text,
            point_kind=point_kind,
            speed_level=attached,
        )
    return Element(kind=ElementKind.UNPARSED, text=word)


@dataclass(frozen=True, slots=True)
class Leg:
    """One published-route leg: from a point, via something, to a point.

    ``via`` is an ATS route designator or :data:`DIRECT`. A leg is the unit
    everything downstream works in, because a minimum en-route altitude, a
    direction of cruising levels and a navigation specification are all
    properties of a segment rather than of a point.
    """

    start: str
    via: str
    end: str

    @property
    def is_direct(self) -> bool:
        return self.via == DIRECT

    def describe(self) -> str:
        return f"{self.start} {self.via} {self.end}"


@dataclass(frozen=True, slots=True)
class FiledRoute:
    """A route as the operator filed it."""

    text: str
    departure: str = ""
    destination: str = ""
    elements: tuple[Element, ...] = ()

    @property
    def points(self) -> tuple[str, ...]:
        """Every significant point named, in order, without duplicates."""
        found: list[str] = []
        for element in self.elements:
            if element.is_point and element.text not in found:
                found.append(element.text)
        return tuple(found)

    @property
    def designators(self) -> tuple[str, ...]:
        found: list[str] = []
        for element in self.elements:
            if element.kind is ElementKind.DESIGNATOR and element.text not in found:
                found.append(element.text)
        return tuple(found)

    @property
    def unparsed(self) -> tuple[str, ...]:
        """Elements read and not understood.

        Never empty-by-omission: an element nobody could read stays visible,
        because a route with an unreadable element is not a route that parsed.
        """
        return tuple(
            e.text for e in self.elements if e.kind is ElementKind.UNPARSED
        )

    @property
    def is_parsed(self) -> bool:
        return bool(self.elements) and not self.unparsed

    @property
    def legs(self) -> tuple[Leg, ...]:
        """The route as a walk from point to point.

        A designator between two points is the airway joining them; two points
        with nothing between are direct, whether or not the string bothered to
        say ``DCT``. The two ends are included where the aerodromes are known,
        because the first and last legs are exactly where a departure or
        arrival procedure attaches.
        """
        walk: list[Leg] = []
        previous = self.departure or ""
        pending = DIRECT
        for element in self.elements:
            if element.kind is ElementKind.DESIGNATOR:
                pending = element.text
                continue
            if element.kind is ElementKind.DIRECT:
                pending = DIRECT
                continue
            if not element.is_point:
                continue
            if previous:
                walk.append(Leg(start=previous, via=pending, end=element.text))
            previous = element.text
            pending = DIRECT
        if previous and self.destination and previous != self.destination:
            walk.append(Leg(start=previous, via=pending, end=self.destination))
        return tuple(walk)

    def describe(self) -> str:
        return " ".join(e.describe() for e in self.elements)


def parse_route_string(
    text: str, *, departure: str = "", destination: str = ""
) -> FiledRoute:
    """Read an ICAO Item 15 route string.

    The one interface every operator already has: a planner pastes the route
    they are about to file and nothing has to be re-keyed. Aerodromes are
    passed separately rather than taken from the string, because Item 15 does
    not carry them — Items 13 and 16 do, and a string that happens to start
    with four letters may be naming a point.
    """
    words = str(text).upper().split()
    return FiledRoute(
        text=str(text).strip(),
        departure=normalise(departure),
        destination=normalise(destination),
        elements=tuple(_classify(word) for word in words if word),
    )


# --------------------------------------------------------------------------
# The published structure — ENR 3
# --------------------------------------------------------------------------


class CruisingLevels(str, Enum):
    """Which cruising levels a segment may be flown at, by direction.

    The semicircular and table-of-levels rules are what make a route legal at
    FL350 eastbound and illegal westbound, and a State may override either on
    a particular airway. Held as published rather than derived from the track,
    because the override is the whole reason ENR 3 prints it.
    """

    ODD = "odd"
    EVEN = "even"
    BOTH = "both"
    NONE = "none"
    """One-way in the other direction, or not available for cruise."""

    def permits(self, level_ft: float) -> bool:
        """Whether a flight level is of the parity this segment allows.

        Read in hundreds of feet, which is how levels are published and flown.
        A level that is not a whole hundred belongs to no parity and is
        refused rather than rounded: rounding here would clear a level nobody
        may fly.
        """
        if self is CruisingLevels.BOTH:
            return True
        if self is CruisingLevels.NONE:
            return False
        hundreds = level_ft / 100.0
        if hundreds != int(hundreds):
            return False
        thousands = int(hundreds) // 10
        return (thousands % 2 == 1) if self is CruisingLevels.ODD else (
            thousands % 2 == 0
        )


@dataclass(frozen=True, slots=True)
class SignificantPoint:
    """One point on the route structure, as ENR 4 publishes it."""

    designator: str
    source: SourceRef
    name: str = ""
    kind: PointKind = PointKind.NAME_CODE
    latitude: float | None = None
    longitude: float | None = None
    reporting: str = ""
    """Compulsory or on-request, as published."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "designator", normalise(self.designator))
        if not self.designator:
            raise ValueError("SignificantPoint.designator must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise TypeError("SignificantPoint.source must be a SourceRef")

    @property
    def key(self) -> str:
        kind = NAVAID if self.kind is PointKind.NAVAID else FIX
        return named(kind, self.designator)


@dataclass(frozen=True, slots=True)
class RouteSegment:
    """One published segment of one ATS route, as ENR 3 prints it.

    The row of the ENR 3 table: this airway, between these two points, with
    this minimum en-route altitude, in this direction, under this navigation
    specification. Everything a level screen needs is here, and nothing is
    derived — a minimum en-route altitude computed from terrain we do not hold
    would be a number nobody published.
    """

    route: str
    start: str
    end: str
    source: SourceRef
    mea_ft: float | None = None
    moca_ft: float | None = None
    maa_ft: float | None = None
    upper_limit_ft: float | None = None
    lower_limit_ft: float | None = None
    direction: CruisingLevels = CruisingLevels.BOTH
    navigation_spec: str = ""
    """The PBN specification this segment requires — ``RNAV 5``, ``RNP 4``.
    Held as the State prints it; whether a tail holds it is a fact about the
    tail."""

    track_deg: float | None = None
    distance_nm: float | None = None
    airspace_class: str = ""
    controlling_unit: str = ""
    region: str = ""
    """The flight information region this segment lies in. ENR 3 is published
    per State, so a segment knows its region for free — and without it a route
    structure cannot be grouped by whose airspace each part of it is in, which
    is the first thing anybody wants to see on a picture of a network."""

    def __post_init__(self) -> None:
        for field in ("route", "start", "end", "airspace_class", "region"):
            object.__setattr__(self, field, normalise(getattr(self, field)))
        object.__setattr__(
            self, "navigation_spec", normalise(self.navigation_spec)
        )
        if not self.route:
            raise ValueError("RouteSegment.route must be a non-empty string")
        if not self.start or not self.end:
            raise ValueError(
                "RouteSegment.start and RouteSegment.end must both be named "
                "— a segment with one end is not a shorter segment, it joins "
                "nothing"
            )
        if self.start == self.end:
            raise ValueError(
                f"{self.route} {self.start} to itself is not a segment"
            )
        if not isinstance(self.direction, CruisingLevels):
            raise TypeError("RouteSegment.direction must be a CruisingLevels")
        if not isinstance(self.source, SourceRef):
            raise TypeError("RouteSegment.source must be a SourceRef")

    @property
    def key(self) -> str:
        return named(ATS_ROUTE, self.route)

    @property
    def floor_ft(self) -> float | None:
        """The lowest level this segment may be flown at.

        The MEA where one is published, otherwise the lower limit of the
        airway. The MOCA is deliberately not used: it guarantees obstacle
        clearance and not navigation signal, so a flight at the MOCA is legal
        only in circumstances this platform cannot know about.
        """
        return self.mea_ft if self.mea_ft is not None else self.lower_limit_ft

    def describe(self) -> str:
        parts = [f"{self.route} {self.start}-{self.end}"]
        if self.mea_ft is not None:
            parts.append(f"MEA {self.mea_ft:.0f}")
        if self.navigation_spec:
            parts.append(self.navigation_spec)
        if self.direction is not CruisingLevels.BOTH:
            parts.append(f"{self.direction.value} levels")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class AtsStructure:
    """The published ATS route catalogue, as far as it has been read."""

    segments: tuple[RouteSegment, ...] = ()
    points: tuple[SignificantPoint, ...] = ()
    procedures: tuple[str, ...] = ()
    """Designators known to be departure or arrival procedures rather than
    airways. Held so the one genuine ambiguity in Item 15 is resolved against
    evidence instead of position."""

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(sorted({s.route for s in self.segments}))

    def on(self, route: str) -> tuple[RouteSegment, ...]:
        wanted = normalise(route)
        if not wanted:
            return ()
        return tuple(s for s in self.segments if s.route == wanted)

    def in_region(self, region: str) -> tuple[RouteSegment, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(s for s in self.segments if s.region == wanted)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({s.region for s in self.segments if s.region}))

    def routes_through(self, point: str) -> tuple[str, ...]:
        """Every airway published as passing through this point.

        The interchange question, and the one a route structure drawing exists
        to answer: where a point carries more than one airway, it is somewhere
        a plan can change airway without a direct leg.
        """
        wanted = normalise(point)
        if not wanted:
            return ()
        return tuple(sorted({
            s.route for s in self.segments if wanted in (s.start, s.end)
        }))

    def points_on(self, route: str) -> tuple[str, ...]:
        """Every point on one airway, in published order.

        Walked rather than sorted: the order an airway is published in is the
        order it is flown, and sorting the designators alphabetically would
        draw a route that goes back on itself.
        """
        segments = self.on(route)
        if not segments:
            return ()
        forward = {s.start: s for s in segments}
        ends = {s.end for s in segments}
        starts = [s.start for s in segments if s.start not in ends]
        at = starts[0] if starts else segments[0].start
        walked = [at]
        seen = {at}
        while at in forward:
            at = forward[at].end
            if at in seen:
                break
            walked.append(at)
            seen.add(at)
        return tuple(walked)

    def point(self, designator: str) -> SignificantPoint | None:
        wanted = normalise(designator)
        return next((p for p in self.points if p.designator == wanted), None)

    def is_procedure(self, designator: str) -> bool:
        return normalise(designator) in {normalise(p) for p in self.procedures}

    def between(self, route: str, start: str, end: str) -> tuple[RouteSegment, ...]:
        """The segments of one airway joining two points, in order of flight.

        Walks the published segments rather than assuming the two points are
        adjacent: a leg filed as ``ALSEM UM688 BAYAN`` may cross a dozen
        published segments, and the highest minimum en-route altitude among
        them is the one that binds.

        Returns empty where no path exists in the held structure. That is a
        coverage answer, not a statement that the airway does not join them.
        """
        first, last = normalise(start), normalise(end)
        segments = self.on(route)
        if not segments:
            return ()

        forward = {s.start: s for s in segments}
        walked: list[RouteSegment] = []
        at = first
        seen = {at}
        while at != last:
            step = forward.get(at)
            if step is None:
                break
            walked.append(step)
            at = step.end
            if at in seen:
                return ()  # a loop in the published data; report nothing
            seen.add(at)
        if at == last:
            return tuple(walked)

        # Airways are published in one direction and flown in both. Try the
        # reverse walk before concluding the structure does not join them.
        backward = {s.end: s for s in segments}
        walked = []
        at = first
        seen = {at}
        while at != last:
            step = backward.get(at)
            if step is None:
                return ()
            walked.append(step)
            at = step.start
            if at in seen:
                return ()
            seen.add(at)
        return tuple(walked)


# --------------------------------------------------------------------------
# The expansion
# --------------------------------------------------------------------------


class Resolution(str, Enum):
    """What happened when a filed leg was looked up.

    Four outcomes, not two, because "no published segment behind this leg" is
    true for three completely different reasons and only one of them is a gap
    in our data.
    """

    RESOLVED = "resolved"
    """Published segments found. Everything downstream can screen it."""

    DIRECT = "direct"
    """Flown direct. There is no published segment behind a DCT, so there is
    no minimum level, no direction of cruising levels and no navigation
    specification to check — a property of the leg, not a gap in the AIP."""

    PROCEDURE = "procedure"
    """A departure or arrival procedure, not an airway. Screened as a
    procedure; not this module's business."""

    UNRESOLVED = "unresolved"
    """The one that is a gap. The airway is not held, or no published path
    joins the two points on it."""

    @property
    def is_checkable(self) -> bool:
        """Whether this leg is one the platform could have screened.

        Coverage is counted against these only. A route filed entirely direct
        has nothing to resolve, and reporting it as nought per cent covered
        would blame the AIP for a decision the operator made.
        """
        return self in (Resolution.RESOLVED, Resolution.UNRESOLVED)


@dataclass(frozen=True, slots=True)
class ExpandedLeg:
    """One filed leg, resolved against the published structure or not."""

    leg: Leg
    segments: tuple[RouteSegment, ...] = ()
    reason: str = ""
    resolution: Resolution = Resolution.UNRESOLVED

    @property
    def is_resolved(self) -> bool:
        return self.resolution is Resolution.RESOLVED

    @property
    def distance_nm(self) -> float | None:
        if not self.segments or any(s.distance_nm is None for s in self.segments):
            return None
        return sum(s.distance_nm for s in self.segments)

    @property
    def highest_mea_ft(self) -> float | None:
        """The binding minimum en-route altitude across this leg.

        The highest of the segments' floors: one segment at FL240 makes the
        whole leg FL240, and averaging or taking the first would clear a level
        that is legal on most of the leg and not on all of it.
        """
        floors = [s.floor_ft for s in self.segments if s.floor_ft is not None]
        return max(floors) if floors else None

    def describe(self) -> str:
        if self.is_resolved:
            count = len(self.segments)
            return (
                f"{self.leg.describe()} — {count} published segment"
                + ("s" if count != 1 else "")
            )
        return f"{self.leg.describe()} — {self.reason or 'not in held structure'}"


@dataclass(frozen=True, slots=True)
class RouteExpansion:
    """A filed route resolved against what the platform holds.

    ``resolved`` against ``filed`` is the number every screen below carries
    with it. A route string parses perfectly whether or not a single fact is
    held about the airspace it crosses, so a screen reporting no findings on an
    unresolved route would be stating the strongest possible conclusion from
    the weakest possible evidence.
    """

    route: FiledRoute
    legs: tuple[ExpandedLeg, ...] = ()

    @property
    def filed(self) -> int:
        return len(self.legs)

    @property
    def checkable(self) -> int:
        """Legs that could have been screened — resolved or not.

        Direct legs and terminal procedures are excluded, because neither has
        a published segment to screen and neither absence is our gap.
        """
        return sum(1 for leg in self.legs if leg.resolution.is_checkable)

    @property
    def resolved(self) -> int:
        return sum(1 for leg in self.legs if leg.is_resolved)

    @property
    def coverage(self) -> tuple[int, int]:
        return (self.resolved, self.checkable)

    @property
    def is_complete(self) -> bool:
        """Whether every leg that could be screened was.

        Also false when the string itself did not fully parse: an element
        nobody could read may have been an airway, and a route missing a leg
        it never knew about screens clean.
        """
        return (
            self.route.is_parsed
            and self.checkable > 0
            and self.resolved == self.checkable
        )

    @property
    def unresolved(self) -> tuple[ExpandedLeg, ...]:
        return tuple(
            leg for leg in self.legs if leg.resolution is Resolution.UNRESOLVED
        )

    @property
    def direct(self) -> tuple[ExpandedLeg, ...]:
        """Legs flown direct.

        Worth surfacing rather than passing over. A long direct leg across a
        foreign FIR is a real planning question — it may not be available, and
        nothing published says so here — even though it is not a gap in what
        we hold.
        """
        return tuple(leg for leg in self.legs if leg.resolution is Resolution.DIRECT)

    @property
    def segments(self) -> tuple[RouteSegment, ...]:
        return tuple(s for leg in self.legs for s in leg.segments)

    @property
    def distance_nm(self) -> float | None:
        """Total published distance, or ``None`` if any leg is unmeasured.

        Deliberately all-or-nothing. A partial total is a smaller number than
        the route, and a planner reading it as the route length would plan
        fuel against a distance nobody flew.
        """
        legs = [leg.distance_nm for leg in self.legs]
        if not legs or any(d is None for d in legs):
            return None
        return sum(legs)

    @property
    def airway_distance_nm(self) -> float | None:
        """Published distance across the resolved legs only.

        Never the route length, and never presented as one. A route with two
        long direct legs has an airway distance a fraction of the distance
        flown, and the two must not be confused — which is why this is a
        separate property with a separate name rather than a fallback inside
        :attr:`distance_nm`.
        """
        measured = [
            leg.distance_nm
            for leg in self.legs
            if leg.is_resolved and leg.distance_nm is not None
        ]
        return sum(measured) if measured else None

    @property
    def highest_mea_ft(self) -> float | None:
        floors = [
            leg.highest_mea_ft for leg in self.legs if leg.highest_mea_ft is not None
        ]
        return max(floors) if floors else None

    @property
    def navigation_specs(self) -> tuple[str, ...]:
        return tuple(
            sorted({s.navigation_spec for s in self.segments if s.navigation_spec})
        )


def expand(route: FiledRoute, structure: AtsStructure) -> RouteExpansion:
    """Resolve each filed leg against the published structure.

    A direct leg resolves to nothing and says so: there is no published
    segment behind a DCT, so there is no minimum en-route altitude, no
    direction of cruising levels and no navigation specification to check
    against. That is a real property of flying direct, not a gap in our data,
    and the two are labelled differently.
    """
    expanded: list[ExpandedLeg] = []
    for leg in route.legs:
        if leg.is_direct:
            expanded.append(
                ExpandedLeg(
                    leg=leg,
                    resolution=Resolution.DIRECT,
                    reason=(
                        "direct — no published segment, so no minimum level, "
                        "direction or navigation specification applies"
                    ),
                )
            )
            continue
        if structure.is_procedure(leg.via):
            expanded.append(
                ExpandedLeg(
                    leg=leg,
                    resolution=Resolution.PROCEDURE,
                    reason=(
                        f"{leg.via} is a terminal procedure, not an airway — "
                        "screened as a procedure, not here"
                    ),
                )
            )
            continue
        segments = structure.between(leg.via, leg.start, leg.end)
        if segments:
            expanded.append(
                ExpandedLeg(
                    leg=leg, segments=segments, resolution=Resolution.RESOLVED
                )
            )
            continue
        known = bool(structure.on(leg.via))
        expanded.append(
            ExpandedLeg(
                leg=leg,
                resolution=Resolution.UNRESOLVED,
                reason=(
                    f"{leg.via} is held, and no published path joins "
                    f"{leg.start} to {leg.end} on it"
                    if known
                    else f"{leg.via} is not in the held structure"
                ),
            )
        )
    return RouteExpansion(route=route, legs=tuple(expanded))


# --------------------------------------------------------------------------
# Screening
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelFinding:
    """One segment on which the planned level does not work."""

    segment: RouteSegment
    planned_ft: float
    reason: str
    blocking: bool = True
    """Whether this stops the route being flown as planned, as against merely
    needing a decision. A level below the minimum en-route altitude is
    blocking; a navigation specification we cannot confirm is not, because
    the operator may well hold it and we simply do not know."""

    def describe(self) -> str:
        mark = "!!" if self.blocking else " ·"
        return f"{mark} {self.segment.describe()} — {self.reason}"


def screen_levels(
    expansion: RouteExpansion,
    *,
    planned_ft: float,
    holds: Iterable[str] = (),
) -> tuple[LevelFinding, ...]:
    """Screen a planned cruising level against every resolved segment.

    Four questions, and each of them is answerable only from published data:
    is the level at or above the minimum en-route altitude, is it below any
    published maximum, is it of the parity the State permits in this
    direction, and does the tail hold the navigation specification the segment
    requires.

    Unresolved legs produce nothing here by construction, which is why
    :attr:`RouteExpansion.coverage` travels with the findings. Screening only
    the segments we hold and reporting no findings would be the most confident
    possible way of saying nothing.
    """
    held = {normalise(spec) for spec in holds if str(spec).strip()}
    findings: list[LevelFinding] = []
    for segment in expansion.segments:
        floor = segment.floor_ft
        if floor is not None and planned_ft < floor:
            findings.append(
                LevelFinding(
                    segment=segment,
                    planned_ft=planned_ft,
                    reason=(
                        f"planned {planned_ft:.0f} ft is below the minimum "
                        f"{floor:.0f} ft"
                    ),
                )
            )
        ceiling = segment.maa_ft if segment.maa_ft is not None else segment.upper_limit_ft
        if ceiling is not None and planned_ft > ceiling:
            findings.append(
                LevelFinding(
                    segment=segment,
                    planned_ft=planned_ft,
                    reason=(
                        f"planned {planned_ft:.0f} ft is above the maximum "
                        f"{ceiling:.0f} ft"
                    ),
                )
            )
        if not segment.direction.permits(planned_ft):
            findings.append(
                LevelFinding(
                    segment=segment,
                    planned_ft=planned_ft,
                    reason=(
                        f"this segment publishes {segment.direction.value} "
                        f"cruising levels and {planned_ft:.0f} ft is not one"
                    ),
                )
            )
        if segment.navigation_spec and segment.navigation_spec not in held:
            findings.append(
                LevelFinding(
                    segment=segment,
                    planned_ft=planned_ft,
                    reason=(
                        f"requires {segment.navigation_spec}, which is not in "
                        "the capabilities given"
                    ),
                    blocking=False,
                )
            )
    return tuple(findings)


# --------------------------------------------------------------------------
# NOTAM along the route
# --------------------------------------------------------------------------


def route_entities(
    expansion: RouteExpansion, structure: AtsStructure | None = None
) -> tuple[str, ...]:
    """Every entity a NOTAM could be filed against on this route.

    The aerodromes at both ends, every significant point, and every airway
    used. The point of listing them is that the NOTAM register is indexed by
    entity: once the route is a list of keys, "what is NOTAMed on this route"
    is a lookup rather than a judgement.

    Points are keyed by what the held structure says they are — a navaid and a
    name-code are different kinds of object and States file against them
    differently. A point we do not hold is keyed as a fix, and that guess is
    the one thing here that could miss a NOTAM; it is preferred to dropping
    the point, which would miss every NOTAM on it.
    """
    found: list[str] = []

    def note(key: str) -> None:
        if key not in found:
            found.append(key)

    route = expansion.route
    for aerodrome in (route.departure, route.destination):
        if aerodrome:
            note(aerodrome)
    for designator in route.points:
        point = structure.point(designator) if structure else None
        note(point.key if point else named(FIX, designator))
    for leg in expansion.legs:
        if not leg.leg.is_direct:
            note(named(ATS_ROUTE, leg.leg.via))
    return tuple(found)


def notams_on_route(
    register: NotamRegister,
    expansion: RouteExpansion,
    moment: datetime,
    *,
    structure: AtsStructure | None = None,
) -> tuple[tuple[str, RegisteredNotam, ForceState], ...]:
    """Every NOTAM in force against anything on this route.

    Each is returned with the entity it was found against, so a briefing can
    say *where* on the route it bites rather than presenting a flat list a
    reader has to place themselves. States of ``SCHEDULE_UNKNOWN`` come
    through as they are: a NOTAM whose window we could not resolve is one a
    planner must consider, not one to quietly drop.
    """
    found: list[tuple[str, RegisteredNotam, ForceState]] = []
    seen: set[tuple[str, str]] = set()
    for entity in route_entities(expansion, structure):
        for notam, state in register.at(entity, moment):
            mark = (entity, notam.identifier)
            if mark in seen:
                continue
            seen.add(mark)
            found.append((entity, notam, state))
    return tuple(found)


# --------------------------------------------------------------------------
# Reading an ENR 3 manifest
# --------------------------------------------------------------------------


def _number(value: object, *, where: str, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field} {value!r} is not a number. A minimum en-route "
            "altitude that cannot be read is left unread, never rounded."
        ) from None


def _direction(value: object, *, where: str) -> CruisingLevels:
    try:
        return CruisingLevels(str(value).strip().lower() or "both")
    except ValueError:
        raise ManifestError(
            f"{where}: direction must be one of "
            f"{', '.join(d.value for d in CruisingLevels)}"
        ) from None


def load_ats_structure(path: Path | str) -> AtsStructure:
    """Read one ENR 3 extract, with every segment cited to it.

    One document, one citation — the rule every manifest in this platform
    keeps. A State's ATS route table is one publication; a file spanning two
    would emit both cited to whichever the header named.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=ATS_PARSER_ID,
    )

    rows = manifest.get("segments", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: segments must be a list")
    segments: list[RouteSegment] = []
    for index, row in enumerate(rows):
        where = f"{path}: segments[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which row of the ENR 3 table "
                "this was read from."
            )
        try:
            segments.append(
                RouteSegment(
                    route=str(row.get("route", "")),
                    start=str(row.get("start", "")),
                    end=str(row.get("end", "")),
                    source=sub_source(document, locator),
                    mea_ft=_number(row.get("mea_ft"), where=where, field="mea_ft"),
                    moca_ft=_number(row.get("moca_ft"), where=where, field="moca_ft"),
                    maa_ft=_number(row.get("maa_ft"), where=where, field="maa_ft"),
                    upper_limit_ft=_number(
                        row.get("upper_limit_ft"), where=where, field="upper_limit_ft"
                    ),
                    lower_limit_ft=_number(
                        row.get("lower_limit_ft"), where=where, field="lower_limit_ft"
                    ),
                    direction=_direction(row.get("direction", "both"), where=where),
                    navigation_spec=str(row.get("navigation_spec", "")),
                    track_deg=_number(row.get("track_deg"), where=where, field="track_deg"),
                    distance_nm=_number(
                        row.get("distance_nm"), where=where, field="distance_nm"
                    ),
                    airspace_class=str(row.get("airspace_class", "")),
                    controlling_unit=str(row.get("controlling_unit", "")),
                    region=str(row.get("region", manifest.get("region", ""))),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    rows = manifest.get("points", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: points must be a list")
    points: list[SignificantPoint] = []
    for index, row in enumerate(rows):
        where = f"{path}: points[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        try:
            kind = PointKind(str(row.get("kind", PointKind.NAME_CODE.value)).lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in PointKind)}"
            ) from None
        try:
            points.append(
                SignificantPoint(
                    designator=str(row.get("designator", "")),
                    source=sub_source(
                        document, str(row.get("locator", "")).strip() or "ENR 4"
                    ),
                    name=str(row.get("name", "")),
                    kind=kind,
                    latitude=_number(row.get("latitude"), where=where, field="latitude"),
                    longitude=_number(
                        row.get("longitude"), where=where, field="longitude"
                    ),
                    reporting=str(row.get("reporting", "")),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    listed = manifest.get("procedures", [])
    if not isinstance(listed, list):
        raise ManifestError(f"{path}: procedures must be a list of designators")

    return AtsStructure(
        segments=tuple(segments),
        points=tuple(points),
        procedures=tuple(str(p) for p in listed),
    )


_STRUCTURE_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "segments": [
        {
            "route": "",
            "start": "",
            "end": "",
            "mea_ft": None,
            "moca_ft": None,
            "maa_ft": None,
            "upper_limit_ft": None,
            "lower_limit_ft": None,
            "direction": "both",
            "navigation_spec": "",
            "track_deg": None,
            "distance_nm": None,
            "airspace_class": "",
            "controlling_unit": "",
            "region": "",
            "locator": "",
        }
    ],
    "points": [
        {
            "designator": "",
            "name": "",
            "kind": "name_code",
            "latitude": None,
            "longitude": None,
            "reporting": "",
            "locator": "",
        }
    ],
    "procedures": [],
}


def structure_template() -> str:
    """A blank ENR 3 extract.

    One row per published segment, in the direction the table prints it —
    :meth:`AtsStructure.between` walks it both ways, so an airway need not be
    entered twice. ``procedures`` lists the SID and STAR designators at the
    aerodromes in scope, which is what lets a filed route tell a departure
    procedure from an airway; the two are the same shape in Item 15 and
    nothing else can separate them.
    """
    return json.dumps(_STRUCTURE_TEMPLATE, indent=2)
