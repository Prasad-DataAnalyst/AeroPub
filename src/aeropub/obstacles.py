"""Obstacles — the arithmetic that is exact, and the geometry that is not.

The plan calls the obstacle alert the highest-value single alert in the
platform, and it is right: a crane appearing off the end of a runway changes
the required climb gradient, and it appears by NOTAM between cycles rather than
at a review.

What this module computes, and why it can
-----------------------------------------
The decisive number is **the climb gradient required to clear an obstacle**, and
it is exact arithmetic on two published figures — how high the obstacle stands
above the runway end, and how far beyond it. Both come straight from AD 2.10 or
a crane NOTAM. The criteria are settled and agree between ICAO PANS-OPS and FAA
TERPS:

- the **obstacle identification surface** rises at **2.5%** (40:1, 152 ft/NM)
  from the departure end of the runway;
- the **standard procedure design gradient** is **3.3%** (200 ft/NM);
- the difference is the **minimum obstacle clearance**, **0.8%** of the distance
  flown from the DER — so ``3.3 = 2.5 + 0.8`` is not a coincidence, it is the
  construction;
- where an obstacle penetrates the OIS, a steeper gradient is promulgated to
  restore that 0.8% clearance.

So the required gradient to clear an obstacle is its own gradient plus 0.8, and
that is what :func:`required_gradient` returns.

Departure area membership — computed, against a named convention
----------------------------------------------------------------
Whether an obstacle lies inside the protected area for a *straight* departure is
published geometry, not judgement: an area beginning 150 m either side of the
extended runway centreline at the DER and splaying outward. Testing whether a
point falls inside a defined shape is the same kind of work as deciding whether
an aeroplane is Code E, and :class:`DepartureArea` does it.

What is genuinely unsettled is the splay, and it is unsettled in the sources
rather than in the arithmetic. Two published surfaces both use the number 15
and mean different things: the classic PANS-OPS Doc 8168 departure area splays
**15 per cent** each side, while the newer Annex 14 obstacle limitation surface
for instrument departures splays **15 degrees**. They diverge quickly — at
5 NM the first is about 1.5 km wide each side and the second about 2.5 km.

So the convention is a parameter with named presets rather than a constant, and
every answer says which one it was computed against. A State may also publish a
non-standard area for a specific procedure, and where it has, that is the one
that governs — :attr:`DepartureArea.name` exists so a reader can see at a glance
that a standard shape was used rather than the State's own.

**The engine-out net flight path is still not computed here.** The plan assigns
it to an engineer and it is certified work: it depends on the aeroplane's actual
net performance, the operator's approved data and a designed escape path.
Obstacles are flagged for that review with the numbers it needs.

Fleet exposure
--------------
Which types are affected needs a climb gradient the aeroplane can actually
achieve, which is certified performance and stays with the operator under plan
decision D. An operator supplies it as a ``climb_gradient_pct`` characteristic
marked :attr:`~aeropub.aircraft.Origin.OPERATOR`, and it then never leaves their
tenant. Without it the gradient is reported and the fleet question is reported
as unanswered — never as "no types affected".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable

from aeropub.aircraft import AircraftType
from aeropub.provenance import SourceRef

__all__ = [
    "MOC_PERCENT",
    "OIS_PERCENT",
    "OLS_INSTRUMENT_DEPARTURE",
    "PANS_OPS_STRAIGHT",
    "STANDARD_PDG_PERCENT",
    "DepartureArea",
    "FleetExposure",
    "Obstacle",
    "ObstacleChange",
    "ObstacleReview",
    "Penetration",
    "Position",
    "compare_cycles",
    "decompose",
    "penetrates_ois",
    "required_gradient",
    "review_runway",
]

#: The obstacle identification surface, per ICAO PANS-OPS and FAA TERPS. Rises
#: from the departure end of the runway at 2.5% — 40:1, or 152 ft per nautical
#: mile.
OIS_PERCENT = 2.5

#: Minimum obstacle clearance in the primary area: 0.8% of the distance flown
#: from the DER. This is the margin a promulgated gradient restores over an
#: obstacle that penetrates the OIS.
MOC_PERCENT = 0.8

#: The standard procedure design gradient, 3.3% — 200 ft per nautical mile.
#: Exactly OIS + MOC, which is the construction rather than a coincidence.
STANDARD_PDG_PERCENT = OIS_PERCENT + MOC_PERCENT

#: One nautical mile in metres, for reporting distances the way charts do.
METRES_PER_NM = 1852.0

#: One metre in feet, for obstacle heights, which States publish either way.
FEET_PER_METRE = 3.280839895


# --------------------------------------------------------------------------
# The protected area for a straight departure
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Position:
    """An obstacle resolved relative to the departure track.

    ``along_track_m`` is the distance beyond the DER measured along the extended
    centreline, and it is the one the gradient arithmetic needs — an obstacle
    off to the side is not as far down the departure path as its straight-line
    range suggests. ``lateral_m`` is how far off that centreline it sits.
    """

    along_track_m: float
    lateral_m: float

    @property
    def offset_m(self) -> float:
        """Absolute distance from the extended centreline, either side."""
        return abs(self.lateral_m)

    @property
    def is_behind(self) -> bool:
        """Whether it lies behind the DER rather than beyond it.

        A departure area extends forward. An obstacle behind the departure end
        is not in it, and reporting a gradient for one would be arithmetic
        about a climb that has already happened.
        """
        return self.along_track_m <= 0


def decompose(
    *, distance_m: float, bearing_deg: float, runway_bearing_deg: float
) -> Position:
    """Resolve a radial position into along-track and lateral components.

    Plain trigonometry on the angle between the obstacle's bearing from the DER
    and the runway's own bearing. Exact, and it is the step that turns "2.1 NM
    on a bearing of 195" into the two numbers every other calculation here
    wants.
    """
    offset = math.radians(bearing_deg - runway_bearing_deg)
    return Position(
        along_track_m=distance_m * math.cos(offset),
        lateral_m=distance_m * math.sin(offset),
    )


@dataclass(frozen=True, slots=True)
class DepartureArea:
    """The protected area for a straight departure, as a shape that can be tested.

    Named rather than anonymous, because two published conventions share the
    number 15 and mean different things, and an answer that does not say which
    it used is not an answer. See the module docstring.
    """

    name: str
    half_width_at_der_m: float = 150.0
    splay_percent: float | None = None
    splay_degrees: float | None = None
    max_half_width_m: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if (self.splay_percent is None) == (self.splay_degrees is None):
            raise ValueError(
                f"{self.name}: give exactly one of splay_percent or "
                "splay_degrees. The two conventions both use the number 15 and "
                "mean different things, so accepting both at once would let a "
                "caller silently get the other one."
            )

    def half_width_at(self, along_track_m: float) -> float:
        """How wide the area is, either side of the centreline, at that distance."""
        if along_track_m <= 0:
            return self.half_width_at_der_m
        if self.splay_percent is not None:
            spread = along_track_m * (self.splay_percent / 100.0)
        else:
            spread = along_track_m * math.tan(math.radians(self.splay_degrees))
        width = self.half_width_at_der_m + spread
        if self.max_half_width_m is not None:
            width = min(width, self.max_half_width_m)
        return width

    def contains(self, position: Position) -> bool:
        """Whether a resolved position falls inside the area."""
        if position.is_behind:
            return False
        return position.offset_m <= self.half_width_at(position.along_track_m)

    def describe(self) -> str:
        splay = (
            f"{self.splay_percent:g}% each side"
            if self.splay_percent is not None
            else f"{self.splay_degrees:g} deg each side"
        )
        cap = (
            f", capped at {self.max_half_width_m:g} m"
            if self.max_half_width_m is not None
            else ""
        )
        return f"{self.name}: {self.half_width_at_der_m:g} m at the DER, {splay}{cap}"


#: The classic PANS-OPS Doc 8168 straight departure area: 150 m either side of
#: the extended centreline at the DER, splaying 15 per cent each side.
PANS_OPS_STRAIGHT = DepartureArea(
    name="PANS-OPS straight departure",
    half_width_at_der_m=150.0,
    splay_percent=15.0,
    note="ICAO Doc 8168 Volume II. Confirm against the current edition, and "
    "against any non-standard area the State has published for the procedure.",
)

#: The Annex 14 obstacle limitation surface for instrument departures, which
#: splays in degrees rather than per cent and therefore widens faster.
OLS_INSTRUMENT_DEPARTURE = DepartureArea(
    name="Annex 14 instrument departure surface",
    half_width_at_der_m=150.0,
    splay_degrees=15.0,
    note="The newer OLS convention. Wider than the PANS-OPS area at every "
    "distance beyond the DER, so an obstacle can be inside this and outside "
    "that.",
)


class Penetration(str, Enum):
    """Whether an obstacle stands above the obstacle identification surface."""

    CLEAR = "clear"
    """Below the OIS. The standard gradient carries the required clearance."""

    PENETRATES = "penetrates"
    """Above the OIS. A steeper gradient is required, and where the State has
    not published one that is a discrepancy worth raising."""

    UNKNOWN = "unknown"
    """Height or distance not held. Not the same as clear, and never rendered
    as though it were."""


@dataclass(frozen=True, slots=True)
class Obstacle:
    """One obstacle, as a State published it.

    Heights are kept as *above the runway end*, not above sea level, because
    that is what the gradient arithmetic needs and converting an elevation
    without the runway end elevation is the commonest way to get this wrong.
    Where a State publishes an elevation, the caller converts once, with the
    runway end elevation in hand, rather than this module guessing.
    """

    identifier: str
    source: SourceRef
    kind: str = ""
    """Crane, mast, building, terrain — as published, not normalised. A State's
    own word is what a reader will find on the chart."""

    height_above_der_m: float | None = None
    distance_from_der_m: float | None = None
    bearing_from_der_deg: float | None = None
    """True bearing. Reported, never used to decide sector membership — see the
    module docstring."""

    lighted: bool | None = None
    marked: bool | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    """Set for a temporary obstacle. A crane whose ``valid_to`` has moved four
    times is exactly what the works-programme view is for."""

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("an obstacle needs an identifier")
        if not isinstance(self.source, SourceRef):
            raise TypeError(
                f"{self.identifier}: an obstacle without provenance cannot "
                "exist. A crane nobody can cite is a rumour."
            )
        for name in ("height_above_der_m", "distance_from_der_m"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{self.identifier}: {name} cannot be negative")

    @property
    def is_temporary(self) -> bool:
        return self.valid_to is not None

    @property
    def height_ft(self) -> float | None:
        if self.height_above_der_m is None:
            return None
        return round(self.height_above_der_m * FEET_PER_METRE, 1)

    @property
    def distance_nm(self) -> float | None:
        if self.distance_from_der_m is None:
            return None
        return round(self.distance_from_der_m / METRES_PER_NM, 2)

    @property
    def is_measurable(self) -> bool:
        """Whether both figures the gradient needs are held."""
        return (
            self.height_above_der_m is not None
            and self.distance_from_der_m is not None
            and self.distance_from_der_m > 0
        )

    def position(self, runway_bearing_deg: float | None) -> Position | None:
        """Resolve against a runway bearing, or ``None`` if it cannot be.

        Without a bearing for the obstacle or for the runway there is nothing
        to decompose, and ``distance_from_der_m`` is then taken as already
        being along-track — which is what a State's AD 2.10 table usually
        gives.
        """
        if not self.is_measurable:
            return None
        if self.bearing_from_der_deg is None or runway_bearing_deg is None:
            return Position(along_track_m=self.distance_from_der_m, lateral_m=0.0)
        return decompose(
            distance_m=self.distance_from_der_m,
            bearing_deg=self.bearing_from_der_deg,
            runway_bearing_deg=runway_bearing_deg,
        )

    def describe(self) -> str:
        if not self.is_measurable:
            return f"{self.identifier} ({self.kind or 'obstacle'}) — position not held"
        window = ""
        if self.valid_to is not None:
            window = f", valid to {self.valid_to}"
        return (
            f"{self.identifier} ({self.kind or 'obstacle'}) — "
            f"{self.height_ft:g} ft at {self.distance_nm:g} NM{window}"
        )


def required_gradient(
    obstacle: Obstacle, *, runway_bearing_deg: float | None = None
) -> float | None:
    """The climb gradient needed to clear this obstacle, as a percentage.

    The obstacle's own gradient plus the 0.8% minimum obstacle clearance, which
    is how a promulgated steeper gradient is constructed.

    **Measured along track, not radially.** Where a bearing is held for both the
    obstacle and the runway, the distance is decomposed first: an obstacle off
    to one side is nearer down the departure path than its straight-line range
    suggests, so using the range under-reports the gradient — and under-reporting
    a required climb gradient errs in the direction that flies an aeroplane into
    something. Without bearings the held distance is taken as already along
    track, which is how AD 2.10 usually gives it.

    Returns ``None`` where a figure is not held, or where the obstacle resolves
    behind the departure end: a gradient for an obstacle already passed is
    arithmetic about a climb that has happened.
    """
    resolved = obstacle.position(runway_bearing_deg)
    if resolved is None or resolved.is_behind:
        return None
    own = (obstacle.height_above_der_m / resolved.along_track_m) * 100.0
    return round(own + MOC_PERCENT, 2)


def penetrates_ois(
    obstacle: Obstacle,
    *,
    ois_origin_m: float = 0.0,
    runway_bearing_deg: float | None = None,
) -> Penetration:
    """Whether the obstacle stands above the obstacle identification surface.

    ``ois_origin_m`` is the height of the surface at the DER. FAA TERPS starts
    it at DER elevation, ICAO PANS-OPS 5 m above; the default of zero is the
    lower surface, so more obstacles are reported as penetrating. That is the
    conservative direction, and conservative is the right default for a check
    whose false negative is an aeroplane climbing into something.
    """
    resolved = obstacle.position(runway_bearing_deg)
    if resolved is None:
        return Penetration.UNKNOWN
    if resolved.is_behind:
        return Penetration.CLEAR
    surface = ois_origin_m + (OIS_PERCENT / 100.0) * resolved.along_track_m
    return (
        Penetration.PENETRATES
        if obstacle.height_above_der_m > surface
        else Penetration.CLEAR
    )


@dataclass(frozen=True, slots=True)
class FleetExposure:
    """Which types can make the required gradient, and which were not assessed."""

    required_percent: float
    capable: tuple[str, ...] = ()
    incapable: tuple[str, ...] = ()
    unassessed: tuple[str, ...] = ()
    """Types with no climb gradient held. Reported by name, never folded into
    ``capable`` — "we did not check" and "it is fine" are opposite answers."""

    @property
    def is_conclusive(self) -> bool:
        return not self.unassessed

    def describe(self) -> str:
        parts = [f"{self.required_percent:g}% required"]
        if self.incapable:
            parts.append(f"beyond {', '.join(self.incapable)}")
        if self.capable:
            parts.append(f"within {', '.join(self.capable)}")
        if self.unassessed:
            parts.append(
                f"not assessed for {', '.join(self.unassessed)} — no climb "
                "gradient held"
            )
        return "; ".join(parts)


def _fleet_exposure(
    required: float, fleet: Iterable[AircraftType]
) -> FleetExposure:
    capable: list[str] = []
    incapable: list[str] = []
    unassessed: list[str] = []
    for aircraft in fleet:
        held = aircraft.value("climb_gradient_pct")
        if held is None:
            unassessed.append(aircraft.designator)
        elif float(held) >= required:
            capable.append(aircraft.designator)
        else:
            incapable.append(aircraft.designator)
    return FleetExposure(
        required_percent=required,
        capable=tuple(sorted(capable)),
        incapable=tuple(sorted(incapable)),
        unassessed=tuple(sorted(unassessed)),
    )


@dataclass(frozen=True, slots=True)
class ObstacleChange:
    """One obstacle, as it stood in two cycles."""

    identifier: str
    before: Obstacle | None
    after: Obstacle | None

    @property
    def appeared(self) -> bool:
        return self.before is None and self.after is not None

    @property
    def removed(self) -> bool:
        return self.before is not None and self.after is None

    @property
    def raised(self) -> bool:
        """Grew taller, which is the change that costs gradient."""
        return (
            self.before is not None
            and self.after is not None
            and self.before.height_above_der_m is not None
            and self.after.height_above_der_m is not None
            and self.after.height_above_der_m > self.before.height_above_der_m
        )

    @property
    def extended(self) -> bool:
        """A temporary obstacle whose end date moved out.

        A crane extended four times is one works programme, not four
        unrelated messages, and this is the signal that clusters them.
        """
        return (
            self.before is not None
            and self.after is not None
            and self.before.valid_to is not None
            and self.after.valid_to is not None
            and self.after.valid_to > self.before.valid_to
        )

    @property
    def changed(self) -> bool:
        return self.appeared or self.removed or self.raised or self.extended

    def describe(self) -> str:
        if self.appeared:
            return f"NEW      {self.after.describe()}"
        if self.removed:
            return f"REMOVED  {self.before.describe()}"
        if self.raised:
            return (
                f"RAISED   {self.identifier} — {self.before.height_ft:g} ft to "
                f"{self.after.height_ft:g} ft"
            )
        if self.extended:
            return (
                f"EXTENDED {self.identifier} — end date {self.before.valid_to} "
                f"to {self.after.valid_to}"
            )
        return f"unchanged {self.identifier}"


def compare_cycles(
    before: Iterable[Obstacle], after: Iterable[Obstacle]
) -> tuple[ObstacleChange, ...]:
    """What moved between two cycles, keyed by obstacle identifier.

    Identity is the State's own identifier. Matching on position instead would
    be a guess about whether two readings describe one obstacle, and a wrong
    guess reads as a removal plus an appearance — which is exactly the alert
    somebody would act on.
    """
    was = {o.identifier: o for o in before}
    now = {o.identifier: o for o in after}
    return tuple(
        ObstacleChange(identifier=key, before=was.get(key), after=now.get(key))
        for key in sorted(set(was) | set(now))
    )


@dataclass(frozen=True, slots=True)
class ObstacleReview:
    """Every obstacle held for one runway end, and what it requires."""

    runway: str
    obstacles: tuple[Obstacle, ...] = ()
    fleet: tuple[AircraftType, ...] = ()
    changes: tuple[ObstacleChange, ...] = ()
    ois_origin_m: float = 0.0

    runway_bearing_deg: float | None = None
    """True bearing of the departure track. With it, radial positions resolve
    into along-track distance and lateral offset; without it, held distances are
    taken as already along track."""

    area: DepartureArea | None = None
    """Which protected area to test against, or ``None`` to skip the test.

    Never defaulted. Two published conventions share the number 15 and mean
    different things, so silently picking one would give an answer whose basis
    the reader cannot see."""

    def gradient_for(self, obstacle: Obstacle) -> float | None:
        return required_gradient(
            obstacle, runway_bearing_deg=self.runway_bearing_deg
        )

    def position_of(self, obstacle: Obstacle) -> Position | None:
        return obstacle.position(self.runway_bearing_deg)

    def contains(self, obstacle: Obstacle) -> bool | None:
        """Whether the obstacle is inside the protected area.

        ``None`` where no area was given or the position cannot be resolved —
        which is not the same as ``False`` and must not print as though it were.
        """
        if self.area is None:
            return None
        resolved = self.position_of(obstacle)
        return self.area.contains(resolved) if resolved else None

    @property
    def inside_area(self) -> tuple[Obstacle, ...]:
        return tuple(o for o in self.obstacles if self.contains(o) is True)

    @property
    def outside_area(self) -> tuple[Obstacle, ...]:
        return tuple(o for o in self.obstacles if self.contains(o) is False)

    @property
    def penetrating(self) -> tuple[Obstacle, ...]:
        return tuple(
            o for o in self.obstacles
            if penetrates_ois(
                o, ois_origin_m=self.ois_origin_m,
                runway_bearing_deg=self.runway_bearing_deg,
            )
            is Penetration.PENETRATES
        )

    @property
    def unmeasured(self) -> tuple[Obstacle, ...]:
        """Held, but without the figures the gradient needs. Not clear."""
        return tuple(o for o in self.obstacles if not o.is_measurable)

    @property
    def assessable(self) -> tuple[Obstacle, ...]:
        """Obstacles the gradient can be computed for.

        Where an area is given, obstacles outside it are excluded — that is the
        point of having one. Where none is given, every measurable obstacle
        counts, which is the conservative reading.
        """
        return tuple(
            o
            for o in self.obstacles
            if self.gradient_for(o) is not None and self.contains(o) is not False
        )

    @property
    def governing(self) -> Obstacle | None:
        """The obstacle demanding the steepest gradient."""
        return max(
            self.assessable, key=lambda o: self.gradient_for(o), default=None
        )

    @property
    def required_percent(self) -> float | None:
        governing = self.governing
        return self.gradient_for(governing) if governing else None

    @property
    def exceeds_standard(self) -> bool:
        required = self.required_percent
        return required is not None and required > STANDARD_PDG_PERCENT

    def exposure(self) -> FleetExposure | None:
        required = self.required_percent
        if required is None:
            return None
        return _fleet_exposure(required, self.fleet)

    def render(self) -> str:
        lines = [f"OBSTACLES — {self.runway}", ""]
        if not self.obstacles:
            lines.append(
                "No obstacles held for this runway end. That is a coverage gap, "
                "not a clear departure sector."
            )
            return "\n".join(lines)

        required = self.required_percent
        if required is None:
            lines.append(
                f"{len(self.obstacles)} obstacles held, none with both a height "
                "and a distance."
            )
            lines.append(
                "No gradient can be computed, and none is assumed."
            )
        else:
            standard = (
                f"steeper than the standard {STANDARD_PDG_PERCENT:g}%"
                if self.exceeds_standard
                else f"within the standard {STANDARD_PDG_PERCENT:g}%"
            )
            lines.append(f"Governing gradient: {required:g}% — {standard}")
            lines.append(f"  set by {self.governing.describe()}")
            resolved = self.position_of(self.governing)
            if resolved is not None and self.runway_bearing_deg is not None:
                lines.append(
                    f"  {resolved.along_track_m / METRES_PER_NM:.2f} NM along "
                    f"track, {resolved.offset_m:.0f} m off the centreline"
                )
            exposed = self.exposure()
            if exposed is not None and self.fleet:
                lines.append(f"  {exposed.describe()}")

        if self.penetrating:
            lines += [
                "",
                f"PENETRATES THE {OIS_PERCENT:g}% SURFACE — "
                f"{len(self.penetrating)} of {len(self.obstacles)}",
            ]
            for obstacle in self.penetrating:
                # self.gradient_for, not the bare function: the review knows
                # the runway bearing and the bare call does not, which printed
                # two different gradients for one obstacle in one document.
                lines.append(
                    f"  {obstacle.describe()} — needs "
                    f"{self.gradient_for(obstacle):g}%"
                )

        if self.unmeasured:
            lines += [
                "",
                "POSITION NOT HELD — these are not clear, they are unmeasured",
            ]
            lines += [f"  {o.describe()}" for o in self.unmeasured]

        moved = [c for c in self.changes if c.changed]
        if moved:
            lines += ["", "SINCE THE LAST CYCLE"]
            lines += [f"  {c.describe()}" for c in moved]

        if self.area is not None:
            lines += ["", f"PROTECTED AREA — {self.area.describe()}"]
            for obstacle in self.obstacles:
                inside = self.contains(obstacle)
                resolved = self.position_of(obstacle)
                where = (
                    "position not resolvable"
                    if resolved is None
                    else f"{resolved.offset_m:.0f} m off centreline at "
                    f"{resolved.along_track_m / METRES_PER_NM:.2f} NM"
                )
                mark = (
                    "INSIDE " if inside is True
                    else "outside" if inside is False
                    else "unknown"
                )
                lines.append(f"  {mark}  {obstacle.identifier:12} {where}")
            if self.area.note:
                lines.append(f"  {self.area.note}")
        else:
            lines += [
                "",
                "No protected area was given, so every measurable obstacle is "
                "counted. Pass one",
                "of PANS_OPS_STRAIGHT or OLS_INSTRUMENT_DEPARTURE to narrow "
                "this to the obstacles",
                "that actually lie in the departure area.",
            ]

        lines += [
            "",
            "The engine-out net flight path is not computed here. It depends on "
            "the aeroplane's",
            "net performance and the operator's approved data, and designing an "
            "escape path is",
            "certified engineering. The numbers that review needs are above.",
        ]
        return "\n".join(lines)


def review_runway(
    runway: str,
    obstacles: Iterable[Obstacle],
    *,
    fleet: Iterable[AircraftType] = (),
    previous: Iterable[Obstacle] | None = None,
    ois_origin_m: float = 0.0,
    runway_bearing_deg: float | None = None,
    area: DepartureArea | None = None,
) -> ObstacleReview:
    """Assemble the obstacle picture for one runway end.

    Supply ``runway_bearing_deg`` and an ``area`` to narrow the assessment to
    the obstacles that actually lie in the departure area, and to measure
    gradients along track rather than radially. Without them every measurable
    obstacle counts, which is the conservative reading and the right default.
    """
    held = tuple(obstacles)
    return ObstacleReview(
        runway=runway,
        obstacles=held,
        fleet=tuple(fleet),
        changes=compare_cycles(previous, held) if previous is not None else (),
        ois_origin_m=ois_origin_m,
        runway_bearing_deg=runway_bearing_deg,
        area=area,
    )
