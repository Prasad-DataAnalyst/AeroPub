"""The route, drawn — where the level fails, and which waypoint it fails at.

A route dossier is a good document and a poor picture. It can tell you that
UM688 between MIDLE and KUKLA has a minimum en-route altitude of FL240 and that
you filed FL200, and a reader still has to hold six segments in their head to
see the shape of the problem. Drawn, it is one glance: a line across the page
at the planned level, boxes standing up from each segment's floor, and one box
whose floor is above the line.

That is the whole design. **The vertical profile is the analysis**, not a
decoration of it, so everything on the page is a published figure positioned by
what it means:

- **Height** is level. A segment's box runs from its minimum en-route altitude
  to its published ceiling, and the planned level is a line across the whole
  page. Where the line passes below a box floor, the route cannot be flown as
  filed, and it is visible before it is read.
- **Width** is distance where the AIP publishes it, and equal spacing where it
  does not — with the difference stated on the page, because a diagram that
  silently switched between the two would be drawing a shape nobody published.
- **Colour** is one thing only: whether the planned level works on that
  segment. Nothing else is coloured, so nothing else competes with it.

What a gap must look like
-------------------------
The rule this module keeps, and the reason it exists rather than a chart
library: **an unresolved leg must never look like a clear one.** A leg flown
direct has no published segment behind it and a leg whose airway we do not hold
has none either, and neither may be drawn as a tidy box. Both are drawn as an
open, hatched band with no floor and no ceiling — visibly a hole in the profile
rather than a low box, because a low box reads as a segment with a low minimum
and that is exactly the wrong conclusion.

The same rule governs the region strip along the top: a region nobody has read
is hatched, not blank. Blank reads as clear.

Why SVG, and why here
---------------------
The output is one self-contained ``<svg>`` element built from strings — no
library, no runtime, and no network. It embeds in the printable dossier, opens
in a browser on its own, and is testable: the tests below assert that a segment
whose floor is above the planned level is drawn in the adverse class, not that
a picture has a particular number of pixels in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from aeropub.ats import ATS_ROUTE, FIX, NAVAID, Resolution, RouteExpansion
from aeropub.entities import normalise

__all__ = [
    "CLOSED",
    "Band",
    "Lane",
    "NetworkDiagram",
    "OPEN",
    "RouteDiagram",
    "UNKNOWN_STATUS",
    "diagram_for",
    "network_for",
    "network_html",
    "network_svg",
    "route_html",
    "route_svg",
]

#: Drawing constants. Kept together so the geometry is in one place rather
#: than scattered through the string building, which is how a diagram acquires
#: an off-by-one nobody can find.
_PLOT_TOP = 96.0
_PLOT_BOTTOM = 400.0
_PLOT_LEFT = 76.0
_REGION_TOP = 44.0
_REGION_HEIGHT = 26.0
_LABEL_BASE = 424.0
_MIN_LEG_WIDTH = 96.0
_HEIGHT = 520.0


def _escape(text: str) -> str:
    """XML-escape. Route data comes from published documents and manifests,
    and a designator with an ampersand in it must not break the drawing."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _flight_level(feet: float) -> str:
    """A level the way a chart prints it."""
    if feet >= 10000:
        return f"FL{feet / 100:.0f}"
    return f"{feet:.0f} ft"


@dataclass(frozen=True, slots=True)
class Band:
    """One leg of the route as it will be drawn.

    Holds only what the drawing needs, resolved from the expansion once, so
    the geometry below is arithmetic on numbers rather than a second walk
    through the route model.
    """

    start: str
    end: str
    via: str
    resolution: Resolution
    floor_ft: float | None = None
    ceiling_ft: float | None = None
    distance_nm: float | None = None
    direction: str = ""
    navigation_spec: str = ""
    notams: int = 0

    @property
    def is_drawn(self) -> bool:
        """Whether there is a published band to draw at all.

        False for a direct leg, an unresolved one, and a resolved one whose
        floor nobody published — all three are holes in the profile, and all
        three are drawn as holes.
        """
        return self.resolution is Resolution.RESOLVED and self.floor_ft is not None

    def fails(self, planned_ft: float | None) -> bool:
        """Whether the planned level cannot be flown on this leg.

        False where there is no planned level or no published floor: an
        unchecked leg is not a passing leg, and the drawing marks it as
        unchecked rather than colouring it as either.
        """
        if planned_ft is None or self.floor_ft is None:
            return False
        if planned_ft < self.floor_ft:
            return True
        return self.ceiling_ft is not None and planned_ft > self.ceiling_ft

    def label(self) -> str:
        if self.resolution is Resolution.DIRECT:
            return "DCT"
        if self.resolution is Resolution.PROCEDURE:
            return f"{self.via} (procedure)"
        return self.via


