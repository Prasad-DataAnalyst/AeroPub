"""ENR 5 — navigation warnings, and the ones that only NOTAM can settle.

ENR 5 is the other half of the en-route picture. :mod:`aeropub.airspace` holds
ENR 2, the airspace a flight is *inside*; this holds what the State publishes
as hazardous, restricted or forbidden within it. Six subsections, and they are
not six flavours of one thing:

============  ====================================================
ENR 5.1       Prohibited, restricted and danger areas
ENR 5.2       Military exercise and training areas, and ADIZ
ENR 5.3       Other activities of a dangerous nature
ENR 5.4       Air navigation obstacles — en-route
ENR 5.5       Aerial sporting and recreational activities
ENR 5.6       Bird migration and areas with sensitive fauna
============  ====================================================

Three verbs, not one severity scale
------------------------------------
Entry to a prohibited area is **forbidden**. Entry to a restricted area is
**conditional** — somebody has to satisfy something, and the published text
says who and how. A danger area **forbids nothing at all**: it warns that an
activity there is hazardous and leaves the decision to the commander. A screen
that reported all three as "airspace warning" would flatten a legal boundary
and a risk assessment into one word, so :class:`HazardKind` keeps the three
verbs apart and every roll-up here is grouped by verb rather than ranked.

Where NOTAM and AIP meet
-------------------------
An area active H24 is answered by the AIP alone. An area active *by NOTAM* is
the AIP saying the AIP is not enough — its published state is "ask the NOTAM",
and a planner who reads only ENR 5 has read half the answer.
:attr:`HazardScreen.needs_notam` is the list that sends them to the other half,
and :func:`notams_on_hazards` follows the pointer.

Seasons are real published facts
---------------------------------
Bird migration corridors and gliding seasons are published with months, not
hours. A migration corridor is not a finding in January and is one in April,
and treating a seasonal entry as continuous would bury the months that matter
under ten that do not. :class:`Activation` therefore carries ``SEASONAL``
alongside the clock-based kinds.

What this does not claim
------------------------
The same limit as everywhere else in the en-route work: **no geometry**. This
never says a route enters an area. Lateral containment needs coordinates for
the area and a track for the flight, and inventing either would produce the
most dangerous output this platform could make — a confident "clear of all
restricted airspace" from a system that never tested it. What it says instead
is what the crossed regions publish, which of it the planned level could not
rule out, and whose permission each one needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.airspace import describe_limits, read_limit
from aeropub.boundary import Boundary, read_boundary_manifest
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
    "Activation",
    "Clearance",
    "ClearanceFinding",
    "ClearanceKind",
    "HAZARD",
    "Hazard",
    "HazardKind",
    "HazardRegister",
    "HazardScreen",
    "hazard_template",
    "load_hazards",
    "notams_on_hazards",
    "screen_clearances",
    "screen_hazards",
]

#: The parser identity written into citations read from an ENR 5 manifest.
HAZARD_PARSER_ID = "aeropub.hazards"

#: The entity kind a navigation warning is keyed under. Free-standing: a
#: danger area belongs to no aerodrome.
HAZARD = "AIRSPACE"
"""Deliberately the same key space as ENR 2 volumes. A State files a NOTAM
against ``R-123`` without caring which section of its own AIP the area was
published in, and two key spaces would mean the NOTAM landed in one of them."""


class HazardKind(str, Enum):
    """What ENR 5 publishes, by subsection and by what it forbids."""

    PROHIBITED = "prohibited"
    """ENR 5.1. Entry forbidden. No condition satisfies it."""

    RESTRICTED = "restricted"
    """ENR 5.1. Entry subject to conditions somebody must satisfy."""

    DANGER = "danger"
    """ENR 5.1. An activity hazardous to flight takes place here. Entry is
    not forbidden — the decision belongs to the commander, which is exactly
    why it must never be reported in the same words as a prohibition."""

    MILITARY = "military"
    """ENR 5.2. Exercise and training areas. Usually a danger or restricted
    area by another name, and published separately because its activation is
    driven by an activity schedule rather than by a standing rule."""

    ADIZ = "adiz"
    """ENR 5.2. An identification requirement, not a reservation. A flight
    that identifies itself may enter, which makes it unlike everything else
    in this section."""

    DANGEROUS_ACTIVITY = "dangerous_activity"
    """ENR 5.3. Firing, blasting, rocket launches, laser emissions, free
    balloons, unmanned aircraft. Often outside any published area at all."""

    OBSTACLE = "obstacle"
    """ENR 5.4. An en-route obstacle. Not a volume — a thing with a height,
    and what it bears on is the minimum level rather than the lateral route."""

    SPORTING = "sporting"
    """ENR 5.5. Gliding, parachuting, hang-gliding, ballooning. Seasonal and
    weather-driven, and rarely NOTAMed for each occasion."""

    BIRD_MIGRATION = "bird_migration"
    """ENR 5.6. Migration corridors and sensitive fauna. Seasonal by nature,
    and the one hazard whose published form is a set of months."""

    @property
    def forbids_entry(self) -> bool:
        return self is HazardKind.PROHIBITED

    @property
    def is_conditional(self) -> bool:
        """Whether somebody's permission or compliance opens it."""
        return self in (
            HazardKind.RESTRICTED,
            HazardKind.MILITARY,
            HazardKind.ADIZ,
        )

    @property
    def is_advisory(self) -> bool:
        """Whether this warns rather than restricts. The commander decides."""
        return self in (
            HazardKind.DANGER,
            HazardKind.DANGEROUS_ACTIVITY,
            HazardKind.SPORTING,
            HazardKind.BIRD_MIGRATION,
        )

    @property
    def is_vertical(self) -> bool:
        """Whether this bears on the minimum level rather than the route.

        An en-route obstacle is not somewhere you avoid laterally at cruise —
        it is why a minimum en-route altitude is what it is, and it belongs
        beside the level screen rather than in the list of areas.
        """
        return self is HazardKind.OBSTACLE

    @property
    def section(self) -> str:
        """Which ENR 5 subsection publishes it."""
        return {
            HazardKind.PROHIBITED: "ENR 5.1",
            HazardKind.RESTRICTED: "ENR 5.1",
            HazardKind.DANGER: "ENR 5.1",
            HazardKind.MILITARY: "ENR 5.2",
            HazardKind.ADIZ: "ENR 5.2",
            HazardKind.DANGEROUS_ACTIVITY: "ENR 5.3",
            HazardKind.OBSTACLE: "ENR 5.4",
            HazardKind.SPORTING: "ENR 5.5",
            HazardKind.BIRD_MIGRATION: "ENR 5.6",
        }[self]


