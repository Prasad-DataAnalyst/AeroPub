"""Route dossiers — and the honest headline a route dossier has to carry.

A route dossier is an assembly, not a new kind of analysis. The aerodromes at
both ends are assessed exactly as :mod:`aeropub.sweep` assesses any aerodrome,
from the same dossier through the same layer three, because a number here that
disagreed with the single-aerodrome report would be a defect in this module and
not a second opinion.

What this adds is the part between the ends: **who else's publications matter.**
A sector from Doha to London crosses six or seven flight information regions,
and each one is a different State with its own AIP, its own publication
conduct, its own NOTAM and its own transition altitude. A route dossier that
does not know which jurisdictions it crosses cannot say whose publications it
is missing — and missing them silently is the whole failure.

The headline
------------
So the number at the top of a route dossier is not a risk score. It is **how
much of this route we can speak for**: places held and current, out of places
crossed. A route with five empty sections must not read like a route with
nothing wrong, and that is exactly what it does read like when the empty
sections are simply absent.

That framing decides the shape of everything below. A jurisdiction we hold
nothing for is a row saying so, not a missing row. An alternate we have never
read is counted as uncovered, never as clear. And :attr:`RouteDossier.open_items`
is the list a dispatcher actually works from: every unresolved thing between
here and operating, in one place, in severity order.

Altimetry, and why it earns its own section
-------------------------------------------
Of everything the plan lists for a route dossier, transition altitude is the
one that is both cheap to hold and routinely wrong in practice. It changes at
FIR boundaries, the change is not announced anywhere en route, and the value
differs by thousands of feet between neighbouring States. :class:`Altimetry`
therefore reports the *changes* along the route rather than a table of values:
the boundary is the finding, and a list of numbers makes the reader find it.

What is not here
----------------
Terrain, driftdown, RAD restrictions, CDR availability, PBN specification per
airspace and RAIM prediction all need ENR content this platform does not yet
parse. They are absent from the assembly rather than approximated in it, and
the dossier says which of them it could not address. An approximated driftdown
corridor is worse than none: it is one somebody might use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from aeropub.aip import AipCoverage
from aeropub.currency import Currency, DataCurrency, assess_currency
from aeropub.entities import named, normalise
from aeropub.notam_register import NotamRegister
from aeropub.operator import Exposure, OperatorProfile, Role, worst_exposure
from aeropub.sweep import DEFAULT_DAYS, NetworkSweep, sweep

__all__ = [
    "Altimetry",
    "AltimetryChange",
    "FIR",
    "Jurisdiction",
    "JurisdictionCover",
    "NOT_YET_ADDRESSED",
    "OpenItem",
    "Route",
    "RouteDossier",
    "build_route_dossier",
]

#: The entity kind a flight information region is keyed under. Free-standing:
#: an FIR belongs to no aerodrome, and rolling one up under one would attribute
#: a whole region's NOTAM to a runway.
FIR = "FIR"

#: The attributes a jurisdiction is read for. Small on purpose — these are the
#: two values that change at a boundary and are not announced en route.
TRANSITION_ALTITUDE = "transition_altitude_ft"
TRANSITION_LEVEL = "transition_level"

#: Route-dossier elements the platform cannot yet address, named so the dossier
#: can say what it did not look at rather than leaving the reader to notice.
#: Every one of these needs ENR content no parser reaches today, and an
#: approximation of any of them would be worse than the gap: a driftdown
#: corridor nobody can source is one somebody might still fly.
NOT_YET_ADDRESSED: tuple[str, ...] = (
    "terrain and vertical profile — Grid MORA, MEA, points of no return",
    "driftdown escape corridors and depressurisation strategy",
    "ATS routing — level bands, flow direction, RAD, CDR and FUA availability",
    "PBN specification per airspace against tail capability, and RAIM prediction",
    "HF, CPDLC and SATCOM coverage along track",
    "payload-range envelope against route length, and the critical fuel scenario",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    """One flight information region the route crosses.

    Held as an entity key rather than a label so that everything else in the
    platform — NOTAM against the region, a supplement about its airspace, a
    reading date — joins to it the same way it joins to an aerodrome.
    """

    designator: str
    name: str = ""
    state: str = ""
    """The State responsible for this region, where it is not the designator's
    own. An FIR and the State that publishes for it are not always one to
    one, and a dossier naming the wrong one sends a reader to the wrong AIP."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "designator", normalise(self.designator))
        object.__setattr__(self, "state", normalise(self.state))
        if not self.designator:
            raise ValueError("Jurisdiction.designator must be a non-empty string")

    @property
    def key(self) -> str:
        return named(FIR, self.designator)

    @property
    def publisher(self) -> str:
        """Whose AIP to look in for this region."""
        return self.state or self.designator

    def describe(self) -> str:
        parts = [self.designator]
        if self.name:
            parts.append(self.name)
        if self.state and self.state != self.designator:
            parts.append(f"published by {self.state}")
        return " — ".join(parts)