@dataclass(frozen=True, slots=True)
class RouteDiagram:
    """Everything the drawing needs, and nothing about how it looks."""

    bands: tuple[Band, ...] = ()
    planned_ft: float | None = None
    regions: tuple[str, ...] = ()
    unread_regions: tuple[str, ...] = ()
    title: str = ""
    to_scale: bool = False
    """Whether the horizontal axis is published distance. False where any leg
    is unmeasured, and stated on the page — a diagram that silently switched
    between the two would be drawing a shape nobody published."""

    @property
    def points(self) -> tuple[str, ...]:
        """Every waypoint, in order, without repeating the joins."""
        if not self.bands:
            return ()
        found = [self.bands[0].start]
        for band in self.bands:
            if band.end != found[-1]:
                found.append(band.end)
        return tuple(found)

    @property
    def failing(self) -> tuple[Band, ...]:
        return tuple(b for b in self.bands if b.fails(self.planned_ft))

    @property
    def unchecked(self) -> tuple[Band, ...]:
        """Legs with no published band behind them. Holes, not low boxes."""
        return tuple(b for b in self.bands if not b.is_drawn)

    @property
    def ceiling(self) -> float:
        """The top of the vertical scale.

        Driven by the highest thing that has to fit on the page — a published
        ceiling, a floor, or the planned level — so nothing is ever drawn off
        the top of the plot.
        """
        found = [
            value
            for band in self.bands
            for value in (band.floor_ft, band.ceiling_ft)
            if value is not None and value != float("inf")
        ]
        if self.planned_ft is not None:
            found.append(self.planned_ft)
        top = max(found) if found else 40000.0
        # Round up to the next 5000 ft so the axis lands on tick marks a
        # reader recognises rather than on the highest published figure.
        return max(5000.0, 5000.0 * ((top // 5000.0) + 1))


def diagram_for(
    expansion: RouteExpansion,
    *,
    planned_ft: float | None = None,
    regions: Iterable[str] = (),
    unread_regions: Iterable[str] = (),
    title: str = "",
    notams: Iterable[tuple] = (),
) -> RouteDiagram:
    """Turn a resolved route into the bands a drawing needs.

    A resolved leg contributes the *binding* figures across every segment it
    crosses: the highest floor and the lowest ceiling. Drawing a leg at its
    first segment's minimum would show a level as flyable on a leg where one
    segment in the middle forbids it, which is the exact error the route
    screen exists to catch.
    """
    counted: dict[str, int] = {}
    for entry in notams:
        key = normalise(entry[0]) if entry else ""
        if key:
            counted[key] = counted.get(key, 0) + 1

    bands: list[Band] = []
    measured = True
    for leg in expansion.legs:
        floors = [s.floor_ft for s in leg.segments if s.floor_ft is not None]
        ceilings = [
            s.maa_ft if s.maa_ft is not None else s.upper_limit_ft
            for s in leg.segments
        ]
        ceilings = [c for c in ceilings if c is not None]
        if leg.distance_nm is None:
            measured = False
        directions = {s.direction.value for s in leg.segments}
        specs = {s.navigation_spec for s in leg.segments if s.navigation_spec}
        # Points are keyed by kind — FIX: or NAVAID: — because a State files
        # against a navaid and a name-code differently. Looking up the bare
        # designator matched nothing and silently drew a clean band over a
        # waypoint with a NOTAM on it.
        hits = sum(
            counted.get(key, 0)
            for key in (
                normalise(f"{FIX}:{leg.leg.end}"),
                normalise(f"{NAVAID}:{leg.leg.end}"),
                normalise(leg.leg.end),
            )
        )
        if not leg.leg.is_direct:
            hits += counted.get(normalise(f"{ATS_ROUTE}:{leg.leg.via}"), 0)
        bands.append(
            Band(
                start=leg.leg.start,
                end=leg.leg.end,
                via=leg.leg.via,
                resolution=leg.resolution,
                # The binding figures, not the first segment's: the highest
                # floor and the lowest ceiling across everything crossed.
                floor_ft=max(floors) if floors else None,
                ceiling_ft=min(ceilings) if ceilings else None,
                distance_nm=leg.distance_nm,
                direction="/".join(sorted(directions)) if len(directions) == 1 else "",
                navigation_spec="/".join(sorted(specs)),
                notams=hits,
            )
        )

    return RouteDiagram(
        bands=tuple(bands),
        planned_ft=planned_ft,
        regions=tuple(normalise(r) for r in regions if str(r).strip()),
        unread_regions=tuple(normalise(r) for r in unread_regions if str(r).strip()),
        title=title or expansion.route.text,
        to_scale=measured and bool(bands),
    )


def _widths(diagram: RouteDiagram, total: float) -> list[float]:
    """How wide each leg is drawn.

    Proportional to published distance where every leg has one, equal
    otherwise. Never a mixture: a diagram part to scale and part not is a
    shape nobody published, and a reader has no way to tell which part is
    which.
    """
    count = len(diagram.bands)
    if not count:
        return []
    if not diagram.to_scale:
        return [max(_MIN_LEG_WIDTH, total / count)] * count
    distances = [b.distance_nm or 0.0 for b in diagram.bands]
    flown = sum(distances) or 1.0
    return [max(_MIN_LEG_WIDTH, total * d / flown) for d in distances]


def route_svg(diagram: RouteDiagram) -> str:
    """Draw the route profile as one self-contained ``<svg>`` element."""
    count = len(diagram.bands)
    widths = _widths(diagram, max(640.0, 132.0 * count))
    plot_width = sum(widths) or 640.0
    width = _PLOT_LEFT + plot_width + 40.0
    ceiling = diagram.ceiling
    span = _PLOT_BOTTOM - _PLOT_TOP

    def y_for(feet: float) -> float:
        return _PLOT_BOTTOM - span * (min(feet, ceiling) / ceiling)

    out: list[str] = [
        f'<svg class="route-profile" viewBox="0 0 {width:.0f} {_HEIGHT:.0f}" '
        f'width="100%" role="img" '
        f'aria-label="Vertical profile of {_escape(diagram.title)}" '
        'xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<pattern id="rp-hatch" width="8" height="8" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="8" height="8" class="rp-hatch-bg"/>'
        '<line x1="0" y1="0" x2="0" y2="8" class="rp-hatch-line"/>'
        "</pattern>",
        "</defs>",
    ]

    if diagram.title:
        out.append(
            f'<text x="{_PLOT_LEFT:.0f}" y="24" class="rp-title">'
            f"{_escape(diagram.title)}</text>"
        )
    scale_note = (
        "horizontal axis to published distance"
        if diagram.to_scale
        else "horizontal axis not to scale — not every leg publishes a distance"
    )
    out.append(
        f'<text x="{_PLOT_LEFT:.0f}" y="{_HEIGHT - 14:.0f}" class="rp-note">'
        f"{_escape(scale_note)}</text>"
    )

    # Level axis. Ticks every 5000 ft, which is what a reader recognises.
    step = 5000.0
    level = 0.0
    while level <= ceiling:
        y = y_for(level)
        out.append(
            f'<line x1="{_PLOT_LEFT:.0f}" y1="{y:.1f}" '
            f'x2="{_PLOT_LEFT + plot_width:.1f}" y2="{y:.1f}" class="rp-grid"/>'
        )
        out.append(
            f'<text x="{_PLOT_LEFT - 10:.0f}" y="{y + 4:.1f}" '
            f'class="rp-axis" text-anchor="end">{_escape(_flight_level(level))}</text>'
        )
        level += step

    # Region strip. A region nobody read is hatched, never blank: blank reads
    # as clear.
    if diagram.regions:
        strip = plot_width / len(diagram.regions)
        for index, region in enumerate(diagram.regions):
            x = _PLOT_LEFT + index * strip
            unread = region in diagram.unread_regions
            fill = ' fill="url(#rp-hatch)"' if unread else ""
            out.append(
                f'<rect x="{x:.1f}" y="{_REGION_TOP:.0f}" width="{strip:.1f}" '
                f'height="{_REGION_HEIGHT:.0f}" class="rp-region'
                f'{" rp-unread" if unread else ""}"{fill}/>'
            )
            out.append(
                f'<text x="{x + strip / 2:.1f}" y="{_REGION_TOP + 17:.0f}" '
                f'class="rp-region-label" text-anchor="middle">'
                f"{_escape(region)}{' — not read' if unread else ''}</text>"
            )

    # The legs.
    x = _PLOT_LEFT
    for band, leg_width in zip(diagram.bands, widths):
        adverse = band.fails(diagram.planned_ft)
        if band.is_drawn:
            top = y_for(band.ceiling_ft) if band.ceiling_ft is not None else _PLOT_TOP
            bottom = y_for(band.floor_ft)
            classes = "rp-band" + (" rp-adverse" if adverse else "")
            out.append(
                f'<rect x="{x + 3:.1f}" y="{top:.1f}" '
                f'width="{leg_width - 6:.1f}" height="{max(2.0, bottom - top):.1f}" '
                f'class="{classes}"/>'
            )
            out.append(
                f'<text x="{x + leg_width / 2:.1f}" y="{bottom - 8:.1f}" '
                f'class="rp-floor" text-anchor="middle">'
                f"{_escape(_flight_level(band.floor_ft))}</text>"
            )
        else:
            # A hole in the profile, drawn as a hole. A low box would read as
            # a segment with a low minimum, which is the wrong conclusion.
            out.append(
                f'<rect x="{x + 3:.1f}" y="{_PLOT_TOP:.0f}" '
                f'width="{leg_width - 6:.1f}" height="{span:.0f}" '
                f'class="rp-hole" fill="url(#rp-hatch)"/>'
            )
            out.append(
                f'<text x="{x + leg_width / 2:.1f}" y="{(_PLOT_TOP + _PLOT_BOTTOM) / 2:.0f}" '
                f'class="rp-hole-label" text-anchor="middle">'
                f"{_escape(band.label())}</text>"
            )

        out.append(
            f'<text x="{x + leg_width / 2:.1f}" y="{_PLOT_TOP - 10:.0f}" '
            f'class="rp-via" text-anchor="middle">{_escape(band.label())}</text>'
        )
        if band.notams:
            out.append(
                f'<text x="{x + leg_width / 2:.1f}" y="{_PLOT_TOP - 24:.0f}" '
                f'class="rp-notam" text-anchor="middle">'
                f"{band.notams} NOTAM</text>"
            )
        x += leg_width

    # The planned level, drawn last so it sits above every band.
    if diagram.planned_ft is not None:
        y = y_for(diagram.planned_ft)
        out.append(
            f'<line x1="{_PLOT_LEFT:.0f}" y1="{y:.1f}" '
            f'x2="{_PLOT_LEFT + plot_width:.1f}" y2="{y:.1f}" class="rp-planned"/>'
        )
        out.append(
            f'<text x="{_PLOT_LEFT + 6:.0f}" y="{y - 7:.1f}" class="rp-planned-label">'
            f"planned {_escape(_flight_level(diagram.planned_ft))}</text>"
        )

    # Waypoint ticks along the bottom, at the joins between legs.
    x = _PLOT_LEFT
    ticks = [(x, diagram.bands[0].start)] if diagram.bands else []
    for band, leg_width in zip(diagram.bands, widths):
        x += leg_width
        ticks.append((x, band.end))
    for position, point in ticks:
        out.append(
            f'<line x1="{position:.1f}" y1="{_PLOT_BOTTOM:.0f}" '
            f'x2="{position:.1f}" y2="{_PLOT_BOTTOM + 8:.0f}" class="rp-tick"/>'
        )
        out.append(
            f'<text x="{position:.1f}" y="{_LABEL_BASE:.0f}" class="rp-point" '
            f'text-anchor="middle">{_escape(point)}</text>'
        )

    out.append(
        f'<line x1="{_PLOT_LEFT:.0f}" y1="{_PLOT_BOTTOM:.0f}" '
        f'x2="{_PLOT_LEFT + plot_width:.1f}" y2="{_PLOT_BOTTOM:.0f}" class="rp-base"/>'
    )
    out.append("</svg>")
    return "\n".join(out)


#: The stylesheet the drawing needs, as one string so a caller embedding the
#: SVG in a page has one thing to include. Theme-aware: every colour is a
#: token, and the dark set redefines the tokens rather than the rules.
ROUTE_PROFILE_CSS = """
.route-profile { --rp-ink: #16202b; --rp-muted: #5b6b7a; --rp-grid: #dfe5ea;
  --rp-band: #b9cbd8; --rp-band-edge: #7d97a8; --rp-adverse: #c0392b;
  --rp-planned: #1b6ca8; --rp-hatch: #eef2f5; --rp-hatch-line: #b3bfc9; }
@media (prefers-color-scheme: dark) {
  .route-profile:not([data-theme="light"]) { --rp-ink: #e6edf3;
    --rp-muted: #93a4b3; --rp-grid: #2a3641; --rp-band: #3d5567;
    --rp-band-edge: #6d8ba1; --rp-adverse: #e5705f; --rp-planned: #63b3ed;
    --rp-hatch: #1b242c; --rp-hatch-line: #3b4855; } }
.rp-title { font: 600 15px/1.3 system-ui, sans-serif; fill: var(--rp-ink); }
.rp-note, .rp-axis, .rp-point, .rp-via, .rp-floor, .rp-region-label,
.rp-hole-label, .rp-notam, .rp-planned-label {
  font: 11px/1.3 system-ui, sans-serif; fill: var(--rp-muted); }
.rp-point, .rp-via { fill: var(--rp-ink); font-weight: 600; }
.rp-axis { font-variant-numeric: tabular-nums; }
.rp-grid { stroke: var(--rp-grid); stroke-width: 1; }
.rp-base, .rp-tick { stroke: var(--rp-band-edge); stroke-width: 1.5; }
.rp-band { fill: var(--rp-band); stroke: var(--rp-band-edge); stroke-width: 1; }
.rp-adverse { fill: var(--rp-adverse); fill-opacity: .28;
  stroke: var(--rp-adverse); stroke-width: 1.5; }
.rp-hole { stroke: var(--rp-hatch-line); stroke-width: 1;
  stroke-dasharray: 4 3; }
.rp-hatch-bg { fill: var(--rp-hatch); }
.rp-hatch-line { stroke: var(--rp-hatch-line); stroke-width: 1.5; }
.rp-region { fill: var(--rp-grid); stroke: var(--rp-band-edge);
  stroke-width: .5; }
.rp-planned { stroke: var(--rp-planned); stroke-width: 2;
  stroke-dasharray: 7 4; }
.rp-planned-label { fill: var(--rp-planned); font-weight: 600; }
.rp-notam { fill: var(--rp-adverse); font-weight: 600; }
.rp-floor { font-variant-numeric: tabular-nums; }
"""


def route_html(diagram: RouteDiagram) -> str:
    """The profile as a whole page, for opening on its own.

    Self-contained: no library, no runtime, no network. What is drawn is what
    was published, and the notes below the drawing say which parts of the
    picture are gaps rather than findings.
    """
    notes: list[str] = []
    if diagram.failing:
        notes.append(
            "<p class=\"rp-flag\">"
            + _escape(
                "The planned level cannot be flown on "
                + ", ".join(f"{b.via} {b.start}-{b.end}" for b in diagram.failing)
                + "."
            )
            + "</p>"
        )
    if diagram.unchecked:
        notes.append(
            "<p>"
            + _escape(
                f"{len(diagram.unchecked)} of {len(diagram.bands)} legs have no "
                "published band behind them and are drawn as gaps rather than "
                "as low boxes: a direct leg has nothing to check, and an "
                "unresolved one has nothing held."
            )
            + "</p>"
        )
    if diagram.unread_regions:
        notes.append(
            "<p>"
            + _escape(
                "No ENR has been read for "
                + ", ".join(diagram.unread_regions)
                + ". Those strips are hatched rather than blank, because blank "
                "reads as clear."
            )
            + "</p>"
        )
    return (
        "<style>"
        + ROUTE_PROFILE_CSS
        + "\nbody{margin:0;padding:24px;font:14px/1.5 system-ui,sans-serif}"
        + ".rp-flag{font-weight:600}</style>\n"
        + route_svg(diagram)
        + "\n"
        + "\n".join(notes)
    )


# --------------------------------------------------------------------------
# The network — airways, the points on them, and where they meet
# --------------------------------------------------------------------------
#
# The profile above answers "can I fly this route at this level". This answers
# a different question: "what is the structure, and what is shut".
#
# There is no geography here and there must not be. The platform holds no
# coordinates, so a map would be an invention. What it holds is connectivity —
# which airway passes through which point, in what published order — and
# connectivity draws perfectly well as a schematic. A transit diagram makes the
# same trade: it abandons geography to make interchanges legible, and nobody
# mistakes it for a map.
#
# The interchange is the point of the drawing. A point carrying two airways is
# somewhere a plan can change airway without a direct leg, and when an airway
# closes it is where the alternative starts. That is the question a planner has
# the moment a NOTAM shuts a route, and it is invisible in a table.


#: What can be said about an airway's availability.
OPEN = "open"
CLOSED = "closed"
UNKNOWN_STATUS = "unknown"

_LANE_TOP = 84.0
_LANE_GAP = 68.0
_STOP_LEFT = 132.0
_STOP_GAP = 104.0


@dataclass(frozen=True, slots=True)
class Lane:
    """One airway, and the points published on it in flown order."""

    route: str
    points: tuple[str, ...] = ()
    region: str = ""
    status: str = UNKNOWN_STATUS
    lowest_ft: float | None = None
    highest_ft: float | None = None
    notams: int = 0

    @property
    def is_closed(self) -> bool:
        return self.status == CLOSED

    def label(self) -> str:
        parts = [self.route]
        if self.lowest_ft is not None:
            parts.append(_flight_level(self.lowest_ft) + "+")
        return "  ".join(parts)


@dataclass(frozen=True, slots=True)
class NetworkDiagram:
    """The route structure as connectivity, with no geography claimed."""

    lanes: tuple[Lane, ...] = ()
    interchanges: tuple[str, ...] = ()
    """Points carrying more than one airway. Where a plan can change airway
    without a direct leg — and where the alternative starts when one shuts."""

    highlight: tuple[str, ...] = ()
    """Points on the filed route, marked so the route can be found inside the
    structure it is flown through."""

    title: str = ""

    @property
    def points(self) -> tuple[str, ...]:
        found: list[str] = []
        for lane in self.lanes:
            for point in lane.points:
                if point not in found:
                    found.append(point)
        return tuple(found)

    @property
    def closed(self) -> tuple[Lane, ...]:
        return tuple(lane for lane in self.lanes if lane.is_closed)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({lane.region for lane in self.lanes if lane.region}))


def network_for(
    structure,
    *,
    closed_routes: Iterable[str] = (),
    notams: Iterable[tuple] = (),
    highlight: Iterable[str] = (),
    title: str = "",
) -> NetworkDiagram:
    """Turn a published route structure into the lanes a schematic needs.

    ``closed_routes`` is what somebody has established is shut — from a NOTAM
    read, or from a State's own withdrawal notice. It is passed in rather than
    inferred: a NOTAM against an airway may close it, may restrict a level band
    on it, or may say something else entirely, and deciding which from the
    presence of a NOTAM would be reading a message this module has not read.
    Airways carrying a NOTAM nobody has interpreted are marked as carrying one,
    which is a different and honest statement.
    """
    shut = {normalise(r) for r in closed_routes if str(r).strip()}
    counted: dict[str, int] = {}
    for entry in notams:
        key = normalise(entry[0]) if entry else ""
        if key:
            counted[key] = counted.get(key, 0) + 1

    lanes: list[Lane] = []
    for route in structure.routes:
        segments = structure.on(route)
        floors = [s.floor_ft for s in segments if s.floor_ft is not None]
        ceilings = [
            s.maa_ft if s.maa_ft is not None else s.upper_limit_ft
            for s in segments
        ]
        ceilings = [c for c in ceilings if c is not None and c != float("inf")]
        regions = {s.region for s in segments if s.region}
        lanes.append(
            Lane(
                route=route,
                points=structure.points_on(route),
                region=regions.pop() if len(regions) == 1 else "",
                status=CLOSED if route in shut else UNKNOWN_STATUS,
                lowest_ft=min(floors) if floors else None,
                highest_ft=max(ceilings) if ceilings else None,
                notams=counted.get(normalise(f"{ATS_ROUTE}:{route}"), 0),
            )
        )

    seen: dict[str, int] = {}
    for lane in lanes:
        for point in lane.points:
            seen[point] = seen.get(point, 0) + 1
    interchanges = tuple(sorted(p for p, count in seen.items() if count > 1))

    return NetworkDiagram(
        lanes=tuple(lanes),
        interchanges=interchanges,
        highlight=tuple(normalise(p) for p in highlight if str(p).strip()),
        title=title,
    )


def network_svg(diagram: NetworkDiagram) -> str:
    """Draw the route structure as a schematic.

    One lane per airway, its points as stops in published order, and a
    vertical connector wherever a point appears on more than one lane. No
    geography: the platform holds no coordinates, and a drawing that implied
    any would be an invention.
    """
    if not diagram.lanes:
        return (
            '<svg class="route-network" viewBox="0 0 640 120" width="100%" '
            'role="img" aria-label="No route structure held" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<text x="24" y="60" class="rn-note">No ATS route structure has '
            "been read. Nothing is drawn, which is a coverage gap and not an "
            "empty network.</text></svg>"
        )

    columns = max(len(lane.points) for lane in diagram.lanes)
    width = _STOP_LEFT + _STOP_GAP * max(1, columns - 1) + 120.0
    height = _LANE_TOP + _LANE_GAP * len(diagram.lanes) + 76.0

    # Every point gets one column, shared across lanes, so a point on two
    # airways lines up vertically and the interchange is drawn rather than
    # asserted.
    column_of: dict[str, int] = {}
    for lane in diagram.lanes:
        for index, point in enumerate(lane.points):
            column_of.setdefault(point, index)

    def x_for(point: str) -> float:
        return _STOP_LEFT + _STOP_GAP * column_of.get(point, 0)

    out: list[str] = [
        f'<svg class="route-network" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="100%" role="img" '
        f'aria-label="ATS route structure schematic{": " + _escape(diagram.title) if diagram.title else ""}" '
        'xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<pattern id="rn-hatch" width="8" height="8" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="8" height="8" class="rn-hatch-bg"/>'
        '<line x1="0" y1="0" x2="0" y2="8" class="rn-hatch-line"/>'
        "</pattern>",
        "</defs>",
    ]
    if diagram.title:
        out.append(f'<text x="24" y="30" class="rn-title">{_escape(diagram.title)}</text>')

    # Interchange connectors first, so the lanes are drawn over them.
    for point in diagram.interchanges:
        rows = [
            _LANE_TOP + _LANE_GAP * index
            for index, lane in enumerate(diagram.lanes)
            if point in lane.points
        ]
        if len(rows) > 1:
            x = x_for(point)
            out.append(
                f'<line x1="{x:.1f}" y1="{min(rows):.1f}" x2="{x:.1f}" '
                f'y2="{max(rows):.1f}" class="rn-interchange"/>'
            )

    for index, lane in enumerate(diagram.lanes):
        y = _LANE_TOP + _LANE_GAP * index
        if not lane.points:
            continue
        first, last = x_for(lane.points[0]), x_for(lane.points[-1])
        classes = "rn-lane" + (" rn-closed" if lane.is_closed else "")
        out.append(
            f'<line x1="{first:.1f}" y1="{y:.1f}" x2="{last:.1f}" y2="{y:.1f}" '
            f'class="{classes}"/>'
        )
        out.append(
            f'<text x="{_STOP_LEFT - 22:.0f}" y="{y + 4:.1f}" class="rn-route" '
            f'text-anchor="end">{_escape(lane.label())}</text>'
        )
        if lane.region:
            out.append(
                f'<text x="{_STOP_LEFT - 22:.0f}" y="{y + 18:.1f}" '
                f'class="rn-region" text-anchor="end">{_escape(lane.region)}</text>'
            )
        if lane.is_closed:
            out.append(
                f'<text x="{last + 14:.1f}" y="{y + 4:.1f}" class="rn-shut">'
                "CLOSED</text>"
            )
        elif lane.notams:
            out.append(
                f'<text x="{last + 14:.1f}" y="{y + 4:.1f}" class="rn-notam">'
                f"{lane.notams} NOTAM</text>"
            )

        for point in lane.points:
            x = x_for(point)
            interchange = point in diagram.interchanges
            marked = point in diagram.highlight
            radius = 7.0 if interchange else 4.5
            stop = "rn-stop"
            if interchange:
                stop += " rn-node"
            if marked:
                stop += " rn-filed"
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
                f'class="{stop}"/>'
            )
            if index == 0 or point not in [
                p for earlier in diagram.lanes[:index] for p in earlier.points
            ]:
                out.append(
                    f'<text x="{x:.1f}" y="{y - 14:.1f}" class="rn-point" '
                    f'text-anchor="middle">{_escape(point)}</text>'
                )

    out.append(
        f'<text x="24" y="{height - 22:.0f}" class="rn-note">'
        "Connectivity only — this platform holds no coordinates, so nothing "
        "here is a map. A larger dot is a point on more than one airway.</text>"
    )
    out.append("</svg>")
    return "\n".join(out)


ROUTE_NETWORK_CSS = """
.route-network { --rn-ink: #16202b; --rn-muted: #5b6b7a; --rn-lane: #7d97a8;
  --rn-node: #1b6ca8; --rn-filed: #1b6ca8; --rn-closed: #c0392b;
  --rn-hatch: #eef2f5; --rn-hatch-line: #b3bfc9; --rn-card: #ffffff; }
@media (prefers-color-scheme: dark) {
  .route-network:not([data-theme="light"]) { --rn-ink: #e6edf3;
    --rn-muted: #93a4b3; --rn-lane: #6d8ba1; --rn-node: #63b3ed;
    --rn-filed: #63b3ed; --rn-closed: #e5705f; --rn-hatch: #1b242c;
    --rn-hatch-line: #3b4855; --rn-card: #161e26; } }
.rn-title { font: 600 15px/1.3 system-ui, sans-serif; fill: var(--rn-ink); }
.rn-note, .rn-region { font: 11px/1.3 system-ui, sans-serif;
  fill: var(--rn-muted); }
.rn-route { font: 600 12px/1.3 system-ui, sans-serif; fill: var(--rn-ink); }
.rn-point { font: 11px/1.3 system-ui, sans-serif; fill: var(--rn-ink); }
.rn-lane { stroke: var(--rn-lane); stroke-width: 4; stroke-linecap: round; }
.rn-closed { stroke: var(--rn-closed); stroke-dasharray: 9 6; }
.rn-interchange { stroke: var(--rn-lane); stroke-width: 2; opacity: .55; }
.rn-stop { fill: var(--rn-card); stroke: var(--rn-lane); stroke-width: 2; }
.rn-node { stroke: var(--rn-node); stroke-width: 2.5; }
.rn-filed { fill: var(--rn-filed); }
.rn-shut, .rn-notam { font: 600 11px/1.3 system-ui, sans-serif;
  fill: var(--rn-closed); }
.rn-hatch-bg { fill: var(--rn-hatch); }
.rn-hatch-line { stroke: var(--rn-hatch-line); stroke-width: 1.5; }
"""


def network_html(diagram: NetworkDiagram) -> str:
    """The schematic as a whole page, for opening on its own."""
    notes: list[str] = []
    if diagram.closed:
        shut = ", ".join(lane.route for lane in diagram.closed)
        alternatives = sorted({
            point
            for lane in diagram.closed
            for point in lane.points
            if point in diagram.interchanges
        })
        notes.append(
            "<p><strong>"
            + _escape(f"{shut} closed.")
            + "</strong> "
            + _escape(
                "The interchanges on it are "
                + (", ".join(alternatives) if alternatives else "none held")
                + " — where a plan can change airway without a direct leg."
            )
            + "</p>"
        )
    return (
        "<style>"
        + ROUTE_NETWORK_CSS
        + "\nbody{margin:0;padding:24px;font:14px/1.5 system-ui,sans-serif}"
        "</style>\n"
        + network_svg(diagram)
        + "\n"
        + "\n".join(notes)
    )