class Activation(str, Enum):
    """When it is active, and therefore what a planner does next."""

    CONTINUOUS = "continuous"
    """H24. The AIP alone answers it."""

    SCHEDULED = "scheduled"
    """Published hours. Answered once the time of flight is known."""

    SEASONAL = "seasonal"
    """Published months. A migration corridor is not a finding in January and
    is one in April, and treating it as continuous buries the two months that
    matter under ten that do not."""

    BY_NOTAM = "by_notam"
    """The AIP saying the AIP is not enough. Its published state is "ask the
    NOTAM", and reading only ENR 5 is reading half the answer."""

    ON_REQUEST = "on_request"
    """Activated when the using agency asks. Needs a call, not a document."""

    UNKNOWN = "unknown"
    """Published without stating when. Reported as unknown rather than assumed
    continuous — over-warning is survivable and under-warning is not, but a
    guess in either direction is a claim nobody published."""

    @property
    def needs_notam(self) -> bool:
        """Whether the AIP alone cannot settle whether this is active."""
        return self in (Activation.BY_NOTAM, Activation.ON_REQUEST, Activation.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Hazard:
    """One ENR 5 entry, as the State publishes it."""

    designator: str
    kind: HazardKind
    source: SourceRef
    name: str = ""
    region: str = ""
    """The flight information region it lies in. What lets a route that
    crosses a region surface what is published there."""

    lower_ft: float | None = None
    upper_ft: float | None = None
    activation: Activation = Activation.UNKNOWN
    hours: str = ""
    months: tuple[int, ...] = ()
    """Months a seasonal entry applies, 1 to 12. Empty on everything else."""

    activity: str = ""
    """What happens there — "gunnery", "parachuting", "raptor migration".
    Held as published, because for an advisory hazard the nature of the
    activity is the whole of what a commander weighs."""

    authority: str = ""
    """Who to ask. On a restricted area this is the difference between a
    finding and an action."""

    elevation_ft: float | None = None
    """Top elevation of an en-route obstacle, above mean sea level. Only
    meaningful for ENR 5.4, and left unset everywhere else."""

    boundary: Boundary | None = None
    """The lateral limits, as the State walks them. Most danger and restricted
    areas are published as a circle or a short coordinate list, so this is the
    one part of ENR 5 that usually *is* fully published.

    Held for drawing. It does not become a containment test here any more than
    it does in :mod:`aeropub.boundary`: the screen above eliminates by altitude
    and says so, and a drawn area is a drawn area."""

    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "designator", normalise(self.designator))
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(self, "months", tuple(sorted(set(self.months))))
        if not self.designator:
            raise ValueError("Hazard.designator must be a non-empty string")
        if self.boundary is not None and not isinstance(self.boundary, Boundary):
            raise TypeError("Hazard.boundary must be a Boundary")
        if not isinstance(self.kind, HazardKind):
            raise TypeError("Hazard.kind must be a HazardKind")
        if not isinstance(self.activation, Activation):
            raise TypeError("Hazard.activation must be an Activation")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Hazard.source must be a SourceRef")
        bad = [m for m in self.months if not 1 <= m <= 12]
        if bad:
            raise ValueError(f"{self.designator}: month {bad[0]} is not 1 to 12")
        if self.activation is Activation.SEASONAL and not self.months:
            raise ValueError(
                f"{self.designator}: a seasonal entry needs the months it "
                "applies. Without them it is active either always or never, "
                "and neither is what the State published."
            )
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
        return named(HAZARD, self.designator)

    def reaches(self, level_ft: float) -> bool | None:
        """Whether the published limits could contain that level.

        **Elimination by altitude only. Lateral position is untested and this
        platform holds nothing to test it with.** True means "not ruled out",
        never "your route enters it".

        ``None`` where the limits are not held: an area whose vertical extent
        nobody has read cannot be eliminated, and reporting it as out of the
        way is the one false negative that matters here.
        """
        if self.lower_ft is None and self.upper_ft is None:
            return None
        if self.lower_ft is not None and level_ft < self.lower_ft:
            return False
        if self.upper_ft is not None and level_ft > self.upper_ft:
            return False
        return True

    def active_at(self, moment: datetime) -> bool | None:
        """Whether it is active then, or ``None`` where the AIP cannot say.

        ``None`` is the commonest and most useful answer: an area activated by
        NOTAM has no schedule to read, and returning False for it would be a
        clear verdict from an absence of evidence.
        """
        if self.activation is Activation.CONTINUOUS:
            return True
        if self.activation is Activation.SEASONAL:
            return moment.month in self.months
        if self.activation is Activation.SCHEDULED and self.hours:
            window = _read_hours(self.hours)
            if window is None:
                return None
            start, end = window
            at = moment.time()
            if start <= end:
                return start <= at <= end
            # A window crossing midnight is two intervals, not one.
            return at >= start or at <= end
        return None

    def describe(self) -> str:
        parts = [f"{self.designator} {self.kind.value.replace('_', ' ')}"]
        if self.name:
            parts.append(self.name)
        if self.kind.is_vertical:
            if self.elevation_ft is not None:
                parts.append(f"{self.elevation_ft:.0f} ft AMSL")
        else:
            parts.append(describe_limits(self.lower_ft, self.upper_ft))
        parts.append(self.activation.value.replace("_", " "))
        if self.hours:
            parts.append(self.hours)
        if self.months:
            parts.append(_describe_months(self.months))
        return "  ·  ".join(parts)


