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

What this module refuses to compute, and why
--------------------------------------------
**Departure sector membership.** Deciding whether an obstacle lies inside the
protected area needs the full PANS-OPS construction — splay rates, area widths
at distance, turning-departure geometry. Approximating it would produce a
confident yes or no about whether an obstacle matters at all, which is the worst
possible thing to be approximately right about. So an obstacle's distance and
bearing from the runway end are reported, and whether it sits in the protected
area is left to the procedure designer who has the criteria.

**The engine-out net flight path.** The plan assigns this to an engineer and
says designing an EOSID is certified engineering work. This module flags
obstacles for that review; it does not do it.

The result is a module that answers "how steep, and is that steeper than
standard" exactly, and says plainly that "is this obstacle in my departure
sector" is a question it has not been given the criteria to answer.

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
    "STANDARD_PDG_PERCENT",
    "FleetExposure",
    "Obstacle",
    "ObstacleChange",
    "ObstacleReview",
    "Penetration",
    "compare_cycles",
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


def required_gradient(obstacle: Obstacle) -> float | None:
    """The climb gradient needed to clear this obstacle, as a percentage.

    The obstacle's own gradient plus the 0.8% minimum obstacle clearance, which
    is how a promulgated steeper gradient is constructed. Returns ``None`` where
    either figure is not held — a gradient computed from a guessed distance is
    worse than no gradient.
    """
    if not obstacle.is_measurable:
        return None
    own = (obstacle.height_above_der_m / obstacle.distance_from_der_m) * 100.0
    return round(own + MOC_PERCENT, 2)


def penetrates_ois(obstacle: Obstacle, *, ois_origin_m: float = 0.0) -> Penetration:
    """Whether the obstacle stands above the obstacle identification surface.

    ``ois_origin_m`` is the height of the surface at the DER. FAA TERPS starts
    it at DER elevation, ICAO PANS-OPS 5 m above; the default of zero is the
    lower surface, so more obstacles are reported as penetrating. That is the
    conservative direction, and conservative is the right default for a check
    whose false negative is an aeroplane climbing into something.
    """
    if not obstacle.is_measurable:
        return Penetration.UNKNOWN
    surface = ois_origin_m + (OIS_PERCENT / 100.0) * obstacle.distance_from_der_m
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

    def gradient_for(self, obstacle: Obstacle) -> float | None:
        return required_gradient(obstacle)

    @property
    def penetrating(self) -> tuple[Obstacle, ...]:
        return tuple(
            o for o in self.obstacles
            if penetrates_ois(o, ois_origin_m=self.ois_origin_m)
            is Penetration.PENETRATES
        )

    @property
    def unmeasured(self) -> tuple[Obstacle, ...]:
        """Held, but without the figures the gradient needs. Not clear."""
        return tuple(o for o in self.obstacles if not o.is_measurable)

    @property
    def governing(self) -> Obstacle | None:
        """The obstacle demanding the steepest gradient."""
        measured = [o for o in self.obstacles if o.is_measurable]
        return max(measured, key=lambda o: required_gradient(o), default=None)

    @property
    def required_percent(self) -> float | None:
        governing = self.governing
        return required_gradient(governing) if governing else None

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
                lines.append(
                    f"  {obstacle.describe()} — needs "
                    f"{required_gradient(obstacle):g}%"
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

        lines += [
            "",
            "Whether any of these lies inside the protected departure area is "
            "not decided here.",
            "That needs the full PANS-OPS construction, and approximating it "
            "would be a confident",
            "answer about whether an obstacle matters at all. Distances and "
            "bearings are given so",
            "a procedure designer can.",
        ]
        return "\n".join(lines)


def review_runway(
    runway: str,
    obstacles: Iterable[Obstacle],
    *,
    fleet: Iterable[AircraftType] = (),
    previous: Iterable[Obstacle] | None = None,
    ois_origin_m: float = 0.0,
) -> ObstacleReview:
    """Assemble the obstacle picture for one runway end."""
    held = tuple(obstacles)
    return ObstacleReview(
        runway=runway,
        obstacles=held,
        fleet=tuple(fleet),
        changes=compare_cycles(previous, held) if previous is not None else (),
        ois_origin_m=ois_origin_m,
    )
