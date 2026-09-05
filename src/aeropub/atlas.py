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
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from aeropub.airspace import Airspace, AirspaceStructure, AirspaceType
from aeropub.ats import ATS_ROUTE, AtsStructure
from aeropub.basemap import NOT_AERONAUTICAL, Basemap, load_basemap
from aeropub.boundary import Boundary
from aeropub.enroute import AirwayProfile, profile_for
from aeropub.entities import named, normalise
from aeropub.geo import (
    Bounds,
    Position,
    bounds_of,
    great_circle_path,
    mercator,
)
from aeropub.hazards import Hazard, HazardRegister
from aeropub.navaids import NavaidRegister
from aeropub.notam_register import NotamRegister

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
        )
        for designator, position in positions.items()
        # A point nothing draws through is still a published point, but a chart
        # of every name in a national table is a chart of nothing. Only the
        # ones the drawn routes use, plus every navaid, which is what an
        # en-route chart shows.
        if designator in through or kinds.get(designator) == "navaid"
    )

    everything = [p.position for p in drawn_points]
    everything += [p for route in drawn_routes for p in route.path]
    everything += [p for area in areas for ring in area.rings for p in ring]
    window = bounds_of(everything)
    padded = window.padded(0.06) if window else None

    return Atlas(
        basemap=(basemap or load_basemap()).clipped(padded),
        areas=tuple(areas),
        routes=tuple(drawn_routes),
        points=drawn_points,
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


def _info(name: str, kind: str, where: str, detail: str, published: str, notams: int) -> str:
    return _escape(
        json.dumps(
            {
                "name": name,
                "kind": kind,
                "position": where,
                "detail": detail,
                "published": published or "source not recorded",
                "notams": notams,
            }
        )
    )


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

    out += ["</g>", "</svg>"]
    return "\n".join(out)


ATLAS_CSS = """
.at-wrap { --at-ink: #16202b; --at-muted: #64757f; --at-ground: #eef1f3;
  --at-coast: #9fb3bd; --at-border: #c6d2d8; --at-fir: #1b6ca8;
  --at-terminal: #7a5ea8; --at-hazard: #c0392b; --at-route: #2f7d5f;
  --at-mark: #16202b; --at-card: #ffffff; }
@media (prefers-color-scheme: dark) {
  .at-wrap:not([data-theme="light"]) { --at-ink: #e6edf3; --at-muted: #93a4b3;
    --at-ground: #101821; --at-coast: #48606d; --at-border: #2c3d47;
    --at-fir: #63b3ed; --at-terminal: #b39ae0; --at-hazard: #e5705f;
    --at-route: #63c69b; --at-mark: #cfd9e2; --at-card: #18222b; } }
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
        ("basemap", "Coast"),
        ("fir", "FIR/UIR"),
        ("terminal", "TMA/CTR"),
        ("routes", "ATS routes"),
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
