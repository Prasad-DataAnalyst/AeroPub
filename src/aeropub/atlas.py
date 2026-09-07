"""One map: ENR 2, ENR 3, ENR 4 and ENR 5 over a real world.

The route chart in :mod:`aeropub.enroute` draws ENR 3 and stops there, because
until now ENR 2 and ENR 5 had no geometry to draw. They do now, so this is the
whole en-route picture: the regions you are inside, the routes through them,
the points those routes are made of, and the areas you may not enter — each
from the section of the AIP that publishes it, over a coastline that says
which part of the world this is.

What a reader gets to know, and where it comes from
----------------------------------------------------
=================  ========================================================
which FIR          ENR 2.1. The designator, class, vertical limits, the unit
                   working it and the frequency to call
which State        the AIP that published the section. Never the country
                   under the point — that is geography, and an FIR is not a
                   country: they run over the high seas and are delegated
which ATS route    ENR 3. Designator, the binding level band, direction of
                   cruising levels, navigation specification, unit
which waypoint     ENR 4.4 and 4.1. Designator, kind, published position, and
                   every airway published through it
what is closed     the NOTAM in force, landed on the thing it names
=================  ========================================================

Every one of those is a lookup in something a State published. None of them is
computed from the picture.

The drawing says how much of itself is published
-------------------------------------------------
An area whose edge is partly prose — *thence along the State boundary* — is
drawn as the open pieces the AIP gave coordinates for, dashed, and never
filled. A filled shape reads as a definite extent, and the extent is exactly
what that publication did not give. The count of such areas travels with the
map.

The refusal, restated because a map is where it gets broken
-------------------------------------------------------------
Nothing here answers whether a point is inside an area. Not the route against
the FIR, not a waypoint against a danger area, not anything. A drawing puts
two things on the same sheet; it does not make one contain the other, and a
containment answer computed from a boundary that is partly prose, stepped
through its arcs and rounded to the second is the most dangerous output this
platform could produce. Airspace is entered on a clearance and a chart.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

from aeropub.airspace import Airspace, AirspaceStructure, AirspaceType
from aeropub.ats import ATS_ROUTE, AtsStructure, FiledRoute, expand
from aeropub.basemap import NOT_AERONAUTICAL, Basemap, load_basemap
from aeropub.boundary import Boundary
from aeropub.enroute import AirwayProfile, profile_for
from aeropub.entities import named, normalise
from aeropub.geo import (
    Bounds,
    Position,
    bounds_of,
    great_circle_nm,
    great_circle_path,
    mercator,
    unmercator,
)
from aeropub.hazards import Hazard, HazardRegister
from aeropub.navaids import NavaidRegister
from aeropub.notam_register import NotamRegister
from aeropub.supplement import SupplementRegister

__all__ = [
    "Atlas",
    "DrawnArea",
    "DrawnPoint",
    "DrawnRoute",
    "atlas_html",
    "atlas_svg",
    "build_atlas",
]

#: Steps per airway leg. An airway leg drawn as a straight line on a
#: projection is a different line from the one flown.
LEG_STEPS = 12


def _count(n: int, singular: str, plural: str = "") -> str:
    """A count that reads like a person wrote it."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


#: Graticule spacings, coarsest first, in degrees. Down to ten minutes, which
#: is as fine as a chart of a route structure is ever read.
_SPACINGS = (30.0, 10.0, 5.0, 2.0, 1.0, 0.5, 1.0 / 6.0)


def _spacing_for(span_deg: float, *, want: int = 6) -> float:
    """The graticule spacing that puts about ``want`` lines across the window."""
    for spacing in _SPACINGS:
        if span_deg / spacing >= want:
            return spacing
    return _SPACINGS[-1]


def _label_degrees(value: float, *, is_latitude: bool) -> str:
    """A graticule label in the form a chart prints."""
    hemisphere = ("N" if value >= 0 else "S") if is_latitude else (
        "E" if value >= 0 else "W"
    )
    size = abs(value)
    degrees = int(size)
    minutes = round((size - degrees) * 60.0)
    if minutes == 60:
        degrees, minutes = degrees + 1, 0
    if minutes:
        return f"{degrees}\u00b0{minutes:02d}'{hemisphere}"
    return f"{degrees}\u00b0{hemisphere}"


def _round_distance(nm: float) -> float:
    """A scale-bar length a person would recognise."""
    for step in (1000.0, 500.0, 250.0, 200.0, 100.0, 50.0, 25.0, 20.0, 10.0, 5.0, 2.0, 1.0):
        if nm >= step:
            return step
    return 1.0


@dataclass(frozen=True, slots=True)
class DrawnArea:
    """One ENR 2 volume or ENR 5 area, as far as its edge was published."""

    designator: str
    layer: str
    """``fir``, ``terminal`` or ``hazard`` — which switch turns it off."""

    rings: tuple[tuple[Position, ...], ...] = ()
    closed: bool = False
    """Whether the published coordinates close the ring on their own. False
    means partly prose, and a partly-prose area is never filled."""

    narrative: int = 0
    arcs: int = 0
    label: str = ""
    detail: str = ""
    published_in: str = ""
    """The document that published it. This is the answer to "whose airspace
    is this" — never the country under the point."""

    notams: int = 0
    supplements: tuple[str, ...] = ()
    """Supplements in force against it. A supplement never changes what is
    drawn — nothing reads a value out of its prose — it says the published
    value is no longer the whole answer."""


    @property
    def is_drawable(self) -> bool:
        return any(len(ring) >= 2 for ring in self.rings)