_MONTH_NAMES = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def _describe_months(months: tuple[int, ...]) -> str:
    return "/".join(_MONTH_NAMES[m - 1] for m in months)


def _read_hours(text: str) -> tuple[time, time] | None:
    """Read a ``HHMM-HHMM`` window, or ``None`` for anything else.

    Deliberately narrow. ENR 5 hours run to prose — ``SR-SS``, ``as
    notified``, ``MON-FRI 0700-1500 EXC HOL`` — and a parser that guessed at
    those would produce an activation answer nobody published. Anything it
    cannot read falls through to unknown, which sends the planner to the text.
    """
    cleaned = "".join(str(text).split()).upper()
    if len(cleaned) != 9 or cleaned[4] != "-":
        return None
    try:
        start = time(int(cleaned[0:2]), int(cleaned[2:4]))
        end = time(int(cleaned[5:7]), int(cleaned[7:9]))
    except ValueError:
        return None
    return (start, end)


# --------------------------------------------------------------------------
# Overflight and landing clearance — GEN 1.2, but it bites on the route
# --------------------------------------------------------------------------


class ClearanceKind(str, Enum):
    """What kind of permission a State requires."""

    OVERFLIGHT = "overflight"
    LANDING = "landing"
    DIPLOMATIC = "diplomatic"
    """Sought through diplomatic channels rather than the civil authority. The
    one with lead times in working days, and the one that strands a charter."""

    @property
    def is_slow(self) -> bool:
        return self is ClearanceKind.DIPLOMATIC