@dataclass(frozen=True, slots=True)
class Route:
    """One sector, as it will actually be flown.

    ``crosses`` is in order of overflight, and the order matters: altimetry
    findings are about boundaries, and a boundary only exists between two
    consecutive jurisdictions.
    """

    departure: str
    destination: str
    alternates: tuple[str, ...] = ()
    takeoff_alternate: str = ""
    enroute_alternates: tuple[str, ...] = ()
    crosses: tuple[Jurisdiction, ...] = ()
    designator: str = ""
    reference: str = ""

    def __post_init__(self) -> None:
        for field in ("departure", "destination", "takeoff_alternate"):
            object.__setattr__(self, field, normalise(getattr(self, field)))
        for field in ("alternates", "enroute_alternates"):
            object.__setattr__(
                self, field, tuple(normalise(a) for a in getattr(self, field))
            )
        object.__setattr__(self, "designator", normalise(self.designator))
        if not self.departure:
            raise ValueError("Route.departure must be a non-empty string")
        if not self.destination:
            raise ValueError("Route.destination must be a non-empty string")
        seen = [j.designator for j in self.crosses]
        repeated = [
            d for i, d in enumerate(seen) if i and d == seen[i - 1]
        ]
        if repeated:
            raise ValueError(
                f"{', '.join(sorted(set(repeated)))} is listed twice in a row. "
                "Consecutive duplicates would produce a boundary between a "
                "region and itself, and altimetry findings are about "
                "boundaries."
            )

    @property
    def aerodromes(self) -> tuple[str, ...]:
        """Every aerodrome this sector uses, in role order, deduplicated."""
        listed = [self.departure, self.destination]
        listed += list(self.alternates)
        if self.takeoff_alternate:
            listed.append(self.takeoff_alternate)
        listed += list(self.enroute_alternates)
        found: list[str] = []
        for where in listed:
            if where and where not in found:
                found.append(where)
        return tuple(found)

    @property
    def label(self) -> str:
        head = f"{self.departure}-{self.destination}"
        return f"{self.reference} ({head})" if self.reference else head

    def position_of(self, aerodrome: str) -> str:
        """What this aerodrome is *on this sector*, in the reader's words.

        Not the same as the layer-three role, and both belong in the document.
        The departure aerodrome is assessed at the destination role because
        that role carries the pavement and fire-category checks that matter at
        the field the aeroplane is sitting on — but printing it as
        "destination" leaves a reader working out which end is which.
        """
        where = normalise(aerodrome)
        if where == self.departure:
            return "departure"
        if where == self.destination:
            return "destination"
        if where == self.takeoff_alternate:
            return "take-off alternate"
        if where in self.enroute_alternates:
            return "en-route alternate"
        if where in self.alternates:
            return "alternate"
        return "on this sector"

    def as_profile(self, fleet, *, name: str = "") -> OperatorProfile:
        """The layer-three profile for this sector.

        Built here rather than taken from the caller so that the roles the
        sweep sees are the roles this route actually has. The departure
        aerodrome enters at the destination role for the reason
        :func:`aeropub.fleet.route_profile` gives: its pavement and fire
        category matter on the day, and the en-route role deliberately
        excludes both.
        """
        from aeropub.operator import Fleet, Network, NetworkEntry

        # No redundancy group on the two ends. A group is a set of
        # interchangeable aerodromes, and the sweep reads a group of one as a
        # region down to its last option — so naming a group here manufactured
        # a CRITICAL "0 of 1 dependable" finding about the departure aerodrome,
        # which is not a redundancy problem. It is simply where the flight
        # starts.
        entries = [
            NetworkEntry(aerodrome=self.departure, role=Role.DESTINATION),
            NetworkEntry(aerodrome=self.destination, role=Role.DESTINATION),
        ]
        entries += [
            NetworkEntry(
                aerodrome=where,
                role=Role.ALTERNATE,
                sole_suitable=len(set(self.alternates)) == 1,
                group=f"{self.destination} alternates",
            )
            for where in self.alternates
        ]
        if self.takeoff_alternate:
            entries.append(
                NetworkEntry(
                    aerodrome=self.takeoff_alternate,
                    role=Role.TAKEOFF_ALTERNATE,
                    sole_suitable=True,
                    group=f"{self.departure} take-off alternates",
                )
            )
        entries += [
            NetworkEntry(
                aerodrome=where,
                role=Role.EDTO_ALTERNATE,
                sole_suitable=len(set(self.enroute_alternates)) == 1,
                group=f"{self.label} en-route alternates",
            )
            for where in self.enroute_alternates
        ]
        return OperatorProfile(
            name=name or self.label,
            fleet=fleet if isinstance(fleet, Fleet) else Fleet(tuple(fleet)),
            network=Network(tuple(entries)),
        )