@dataclass(frozen=True, slots=True)
class DrawnRoute:
    """One ATS route, drawn through the points ENR 4 gives positions for."""

    designator: str
    path: tuple[Position, ...] = ()
    gaps: int = 0
    one_way: bool = False
    closed: bool = False
    detail: str = ""
    published_in: str = ""
    notams: int = 0
    supplements: tuple[str, ...] = ()

    @property
    def is_drawable(self) -> bool:
        return len(self.path) >= 2


@dataclass(frozen=True, slots=True)
class DrawnPoint:
    """One significant point or navaid, at the position it was published at."""

    designator: str
    position: Position
    kind: str = "fix"
    routes: tuple[str, ...] = ()
    detail: str = ""
    published_in: str = ""
    notams: int = 0
    supplements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DrawnTrack:
    """The filed route, drawn through the structure it was resolved against.

    Not a straight line between the filed points: a leg filed as ``ALSEM UM688
    KUKLA`` crosses every published segment between them, and the track flown
    goes through each one. Where the route resolved, this is drawn through the
    intermediate points; where it did not, it is drawn through the points the
    string itself names and says so.
    """

    points: tuple[str, ...] = ()
    path: tuple[Position, ...] = ()
    unplaced: tuple[str, ...] = ()
    filed: str = ""
    resolved: int = 0
    checkable: int = 0

    @property
    def is_drawable(self) -> bool:
        return len(self.path) >= 2

    @property
    def distance_nm(self) -> float | None:
        """Great-circle length of what is drawn, or nothing.

        ``None`` while any point on the track has no held position: a partial
        total is a smaller number than the route and a reader would take it
        for the route length.
        """
        if self.unplaced or len(self.path) < 2:
            return None
        return sum(
            great_circle_nm(a, b) for a, b in zip(self.path, self.path[1:])
        )

    def describe(self) -> str:
        parts = [f"{len(self.points)} points"]
        if self.checkable:
            parts.append(f"{self.resolved} of {self.checkable} legs resolved")
        length = self.distance_nm
        parts.append(
            f"{length:.0f} NM computed from the published positions"
            if length is not None
            else "length not computed — a point on it has no published position"
        )
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class Atlas:
    """Everything to be drawn, and everything that could not be."""

    basemap: Basemap
    areas: tuple[DrawnArea, ...] = ()
    routes: tuple[DrawnRoute, ...] = ()
    points: tuple[DrawnPoint, ...] = ()
    unplaced: tuple[str, ...] = ()
    """Named by a section and given no position or no boundary. Listed under
    the map, never placed on it."""

    track: DrawnTrack | None = None
    """The filed route over the structure. ``None`` where none was given,
    which prints differently from one nobody could draw."""

    regions: tuple[str, ...] = ()
    level_ft: float | None = None
    title: str = ""
    bounds: Bounds | None = None

    @property
    def firs(self) -> tuple[DrawnArea, ...]:
        return tuple(a for a in self.areas if a.layer == "fir")

    @property
    def terminals(self) -> tuple[DrawnArea, ...]:
        return tuple(a for a in self.areas if a.layer == "terminal")

    @property
    def hazards(self) -> tuple[DrawnArea, ...]:
        return tuple(a for a in self.areas if a.layer == "hazard")

    @property
    def open_edges(self) -> tuple[DrawnArea, ...]:
        """Areas whose edge is partly prose. Drawn open, never filled."""
        return tuple(a for a in self.areas if a.is_drawable and not a.closed)

    @property
    def is_complete(self) -> bool:
        """Whether everything named could be drawn, closed, in full.

        Never true because the picture looks full: an area drawn through the
        published half of its edge looks like an area.
        """
        return (
            bool(self.areas or self.routes or self.points)
            and not self.unplaced
            and not self.open_edges
            and not any(r.gaps for r in self.routes)
            # A sheet drawn entirely from a base AIP that a supplement has
            # superseded is a complete drawing of the wrong thing.
            and not self.superseded
        )

    @property
    def superseded(self) -> tuple[DrawnArea | DrawnRoute | DrawnPoint, ...]:
        """Everything drawn that a supplement in force bears on.

        What is drawn is the base AIP. Nothing here reads a value out of a
        supplement's prose, so the drawing is not wrong — it is no longer the
        whole answer, and that is a different thing to say.
        """
        return tuple(
            thing
            for group in (self.areas, self.routes, self.points)
            for thing in group
            if thing.supplements
        )

    def render(self) -> str:
        lines = [
            "ATLAS — ENR 2, 3, 4 and 5 on one sheet"
            + (f": {self.title}" if self.title else ""),
            _count(len(self.firs), "region")
            + "  ·  "
            + _count(len(self.terminals), "terminal area")
            + "  ·  "
            + _count(len(self.routes), "route")
            + "  ·  "
            + _count(len(self.points), "point")
            + "  ·  "
            + _count(len(self.hazards), "hazard area")
            + (f"  ·  at {self.level_ft:.0f} ft" if self.level_ft is not None else ""),
        ]
        if self.track is not None:
            lines += ["", "FILED ROUTE", f"  {self.track.describe()}"]
            if self.track.filed:
                lines.append(f"  as filed: {self.track.filed}")
            if self.track.unplaced:
                lines.append(
                    "  not drawn: " + ", ".join(self.track.unplaced)
                )
        if self.unplaced:
            lines += [
                "",
                "!! NAMED AND NOT DRAWN — no published position or boundary read",
                "   " + ", ".join(self.unplaced),
            ]
        if self.open_edges:
            lines += [
                "",
                "EDGE PARTLY DESCRIBED IN WORDS — drawn open, never filled",
            ]
            for area in self.open_edges:
                lines.append(
                    f"  {area.designator}: "
                    + _count(area.narrative, "edge")
                    + " the AIP gives no coordinates for"
                )
        superseded = [
            (thing.designator, thing.supplements)
            for group in (self.areas, self.routes, self.points)
            for thing in group
            if thing.supplements
        ]
        if superseded:
            lines += [
                "",
                "A SUPPLEMENT IS IN FORCE AGAINST THESE",
                "  What is drawn is the base AIP. A supplement outranks it, and",
                "  nothing here reads a value out of one — go and read it.",
            ]
            for designator, held in superseded:
                lines.append(f"  {designator}: SUP {', '.join(held)}")

        partial = [r for r in self.routes if r.gaps]
        if partial:
            lines += ["", "ROUTES DRAWN THROUGH FEWER POINTS THAN PUBLISHED"]
            for route in partial:
                lines.append(
                    f"  {route.designator}: "
                    + _count(route.gaps, "point")
                    + " with no published position"
                )
        lines += ["", NOT_AERONAUTICAL]
        return "\n".join(lines)