@dataclass(frozen=True, slots=True)
class Clearance:
    """One State's permission requirement, as published."""

    state: str
    kind: ClearanceKind
    source: SourceRef
    required: bool = True
    lead_time_hours: float | None = None
    working_days: bool = False
    """Whether the lead time is counted in working days. Forty-eight working
    hours across a weekend is four days, not two — which is exactly the
    arithmetic a charter gets wrong."""

    applies_to: str = ""
    """Which operations it binds — "non-scheduled", "state aircraft". A
    requirement that does not apply is not a finding."""

    authority: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", normalise(self.state))
        if not self.state:
            raise ValueError("Clearance.state must be a non-empty string")
        if not isinstance(self.kind, ClearanceKind):
            raise TypeError("Clearance.kind must be a ClearanceKind")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Clearance.source must be a SourceRef")

    @property
    def lead_time_known(self) -> bool:
        return self.lead_time_hours is not None

    def describe(self) -> str:
        if not self.required:
            return f"{self.state}: no {self.kind.value} clearance required"
        if self.lead_time_hours is None:
            lead = ", lead time not held"
        else:
            unit = "working hours" if self.working_days else "hours"
            lead = f", {self.lead_time_hours:.0f} {unit} ahead"
        who = f" via {self.authority}" if self.authority else ""
        return f"{self.state}: {self.kind.value} clearance required{lead}{who}"


@dataclass(frozen=True, slots=True)
class ClearanceFinding:
    """A permission that cannot be obtained in the notice available."""

    clearance: Clearance
    notice_hours: float

    @property
    def short_by_hours(self) -> float:
        return (self.clearance.lead_time_hours or 0.0) - self.notice_hours

    def describe(self) -> str:
        return (
            f"{self.clearance.state}: {self.clearance.kind.value} clearance "
            f"needs {self.clearance.lead_time_hours:.0f} hours and there are "
            f"{self.notice_hours:.0f} — short by {self.short_by_hours:.0f}"
            + (
                " (counted in working days, so a weekend makes it worse)"
                if self.clearance.working_days
                else ""
            )
        )