# --------------------------------------------------------------------------
# Jurisdictions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JurisdictionCover:
    """What the platform can say about one region on this route."""

    jurisdiction: Jurisdiction
    facts_held: int = 0
    transition_altitude_ft: float | None = None
    transition_level: str = ""
    currency: DataCurrency | None = None

    @property
    def is_covered(self) -> bool:
        """Whether anything at all has been read for this region.

        Zero facts is the number that matters. A region nobody has read
        produces no findings, and no findings is exactly what a region with
        nothing wrong produces.
        """
        return self.facts_held > 0

    @property
    def is_current(self) -> bool:
        return self.currency is not None and self.currency.currency is Currency.CURRENT

    def describe(self) -> str:
        if not self.is_covered:
            return f"{self.jurisdiction.describe()}: never read"
        parts = []
        if self.transition_altitude_ft is not None:
            parts.append(f"TA {self.transition_altitude_ft:.0f} ft")
        else:
            parts.append("TA not held")
        if self.transition_level:
            parts.append(f"TL {self.transition_level}")
        if self.currency is not None:
            parts.append(self.currency.currency.value)
        return f"{self.jurisdiction.describe()}: " + "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class AltimetryChange:
    """A transition altitude changing at a boundary between two regions.

    The boundary is the finding. A crew reading a table of six transition
    altitudes has to work out where each one starts; a crew reading three
    boundaries has the answer.
    """

    leaving: Jurisdiction
    entering: Jurisdiction
    from_ft: float | None
    to_ft: float | None

    @property
    def is_known(self) -> bool:
        """Whether both sides of this boundary are held.

        A boundary with one side missing is not a boundary with no change. It
        is a boundary nobody can speak for, and it is reported as such.
        """
        return self.from_ft is not None and self.to_ft is not None

    @property
    def delta_ft(self) -> float | None:
        if not self.is_known:
            return None
        return self.to_ft - self.from_ft

    def describe(self) -> str:
        boundary = f"{self.leaving.designator} → {self.entering.designator}"
        if not self.is_known:
            missing = []
            if self.from_ft is None:
                missing.append(self.leaving.designator)
            if self.to_ft is None:
                missing.append(self.entering.designator)
            return f"{boundary}: not held for {', '.join(missing)}"
        return (
            f"{boundary}: TA {self.from_ft:.0f} → {self.to_ft:.0f} ft "
            f"({self.delta_ft:+.0f})"
        )


@dataclass(frozen=True, slots=True)
class Altimetry:
    """Transition altitude across the route, reported by boundary."""

    covers: tuple[JurisdictionCover, ...] = ()

    @property
    def boundaries(self) -> tuple[AltimetryChange, ...]:
        found = []
        for before, after in zip(self.covers, self.covers[1:]):
            found.append(
                AltimetryChange(
                    leaving=before.jurisdiction,
                    entering=after.jurisdiction,
                    from_ft=before.transition_altitude_ft,
                    to_ft=after.transition_altitude_ft,
                )
            )
        return tuple(found)

    @property
    def changes(self) -> tuple[AltimetryChange, ...]:
        """Boundaries where the transition altitude actually moves."""
        return tuple(
            b for b in self.boundaries if b.is_known and b.delta_ft not in (0, 0.0)
        )

    @property
    def unknown(self) -> tuple[AltimetryChange, ...]:
        return tuple(b for b in self.boundaries if not b.is_known)

    @property
    def is_complete(self) -> bool:
        return bool(self.covers) and not self.unknown


# --------------------------------------------------------------------------
# The dossier
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenItem:
    """Something that must be decided before this sector is operated.

    Deliberately flat and deliberately in one list. A dispatcher works from a
    list of open items, not from six sections each of which might contain one.
    """

    where: str
    what: str
    severity: Exposure
    why: str = ""

    def describe(self) -> str:
        tail = f" — {self.why}" if self.why else ""
        return f"[{self.severity.value.upper()}] {self.where}: {self.what}{tail}"