def _area_detail(volume: Airspace) -> str:
    parts = [volume.kind.value.upper().replace("_", " ")]
    if volume.name:
        parts.append(volume.name)
    if volume.airspace_class.value != "unclassified":
        parts.append(f"Class {volume.airspace_class.value.upper()}")
    low = f"{volume.lower_ft:.0f}" if volume.lower_ft is not None else "?"
    high = "UNL" if volume.is_unlimited_upper else (
        f"{volume.upper_ft:.0f}" if volume.upper_ft is not None else "?"
    )
    parts.append(f"{low}–{high} ft")
    if volume.unit:
        parts.append(volume.unit)
    if volume.frequency_mhz is not None:
        parts.append(f"{volume.frequency_mhz:.3f}")
    if volume.hours:
        parts.append(volume.hours)
    return "  ·  ".join(parts)


def _hazard_detail(hazard: Hazard) -> str:
    parts = [hazard.kind.value.upper().replace("_", " ")]
    if hazard.name:
        parts.append(hazard.name)
    low = f"{hazard.lower_ft:.0f}" if hazard.lower_ft is not None else "?"
    high = f"{hazard.upper_ft:.0f}" if hazard.upper_ft is not None else "?"
    parts.append(f"{low}–{high} ft")
    parts.append(hazard.activation.value.replace("_", " "))
    if hazard.activity:
        parts.append(hazard.activity)
    if hazard.authority:
        parts.append(f"ask {hazard.authority}")
    return "  ·  ".join(parts)


def _drawn_from(
    designator: str,
    layer: str,
    boundary: Boundary | None,
    *,
    label: str,
    detail: str,
    published_in: str,
    notams: int = 0,
    supplements: tuple[str, ...] = (),
) -> DrawnArea | None:
    if boundary is None or not boundary.is_held:
        return None
    rings = boundary.segments()
    if not rings:
        return None
    return DrawnArea(
        designator=designator,
        layer=layer,
        rings=rings,
        closed=boundary.is_closed,
        narrative=len(boundary.narrative_edges),
        arcs=boundary.arc_count,
        label=label or designator,
        detail=detail,
        published_in=published_in,
        notams=notams,
        supplements=tuple(dict.fromkeys(supplements)),
    )