def screen_clearances(
    clearances: Iterable[Clearance], *, notice_hours: float
) -> tuple[ClearanceFinding, ...]:
    """Which permissions cannot be obtained in the notice available.

    A requirement with no published lead time produces nothing here, and is
    reported separately by the screen. "Clearance required, lead time not
    held" and "clearance required, and you are late" are different problems,
    and only the second is arithmetic.
    """
    return tuple(
        ClearanceFinding(clearance=c, notice_hours=notice_hours)
        for c in clearances
        if c.required and c.lead_time_hours is not None
        and c.lead_time_hours > notice_hours
    )


# --------------------------------------------------------------------------
# The register and the screen
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HazardRegister:
    """Every ENR 5 entry and clearance requirement read so far."""

    hazards: tuple[Hazard, ...] = ()
    clearances: tuple[Clearance, ...] = ()

    def __len__(self) -> int:
        return len(self.hazards)

    def __iter__(self):
        return iter(self.hazards)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({h.region for h in self.hazards if h.region}))

    def hazard(self, designator: str) -> Hazard | None:
        wanted = normalise(designator)
        return next((h for h in self.hazards if h.designator == wanted), None)

    def in_region(self, region: str) -> tuple[Hazard, ...]:
        wanted = normalise(region)
        return tuple(h for h in self.hazards if h.region == wanted)

    def of_kind(self, kind: HazardKind) -> tuple[Hazard, ...]:
        return tuple(h for h in self.hazards if h.kind is kind)

    def for_state(self, state: str) -> tuple[Clearance, ...]:
        wanted = normalise(state)
        return tuple(c for c in self.clearances if c.state == wanted)