@dataclass(frozen=True, slots=True)
class RouteDossier:
    """One sector, assembled from everything the platform holds about it."""

    route: Route
    as_at: datetime
    on: date
    sweep: NetworkSweep
    jurisdictions: tuple[JurisdictionCover, ...] = ()
    altimetry: Altimetry = Altimetry()
    open_items: tuple[OpenItem, ...] = ()
    not_addressed: tuple[str, ...] = NOT_YET_ADDRESSED

    @property
    def places(self) -> int:
        """Every aerodrome and region this sector depends on."""
        return len(self.route.aerodromes) + len(self.jurisdictions)

    @property
    def spoken_for(self) -> int:
        """How many of those the platform has actually read."""
        read = sum(1 for e in self.sweep.entries if e.facts_held)
        return read + sum(1 for j in self.jurisdictions if j.is_covered)

    @property
    def coverage(self) -> tuple[int, int]:
        return (self.spoken_for, self.places)

    @property
    def overall(self) -> Exposure:
        """The worst exposure anywhere on this sector.

        Taken from the sweep and the open items together, so a jurisdiction
        finding cannot be outranked into invisibility by an aerodrome that is
        merely fine.
        """
        levels = [self.sweep.overall] + [item.severity for item in self.open_items]
        return worst_exposure(levels)

    @property
    def is_conclusive(self) -> bool:
        """Whether this dossier speaks for the whole sector.

        False the moment any place on the route is unread or any altimetry
        boundary is unknown. There is no partial credit here: a dossier that
        covered the two ends and none of the middle would otherwise print its
        conclusions with the same confidence as one that covered everything.
        """
        return (
            self.spoken_for == self.places
            and self.places > 0
            and self.altimetry.is_complete
        )

    def items_at(self, level: Exposure) -> tuple[OpenItem, ...]:
        return tuple(item for item in self.open_items if item.severity is level)

    def render(self) -> str:
        read, total = self.coverage
        lines = [
            f"ROUTE DOSSIER — {self.route.label}"
            + (f"  ·  {self.route.designator}" if self.route.designator else ""),
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}  ·  "
            f"effective state on {self.on:%Y-%m-%d}",
            "",
            f"Speaks for {read} of {total} places on this route"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE"),
            f"Overall: {self.overall.value.upper()}  ·  "
            f"{len(self.open_items)} open items",
            "",
        ]

        if read < total:
            lines += [
                f"!! {total - read} of the {total} places this sector depends on "
                "have never been read.",
                "   They produce no findings, and no findings is what a place "
                "with nothing wrong produces.",
                "",
            ]

        lines.append("AERODROMES")
        for entry in self.sweep.ranked:
            state = (
                f"{entry.assessment.overall.value.upper()}"
                if entry.facts_held
                else "NEVER READ"
            )
            sole = "  · sole suitable" if entry.sole_suitable else ""
            position = self.route.position_of(entry.aerodrome)
            lines.append(
                f"  {entry.aerodrome:<6} {position:<19} {state}{sole}"
            )

        if self.jurisdictions:
            lines += ["", "JURISDICTIONS CROSSED"]
            for cover in self.jurisdictions:
                lines.append(f"  {cover.describe()}")
        else:
            lines += [
                "",
                "JURISDICTIONS CROSSED — none listed. The regions between the "
                "two ends were not",
                "named, so nothing was checked in any of them. That is a gap in "
                "the route, not a",
                "quiet route.",
            ]

        if self.altimetry.changes:
            lines += ["", "ALTIMETRY — where the transition altitude moves"]
            for boundary in self.altimetry.changes:
                lines.append(f"  {boundary.describe()}")
        if self.altimetry.unknown:
            lines += ["", "ALTIMETRY — boundaries nobody can speak for"]
            for boundary in self.altimetry.unknown:
                lines.append(f"  {boundary.describe()}")

        if self.open_items:
            lines += ["", "OPEN ITEMS — decide these before operating"]
            for item in self.open_items:
                lines.append(f"  {item.describe()}")

        if self.not_addressed:
            lines += [
                "",
                "NOT ADDRESSED — this dossier did not look at these at all",
            ]
            for element in self.not_addressed:
                lines.append(f"  · {element}")
            lines += [
                "",
                "  Absent rather than approximated. A driftdown corridor nobody "
                "can source is",
                "  worse than none: it is one somebody might still fly.",
            ]
        return "\n".join(lines)


