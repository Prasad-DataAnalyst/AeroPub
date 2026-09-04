"""One flight, one aeroplane, one date — the business aviation entry point.

An airline has a persistent network. A flight department has no network at all:
it flies to airports it has never studied, at days' or hours' notice, and the
question is not "what changed this cycle" but **"can I take this aeroplane into
that airport on Thursday, and what will bite me?"**

The plan calls this the faster path to a first paying customer — acute pain, no
incumbent team defending the status quo, short procurement. It is also the
lighter build, because a :class:`Trip` is not a second engine. It produces an
:class:`~aeropub.operator.OperatorProfile` and everything downstream runs
unchanged: same suitability checks, same layer-three grading, same citations.
Different entry point, identical rules.

The two things a trip does that a network cannot
------------------------------------------------
**It is assessed for the day of the flight, not today.** A network sweep asks
what is true now. A trip on the 25th must be resolved against the effective
state *on the 25th* — a supplement that lapses on the 21st has already lapsed
by the time this aeroplane arrives, and assessing it today would clear an
aerodrome that will not be clear when it matters.

**It reports what changes between now and then.** This is the part no
dispatcher can do by hand and the reason the forward view exists: *"it is fine
today; on the 21st a supplement lapses and the fire category drops below what
you need, and nobody will publish a word about it."* Three weeks of notice on a
trip that has already been quoted.

What binds a business aviation trip
-----------------------------------
Different constraints from an airline's, and the plan lists them: runway length,
RFFS availability, PPR lead time, customs and immigration hours, FBO and fuel,
noise curfews. Several of those live in sections that are rarely read — AD 2.3
operational hours, AD 2.20 local regulations, AD 2.21 noise abatement — so a gap
in them is named specifically rather than folded into a generic coverage list.
"We do not know whether it is open when you arrive" is a different sentence from
"AD 2.3 not held", and only one of them stops a crew.

Expiry
------
A trip is a question about a date. Once that date has passed it is not a live
question, and :meth:`Trip.is_expired` says so rather than letting a stale
assessment sit in a list looking current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from aeropub.aip import AipCoverage, HoldingState
from aeropub.aircraft import AircraftType
from aeropub.dossier import build
from aeropub.entities import normalise
from aeropub.horizon import DEFAULT_DAYS, Transition, horizon
from aeropub.notam_register import NotamRegister
from aeropub.operator import (
    Exposure,
    Fleet,
    Network,
    NetworkEntry,
    OperatorAssessment,
    OperatorProfile,
    Role,
    assess_operator,
    worst_exposure,
)

__all__ = [
    "BIZAV_SECTIONS",
    "LegAssessment",
    "Trip",
    "TripAssessment",
    "assess_trip",
]

#: The AIP sections a business aviation trip turns on, and what a gap in each
#: actually means to the person asking. Named individually because "AD 2.3 not
#: held" and "we do not know whether it is open when you arrive" are different
#: sentences, and only the second one stops a crew.
BIZAV_SECTIONS: dict[str, str] = {
    "AD 2.3": "whether the aerodrome is open when you arrive, and what PPR "
    "lead time applies",
    "AD 2.6": "whether the fire category covers this aeroplane, and at what "
    "hours",
    "AD 2.12": "runway dimensions and pavement strength",
    "AD 2.13": "declared distances",
    "AD 2.20": "local regulations — PPR, handling, customs and immigration",
    "AD 2.21": "noise restrictions and curfew",
}


@dataclass(frozen=True, slots=True)
class Trip:
    """One flight: an aeroplane, a date, and the aerodromes it depends on."""

    reference: str
    """The operator's own trip number. Theirs, not ours — it is what they will
    quote back when they ring about it."""

    aircraft: AircraftType
    on: date
    departure: str
    destination: str
    alternates: tuple[str, ...] = ()
    """Destination alternates. Nominated, so a failure at one is a swap rather
    than a cancellation — unless it was the only one."""

    takeoff_alternate: str | None = None
    """Required when the departure aerodrome is below landing minima, and
    relied on at dispatch with no time to substitute."""

    enroute_alternates: tuple[str, ...] = ()
    operator: str = ""

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("a trip needs a reference — the operator's own")
        for name in ("departure", "destination"):
            if not normalise(getattr(self, name)):
                raise ValueError(f"a trip needs a {name}")
        object.__setattr__(self, "departure", normalise(self.departure))
        object.__setattr__(self, "destination", normalise(self.destination))
        object.__setattr__(
            self, "alternates", tuple(normalise(a) for a in self.alternates)
        )
        object.__setattr__(
            self,
            "enroute_alternates",
            tuple(normalise(a) for a in self.enroute_alternates),
        )
        if self.takeoff_alternate:
            object.__setattr__(
                self, "takeoff_alternate", normalise(self.takeoff_alternate)
            )

    @property
    def aerodromes(self) -> tuple[str, ...]:
        """Every aerodrome this flight depends on, deduplicated, in order."""
        ordered = [self.departure, self.destination]
        ordered += list(self.alternates)
        if self.takeoff_alternate:
            ordered.append(self.takeoff_alternate)
        ordered += list(self.enroute_alternates)
        seen: list[str] = []
        for key in ordered:
            if key and key not in seen:
                seen.append(key)
        return tuple(seen)

    @property
    def sole_alternate(self) -> bool:
        """Whether exactly one destination alternate is nominated.

        Derived, not declared. A single nominated alternate has nothing to swap
        to, which is the same condition an airline records as sole-suitable —
        and a flight department nominating one alternate is usually not
        thinking of it that way.
        """
        return len(self.alternates) == 1

    def role_of(self, aerodrome: str) -> Role:
        key = normalise(aerodrome)
        if key == self.destination:
            return Role.DESTINATION
        if key == self.takeoff_alternate:
            return Role.TAKEOFF_ALTERNATE
        if key in self.alternates:
            return Role.ALTERNATE
        if key in self.enroute_alternates:
            return Role.EDTO_ALTERNATE
        if key == self.departure:
            # A departure aerodrome is landed at only if something goes wrong,
            # but the aeroplane must physically fit it to leave, so the fit
            # checks apply exactly as they do at a destination.
            return Role.DESTINATION
        return Role.NOT_IN_NETWORK

    def as_profile(self) -> OperatorProfile:
        """The same engine, entered differently.

        A trip is a one-flight network. Producing a profile rather than a
        parallel assessment path is what keeps a flight department's answer
        identical to an airline's for the same aeroplane at the same aerodrome
        — which it should be, because the aerodrome does not know who is
        asking.
        """
        return OperatorProfile(
            name=self.operator or f"trip {self.reference}",
            fleet=Fleet((self.aircraft,)),
            network=Network(
                tuple(
                    NetworkEntry(
                        aerodrome=key,
                        role=self.role_of(key),
                        sole_suitable=(
                            self.sole_alternate and key in self.alternates
                        ),
                        group=f"trip {self.reference} alternates"
                        if key in self.alternates
                        else "",
                    )
                    for key in self.aerodromes
                )
            ),
        )

    def days_away(self, as_of: date) -> int:
        return (self.on - as_of).days

    def is_expired(self, as_of: date) -> bool:
        """Whether the flight date has passed.

        A trip is a question about a date. Once it is behind us the answer is
        history, and a stale assessment sitting in a list looking current is
        the failure this exists to prevent.
        """
        return self.on < as_of


@dataclass(frozen=True, slots=True)
class LegAssessment:
    """One aerodrome on the trip, assessed for the day of the flight."""

    aerodrome: str
    role: Role
    assessment: OperatorAssessment
    changes_before: tuple[Transition, ...] = ()
    """Dated changes taking effect between today and the flight."""

    unannounced_before: tuple[Transition, ...] = ()
    missing_sections: tuple[tuple[str, str], ...] = ()
    """The business-aviation sections not held, each with what its absence
    actually means. Section code and consequence, not code alone."""

    @property
    def exposure(self) -> Exposure:
        return self.assessment.overall

    @property
    def needs_action(self) -> bool:
        return self.exposure.needs_action

    def describe(self) -> str:
        ahead = (
            f"  ·  {len(self.changes_before)} change"
            f"{'s' if len(self.changes_before) != 1 else ''} before departure"
            if self.changes_before
            else ""
        )
        return (
            f"[{self.exposure.value}] {self.aerodrome} ({self.role.value}){ahead}"
        )


@dataclass(frozen=True, slots=True)
class TripAssessment:
    """One flight, assessed for its own date, with what changes before it."""

    trip: Trip
    as_at: datetime
    legs: tuple[LegAssessment, ...] = ()
    expired: bool = False

    @property
    def overall(self) -> Exposure:
        return worst_exposure(leg.exposure for leg in self.legs)

    @property
    def is_conclusive(self) -> bool:
        """Whether this answer covers everything a trip turns on.

        Not only whether the fit checks could be made. A trip whose fire
        category and pavement both check out, at an aerodrome whose opening
        hours nobody holds, is not a conclusive answer to "can I go on
        Thursday" — the aeroplane fits an aerodrome that may be shut. So a
        missing section from :data:`BIZAV_SECTIONS` makes this false, exactly
        as an unmade check does.
        """
        return bool(self.legs) and all(
            leg.assessment.is_conclusive and not leg.missing_sections
            for leg in self.legs
        )

    @property
    def blocking(self) -> tuple[LegAssessment, ...]:
        return tuple(
            sorted(
                (leg for leg in self.legs if leg.needs_action),
                key=lambda leg: (leg.exposure.rank, leg.aerodrome),
            )
        )

    @property
    def changing_before_departure(self) -> tuple[LegAssessment, ...]:
        return tuple(leg for leg in self.legs if leg.changes_before)

    @property
    def unannounced_before_departure(self) -> tuple[LegAssessment, ...]:
        return tuple(leg for leg in self.legs if leg.unannounced_before)

    def leg(self, aerodrome: str) -> LegAssessment | None:
        key = normalise(aerodrome)
        return next((leg for leg in self.legs if leg.aerodrome == key), None)

    def render(self) -> str:
        days = self.trip.days_away(self.as_at.date())
        when = (
            f"{self.trip.on:%Y-%m-%d}"
            + (f", T+{days}" if days > 0 else ", today" if days == 0 else "")
        )
        lines = [
            f"TRIP {self.trip.reference} — {self.trip.aircraft.designator}",
            f"{self.trip.departure} to {self.trip.destination} on {when}",
            f"assessed as at {self.as_at:%Y-%m-%d %H:%MZ}, for the state in "
            f"force on {self.trip.on:%Y-%m-%d}",
            "",
        ]
        if self.expired:
            lines += [
                "!! This trip's date has passed. The assessment below is "
                "history, not a live answer.",
                "",
            ]
        lines.append(
            f"Overall: {self.overall.value.upper()}"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE")
        )
        lines.append("")

        if not self.legs:
            lines.append(
                "No aerodromes on this trip. An empty trip is not a clear one."
            )
            return "\n".join(lines)

        lines.append("LEGS")
        lines += [f"  {leg.describe()}" for leg in self.legs]

        if self.blocking:
            lines += ["", "NEEDS ACTION BEFORE DEPARTURE"]
            for leg in self.blocking:
                lines.append(f"  {leg.aerodrome} ({leg.role.value})")
                for finding in leg.assessment.actionable[:3]:
                    lines.append(f"      {finding.describe()}")

        changing = self.changing_before_departure
        if changing:
            lines += [
                "",
                f"CHANGES BETWEEN NOW AND DEPARTURE — {days} day"
                f"{'s' if days != 1 else ''} away",
            ]
            for leg in changing:
                lines.append(f"  {leg.aerodrome}")
                for transition in leg.changes_before[:4]:
                    mark = (
                        "  <- nothing will be published"
                        if transition in leg.unannounced_before
                        else ""
                    )
                    lines.append(f"      {transition.describe()}{mark}")

        gaps = [leg for leg in self.legs if leg.missing_sections]
        if gaps:
            lines += [
                "",
                "NOT HELD — these are the ones that bite a business aviation "
                "trip.",
                "The aeroplane fitting an aerodrome says nothing about whether "
                "it is open when you arrive.",
            ]
            for leg in gaps:
                lines.append(f"  {leg.aerodrome}")
                for code, meaning in leg.missing_sections:
                    lines.append(f"      {code:9} {meaning}")

        lines += [
            "",
            "A fit and exposure assessment for the day of the flight. It is "
            "not a performance",
            "calculation, not a dispatch release, and not a substitute for the "
            "published AIP.",
        ]
        return "\n".join(lines)


def _missing_bizav_sections(
    coverage: AipCoverage, aerodrome: str
) -> tuple[tuple[str, str], ...]:
    """Which trip-binding sections are not held, and what each absence means."""
    missing: list[tuple[str, str]] = []
    for code, meaning in BIZAV_SECTIONS.items():
        holding = coverage.holding(aerodrome, code)
        if holding.state is not HoldingState.HELD:
            missing.append((code, meaning))
    return tuple(missing)


def assess_trip(
    store,
    trip: Trip,
    *,
    as_at: datetime | None = None,
    register: NotamRegister | None = None,
    coverage: AipCoverage | None = None,
) -> TripAssessment:
    """Assess one flight, for its own date.

    The effective state is resolved for ``trip.on`` rather than for today,
    because a supplement that lapses before the flight has already lapsed by
    the time the aeroplane arrives. The forward view runs from today to the
    flight date, so what changes in between is reported rather than discovered
    on the ramp.
    """
    moment = as_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")
    today = moment.date()
    held = coverage or AipCoverage()
    profile = trip.as_profile()

    legs: list[LegAssessment] = []
    for aerodrome in trip.aerodromes:
        dossier = build(
            aerodrome,
            facts=store,
            coverage=held,
            register=register or NotamRegister(),
            as_at=moment,
            on=trip.on,
        )
        # The forward window runs from today to the flight, not the default 84
        # days: what changes after the aeroplane has left is somebody else's
        # trip.
        days = max(trip.days_away(today), 0)
        ahead = (
            horizon(store, aerodrome, from_date=today, days=days)
            if days
            else None
        )
        legs.append(
            LegAssessment(
                aerodrome=aerodrome,
                role=trip.role_of(aerodrome),
                assessment=assess_operator(dossier, profile),
                changes_before=ahead.transitions if ahead else (),
                unannounced_before=ahead.unannounced if ahead else (),
                missing_sections=_missing_bizav_sections(held, aerodrome),
            )
        )

    return TripAssessment(
        trip=trip,
        as_at=moment,
        legs=tuple(legs),
        expired=trip.is_expired(today),
    )