@dataclass(frozen=True, slots=True)
class HazardScreen:
    """What the crossed regions publish as hazardous, restricted or forbidden.

    A screening list, not a verdict. Nothing here says a route enters an area,
    because nothing here holds the geometry that would settle it — and the
    document says so rather than leaving a reader to assume the stronger claim.
    """

    regions: tuple[str, ...] = ()
    planned_ft: float | None = None
    at: datetime | None = None
    candidates: tuple[Hazard, ...] = ()
    """Entries in the crossed regions that altitude does not rule out."""

    eliminated: tuple[Hazard, ...] = ()
    unbounded: tuple[Hazard, ...] = ()
    """Vertical limits not held, so nothing could be ruled out. Our gap, not
    the airspace's, and kept apart for that reason."""

    unread_regions: tuple[str, ...] = ()
    clearances: tuple[Clearance, ...] = ()
    clearance_findings: tuple[ClearanceFinding, ...] = ()

    @property
    def prohibited(self) -> tuple[Hazard, ...]:
        return tuple(h for h in self.candidates if h.kind.forbids_entry)

    @property
    def conditional(self) -> tuple[Hazard, ...]:
        return tuple(h for h in self.candidates if h.kind.is_conditional)

    @property
    def advisory(self) -> tuple[Hazard, ...]:
        return tuple(h for h in self.candidates if h.kind.is_advisory)

    @property
    def obstacles(self) -> tuple[Hazard, ...]:
        """ENR 5.4 entries. Reported beside the level, not beside the areas."""
        return tuple(h for h in self.candidates if h.kind.is_vertical)

    @property
    def needs_notam(self) -> tuple[Hazard, ...]:
        """Entries whose activity the AIP alone cannot settle.

        The list that sends a planner to the other half of the answer, and the
        reason this module and the NOTAM register belong in one document.
        """
        return tuple(h for h in self.candidates if h.activation.needs_notam)

    def active_at(self, moment: datetime) -> tuple[Hazard, ...]:
        return tuple(h for h in self.candidates if h.active_at(moment) is True)

    @property
    def clearances_without_lead_time(self) -> tuple[Clearance, ...]:
        return tuple(
            c for c in self.clearances if c.required and not c.lead_time_known
        )

    @property
    def is_conclusive(self) -> bool:
        """Never true merely because nothing came back.

        A screen over regions nobody has read produces an empty candidate
        list, and an empty list is the same shape as a clear one.
        """
        return bool(self.regions) and not self.unread_regions and not self.unbounded

    def render(self) -> str:
        lines = [
            "NAVIGATION WARNINGS — what the regions you cross publish",
            f"{len(self.regions)} regions"
            + (
                f"  ·  at {self.planned_ft:.0f} ft"
                if self.planned_ft is not None
                else ""
            )
            + f"  ·  {len(self.candidates)} not ruled out"
            + (
                f"  ·  {len(self.eliminated)} ruled out by altitude"
                if self.eliminated
                else ""
            ),
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 5 has been read for {', '.join(self.unread_regions)}. "
                "Nothing was screened",
                "   in those regions, and nothing screened is the same shape as "
                "nothing found.",
            ]

        for label, found in (
            ("PROHIBITED — entry forbidden", self.prohibited),
            (
                "CONDITIONAL — entry subject to conditions somebody must satisfy",
                self.conditional,
            ),
            (
                "ADVISORY — hazardous activity; the decision is the commander's",
                self.advisory,
            ),
            ("EN-ROUTE OBSTACLES — why a minimum level is what it is", self.obstacles),
        ):
            if found:
                lines += ["", label]
                for hazard in found:
                    lines.append(f"  [{hazard.kind.section}] {hazard.describe()}")
                    if hazard.activity:
                        lines.append(f"      {hazard.activity}")
                    if hazard.authority:
                        lines.append(f"      ask: {hazard.authority}")

        if self.unbounded:
            lines += [
                "",
                "LIMITS NOT HELD — could not be ruled out because nobody read "
                "their vertical extent",
            ]
            for hazard in self.unbounded:
                lines.append(f"  [{hazard.kind.section}] {hazard.describe()}")

        if self.needs_notam:
            lines += [
                "",
                "ACTIVE BY NOTAM — the AIP says the AIP is not enough for these",
            ]
            for hazard in self.needs_notam:
                lines.append(
                    f"  {hazard.designator} — {hazard.activation.value.replace('_', ' ')}"
                )

        if self.clearance_findings:
            lines += ["", "CLEARANCE — permissions that cannot be got in time"]
            for finding in self.clearance_findings:
                lines.append(f"  !! {finding.describe()}")
        if self.clearances_without_lead_time:
            lines += [
                "",
                "CLEARANCE — required, and the lead time is not held",
            ]
            for clearance in self.clearances_without_lead_time:
                lines.append(f"  {clearance.describe()}")

        if self.candidates or self.unbounded:
            lines += [
                "",
                "None of this says your route enters any of them. This platform "
                "holds no geometry,",
                "so the list is what the regions publish and what altitude could "
                "not rule out.",
            ]
        elif self.regions and not self.unread_regions:
            lines += [
                "",
                "Nothing published in these regions reaches the planned level. "
                "That is elimination",
                "by altitude only — lateral position was never tested, and this "
                "platform holds",
                "nothing to test it with.",
            ]
        return "\n".join(lines)