def build_atlas(
    *,
    airspace: AirspaceStructure | None = None,
    structure: AtsStructure | None = None,
    navaids: NavaidRegister | None = None,
    hazards: HazardRegister | None = None,
    regions: Iterable[str] = (),
    routes: Iterable[str] = (),
    level_ft: float | None = None,
    notams: NotamRegister | None = None,
    at: datetime | None = None,
    supplements: SupplementRegister | None = None,
    on: date | None = None,
    filed: FiledRoute | None = None,
    basemap: Basemap | None = None,
    title: str = "",
) -> Atlas:
    """Assemble the whole en-route picture from what has been read.

    Each section contributes what it publishes and nothing else. A section not
    supplied contributes nothing, which is visibly different from a section
    that turned out to be empty — the counts in :meth:`Atlas.render` say which.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))
    named_routes = {normalise(r) for r in routes if str(r).strip()}
    unplaced: list[str] = []

    def against(key: str) -> int:
        if notams is None or at is None:
            return 0
        return len(notams.at(key, at))

    # A supplement outranks the AIP and is outranked by a NOTAM. It never
    # changes what is drawn — nothing here reads a value out of its prose — it
    # says the published value is no longer the whole answer.
    day = on or (at.date() if at is not None else None)

    def modified_by(key: str) -> tuple[str, ...]:
        if supplements is None or day is None:
            return ()
        return tuple(s.identifier for s, _ in supplements.at(key, day))

    def modifying(code: str) -> tuple[str, ...]:
        if supplements is None or day is None or not code:
            return ()
        return tuple(s.identifier for s, _ in supplements.for_section(code, day))

    # ---- ENR 2: the regions and the terminal areas inside them -----------
    areas: list[DrawnArea] = []
    for volume in (airspace.volumes if airspace is not None else ()):
        if wanted and volume.belongs_to not in wanted and volume.designator not in wanted:
            continue
        if level_ft is not None and volume.reaches(level_ft) is False:
            continue
        layer = "fir" if volume.kind.is_region else "terminal"
        drawn = _drawn_from(
            volume.designator,
            layer,
            volume.boundary,
            label=volume.designator,
            detail=_area_detail(volume),
            published_in=volume.source.document,
            notams=against(volume.key),
            supplements=modified_by(volume.key)
            + modifying(volume.source.locator.split(" row")[0].strip()),
        )
        if drawn is None:
            unplaced.append(f"{volume.designator} (ENR 2, no boundary read)")
            continue
        areas.append(drawn)

    # ---- ENR 5: what you may not enter -----------------------------------
    for hazard in (hazards.hazards if hazards is not None else ()):
        if wanted and hazard.region not in wanted:
            continue
        drawn = _drawn_from(
            hazard.designator,
            "hazard",
            hazard.boundary,
            label=hazard.designator,
            detail=_hazard_detail(hazard),
            published_in=hazard.source.document,
            notams=against(hazard.key),
            # ENR 5.1 replaced for a month names no area in its heading, and
            # that is the commonest section-wide supplement there is.
            supplements=modified_by(hazard.key)
            + modifying(hazard.source.locator.split(" row")[0].strip()),
        )
        if drawn is None:
            unplaced.append(f"{hazard.designator} (ENR 5, no boundary read)")
            continue
        areas.append(drawn)

    # ---- ENR 4: the points, and where they were published ----------------
    positions: dict[str, Position] = {}
    published_in: dict[str, str] = {}
    kinds: dict[str, str] = {}
    details: dict[str, str] = {}
    if structure is not None:
        for point in structure.points:
            held = point.position
            if held is None:
                continue
            positions[point.designator] = held
            published_in[point.designator] = point.source.document
            kinds[point.designator] = "fix"
            details[point.designator] = point.kind.value.replace("_", " ") + (
                f"  ·  {point.name}" if point.name else ""
            )
    if navaids is not None:
        for aid in navaids:
            held = aid.position
            if held is None:
                continue
            kinds.setdefault(aid.ident, "navaid")
            if aid.ident not in positions:
                positions[aid.ident] = held
                published_in[aid.ident] = aid.source.document
            details.setdefault(aid.ident, aid.describe())

    # ---- ENR 3: the routes through them ----------------------------------
    drawn_routes: list[DrawnRoute] = []
    through: dict[str, list[str]] = {}
    if structure is not None:
        in_scope = [
            r
            for r in structure.routes
            if (not named_routes or r in named_routes)
            and (
                not wanted
                or any(s.region in wanted for s in structure.on(r))
            )
        ]
        for designator in in_scope:
            profile = profile_for(structure, designator)
            if profile is None:
                continue
            if level_ft is not None and profile.admits(level_ft) is False:
                continue
            held = [p for p in profile.points if p in positions]
            for point in held:
                through.setdefault(point, []).append(designator)
            path: list[Position] = []
            for start, end in zip(held, held[1:]):
                leg = great_circle_path(
                    positions[start], positions[end], steps=LEG_STEPS
                )
                path.extend(leg if not path else leg[1:])
            drawn_routes.append(
                DrawnRoute(
                    designator=designator,
                    path=tuple(path),
                    gaps=len(profile.points) - len(held),
                    one_way=profile.is_one_way,
                    detail=profile.describe(),
                    published_in=(
                        structure.on(designator)[0].source.document
                        if structure.on(designator)
                        else ""
                    ),
                    notams=against(named(ATS_ROUTE, designator)),
                    supplements=modified_by(named(ATS_ROUTE, designator)),
                )
            )
            for point in profile.points:
                if point not in positions:
                    unplaced.append(f"{point} (on {designator}, no position read)")

    drawn_points = tuple(
        DrawnPoint(
            designator=designator,
            position=position,
            kind=kinds.get(designator, "fix"),
            routes=tuple(sorted(set(through.get(designator, ())))),
            detail=details.get(designator, ""),
            published_in=published_in.get(designator, ""),
            notams=against(f"FIX:{designator}") + against(f"NAVAID:{designator}"),
            supplements=modified_by(f"FIX:{designator}")
            + modified_by(f"NAVAID:{designator}"),
        )
        for designator, position in positions.items()
        # A point nothing draws through is still a published point, but a chart
        # of every name in a national table is a chart of nothing. Only the
        # ones the drawn routes use, plus every navaid, which is what an
        # en-route chart shows.
        if designator in through or kinds.get(designator) == "navaid"
    )

    # ---- the filed route over the structure ------------------------------
    track: DrawnTrack | None = None
    if filed is not None:
        walked: list[str] = []
        resolved = checkable = 0
        if structure is not None:
            expansion = expand(filed, structure)
            resolved, checkable = expansion.coverage
            for leg in expansion.legs:
                if not walked:
                    walked.append(leg.leg.start)
                if leg.segments:
                    # Through every published segment, not straight between the
                    # filed points: the airway is the route, and it bends.
                    walked.extend(segment.end for segment in leg.segments)
                else:
                    walked.append(leg.leg.end)
        else:
            walked = list(filed.points)
        walked = [p for i, p in enumerate(walked) if i == 0 or p != walked[i - 1]]

        held = [p for p in walked if p in positions]
        missing = [p for p in walked if p not in positions]
        path: list[Position] = []
        for start, end in zip(held, held[1:]):
            leg_path = great_circle_path(
                positions[start], positions[end], steps=LEG_STEPS
            )
            path.extend(leg_path if not path else leg_path[1:])
        track = DrawnTrack(
            points=tuple(walked),
            path=tuple(path),
            unplaced=tuple(dict.fromkeys(missing)),
            filed=filed.text,
            resolved=resolved,
            checkable=checkable,
        )
        for point in track.unplaced:
            unplaced.append(f"{point} (on the filed route, no position read)")

    everything = [p.position for p in drawn_points]
    everything += [p for route in drawn_routes for p in route.path]
    if track is not None:
        everything += list(track.path)
    everything += [p for area in areas for ring in area.rings for p in ring]
    window = bounds_of(everything)
    padded = window.padded(0.06) if window else None

    return Atlas(
        basemap=(basemap or load_basemap()).clipped(padded),
        areas=tuple(areas),
        routes=tuple(drawn_routes),
        points=drawn_points,
        track=track,
        unplaced=tuple(dict.fromkeys(unplaced)),
        regions=wanted,
        level_ft=level_ft,
        title=title or (", ".join(wanted) if wanted else ""),
        bounds=padded,
    )


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _info(
    name: str,
    kind: str,
    where: str,
    detail: str,
    published: str,
    notams: int,
    supplements: tuple[str, ...] = (),
) -> str:
    payload = {
        "name": name,
        "kind": kind,
        "position": where,
        "detail": detail,
        "published": published or "source not recorded",
        "notams": notams,
    }
    if supplements:
        # Named, not counted. A reader has to go and read the supplement, and
        # a number does not tell them which one.
        payload["supplements"] = ", ".join(supplements)
    return _escape(json.dumps(payload))


def atlas_svg(atlas: Atlas, *, width: float = 1100.0, height: float = 680.0) -> str:
    """Draw the atlas as one ``<svg>``, one group per layer."""
    if atlas.bounds is None:
        return (
            '<svg class="at" viewBox="0 0 1100 200" width="100%" role="img" '
            'aria-label="Nothing to draw" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<text x="24" y="100" class="at-note">No published position or '
            "boundary has been read for anything in scope. Nothing is drawn, "
            "which is a coverage gap and not empty airspace.</text></svg>"
        )

    box = atlas.bounds
    span = max(box.width, box.height * (width / height))
    span_y = span * (height / width)
    cx = (box.min_x + box.max_x) / 2.0
    cy = (box.min_y + box.max_y) / 2.0

    def xy(position: Position) -> tuple[float, float]:
        x, y = mercator(position)
        return (
            width * (0.5 + (x - cx) / span),
            height * (0.5 - (y - cy) / span_y),
        )

    def points_of(line: Sequence[Position]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in map(xy, line))

    out: list[str] = [
        f'<svg class="at" id="at" viewBox="0 0 {width:.0f} {height:.0f}" '
        'width="100%" role="img" '
        f'aria-label="En-route atlas{": " + _escape(atlas.title) if atlas.title else ""}" '
        'xmlns="http://www.w3.org/2000/svg">',
        '<g id="at-pan">',
    ]

    # Geography first and underneath, in a colour that reads as background.
    # The graticule, first and underneath everything. Computed from the
    # projection, so it is the one layer here that cannot be out of date.
    south_west = unmercator(box.min_x, box.min_y)
    north_east = unmercator(box.max_x, box.max_y)
    out.append('<g class="at-layer" data-layer="graticule">')
    lat_step = _spacing_for(abs(north_east.latitude - south_west.latitude))
    lon_step = _spacing_for(abs(north_east.longitude - south_west.longitude))
    first = math.ceil(south_west.latitude / lat_step) * lat_step
    parallel = first
    while parallel <= north_east.latitude + 1e-9:
        line = [
            Position(latitude=parallel, longitude=lon)
            for lon in _steps(south_west.longitude, north_east.longitude, 24)
        ]
        out.append(f'<polyline points="{points_of(line)}" class="at-grid"/>')
        _, gy = xy(Position(latitude=parallel, longitude=south_west.longitude))
        out.append(
            f'<text x="4" y="{gy - 3:.1f}" class="at-grid-label">'
            f"{_label_degrees(parallel, is_latitude=True)}</text>"
        )
        parallel += lat_step
    first = math.ceil(south_west.longitude / lon_step) * lon_step
    meridian = first
    while meridian <= north_east.longitude + 1e-9:
        line = [
            Position(latitude=lat, longitude=meridian)
            for lat in _steps(south_west.latitude, north_east.latitude, 24)
        ]
        out.append(f'<polyline points="{points_of(line)}" class="at-grid"/>')
        gx, _ = xy(Position(latitude=south_west.latitude, longitude=meridian))
        out.append(
            f'<text x="{gx + 3:.1f}" y="{height - 6:.1f}" class="at-grid-label">'
            f"{_label_degrees(meridian, is_latitude=False)}</text>"
        )
        meridian += lon_step
    out.append("</g>")

    out.append('<g class="at-layer" data-layer="basemap">')
    for line in atlas.basemap.borders:
        out.append(f'<polyline points="{points_of(line)}" class="at-border"/>')
    for line in atlas.basemap.coastline:
        out.append(f'<polyline points="{points_of(line)}" class="at-coast"/>')
    out.append("</g>")

    for layer, css in (("hazard", "at-hazard"), ("fir", "at-fir"), ("terminal", "at-terminal")):
        out.append(f'<g class="at-layer" data-layer="{layer}">')
        for area in atlas.areas:
            if area.layer != layer or not area.is_drawable:
                continue
            info = _info(
                area.designator,
                layer,
                (
                    "edge fully published"
                    if area.closed
                    else f"{area.narrative} edge(s) described in words"
                ),
                area.detail,
                area.published_in,
                area.notams,
                area.supplements,
            )
            classes = css + ("" if area.closed else f" {css}-open")
            out.append(
                f'<g class="at-area {classes}" tabindex="0" role="button" '
                f'data-info="{info}" aria-label="{_escape(area.designator)}">'
            )
            for ring in area.rings:
                if len(ring) < 2:
                    continue
                # Filled only when the AIP closed it. A filled shape reads as a
                # definite extent, and the extent is what a prose edge withheld.
                tag = "polygon" if area.closed else "polyline"
                out.append(f'<{tag} points="{points_of(ring)}" class="{css}-line"/>')
            head = next((r[0] for r in area.rings if r), None)
            if head is not None:
                hx, hy = xy(head)
                out.append(
                    f'<text x="{hx + 6:.1f}" y="{hy - 6:.1f}" class="at-label '
                    f'{css}-label">{_escape(area.label)}'
                    f'{"" if area.closed else " (edge partly in words)"}</text>'
                )
            out.append("</g>")
        out.append("</g>")

    out.append('<g class="at-layer" data-layer="routes">')
    for route in atlas.routes:
        if not route.is_drawable:
            continue
        info = _info(
            route.designator,
            "ATS route",
            f"{len(route.path)} points drawn"
            + (f", {route.gaps} with no position" if route.gaps else ""),
            route.detail,
            route.published_in,
            route.notams,
            route.supplements,
        )
        classes = "at-route" + (" at-route-notam" if route.notams else "")
        out.append(
            f'<g class="at-routeg" tabindex="0" role="button" data-info="{info}" '
            f'aria-label="{_escape(route.designator)}">'
        )
        out.append(f'<polyline points="{points_of(route.path)}" class="{classes}"/>')
        hx, hy = xy(route.path[0])
        out.append(
            f'<text x="{hx + 6:.1f}" y="{hy + 12:.1f}" class="at-label at-route-label">'
            f"{_escape(route.designator)}</text>"
        )
        out.append("</g>")
    out.append("</g>")

    if atlas.track is not None and atlas.track.is_drawable:
        out.append('<g class="at-layer" data-layer="track">')
        info = _info(
            "Filed route",
            "filed route",
            f"{len(atlas.track.points)} points"
            + (
                f", {len(atlas.track.unplaced)} with no position"
                if atlas.track.unplaced
                else ""
            ),
            atlas.track.describe(),
            atlas.track.filed or "as filed",
            0,
        )
        out.append(
            f'<g class="at-trackg" tabindex="0" role="button" data-info="{info}" '
            'aria-label="Filed route">'
        )
        out.append(
            f'<polyline points="{points_of(atlas.track.path)}" class="at-track"/>'
        )
        out.append("</g></g>")

    out.append('<g class="at-layer" data-layer="points">')
    for point in atlas.points:
        x, y = xy(point.position)
        info = _info(
            point.designator,
            point.kind,
            point.position.describe(),
            point.detail
            + (
                "  ·  on " + ", ".join(point.routes)
                if point.routes
                else "  ·  on no drawn route"
            ),
            point.published_in,
            point.notams,
            point.supplements,
        )
        out.append(
            f'<g class="at-point at-{point.kind}" tabindex="0" role="button" '
            f'data-info="{info}" aria-label="{_escape(point.designator)}">'
        )
        if point.kind == "navaid":
            out.append(
                f'<polygon points="{x:.1f},{y - 5:.1f} {x + 5:.1f},{y:.1f} '
                f'{x:.1f},{y + 5:.1f} {x - 5:.1f},{y:.1f}" class="at-mark"/>'
            )
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" class="at-mark"/>')
        if point.notams:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" class="at-ring"/>')
        out.append(
            f'<text x="{x:.1f}" y="{y - 9:.1f}" class="at-label at-point-label" '
            f'text-anchor="middle">{_escape(point.designator)}</text>'
        )
        out.append("</g>")
    out.append("</g>")

    out.append("</g>")

    # The scale bar sits outside the pan group: it describes the drawing as
    # published, and a reader who has zoomed in has changed the drawing.
    middle_lat = (south_west.latitude + north_east.latitude) / 2.0
    left = Position(latitude=middle_lat, longitude=south_west.longitude)
    right = Position(latitude=middle_lat, longitude=north_east.longitude)
    across_nm = great_circle_nm(left, right)
    if across_nm > 0:
        bar_nm = _round_distance(across_nm / 4.0)
        x0, _ = xy(left)
        x1, _ = xy(right)
        pixels = abs(x1 - x0) * (bar_nm / across_nm)
        base_y = height - 18.0
        out.append('<g class="at-scale">')
        out.append(
            f'<line x1="16" y1="{base_y:.1f}" x2="{16 + pixels:.1f}" '
            f'y2="{base_y:.1f}" class="at-scale-bar"/>'
        )
        for tick in (16.0, 16.0 + pixels):
            out.append(
                f'<line x1="{tick:.1f}" y1="{base_y - 4:.1f}" x2="{tick:.1f}" '
                f'y2="{base_y + 4:.1f}" class="at-scale-bar"/>'
            )
        # Named for the latitude it is true at. On Mercator the scale grows
        # with latitude, so a bar with no latitude on it is wrong everywhere
        # except one line the reader cannot see.
        out.append(
            f'<text x="16" y="{base_y - 7:.1f}" class="at-scale-label">'
            f"{bar_nm:.0f} NM at "
            f"{_label_degrees(middle_lat, is_latitude=True)}</text>"
        )
        out.append("</g>")

    out.append("</svg>")
    return "\n".join(out)


def _steps(start: float, end: float, count: int) -> list[float]:
    """``count`` values from start to end inclusive, for drawing a curve."""
    if count < 2:
        return [start, end]
    span = end - start
    return [start + span * i / (count - 1) for i in range(count)]


ATLAS_CSS = """
.at-wrap { --at-ink: #16202b; --at-muted: #64757f; --at-ground: #eef1f3;
  --at-coast: #9fb3bd; --at-border: #c6d2d8; --at-fir: #1b6ca8;
  --at-terminal: #7a5ea8; --at-hazard: #c0392b; --at-route: #2f7d5f; --at-flight: #b8541c;
  --at-mark: #16202b; --at-card: #ffffff; }
@media (prefers-color-scheme: dark) {
  .at-wrap:not([data-theme="light"]) { --at-ink: #e6edf3; --at-muted: #93a4b3;
    --at-ground: #101821; --at-coast: #48606d; --at-border: #2c3d47;
    --at-fir: #63b3ed; --at-terminal: #b39ae0; --at-hazard: #e5705f;
    --at-route: #63c69b; --at-flight: #f0913f; --at-mark: #cfd9e2; --at-card: #18222b; } }
.at-wrap { background: var(--at-ground); border: 1px solid var(--at-border);
  border-radius: 3px; position: relative; overflow: hidden; }
.at { display: block; touch-action: none; cursor: grab; }
.at:active { cursor: grabbing; }
.at-coast { fill: none; stroke: var(--at-coast); stroke-width: 0.8; }
.at-border { fill: none; stroke: var(--at-border); stroke-width: 0.6;
  stroke-dasharray: 3 3; }
.at-fir-line { fill: rgba(27,108,168,0.06); stroke: var(--at-fir);
  stroke-width: 1.6; }
.at-fir-open-line, .at-fir-open .at-fir-line { fill: none;
  stroke-dasharray: 7 4; }
.at-terminal-line { fill: rgba(122,94,168,0.09); stroke: var(--at-terminal);
  stroke-width: 1.2; }
.at-terminal-open .at-terminal-line { fill: none; stroke-dasharray: 6 4; }
.at-hazard-line { fill: rgba(192,57,43,0.14); stroke: var(--at-hazard);
  stroke-width: 1.2; }
.at-hazard-open .at-hazard-line { fill: none; stroke-dasharray: 5 4; }
.at-route { fill: none; stroke: var(--at-route); stroke-width: 1.6; }
.at-track { fill: none; stroke: var(--at-flight); stroke-width: 2.6;
  stroke-linejoin: round; stroke-linecap: round; }
.at-trackg { cursor: pointer; }
.at-trackg:hover .at-track, .at-trackg:focus-visible .at-track {
  stroke-width: 4; }
.at-route-notam { stroke-dasharray: 6 3; }
.at-mark { fill: var(--at-mark); }
.at-ring { fill: none; stroke: var(--at-hazard); stroke-width: 1.4; }
.at-label { font: 10px/1.2 ui-sans-serif, system-ui, sans-serif;
  fill: var(--at-muted); paint-order: stroke; stroke: var(--at-ground);
  stroke-width: 2.5px; }
.at-fir-label { fill: var(--at-fir); font-weight: 600; }
.at-terminal-label { fill: var(--at-terminal); }
.at-hazard-label { fill: var(--at-hazard); font-weight: 600; }
.at-route-label { fill: var(--at-route); font-weight: 600; }
.at-point-label { fill: var(--at-ink); }
.at-note { font: 12px ui-sans-serif, system-ui, sans-serif; fill: var(--at-ink); }
.at-grid { fill: none; stroke: var(--at-border); stroke-width: 0.5;
  stroke-dasharray: 2 4; }
.at-grid-label { font: 9px ui-monospace, SFMono-Regular, Menlo, monospace;
  fill: var(--at-muted); paint-order: stroke; stroke: var(--at-ground);
  stroke-width: 2.5px; }
.at-scale-bar { stroke: var(--at-ink); stroke-width: 1.4; }
.at-scale-label { font: 10px ui-monospace, SFMono-Regular, Menlo, monospace;
  fill: var(--at-ink); paint-order: stroke; stroke: var(--at-ground);
  stroke-width: 3px; }
.at-area, .at-routeg, .at-point { cursor: pointer; }
.at-area:hover .at-fir-line, .at-area:focus-visible .at-fir-line,
.at-area:hover .at-terminal-line, .at-area:focus-visible .at-terminal-line,
.at-area:hover .at-hazard-line, .at-area:focus-visible .at-hazard-line,
.at-routeg:hover .at-route, .at-routeg:focus-visible .at-route {
  stroke-width: 3; }
.at-controls { position: absolute; top: 8px; right: 8px; display: flex;
  flex-wrap: wrap; gap: 4px; justify-content: flex-end; max-width: 60%; }
.at-controls button { font: 500 11px ui-sans-serif, system-ui, sans-serif;
  padding: 4px 8px; border: 1px solid var(--at-border); border-radius: 2px;
  background: var(--at-card); color: var(--at-muted); cursor: pointer; }
.at-controls button[aria-pressed="true"] { color: var(--at-ink);
  border-color: var(--at-ink); }
.at-panel { position: absolute; left: 8px; bottom: 8px; max-width: 340px;
  background: var(--at-card); border: 1px solid var(--at-border);
  border-radius: 3px; padding: 10px 12px;
  font: 12px/1.45 ui-sans-serif, system-ui, sans-serif; color: var(--at-ink); }
.at-panel h3 { margin: 0 0 6px; font-size: 13px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.at-panel dl { margin: 0; display: grid; grid-template-columns: auto 1fr;
  gap: 2px 10px; }
.at-panel dt { color: var(--at-muted); }
.at-panel dd { margin: 0; }
"""


ATLAS_JS = """
(function () {
  var svg = document.getElementById('at');
  if (!svg) return;
  var pan = document.getElementById('at-pan');
  var at = { x: 0, y: 0, k: 1 }, drag = null;
  function apply() {
    pan.setAttribute('transform',
      'translate(' + at.x + ',' + at.y + ') scale(' + at.k + ')');
  }
  svg.addEventListener('pointerdown', function (e) {
    drag = { x: e.clientX - at.x, y: e.clientY - at.y };
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove', function (e) {
    if (!drag) return;
    at.x = e.clientX - drag.x; at.y = e.clientY - drag.y; apply();
  });
  svg.addEventListener('pointerup', function () { drag = null; });
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    var box = svg.getBoundingClientRect();
    var scale = svg.viewBox.baseVal.width / box.width;
    var px = (e.clientX - box.left) * scale, py = (e.clientY - box.top) * scale;
    var next = Math.min(40, Math.max(0.5, at.k * (e.deltaY < 0 ? 1.15 : 0.87)));
    at.x = px - (px - at.x) * (next / at.k);
    at.y = py - (py - at.y) * (next / at.k);
    at.k = next; apply();
  }, { passive: false });

  document.querySelectorAll('.at-controls button[data-layer]').forEach(
    function (b) {
      b.addEventListener('click', function () {
        var on = b.getAttribute('aria-pressed') !== 'true';
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        document.querySelectorAll('[data-layer="' + b.dataset.layer + '"]')
          .forEach(function (g) { g.hidden = !on; });
      });
    });
  var reset = document.querySelector('.at-controls button[data-reset]');
  if (reset) reset.addEventListener('click', function () {
    at = { x: 0, y: 0, k: 1 }; apply();
  });

  var panel = document.querySelector('.at-panel');
  function row(list, term, value) {
    var dt = document.createElement('dt'); dt.textContent = term;
    var dd = document.createElement('dd'); dd.textContent = value;
    list.appendChild(dt); list.appendChild(dd);
  }
  function show(info) {
    if (!panel) return;
    // Nodes, not markup: every value is text read out of a publication.
    panel.textContent = '';
    var h = document.createElement('h3'); h.textContent = info.name;
    panel.appendChild(h);
    var dl = document.createElement('dl');
    row(dl, 'type', info.kind);
    row(dl, 'where', info.position);
    if (info.detail) row(dl, 'published', info.detail);
    row(dl, 'source', info.published);
    if (info.supplements) row(dl, 'supplement', info.supplements + ' in force');
    if (info.notams) row(dl, 'NOTAM', info.notams + ' in force');
    panel.appendChild(dl);
  }
  document.querySelectorAll('[data-info]').forEach(function (node) {
    function open(e) { if (e) e.stopPropagation(); show(JSON.parse(node.dataset.info)); }
    node.addEventListener('click', open);
    node.addEventListener('focus', open);
    node.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); }
    });
  });
})();
"""


def atlas_html(atlas: Atlas) -> str:
    """The atlas as a standalone page: no library, no network, no runtime."""
    layers = (
        ("graticule", "Grid"),
        ("basemap", "Coast"),
        ("fir", "FIR/UIR"),
        ("terminal", "TMA/CTR"),
        ("routes", "ATS routes"),
        ("track", "Filed route"),
        ("points", "Points"),
        ("hazard", "P/R/D"),
    )
    buttons = "".join(
        f'<button type="button" data-layer="{key}" aria-pressed="true">'
        f"{label}</button>"
        for key, label in layers
    )
    notes: list[str] = []
    if atlas.unplaced:
        notes.append(
            "<p><strong>Named and not drawn.</strong> "
            + ", ".join(_escape(u) for u in atlas.unplaced)
            + " — the AIP names these and no position or boundary was read for "
            "them. A point in the wrong place is a map; a point missing is a "
            "gap.</p>"
        )
    if atlas.open_edges:
        names = ", ".join(_escape(a.designator) for a in atlas.open_edges)
        notes.append(
            "<p><strong>Edge partly described in words.</strong> "
            + names
            + " are drawn as the pieces the AIP gave coordinates for, dashed "
            "and unfilled. A filled shape reads as a definite extent, and the "
            "extent is exactly what that publication did not give.</p>"
        )
    return (
        f"<title>En-route atlas{': ' + _escape(atlas.title) if atlas.title else ''}"
        "</title>\n<style>"
        + ATLAS_CSS
        + "\nbody{margin:0;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;"
        "background:var(--page,#eef1f3);color:#16202b;padding:20px}"
        "@media(prefers-color-scheme:dark){body{background:#0c1218;color:#e6edf3}}"
        ".at-notes{max-width:70ch;margin:16px 0 0;font-size:13px}"
        "</style>\n"
        '<div class="at-wrap">'
        + atlas_svg(atlas)
        + f'<div class="at-controls">{buttons}'
        '<button type="button" data-reset>Reset</button></div>'
        '<aside class="at-panel" aria-live="polite"><h3>Click anything</h3>'
        "<p>An area, a route or a point gives what the AIP published about it, "
        "and which document that was.</p></aside></div>"
        f'<div class="at-notes">{"".join(notes)}'
        f"<p>{_escape(NOT_AERONAUTICAL)} {_escape(atlas.basemap.attribution)}.</p>"
        "<p>Nothing here answers whether a point is inside an area. A drawing "
        "puts two things on the same sheet; it does not make one contain the "
        "other.</p></div>"
        "<script>" + ATLAS_JS + "</script>"
    )
