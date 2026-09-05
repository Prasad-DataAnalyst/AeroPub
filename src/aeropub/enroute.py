"""ENR 6 — the en-route chart, drawn from ENR 3 rather than from a caller.

:mod:`aeropub.planview` can draw a map. Until now it was handed the positions
and the airways by whoever called it, which meant the drawing was as good as
the argument list and no better. This module closes that: it takes the
published route structure — every segment of ENR 3, every significant point of
ENR 4.4, every aid of ENR 4.1 — and produces the chart from it.

That is the right direction of authority. ENR 3 is what a State publishes its
route network as; the chart is the picture of it. Nothing here extracts route
structure *from* a chart, and nothing should.

What a route structure chart has to show
----------------------------------------
An airway drawn as a line and nothing else is a line. What makes an en-route
chart usable is the numbers against it, and ENR 3 publishes all of them:

============  ================================================================
levels        The band the airway may be flown in — the MEA at the bottom,
              the MAA or upper limit at the top. Reported as the *binding*
              floor across the segments drawn: the highest MEA on the airway
              is the one that decides whether a level is available end to end
direction     Whether cruising levels run both ways or one. A one-way airway
              flown the other way is not a longer route
spec          The navigation specification required — RNAV 5, RNP 4. A
              capability question about the tail, answered elsewhere
unit          Who controls it, and in whose region it lies
============  ================================================================

The level filter, and why it is a filter and not a verdict
-----------------------------------------------------------
Asked for a level, the chart draws the airways that publish a band containing
it and *sets aside* the others, saying how many and why. It does not delete
them and it does not draw them dimmed as though they were an option: an airway
whose MEA is above the planned level is not available at that level, and the
count of what was set aside travels with the drawing.

An airway with no published band is never filtered out. Not knowing the floor
is not the same as the floor being satisfied, and dropping it would make a
coverage gap look like a level restriction.

What is not drawn
-----------------
A point with no published position is listed, never placed — the discipline
:mod:`aeropub.planview` already enforces. The consequence here is worth saying
plainly: an ENR 3 read without the ENR 4.4 coordinate table produces a chart
with airways and no geometry, and it says so rather than drawing something.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Iterable, Mapping

from aeropub.ats import (
    ATS_ROUTE,
    AtsStructure,
    CruisingLevels,
    RouteSegment,
)
from aeropub.entities import named, normalise
from aeropub.geo import Position
from aeropub.navaids import NavaidRegister
from aeropub.notam_register import NotamRegister
from aeropub.planview import PlanView, plan_html, plan_view

__all__ = [
    "AirwayProfile",
    "EnrouteChart",
    "chart_for",
    "chart_html",
    "profile_for",
]


@dataclass(frozen=True, slots=True)
class AirwayProfile:
    """One airway of ENR 3, summarised across the segments held for it."""

    route: str
    points: tuple[str, ...] = ()
    segments: int = 0
    regions: tuple[str, ...] = ()
    floor_ft: float | None = None
    """The *binding* floor: the highest published minimum across the segments.
    The lowest would be a level available on part of the airway and not on the
    rest, which is the number that gets a flight planned onto it."""

    ceiling_ft: float | None = None
    """The lowest published maximum, for the same reason in the other
    direction."""

    direction: CruisingLevels = CruisingLevels.BOTH
    navigation_specs: tuple[str, ...] = ()
    """Every specification published across the segments. More than one means
    the requirement changes along the airway, which is a finding rather than a
    tidy single value."""

    units: tuple[str, ...] = ()
    unbounded: int = 0
    """Segments publishing no floor at all. Never filtered out on level: not
    knowing the floor is not the same as the floor being satisfied."""

    @property
    def is_one_way(self) -> bool:
        return self.direction is not CruisingLevels.BOTH

    @property
    def band_known(self) -> bool:
        return self.floor_ft is not None or self.ceiling_ft is not None

    def admits(self, level_ft: float) -> bool | None:
        """Whether this airway publishes a band containing that level.

        ``None`` where no band is published — which is not permission and not
        refusal, and is why the caller must not treat it as either.
        """
        if not self.band_known:
            return None
        if self.floor_ft is not None and level_ft < self.floor_ft:
            return False
        if self.ceiling_ft is not None and level_ft > self.ceiling_ft:
            return False
        return True

    def describe(self) -> str:
        parts = [self.route]
        if self.regions:
            parts.append("/".join(self.regions))
        if self.floor_ft is not None and self.ceiling_ft is not None:
            parts.append(f"{self.floor_ft:.0f}–{self.ceiling_ft:.0f} ft")
        elif self.floor_ft is not None:
            parts.append(f"{self.floor_ft:.0f} ft and above, no maximum published")
        elif self.ceiling_ft is not None:
            parts.append(f"up to {self.ceiling_ft:.0f} ft, no minimum published")
        else:
            parts.append("no level band published")
        if self.is_one_way:
            parts.append(f"{self.direction.value} levels only")
        if self.navigation_specs:
            parts.append(", ".join(self.navigation_specs))
        if self.units:
            parts.append("; ".join(self.units))
        parts.append(
            f"{self.segments} segment{'' if self.segments == 1 else 's'}, "
            f"{len(self.points)} points"
        )
        if self.unbounded:
            parts.append(f"{self.unbounded} with no published floor")
        return "  ·  ".join(parts)


def profile_for(structure: AtsStructure, route: str) -> AirwayProfile | None:
    """Summarise one airway across every segment held for it.

    ``None`` where nothing is held for the designator, which is a coverage
    answer and not a statement that the airway does not exist.
    """
    segments = structure.on(route)
    if not segments:
        return None

    floors = [s.floor_ft for s in segments if s.floor_ft is not None]
    ceilings = [
        c
        for c in (
            s.maa_ft if s.maa_ft is not None else s.upper_limit_ft
            for s in segments
        )
        if c is not None
    ]
    directions = {s.direction for s in segments}
    return AirwayProfile(
        route=normalise(route),
        points=structure.points_on(route),
        segments=len(segments),
        regions=tuple(sorted({s.region for s in segments if s.region})),
        # Highest floor and lowest ceiling: the band available end to end.
        floor_ft=max(floors) if floors else None,
        ceiling_ft=min(ceilings) if ceilings else None,
        direction=(
            directions.pop()
            if len(directions) == 1
            else CruisingLevels.BOTH
        ),
        navigation_specs=tuple(
            sorted({s.navigation_spec for s in segments if s.navigation_spec})
        ),
        units=tuple(
            sorted({s.controlling_unit for s in segments if s.controlling_unit})
        ),
        unbounded=sum(1 for s in segments if s.floor_ft is None),
    )


@dataclass(frozen=True, slots=True)
class EnrouteChart:
    """A route structure chart, and everything it could not draw."""

    view: PlanView
    profiles: tuple[AirwayProfile, ...] = ()
    regions: tuple[str, ...] = ()
    level_ft: float | None = None
    excluded: tuple[tuple[str, str], ...] = ()
    """Airways set aside by the level filter, each with why. Not deleted and
    not dimmed: an airway whose band does not contain the level is not an
    option at that level, and the count travels with the drawing."""

    unbanded: tuple[str, ...] = ()
    """Airways drawn despite the level filter because they publish no band.
    Not knowing the floor is not the same as the floor being satisfied."""

    @property
    def is_conclusive(self) -> bool:
        """Whether everything the structure names could be drawn.

        False while any point is unplottable or any airway is short of the
        points it publishes.
        """
        return self.view.is_complete

    def profile(self, route: str) -> AirwayProfile | None:
        wanted = normalise(route)
        return next((p for p in self.profiles if p.route == wanted), None)

    def render(self) -> str:
        lines = [
            "ATS ROUTE STRUCTURE — ENR 3, drawn",
            f"{len(self.regions) or 'all'} regions"
            f"  ·  {len(self.profiles)} airways drawn"
            f"  ·  {len(self.view.points)} points plotted"
            + (
                f"  ·  at {self.level_ft:.0f} ft"
                if self.level_ft is not None
                else ""
            ),
        ]
        if self.view.unplottable:
            lines += [
                "",
                "!! NO PUBLISHED POSITION — named by ENR 3 and not placed",
                "   " + ", ".join(self.view.unplottable),
                "   Listed rather than drawn. A point in the wrong place is a "
                "map; a point missing is a gap.",
            ]
        partial = [a for a in self.view.airways if a.gaps]
        if partial:
            lines += ["", "DRAWN THROUGH FEWER POINTS THAN PUBLISHED"]
            for airway in partial:
                lines.append(
                    f"  {airway.route}: {len(airway.positions)} of "
                    f"{len(airway.positions) + airway.gaps} points — a "
                    "different shape from the published airway"
                )
        if self.excluded:
            lines += ["", f"NOT AVAILABLE AT {self.level_ft:.0f} FT"]
            for route, why in self.excluded:
                lines.append(f"  {route}: {why}")
        if self.unbanded:
            lines += [
                "",
                "DRAWN WITHOUT A LEVEL CHECK — no band published",
                "  " + ", ".join(self.unbanded),
                "  Not knowing the floor is not the same as the floor being "
                "satisfied.",
            ]
        if self.profiles:
            lines += ["", "AIRWAYS"]
            for profile in self.profiles:
                lines.append(f"  {profile.describe()}")
        return "\n".join(lines)


def chart_for(
    structure: AtsStructure,
    *,
    regions: Iterable[str] = (),
    routes: Iterable[str] = (),
    navaids: NavaidRegister | None = None,
    aerodromes: Mapping[str, Position] | None = None,
    level_ft: float | None = None,
    closed_routes: Iterable[str] = (),
    notams: NotamRegister | None = None,
    at: datetime | None = None,
    route_points: Iterable[str] = (),
    title: str = "",
) -> EnrouteChart:
    """Draw the published route structure.

    ``regions`` scopes the chart to one or more FIRs; empty draws everything
    held. ``routes`` narrows further to named airways. ``level_ft`` filters to
    the airways publishing a band that contains it, and says what it set
    aside.

    Positions come from ENR 4.4 through the structure's own significant points
    and from ENR 4.1 through ``navaids``. Nothing is placed from anywhere else,
    so an ENR 3 read without a coordinate table draws no geometry and says so.
    """
    wanted_regions = tuple(
        dict.fromkeys(normalise(r) for r in regions if normalise(r))
    )
    named_routes = {normalise(r) for r in routes if str(r).strip()}

    segments: tuple[RouteSegment, ...] = structure.segments
    if wanted_regions:
        segments = tuple(s for s in segments if s.region in wanted_regions)
    if named_routes:
        segments = tuple(s for s in segments if s.route in named_routes)

    in_scope = tuple(dict.fromkeys(s.route for s in segments))
    profiles = [p for p in (profile_for(structure, r) for r in in_scope) if p]

    drawn: list[AirwayProfile] = []
    excluded: list[tuple[str, str]] = []
    unbanded: list[str] = []
    for profile in profiles:
        if level_ft is None:
            drawn.append(profile)
            continue
        verdict = profile.admits(level_ft)
        if verdict is None:
            # Never filtered out. A coverage gap must not read as a level
            # restriction.
            unbanded.append(profile.route)
            drawn.append(profile)
        elif verdict:
            drawn.append(profile)
        else:
            excluded.append((profile.route, _why_excluded(profile, level_ft)))

    # Positions: ENR 4.4 through the structure, then ENR 4.1 for anything the
    # structure names without coordinates of its own.
    positions: dict[str, Position] = {}
    for point in structure.points:
        held = point.position
        if held is not None:
            positions[point.designator] = held
    navaid_names: list[str] = []
    if navaids is not None:
        for aid in navaids:
            held = aid.position
            if held is None:
                continue
            navaid_names.append(aid.ident)
            positions.setdefault(aid.ident, held)
    for designator, position in (aerodromes or {}).items():
        positions[normalise(designator)] = position

    airways = {p.route: p.points for p in drawn}
    details = {
        point.designator: _point_detail(point)
        for point in structure.points
    }
    if navaids is not None:
        for aid in navaids:
            details.setdefault(aid.ident, aid.describe())

    marks: list[tuple] = []
    if notams is not None and at is not None:
        for profile in drawn:
            for message, state in notams.at(named(ATS_ROUTE, profile.route), at):
                marks.append((named(ATS_ROUTE, profile.route), message, state))
        for designator in dict.fromkeys(list(positions) + list(details)):
            for key in (f"FIX:{designator}", f"NAVAID:{designator}"):
                for message, state in notams.at(key, at):
                    marks.append((key, message, state))

    view = plan_view(
        positions=positions,
        route_points=tuple(normalise(p) for p in route_points if str(p).strip()),
        airways=airways,
        navaids=navaid_names,
        aerodromes=tuple(normalise(a) for a in (aerodromes or {})),
        closed_routes=closed_routes,
        notams=marks,
        details=details,
        airway_details={p.route: p.describe() for p in drawn},
        one_way=[p.route for p in drawn if p.is_one_way],
        title=title
        or (
            f"ATS routes — {', '.join(wanted_regions)}"
            if wanted_regions
            else "ATS routes"
        ),
    )

    return EnrouteChart(
        view=view,
        profiles=tuple(drawn),
        regions=wanted_regions,
        level_ft=level_ft,
        excluded=tuple(excluded),
        unbanded=tuple(unbanded),
    )


def _why_excluded(profile: AirwayProfile, level_ft: float) -> str:
    if profile.floor_ft is not None and level_ft < profile.floor_ft:
        return (
            f"the highest published minimum on it is {profile.floor_ft:.0f} ft"
        )
    return f"the lowest published maximum on it is {profile.ceiling_ft:.0f} ft"


def _point_detail(point) -> str:
    parts = [point.kind.value.replace("_", " ")]
    if point.name:
        parts.append(point.name)
    if point.reporting:
        parts.append(point.reporting)
    return "  ·  ".join(parts)


def chart_html(chart: EnrouteChart) -> str:
    """The chart as a standalone page, with the route structure beneath it."""
    return plan_html(chart.view)