def screen_hazards(
    register: HazardRegister,
    *,
    regions: Iterable[str],
    planned_ft: float | None = None,
    at: datetime | None = None,
    notice_hours: float | None = None,
    states: Iterable[str] = (),
) -> HazardScreen:
    """List what the crossed regions publish, minus what altitude rules out.

    ``planned_ft`` is the only filter applied to the areas, and it is applied
    honestly: an entry whose limits are not held is never eliminated, because
    the one false negative that matters is telling somebody an area is out of
    the way when nobody read how high it goes.

    ``notice_hours`` and ``states`` bring in the clearance half. Left out, the
    clearances are listed and not screened — which is right, because how much
    notice a flight has is not something the AIP knows.
    """
    crossed = tuple(normalise(r) for r in regions if str(r).strip())
    read = set(register.regions)
    unread = tuple(r for r in crossed if r not in read)

    candidates: list[Hazard] = []
    eliminated: list[Hazard] = []
    unbounded: list[Hazard] = []
    for region in crossed:
        for hazard in register.in_region(region):
            if planned_ft is None or hazard.kind.is_vertical:
                # An en-route obstacle is not eliminated by cruising level: it
                # is the reason the minimum level is what it is, and a screen
                # that dropped it at FL350 would remove the evidence behind
                # the number it was screening against.
                candidates.append(hazard)
                continue
            verdict = hazard.reaches(planned_ft)
            if verdict is None:
                unbounded.append(hazard)
            elif verdict:
                candidates.append(hazard)
            else:
                eliminated.append(hazard)

    wanted = tuple(normalise(s) for s in states if str(s).strip())
    clearances = (
        tuple(c for s in wanted for c in register.for_state(s))
        if wanted
        else register.clearances
    )
    findings = (
        screen_clearances(clearances, notice_hours=notice_hours)
        if notice_hours is not None
        else ()
    )

    return HazardScreen(
        regions=crossed,
        planned_ft=planned_ft,
        at=at,
        candidates=tuple(candidates),
        eliminated=tuple(eliminated),
        unbounded=tuple(unbounded),
        unread_regions=unread,
        clearances=tuple(clearances),
        clearance_findings=findings,
    )


def notams_on_hazards(
    register: NotamRegister, screen: HazardScreen, moment: datetime
) -> tuple[tuple[str, RegisteredNotam, ForceState], ...]:
    """NOTAM in force against anything the screen could not rule out.

    The join this module exists for. An area published as active by NOTAM is
    a pointer, and this follows it. Entries whose limits were not held are
    included: an area we could not eliminate is one whose NOTAM still matters.
    """
    found: list[tuple[str, RegisteredNotam, ForceState]] = []
    seen: set[tuple[str, str]] = set()
    for hazard in screen.candidates + screen.unbounded:
        for notam, state in register.at(hazard.key, moment):
            mark = (hazard.key, notam.identifier)
            if mark in seen:
                continue
            seen.add(mark)
            found.append((hazard.key, notam, state))
    return tuple(found)


# --------------------------------------------------------------------------
# Reading an ENR 5 manifest
# --------------------------------------------------------------------------


def _months(value: object, *, where: str) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if not isinstance(value, list):
        raise ManifestError(f"{where}: months must be a list of 1 to 12")
    found: list[int] = []
    for entry in value:
        try:
            found.append(int(entry))
        except (TypeError, ValueError):
            raise ManifestError(
                f"{where}: month {entry!r} is not a number 1 to 12"
            ) from None
    return tuple(found)


