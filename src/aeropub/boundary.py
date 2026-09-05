"""The geometry an AIP publishes, and the part of it that is prose.

ENR 2 and ENR 5 describe airspace by walking its edge. A boundary in an AIP
reads like this::

    251500N 0510000E - 254500N 0522000E - thence a clockwise arc of 30 NM
    radius centred on 251500N 0511500E to 250000N 0505000E - thence along
    the State boundary to the point of origin

Three of those four elements are coordinates and arithmetic. The fourth —
*thence along the State boundary* — is a reference to something the AIP does
not give coordinates for, and it is the whole reason this module is shaped the
way it is.

The narrative edge, and why it is not a line
---------------------------------------------
A drawing that joined the two ends of "along the State boundary" with a
straight line would put a border where no State published one, and it would
look exactly as authoritative as the parts that were published. So a narrative
edge is kept as an edge, carried through to the drawing, and **breaks the
ring**: :meth:`Boundary.segments` returns the published pieces as separate open
polylines, and :meth:`Boundary.outline` returns a closed ring only when every
edge came with coordinates.

An area drawn with a visible gap is a correct drawing of an area whose edge is
partly described in words. A closed one is a lie with a nice shape.

Arcs are drawn, never resolved
-------------------------------
An arc of N NM about a centre is exact as published and approximate as drawn:
a polyline through it is a chord sequence, always inside the true arc. The
count of arcs approximated travels with the boundary so a drawing can say how
much of its edge is stepped rather than published.

What this module will not do
-----------------------------
**There is no containment test here, and there must not be.** ``is this point
inside this area`` is the single most dangerous question this platform could
answer, because the answer looks the same whether the boundary was fully
published, partly prose, or stitched across an arc at twelve degrees a step.
Airspace is entered on a clearance and a chart, not on a point-in-polygon
result computed from a table somebody typed. This module draws what was
published and stops.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from aeropub.geo import (
    Bounds,
    CoordinateError,
    Position,
    bounds_of,
    destination,
    great_circle_nm,
    initial_bearing,
    parse_coordinate,
)

__all__ = [
    "Boundary",
    "BoundaryEdge",
    "Circle",
    "EdgeKind",
    "boundary_from_points",
    "parse_boundary",
    "read_edges",
]

#: Degrees of arc per drawn step. Twelve segments to a quadrant keeps a 30 NM
#: arc within about a tenth of a mile of the true edge, which is finer than a
#: boundary is published to and far finer than it can be flown.
ARC_STEP_DEG = 7.5

#: Steps around a full circle. A circle is a boundary a State publishes as a
#: radius, so this is only a drawing choice.
CIRCLE_STEPS = 72


class EdgeKind(str, Enum):
    """How the boundary gets from the previous point to this one."""

    GREAT_CIRCLE = "great_circle"
    """A direct line. What an AIP means by "thence to", and what it draws as on
    a chart: the geodesic, not a straight line on the projection."""

    PARALLEL = "parallel"
    """Along a parallel of latitude — "thence east along parallel 24N". Not the
    same line as the geodesic, and on a long edge the two are miles apart."""

    ARC = "arc"
    """A circular arc about a published centre, at a published radius."""

    NARRATIVE = "narrative"
    """Described in words, with no coordinates given: "along the State
    boundary", "along the coastline", "along the FIR boundary". Carried, never
    guessed at, and it breaks the ring."""

    @property
    def is_drawable(self) -> bool:
        """Whether the edge can be drawn from what the AIP published."""
        return self is not EdgeKind.NARRATIVE


@dataclass(frozen=True, slots=True)
class BoundaryEdge:
    """One step along the edge of an airspace, as published."""

    kind: EdgeKind = EdgeKind.GREAT_CIRCLE
    to: Position | None = None
    """Where this edge ends. ``None`` only for a narrative edge whose far end
    the publication did not give either."""

    centre: Position | None = None
    radius_nm: float | None = None
    clockwise: bool | None = None
    text: str = ""
    """As published. For a narrative edge this is the only content there is,
    and it is what a reader has to act on."""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EdgeKind):
            raise TypeError("BoundaryEdge.kind must be an EdgeKind")
        if self.kind is EdgeKind.ARC:
            if self.centre is None or self.radius_nm is None:
                raise ValueError(
                    "an arc edge needs both a centre and a radius. An arc "
                    "without them is a line somebody would draw straight."
                )
            if self.clockwise is None:
                raise ValueError(
                    "an arc edge needs a direction: the two ways round a "
                    "circle enclose different airspace."
                )
            if self.radius_nm <= 0:
                raise ValueError("an arc radius must be positive")
        if self.kind is not EdgeKind.NARRATIVE and self.to is None:
            raise ValueError(
                f"a {self.kind.value} edge must end somewhere. Only a "
                "narrative edge may have no published end point."
            )
        if self.kind is EdgeKind.NARRATIVE and not self.text.strip():
            raise ValueError(
                "a narrative edge must carry the words it was published as — "
                "they are the only content it has."
            )

    def describe(self) -> str:
        if self.kind is EdgeKind.NARRATIVE:
            return f"thence {self.text}"
        assert self.to is not None
        if self.kind is EdgeKind.ARC and self.centre is not None:
            way = "clockwise" if self.clockwise else "anticlockwise"
            return (
                f"thence a {way} arc of {self.radius_nm:g} NM centred on "
                f"{self.centre.describe()} to {self.to.describe()}"
            )
        if self.kind is EdgeKind.PARALLEL:
            return f"thence along the parallel to {self.to.describe()}"
        return f"thence to {self.to.describe()}"


@dataclass(frozen=True, slots=True)
class Circle:
    """An airspace published as a radius about a point.

    The common form for a control zone and for most danger areas, and the one
    case where the published description is complete in two numbers.
    """

    centre: Position
    radius_nm: float

    def __post_init__(self) -> None:
        if self.radius_nm <= 0:
            raise ValueError("Circle.radius_nm must be positive")

    def outline(self, *, steps: int = CIRCLE_STEPS) -> tuple[Position, ...]:
        """The circle as a drawable ring, closed.

        A polyline, and therefore an approximation of a circle that is exact
        as published. Every vertex is on the true edge and every chord is
        inside it.
        """
        walked = [
            destination(self.centre, i * 360.0 / steps, self.radius_nm)
            for i in range(steps)
        ]
        return tuple(walked) + (walked[0],)

    def describe(self) -> str:
        return (
            f"a circle of {self.radius_nm:g} NM radius centred on "
            f"{self.centre.describe()}"
        )


def _arc_points(
    start: Position, edge: BoundaryEdge, *, step_deg: float = ARC_STEP_DEG
) -> list[Position]:
    """Walk an arc from where we are to where the edge ends.

    The radius used is the published one, at both ends. Where the published
    end point does not sit exactly on that radius — which happens, because
    boundaries are printed to the second and radii to the mile — the walk
    still ends at the published point rather than at the point the radius
    implies. The AIP's coordinate wins over the AIP's radius, because the next
    edge starts from the coordinate.
    """
    assert edge.centre is not None and edge.to is not None
    centre, radius = edge.centre, edge.radius_nm or 0.0
    first = initial_bearing(centre, start)
    last = initial_bearing(centre, edge.to)
    if edge.clockwise:
        sweep = (last - first) % 360.0
        direction = 1.0
    else:
        sweep = (first - last) % 360.0
        direction = -1.0
    if sweep == 0.0:
        # Start and end on the same bearing: the AIP means the whole way round.
        sweep = 360.0
    steps = max(2, int(math.ceil(sweep / step_deg)))
    walked = [
        destination(centre, first + direction * sweep * i / steps, radius)
        for i in range(1, steps)
    ]
    return walked + [edge.to]


def _parallel_points(start: Position, end: Position, *, steps: int = 12) -> list[Position]:
    """Walk along a parallel of latitude rather than along a great circle.

    "Thence east along parallel 24N" is a rhumb line, and on a long edge it
    is a different line from the geodesic by tens of miles. Drawn as a
    sequence of points at constant latitude so the projection bends it the way
    a chart does.
    """
    span = end.longitude - start.longitude
    # Take the short way round unless the publication really did cross the
    # antimeridian; a 350° edge along a parallel is a reading error.
    if abs(span) > 180.0:
        span -= math.copysign(360.0, span)
    walked = []
    for i in range(1, steps + 1):
        lon = start.longitude + span * i / steps
        walked.append(
            Position(
                latitude=start.latitude,
                longitude=(lon + 540.0) % 360.0 - 180.0,
            )
        )
    return walked


@dataclass(frozen=True, slots=True)
class Boundary:
    """The edge of one airspace, as the AIP walks it."""

    start: Position | None = None
    """Where the walk begins. ``None`` where the boundary is a circle, or
    where nothing readable was held."""

    edges: tuple[BoundaryEdge, ...] = ()
    circle: Circle | None = None
    published_as: str = ""
    """The description as printed, kept whole. A reader who disagrees with how
    this was read needs the words, not our reading of them."""

    def __post_init__(self) -> None:
        if self.circle is not None and self.edges:
            raise ValueError(
                "a boundary is published as a circle or as a walk, not both. "
                "Holding both would leave two different shapes with equal "
                "claim to being the airspace."
            )
        if self.edges and self.start is None:
            raise ValueError(
                "a walked boundary must say where the walk starts — the first "
                "edge goes from somewhere."
            )

    @property
    def is_held(self) -> bool:
        """Whether anything drawable was read at all."""
        return self.circle is not None or bool(self.edges)

    @property
    def narrative_edges(self) -> tuple[BoundaryEdge, ...]:
        """The parts described in words, with no coordinates published."""
        return tuple(e for e in self.edges if e.kind is EdgeKind.NARRATIVE)

    @property
    def arc_count(self) -> int:
        """Arcs on this boundary, each drawn as a stepped approximation."""
        return sum(1 for e in self.edges if e.kind is EdgeKind.ARC)

    @property
    def is_closed(self) -> bool:
        """Whether the published coordinates close the ring on their own.

        False the moment any edge is narrative. A boundary partly described in
        words has no closed shape that came from the AIP, and this is the
        property everything downstream asks before drawing a filled area.
        """
        if self.circle is not None:
            return True
        if not self.edges or self.start is None:
            return False
        if self.narrative_edges:
            return False
        last = self.edges[-1].to
        if last is None:
            return False
        return great_circle_nm(last, self.start) < 0.5

    def segments(self, *, step_deg: float = ARC_STEP_DEG) -> tuple[tuple[Position, ...], ...]:
        """The published parts of the edge, as open polylines.

        A narrative edge ends one segment and the next published edge starts
        another, so the drawing shows exactly what the State gave coordinates
        for and stops where the words begin. That gap is the finding.
        """
        if self.circle is not None:
            return (self.circle.outline(),)
        if self.start is None or not self.edges:
            return ()

        runs: list[list[Position]] = []
        current: list[Position] = [self.start]
        at = self.start
        for edge in self.edges:
            if edge.kind is EdgeKind.NARRATIVE:
                if len(current) > 1:
                    runs.append(current)
                if edge.to is None:
                    current = []
                    continue
                current = [edge.to]
                at = edge.to
                continue
            assert edge.to is not None
            if edge.kind is EdgeKind.ARC:
                current.extend(_arc_points(at, edge, step_deg=step_deg))
            elif edge.kind is EdgeKind.PARALLEL:
                current.extend(_parallel_points(at, edge.to))
            else:
                current.append(edge.to)
            at = edge.to
        if len(current) > 1:
            runs.append(current)
        return tuple(tuple(run) for run in runs)

    def outline(self, *, step_deg: float = ARC_STEP_DEG) -> tuple[Position, ...]:
        """The boundary as one closed ring, or nothing.

        Empty unless :attr:`is_closed`. There is no partial ring: joining
        across a narrative edge would draw a border nobody published, and it
        would look exactly as authoritative as the rest of the shape.
        """
        if not self.is_closed:
            return ()
        runs = self.segments(step_deg=step_deg)
        if not runs:
            return ()
        ring = list(runs[0])
        if self.circle is None and ring and ring[-1] != ring[0]:
            ring.append(ring[0])
        return tuple(ring)

    @property
    def bounds(self) -> Bounds | None:
        """The window this boundary occupies, for fitting a drawing to it."""
        return bounds_of(p for run in self.segments() for p in run)

    def describe(self) -> str:
        if self.circle is not None:
            return self.circle.describe()
        if not self.edges:
            return "no boundary read"
        parts = [f"{len(self.edges)} edges"]
        if self.arc_count:
            parts.append(f"{self.arc_count} arcs, drawn stepped")
        if self.narrative_edges:
            parts.append(
                f"{len(self.narrative_edges)} described in words: "
                + "; ".join(e.text for e in self.narrative_edges)
            )
            parts.append("the ring does not close from published coordinates")
        elif not self.is_closed:
            parts.append("the walk does not return to its start")
        return "  ·  ".join(parts)


def boundary_from_points(
    points: Sequence[Position], *, published_as: str = ""
) -> Boundary:
    """A boundary that is nothing but a list of corners joined directly.

    The common simple case, and a convenience so a caller with a coordinate
    list does not have to build edges by hand.
    """
    if len(points) < 3:
        raise ValueError(
            "a boundary needs at least three points. Two points are a line, "
            "and a line does not enclose airspace."
        )
    rest = points[1:]
    # A repeated final point is how a coordinate list usually closes; keeping
    # it would put a zero-length edge on the ring.
    if len(rest) > 1 and rest[-1] == points[0]:
        rest = rest[:-1]
    edges = [BoundaryEdge(kind=EdgeKind.GREAT_CIRCLE, to=p) for p in rest]
    edges.append(BoundaryEdge(kind=EdgeKind.GREAT_CIRCLE, to=points[0]))
    return Boundary(
        start=points[0], edges=tuple(edges), published_as=published_as
    )


# --------------------------------------------------------------------------
# Reading a published description
# --------------------------------------------------------------------------

#: A coordinate pair as an AIP prints it, in either order of separator.
_PAIR = re.compile(
    r"(?P<lat>\d{6}(?:\.\d+)?[NS])\s*[/, ]?\s*(?P<lon>\d{7}(?:\.\d+)?[EW])"
)

_ARC = re.compile(
    r"(?P<way>clockwise|anti-?clockwise|counter-?clockwise)\s+arc"
    r".{0,40}?(?P<radius>\d+(?:\.\d+)?)\s*(?:NM|nm)"
    r".{0,40}?cent(?:re|er)(?:d|ed)?\s+(?:on|at)\s+"
    r"(?P<clat>\d{6}(?:\.\d+)?[NS])\s*[/, ]?\s*(?P<clon>\d{7}(?:\.\d+)?[EW])",
    re.IGNORECASE | re.DOTALL,
)

_ALONG = re.compile(
    r"along\s+(?:the\s+)?(?P<what>[a-z ]{0,40}?)\s*"
    r"(?:boundary|border|coast(?:line)?|shore(?:line)?|median\s+line)",
    re.IGNORECASE,
)

#: Words that join two coordinates and mean "a direct line". Everything else
#: between two coordinates is a clause somebody wrote for a reason, and this
#: module will not decide it meant nothing.
_JOINING_WORDS = frozenset(
    {"thence", "to", "then", "direct", "dct", "and", "a", "point", "of", "origin"}
)


def read_edges(text: str) -> tuple[Position | None, tuple[BoundaryEdge, ...]]:
    """Read a published boundary description into a start point and edges.

    Deliberately conservative. What it recognises is the coordinate pair, the
    arc, and the phrases that say the edge follows something the AIP has not
    given coordinates for. **Everything else between two coordinates becomes a
    narrative edge**, which is the safe direction to fail in: an unread clause
    turns into a visible gap in the drawing rather than into a straight line
    through airspace nobody described that way.

    Returns the start and the edges. A caller that wants the whole thing with
    its citation uses :func:`parse_boundary`.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return None, ()

    # Arc clauses first: an arc's centre is a published coordinate and it is
    # *not* a corner of the boundary. Reading it as one puts a vertex in the
    # middle of the airspace and turns one arc into two straight lines.
    arcs = list(_ARC.finditer(body))
    centres = [(m.start("clat"), m.end("clon")) for m in arcs]

    def is_a_centre(match: re.Match) -> bool:
        return any(
            start <= match.start() and match.end() <= end for start, end in centres
        )

    found = [m for m in _PAIR.finditer(body) if not is_a_centre(m)]
    if not found:
        return None, ()

    def at(match: re.Match) -> Position:
        return Position(
            latitude=parse_coordinate(match.group("lat"), is_latitude=True),
            longitude=parse_coordinate(match.group("lon"), is_latitude=False),
        )

    start = at(found[0])
    edges: list[BoundaryEdge] = []
    for previous, current in zip(found, found[1:]):
        between = body[previous.end() : current.start()]
        edges.append(_edge_between(between, at(current)))
    tail = body[found[-1].end() :]
    trailing = _ALONG.search(tail)
    if trailing:
        # "...to X, thence along the State boundary to the point of origin"
        edges.append(
            BoundaryEdge(
                kind=EdgeKind.NARRATIVE,
                text=trailing.group(0).strip(),
            )
        )
    return start, tuple(edges)