def _read_jurisdiction(store, jurisdiction: Jurisdiction, on: date) -> JurisdictionCover:
    key = jurisdiction.key
    # Read through the store's common surface — entities, attributes and the
    # effective value on the day — so the in-memory and durable stores answer
    # this identically. Anything richer would work against one of them only.
    attributes = store.attributes(key) if key in store.entities() else set()

    altitude: float | None = None
    level = ""
    facts = 0
    for attribute in sorted(attributes):
        fact = store.effective(key, attribute, on)
        if fact is None:
            # Held, but not in force on this date. Not a value we may quote,
            # and not evidence that the region was never read either.
            continue
        facts += 1
        if attribute == TRANSITION_ALTITUDE:
            try:
                altitude = float(fact.value)
            except (TypeError, ValueError):
                # A transition altitude that will not parse is left unread.
                # Rounding one into place here is how a crew gets a number
                # nobody published.
                altitude = None
        elif attribute == TRANSITION_LEVEL:
            level = str(fact.value)
    return JurisdictionCover(
        jurisdiction=jurisdiction,
        facts_held=facts,
        transition_altitude_ft=altitude,
        transition_level=level,
        currency=assess_currency(store, key, as_of=on) if facts else None,
    )


def _open_items(
    sweep_result: NetworkSweep, jurisdictions: Iterable[JurisdictionCover]
) -> tuple[OpenItem, ...]:
    """Everything unresolved, from every part of the assembly, in one list."""
    items: list[OpenItem] = []

    for entry in sweep_result.entries:
        if not entry.facts_held:
            items.append(
                OpenItem(
                    where=entry.aerodrome,
                    what="never read",
                    severity=Exposure.UNKNOWN,
                    why=f"{entry.role.value} on this sector, and nothing is held for it",
                )
            )
            continue
        for finding in entry.assessment.findings:
            if finding.exposure is Exposure.NONE:
                continue
            items.append(
                OpenItem(
                    where=entry.aerodrome,
                    what=finding.check.name,
                    severity=finding.exposure,
                    why=finding.reason,
                )
            )

    for group in sweep_result.at_risk_groups:
        items.append(
            OpenItem(
                where=group.name,
                what="redundancy",
                severity=group.exposure,
                why=(
                    f"{group.remaining} of {len(group.members)} dependable"
                    + (
                        f", {len(group.unreliable)} unread or stale"
                        if group.unreliable
                        else ""
                    )
                ),
            )
        )

    for cover in jurisdictions:
        if not cover.is_covered:
            items.append(
                OpenItem(
                    where=cover.jurisdiction.designator,
                    what="never read",
                    severity=Exposure.UNKNOWN,
                    why=(
                        "the sector crosses this region and nothing has been "
                        f"read from {cover.jurisdiction.publisher}"
                    ),
                )
            )
        elif cover.transition_altitude_ft is None:
            items.append(
                OpenItem(
                    where=cover.jurisdiction.designator,
                    what="transition altitude not held",
                    severity=Exposure.MEDIUM,
                    why="it changes at the boundary and is announced nowhere en route",
                )
            )

    rank = {
        Exposure.CRITICAL: 0,
        Exposure.HIGH: 1,
        Exposure.MEDIUM: 2,
        Exposure.UNKNOWN: 3,
        Exposure.LOW: 4,
        Exposure.NONE: 5,
    }
    return tuple(sorted(items, key=lambda i: (rank.get(i.severity, 9), i.where)))


def build_route_dossier(
    store,
    route: Route,
    *,
    fleet,
    as_at: datetime | None = None,
    on: date | None = None,
    days: int = DEFAULT_DAYS,
    register: NotamRegister | None = None,
    coverage: AipCoverage | None = None,
    not_addressed: tuple[str, ...] = NOT_YET_ADDRESSED,
) -> RouteDossier:
    """Assemble everything the platform holds about one sector.

    The aerodromes go through :func:`aeropub.sweep.sweep` unchanged, so the
    verdict for any one of them is the same verdict the single-aerodrome report
    gives. What is added is the jurisdictions between them, the altimetry
    boundaries those create, and one consolidated list of open items.
    """
    moment = as_at or _utcnow()
    if moment.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")
    day = on or moment.date()

    profile = route.as_profile(fleet)
    swept = sweep(
        store,
        profile,
        as_at=moment,
        on=day,
        days=days,
        register=register,
        coverage=coverage,
    )

    jurisdictions = tuple(
        _read_jurisdiction(store, j, day) for j in route.crosses
    )
    return RouteDossier(
        route=route,
        as_at=moment,
        on=day,
        sweep=swept,
        jurisdictions=jurisdictions,
        altimetry=Altimetry(covers=jurisdictions),
        open_items=_open_items(swept, jurisdictions),
        not_addressed=tuple(not_addressed),
    )