def load_hazards(path: Path | str) -> HazardRegister:
    """Read one ENR 5 extract, with every entry cited to it.

    One document, one citation. ``clearances`` may sit in the same file where
    the same publication carries them, and must not otherwise: a citation
    pointing at a page that does not contain the statement is worse than no
    citation, because it is the one a reviewer stops checking.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=HAZARD_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    rows = manifest.get("hazards", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: hazards must be a list")
    hazards: list[Hazard] = []
    for index, row in enumerate(rows):
        where = f"{path}: hazards[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which row of ENR 5 this came "
                "from."
            )
        try:
            kind = HazardKind(str(row.get("kind", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in HazardKind)}. Prohibited, "
                "restricted and danger forbid different things, so there is "
                "no safe default."
            ) from None
        try:
            activation = Activation(
                str(row.get("activation", Activation.UNKNOWN.value)).strip().lower()
            )
        except ValueError:
            raise ManifestError(
                f"{where}: activation must be one of "
                f"{', '.join(a.value for a in Activation)}"
            ) from None
        region = str(row.get("region", default_region)).strip()
        if not region:
            raise ManifestError(
                f"{where}: region is required — which flight information "
                "region this lies in. Without it no route can surface it."
            )
        boundary = None
        if row.get("boundary") is not None:
            try:
                boundary = read_boundary_manifest(
                    row["boundary"], where=f"{where}: boundary"
                )
            except ValueError as error:
                raise ManifestError(str(error)) from None

        try:
            hazards.append(
                Hazard(
                    designator=str(row.get("designator", "")),
                    kind=kind,
                    source=sub_source(document, locator),
                    name=str(row.get("name", "")).strip(),
                    region=region,
                    lower_ft=read_limit(row.get("lower"), where=where, field="lower"),
                    upper_ft=read_limit(row.get("upper"), where=where, field="upper"),
                    activation=activation,
                    hours=str(row.get("hours", "")).strip(),
                    months=_months(row.get("months"), where=where),
                    activity=str(row.get("activity", "")).strip(),
                    authority=str(row.get("authority", "")).strip(),
                    elevation_ft=read_limit(
                        row.get("elevation"), where=where, field="elevation"
                    ),
                    boundary=boundary,
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    rows = manifest.get("clearances", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: clearances must be a list")
    clearances: list[Clearance] = []
    for index, row in enumerate(rows):
        where = f"{path}: clearances[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        try:
            kind = ClearanceKind(str(row.get("kind", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in ClearanceKind)}"
            ) from None
        lead = row.get("lead_time_hours")
        if lead is not None and lead != "":
            try:
                lead = float(lead)
            except (TypeError, ValueError):
                raise ManifestError(
                    f"{where}: lead_time_hours {lead!r} is not a number"
                ) from None
        else:
            lead = None
        try:
            clearances.append(
                Clearance(
                    state=str(row.get("state", "")),
                    kind=kind,
                    source=sub_source(
                        document, str(row.get("locator", "")).strip() or "GEN 1.2"
                    ),
                    required=bool(row.get("required", True)),
                    lead_time_hours=lead,
                    working_days=bool(row.get("working_days", False)),
                    applies_to=str(row.get("applies_to", "")).strip(),
                    authority=str(row.get("authority", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return HazardRegister(hazards=tuple(hazards), clearances=tuple(clearances))


_HAZARD_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "hazards": [
        {
            "designator": "",
            "kind": "restricted",
            "name": "",
            "region": "",
            "lower": "SFC",
            "upper": "UNL",
            "activation": "by_notam",
            "hours": "",
            "months": [],
            "activity": "",
            "authority": "",
            "elevation": None,
            "boundary": {
                "described_as": "",
                "points": [],
                "edges": [],
                "circle": None,
            },
            "remarks": "",
            "locator": "",
        }
    ],
    "clearances": [
        {
            "state": "",
            "kind": "overflight",
            "required": True,
            "lead_time_hours": None,
            "working_days": False,
            "applies_to": "",
            "authority": "",
            "locator": "",
        }
    ],
}


def hazard_template() -> str:
    """A blank ENR 5 extract.

    ``region`` at the top applies to every entry that does not name its own,
    so a State's whole table needs it written once. ``lower`` and ``upper``
    take the forms an AIP prints — ``SFC``, ``GND``, ``UNL``, ``FL195``, or
    feet — and anything else is refused rather than guessed, because a guessed
    limit rules an area out of a screen and nobody sees it happen.
    ``activation`` of ``by_notam`` is the common and important case: it is the
    AIP saying the AIP is not enough. ``months`` is required for a seasonal
    entry and empty for every other kind.
    """
    return json.dumps(_HAZARD_TEMPLATE, indent=2)