def _is_only_joining(text: str) -> bool:
    """Whether the words between two coordinates just mean "a direct line"."""
    words = re.findall(r"[A-Za-z]+", text.lower())
    return all(word in _JOINING_WORDS for word in words)


def _edge_between(text: str, end: Position) -> BoundaryEdge:
    """Classify the words between two published coordinates."""
    arc = _ARC.search(text)
    if arc is not None:
        way = arc.group("way").lower()
        return BoundaryEdge(
            kind=EdgeKind.ARC,
            to=end,
            centre=Position(
                latitude=parse_coordinate(arc.group("clat"), is_latitude=True),
                longitude=parse_coordinate(arc.group("clon"), is_latitude=False),
            ),
            radius_nm=float(arc.group("radius")),
            clockwise=way.startswith("clock"),
            text=arc.group(0).strip(),
        )
    along = _ALONG.search(text)
    if along is not None:
        return BoundaryEdge(
            kind=EdgeKind.NARRATIVE, to=end, text=along.group(0).strip()
        )
    if re.search(r"along\s+(the\s+)?parallel", text, re.IGNORECASE):
        return BoundaryEdge(kind=EdgeKind.PARALLEL, to=end, text=text.strip())
    if _is_only_joining(text):
        return BoundaryEdge(kind=EdgeKind.GREAT_CIRCLE, to=end)
    # Words this parser does not recognise. They became a narrative edge rather
    # than a straight line, which draws a visible gap instead of a border
    # through airspace nobody described that way — the safe direction to fail.
    return BoundaryEdge(kind=EdgeKind.NARRATIVE, to=end, text=text.strip())


_CIRCLE = re.compile(
    r"circle\s+(?:of\s+)?(?P<radius>\d+(?:\.\d+)?)\s*(?:NM|nm|KM|km)?"
    r"\s*(?:radius)?.{0,40}?cent(?:re|er)(?:d|ed)?\s+(?:on|at)\s+"
    r"(?P<clat>\d{6}(?:\.\d+)?[NS])\s*[/, ]?\s*(?P<clon>\d{7}(?:\.\d+)?[EW])",
    re.IGNORECASE | re.DOTALL,
)


def parse_boundary(text: str) -> Boundary:
    """Read a whole published boundary description.

    Recognises the circle form first, because "a circle of 5 NM radius centred
    on X" contains one coordinate pair and walking it as a boundary would
    produce a single point.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return Boundary(published_as="")

    circle = _CIRCLE.search(body)
    if circle is not None:
        return Boundary(
            circle=Circle(
                centre=Position(
                    latitude=parse_coordinate(circle.group("clat"), is_latitude=True),
                    longitude=parse_coordinate(
                        circle.group("clon"), is_latitude=False
                    ),
                ),
                radius_nm=float(circle.group("radius")),
            ),
            published_as=body,
        )

    start, edges = read_edges(body)
    if start is None:
        return Boundary(published_as=body)
    return Boundary(start=start, edges=edges, published_as=body)


def read_boundary_manifest(
    row: Mapping[str, object], *, where: str
) -> Boundary | None:
    """Build a boundary from a manifest object.

    Two forms, matching how AIPs publish. ``circle`` is a centre and a radius.
    ``points`` is the coordinate list, optionally with ``edges`` naming what
    happens between them where it is not a direct line. ``described_as`` keeps
    the printed words either way.
    """
    if not isinstance(row, Mapping):
        raise ValueError(f"{where}: boundary must be an object")

    described = str(row.get("described_as", "")).strip()

    circle = row.get("circle")
    if circle is not None:
        if not isinstance(circle, Mapping):
            raise ValueError(f"{where}: boundary.circle must be an object")
        values = {}
        for field, is_latitude in (("latitude", True), ("longitude", False)):
            try:
                values[field] = parse_coordinate(
                    circle.get(field), is_latitude=is_latitude
                )
            except (CoordinateError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{where}: boundary.circle {field} {error}"
                ) from None
        centre = Position(
            latitude=values["latitude"], longitude=values["longitude"]
        )
        try:
            radius = float(circle.get("radius_nm"))
        except (TypeError, ValueError):
            raise ValueError(
                f"{where}: boundary.circle radius_nm "
                f"{circle.get('radius_nm')!r} is not a number"
            ) from None
        return Boundary(
            circle=Circle(centre=centre, radius_nm=radius), published_as=described
        )

    described_only = row.get("points") is None and row.get("edges") is None
    if described_only:
        if not described:
            return None
        # Words and nothing else: read what can be read, and whatever cannot be
        # becomes a narrative edge rather than disappearing.
        return parse_boundary(described)

    points = row.get("points") or []
    if not isinstance(points, list):
        raise ValueError(f"{where}: boundary.points must be a list")
    read: list[Position] = []
    for index, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise ValueError(f"{where}: boundary.points[{index}] must be an object")
        # Read one field at a time so the message names the column. A boundary
        # table is forty rows of two coordinates, and "one of these is wrong"
        # is not a finding somebody can act on.
        values: dict[str, float] = {}
        for field, is_latitude in (("latitude", True), ("longitude", False)):
            try:
                values[field] = parse_coordinate(
                    point.get(field), is_latitude=is_latitude
                )
            except (CoordinateError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{where}: boundary.points[{index}] {field} {error}"
                ) from None
        read.append(
            Position(latitude=values["latitude"], longitude=values["longitude"])
        )
    if len(read) < 3:
        raise ValueError(
            f"{where}: a boundary needs at least three points. Two points are "
            "a line, and a line does not enclose airspace."
        )
    built = boundary_from_points(read, published_as=described)

    overrides = row.get("edges") or []
    if not overrides:
        return built
    if not isinstance(overrides, list):
        raise ValueError(f"{where}: boundary.edges must be a list")
    return _apply_edges(built, overrides, where=where)


def _apply_edges(
    built: Boundary, overrides: Iterable[Mapping[str, object]], *, where: str
) -> Boundary:
    """Replace the direct edges the AIP does not describe as direct.

    Each override names the edge by the index of the point it *ends at*, so a
    reader working from the printed list numbers the same way the list does.
    """
    edges = list(built.edges)
    for index, override in enumerate(overrides):
        if not isinstance(override, Mapping):
            raise ValueError(f"{where}: boundary.edges[{index}] must be an object")
        try:
            at = int(override.get("to_point"))
        except (TypeError, ValueError):
            raise ValueError(
                f"{where}: boundary.edges[{index}] needs to_point — the index "
                "of the point this edge ends at."
            ) from None
        # Point 0 is the start of the walk; the edge ending at it is the last.
        slot = len(edges) - 1 if at == 0 else at - 1
        if not 0 <= slot < len(edges):
            raise ValueError(
                f"{where}: boundary.edges[{index}] to_point {at} is not a point "
                f"in this boundary (it has {len(edges)})"
            )
        existing = edges[slot]
        kind_text = str(override.get("kind", "great_circle")).strip().lower()
        try:
            kind = EdgeKind(kind_text)
        except ValueError:
            raise ValueError(
                f"{where}: boundary.edges[{index}] kind must be one of "
                f"{', '.join(k.value for k in EdgeKind)}"
            ) from None
        centre = None
        if override.get("centre_latitude") is not None:
            try:
                centre = Position(
                    latitude=parse_coordinate(
                        override.get("centre_latitude"), is_latitude=True
                    ),
                    longitude=parse_coordinate(
                        override.get("centre_longitude"), is_latitude=False
                    ),
                )
            except (CoordinateError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{where}: boundary.edges[{index}] centre {error}"
                ) from None
        radius = override.get("radius_nm")
        try:
            edges[slot] = BoundaryEdge(
                kind=kind,
                to=existing.to,
                centre=centre,
                radius_nm=float(radius) if radius is not None else None,
                clockwise=(
                    bool(override["clockwise"])
                    if override.get("clockwise") is not None
                    else None
                ),
                text=str(override.get("text", "")).strip(),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"{where}: boundary.edges[{index}] {error}") from None
    return Boundary(
        start=built.start, edges=tuple(edges), published_as=built.published_as
    )
