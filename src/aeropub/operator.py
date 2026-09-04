"""Layer three — what a change means for *this* operator, at *this* aerodrome.

Everything beneath this module is deliberately operator-agnostic. The change
record says what changed, the impact statement says why it matters in general,
and the suitability assessment says whether an aeroplane fits — none of them
knowing who is asking. This is the layer that knows, and it is the only one
that may.

The headline case, which the plan states as the product's reason to exist: an
RFFS downgrade from Category 9 to Category 7 is **critical** at a sole-suitable
EDTO alternate for a 777 and **irrelevant** to an A320 operator that needs
Category 6. Same publication, same change record, same generic impact. Two
different answers, and neither of them is a property of the change.

Severity is derived, never asserted
-----------------------------------
Nothing here carries a table of "RFFS downgrade = critical". Exposure falls out
of three things that are each held and cited: what the fleet's aeroplanes
actually require, what the aerodrome actually publishes, and what role the
operator has given the aerodrome. Change any one and the answer changes, which
is what makes it an assessment rather than an opinion.

Role is the multiplier. The same failed check is a different problem at a
destination you can decline to serve, an alternate you can swap, and an EDTO
en-route alternate that a flight has already been dispatched against. A
sole-suitable aerodrome has no swap available, and that is recorded rather than
inferred, because only the operator knows their own region.

Three rules this layer keeps
----------------------------
**"No exposure" is a real answer, and the record beneath it survives in full.**
An aerodrome outside the network resolves to :attr:`Exposure.NONE`, and the
complete suitability assessment is still attached. Nothing is skipped to save
work — when the operator adds a destination, the answer is already computed.

**Unknown never becomes "no exposure".** Not being able to make a check and
having no exposure are opposite conclusions that would print the same
comforting word. A check that could not be made is :attr:`Exposure.UNKNOWN`,
and at a role that demands certainty it is worse than that: dispatching against
an EDTO alternate on a check nobody made is the failure this whole platform
exists to prevent.

**Every finding names the type it is about.** "Your fleet is exposed" is not
actionable; "the B77W is, the A320 is not" is. A fleet-level roll-up is
offered, and it is the worst case across the types, never an average.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from aeropub.acap import load_aircraft, merge
from aeropub.aircraft import AircraftType
from aeropub.dossier import AerodromeDossier
from aeropub.entities import normalise
from aeropub.manifest import ManifestError, read_manifest
from aeropub.suitability import Assessment, Check, Suitability, assess_suitability

__all__ = [
    "Exposure",
    "ExposureFinding",
    "Fleet",
    "Network",
    "NetworkEntry",
    "OperatorAssessment",
    "OperatorProfile",
    "Role",
    "assess_operator",
    "load_profile",
    "profile_template",
]


class Role(str, Enum):
    """What the operator uses an aerodrome for. The multiplier on severity."""

    DESTINATION = "destination"
    """Planned landing. A failed check means the flight cannot be planned."""

    ALTERNATE = "alternate"
    """Destination or departure alternate. A failed check usually means
    choosing another one — unless there is no other one."""

    EDTO_ALTERNATE = "edto_alternate"
    """En-route alternate for extended diversion time operations. The most
    demanding role there is: a flight is dispatched *relying* on it being
    available, and by the time it is needed there is no alternative."""

    TAKEOFF_ALTERNATE = "takeoff_alternate"
    """Required when the departure aerodrome is below landing minima. Also
    relied upon at dispatch, with no time to substitute."""

    ENROUTE = "enroute"
    """Overflown, not landed at. Airspace and route changes matter here;
    pavement and fire category do not."""

    NOT_IN_NETWORK = "not_in_network"
    """The operator does not use this aerodrome. A real answer, and the
    assessment beneath it is still computed in full."""

    @property
    def is_planned_landing(self) -> bool:
        return self in (
            Role.DESTINATION,
            Role.ALTERNATE,
            Role.EDTO_ALTERNATE,
            Role.TAKEOFF_ALTERNATE,
        )

    @property
    def demands_certainty(self) -> bool:
        """Whether a flight is dispatched relying on this aerodrome.

        An unmade check at a destination can be made before the next flight is
        planned. An unmade check at an EDTO or take-off alternate is a gap in
        something already being relied upon.
        """
        return self in (Role.EDTO_ALTERNATE, Role.TAKEOFF_ALTERNATE)


#: How demanding each role is, most first. An aerodrome serving several roles
#: is assessed at the most demanding of them: an aerodrome that is somebody's
#: EDTO alternate does not stop being one because it is also a destination.
_ROLE_ORDER = (
    Role.EDTO_ALTERNATE,
    Role.TAKEOFF_ALTERNATE,
    Role.DESTINATION,
    Role.ALTERNATE,
    Role.ENROUTE,
    Role.NOT_IN_NETWORK,
)
_ROLE_RANK = {role: index for index, role in enumerate(_ROLE_ORDER)}


class Exposure(str, Enum):
    """What one finding means for this operator."""

    CRITICAL = "critical"
    """The operation as planned is not available. An alternate that no longer
    qualifies, a destination that cannot take the aeroplane."""

    HIGH = "high"
    """Something must be recomputed or replanned before operating, or a check
    that a dispatch relies on has not been made."""

    MEDIUM = "medium"
    """A condition to observe or a document to amend. The operation stands."""

    LOW = "low"
    """Worth knowing. No action follows from it on its own."""

    NONE = "none"
    """No exposure. Either the check passed, or the operator does not use this
    aerodrome in a way the check bears on."""

    UNKNOWN = "unknown"
    """The check could not be made. **Not** the same as no exposure, and never
    printed as though it were."""

    @property
    def needs_action(self) -> bool:
        return self in (Exposure.CRITICAL, Exposure.HIGH)

    @property
    def is_conclusive(self) -> bool:
        return self is not Exposure.UNKNOWN


#: Worst first. ``UNKNOWN`` sits above every pass because the check that was
#: not made is the one that could still turn out to be the failure — the same
#: ordering the suitability layer uses, for the same reason.
_EXPOSURE_ORDER = (
    Exposure.CRITICAL,
    Exposure.HIGH,
    Exposure.MEDIUM,
    Exposure.UNKNOWN,
    Exposure.LOW,
    Exposure.NONE,
)
_EXPOSURE_RANK = {level: index for index, level in enumerate(_EXPOSURE_ORDER)}


def _worst(levels: Iterable[Exposure]) -> Exposure:
    found = list(levels)
    if not found:
        return Exposure.UNKNOWN
    return min(found, key=lambda level: _EXPOSURE_RANK[level])


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetworkEntry:
    """One aerodrome and what this operator uses it for."""

    aerodrome: str
    role: Role
    sole_suitable: bool = False
    """Whether this is the only usable aerodrome for its purpose in the region.

    Recorded, never inferred: only the operator knows what else is within
    reach, what their approvals cover and what their handling arrangements
    allow. It is the difference between "choose another alternate" and "there
    is no other alternate"."""

    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        if not self.aerodrome:
            raise ValueError("NetworkEntry.aerodrome must be a non-empty string")
        if not isinstance(self.role, Role):
            raise TypeError("NetworkEntry.role must be a Role")


@dataclass(frozen=True, slots=True)
class Network:
    """Where this operator flies, and in what capacity."""

    entries: tuple[NetworkEntry, ...] = ()

    def __iter__(self) -> Iterator[NetworkEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def entries_for(self, aerodrome: str) -> tuple[NetworkEntry, ...]:
        key = normalise(aerodrome)
        return tuple(e for e in self.entries if e.aerodrome == key)

    def role_of(self, aerodrome: str) -> Role:
        """The most demanding role this operator gives the aerodrome.

        An aerodrome can be a destination on one route and an EDTO alternate on
        another. It does not stop being an EDTO alternate because it is also a
        destination, so the most demanding role governs.
        """
        found = self.entries_for(aerodrome)
        if not found:
            return Role.NOT_IN_NETWORK
        return min((e.role for e in found), key=lambda r: _ROLE_RANK[r])

    def is_sole_suitable(self, aerodrome: str) -> bool:
        return any(e.sole_suitable for e in self.entries_for(aerodrome))


@dataclass(frozen=True, slots=True)
class Fleet:
    """The aircraft types this operator flies, each carrying its own citations."""

    types: tuple[AircraftType, ...] = ()

    def __iter__(self) -> Iterator[AircraftType]:
        return iter(self.types)

    def __len__(self) -> int:
        return len(self.types)

    def __post_init__(self) -> None:
        designators = [t.designator for t in self.types]
        duplicated = {d for d in designators if designators.count(d) > 1}
        if duplicated:
            raise ValueError(
                f"the fleet lists {', '.join(sorted(duplicated))} more than once. "
                "Two manifests for one type are merged, not listed twice — "
                "otherwise one of them silently answers and the other does not."
            )

    def type(self, designator: str) -> AircraftType | None:
        wanted = designator.strip().upper()
        return next((t for t in self.types if t.designator.upper() == wanted), None)


@dataclass(frozen=True, slots=True)
class OperatorProfile:
    """One tenant: who they are, what they fly, and where.

    ``name`` exists so a finding can say whose it is. It never appears in any
    layer one or layer two document, and there are tests asserting that.
    """

    name: str
    fleet: Fleet = field(default_factory=Fleet)
    network: Network = field(default_factory=Network)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("OperatorProfile.name must be a non-empty string")


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def _grade(check: Check, *, role: Role, sole_suitable: bool) -> tuple[Exposure, str]:
    """One suitability check, read through one operator's use of the aerodrome.

    Returns the exposure and the reason it came out that way. The reason is
    always about *this* operator — the check's own detail already says what is
    true in general, and repeating it here would say nothing new.
    """
    if role is Role.NOT_IN_NETWORK:
        return (
            Exposure.NONE,
            "no exposure: this aerodrome is not in the network. The assessment "
            "beneath is complete, so adding it later needs no catch-up.",
        )

    if not role.is_planned_landing:
        # Overflown, not landed at. Fit checks bear on landing.
        return (
            Exposure.NONE,
            f"no exposure: this aerodrome is {role.value} only, and a fit check "
            "bears on landing there.",
        )

    only = " and it is the only one available in the region" if sole_suitable else ""

    if check.assessment is Assessment.NOT_SUITABLE:
        if role is Role.EDTO_ALTERNATE:
            return (
                Exposure.CRITICAL,
                "the aerodrome no longer qualifies for a type dispatched against "
                f"it as an EDTO en-route alternate{only}. A flight already "
                "airborne has no substitute.",
            )
        if role is Role.TAKEOFF_ALTERNATE:
            return (
                Exposure.CRITICAL,
                f"the aerodrome no longer qualifies as a take-off alternate{only}, "
                "and a departure below landing minima is planned against it.",
            )
        if role is Role.DESTINATION:
            return (
                Exposure.CRITICAL,
                "the aerodrome cannot take this type as a destination.",
            )
        if sole_suitable:
            return (
                Exposure.CRITICAL,
                "the aerodrome no longer qualifies as an alternate and it is the "
                "only one available in the region — there is nothing to swap to.",
            )
        return (
            Exposure.HIGH,
            "the aerodrome no longer qualifies as an alternate for this type. "
            "Another must be nominated before it is planned against again.",
        )

    if check.assessment is Assessment.RESTRICTED:
        if role.demands_certainty or sole_suitable:
            return (
                Exposure.HIGH,
                f"a condition now attaches at an aerodrome dispatch relies on{only}. "
                "It must be satisfiable every time, not on the day.",
            )
        return (
            Exposure.MEDIUM,
            "a condition now attaches to operating here. The operation stands "
            "once it is observed.",
        )

    if check.assessment is Assessment.UNKNOWN:
        if role.demands_certainty or sole_suitable:
            return (
                Exposure.HIGH,
                "this check could not be made at an aerodrome dispatch relies "
                f"on{only}. Not being able to check and having no exposure are "
                "opposite conclusions, and a flight cannot be planned against "
                "the second when the first is true.",
            )
        return (
            Exposure.UNKNOWN,
            "this check could not be made. It is not a pass, and it is not "
            "an absence of exposure.",
        )

    return (Exposure.NONE, "the check passed on held, cited data.")


@dataclass(frozen=True, slots=True)
class ExposureFinding:
    """One check, one aircraft type, and what it means for this operator."""

    designator: str
    check: Check
    exposure: Exposure
    reason: str
    role: Role
    sole_suitable: bool = False

    @property
    def needs_action(self) -> bool:
        return self.exposure.needs_action

    def describe(self) -> str:
        only = ", sole suitable" if self.sole_suitable else ""
        return (
            f"[{self.exposure.value}] {self.designator} · {self.check.name}"
            f"{'' if self.check.scope == 'aerodrome' else ' · ' + self.check.scope}"
            f" ({self.role.value}{only}) — {self.reason}"
        )

    def citations(self) -> tuple[str, ...]:
        return self.check.citations()


@dataclass(frozen=True, slots=True)
class OperatorAssessment:
    """One aerodrome, read for one operator, across their whole fleet."""

    operator: str
    aerodrome: str
    as_at: datetime
    role: Role
    sole_suitable: bool = False
    findings: tuple[ExposureFinding, ...] = ()
    suitability: tuple[Suitability, ...] = ()
    """The full layer-two assessment per type, kept even where exposure is
    ``NONE``. The record beneath survives; nothing is skipped to save work."""

    @property
    def overall(self) -> Exposure:
        """The worst exposure across the fleet. Never an average.

        An operator whose 777 cannot use an aerodrome is not "medium" because
        their A320 can.
        """
        return _worst(f.exposure for f in self.findings)

    @property
    def is_conclusive(self) -> bool:
        """Whether every check behind this was actually made and is current."""
        return bool(self.findings) and all(
            s.is_conclusive for s in self.suitability
        )

    @property
    def actionable(self) -> tuple[ExposureFinding, ...]:
        """What needs doing, worst first.

        Reading order, not a second severity. A reader scanning this list must
        meet the critical finding before three high ones, or the list buries
        the thing it exists to surface.
        """
        return tuple(
            sorted(
                (f for f in self.findings if f.needs_action),
                key=lambda f: (
                    _EXPOSURE_RANK[f.exposure], f.designator, f.check.name, f.check.scope
                ),
            )
        )

    @property
    def unknown(self) -> tuple[ExposureFinding, ...]:
        return tuple(f for f in self.findings if f.exposure is Exposure.UNKNOWN)

    def for_type(self, designator: str) -> tuple[ExposureFinding, ...]:
        wanted = designator.strip().upper()
        return tuple(f for f in self.findings if f.designator.upper() == wanted)

    def worst_by_type(self) -> dict[str, Exposure]:
        """The headline the plan asks for: the same change, two aeroplanes."""
        by_type: dict[str, list[Exposure]] = {}
        for finding in self.findings:
            by_type.setdefault(finding.designator, []).append(finding.exposure)
        return {name: _worst(levels) for name, levels in by_type.items()}

    def render(self) -> str:
        only = "  ·  SOLE SUITABLE" if self.sole_suitable else ""
        lines = [
            f"OPERATOR EXPOSURE — {self.aerodrome} for {self.operator}",
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}  ·  role: {self.role.value}{only}",
            "",
            f"Overall: {self.overall.value.upper()}"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE"),
            "",
            "This is exposure for one operator. The publication record and the "
            "generic",
            "assessment beneath it are the same for everyone and are unchanged "
            "by it.",
            "",
        ]
        if not self.findings:
            lines.append(
                "No aircraft type in the fleet produced a finding. A fleet with "
                "no types is not a clean answer."
            )
            return "\n".join(lines)

        worst = self.worst_by_type()
        lines.append("By type")
        for designator in sorted(worst):
            lines.append(f"  {designator:8} {worst[designator].value}")

        if self.actionable:
            lines += ["", "NEEDS ACTION"]
            for finding in self.actionable:
                lines.append(f"  {finding.describe()}")
                lines.append(f"      {finding.check.detail}")
                for citation in finding.citations():
                    lines.append(f"      {citation}")

        if self.unknown:
            lines += [
                "",
                "NOT CHECKED — these are not an absence of exposure",
            ]
            lines += [f"  {f.describe()}" for f in self.unknown]

        rest = [
            f
            for f in self.findings
            if not f.needs_action and f.exposure is not Exposure.UNKNOWN
        ]
        if rest:
            lines += ["", "No action"]
            lines += [f"  {f.describe()}" for f in rest]
        return "\n".join(lines)


def assess_operator(
    dossier: AerodromeDossier,
    profile: OperatorProfile,
) -> OperatorAssessment:
    """Assess one aerodrome for one operator, across their whole fleet.

    Every type is assessed even when the aerodrome is outside the network, and
    the full suitability record is attached to the result. That is deliberate:
    the plan's ordering means adding a destination needs no catch-up run,
    because the work was already done.
    """
    role = profile.network.role_of(dossier.aerodrome)
    sole = profile.network.is_sole_suitable(dossier.aerodrome)

    assessments: list[Suitability] = []
    findings: list[ExposureFinding] = []
    for aircraft in profile.fleet:
        assessment = assess_suitability(dossier, aircraft)
        assessments.append(assessment)
        for check in assessment.checks:
            exposure, reason = _grade(check, role=role, sole_suitable=sole)
            findings.append(
                ExposureFinding(
                    designator=aircraft.designator,
                    check=check,
                    exposure=exposure,
                    reason=reason,
                    role=role,
                    sole_suitable=sole,
                )
            )

    return OperatorAssessment(
        operator=profile.name,
        aerodrome=dossier.aerodrome,
        as_at=dossier.as_at,
        role=role,
        sole_suitable=sole,
        findings=tuple(findings),
        suitability=tuple(assessments),
    )


# --------------------------------------------------------------------------
# Loading a profile
# --------------------------------------------------------------------------
#
# A profile is not a citation manifest and deliberately carries no document
# hash. The other manifests describe something somebody *read* — an AIP page,
# an ACAP table — and the hash is what makes the reading resolvable. A profile
# describes the operator's own operation: which aerodromes they serve and in
# what capacity. There is no external document to be right or wrong about, and
# demanding a hash for one would be provenance theatre. What it does reference
# is aircraft manifests, and those carry their citations as usual.


def load_profile(path: Path | str) -> OperatorProfile:
    """Read an operator profile, and the cited aircraft manifests it names.

    ``fleet`` is a list of aircraft manifest paths, resolved relative to the
    profile. Several manifests for one designator are merged, so an ACAP
    document and an operator's own document describe one aeroplane while each
    figure keeps the citation it was read with.
    """
    path = Path(path)
    manifest = read_manifest(path)
    base = path.parent

    name = str(manifest.get("name", "")).strip()
    if not name:
        raise ManifestError(
            f"{path}: name is required — whose operation this describes. A "
            "finding has to be able to say whose it is."
        )

    listed = manifest.get("fleet", [])
    if not isinstance(listed, list):
        raise ManifestError(f"{path}: fleet must be a list of aircraft manifest paths")
    by_designator: dict[str, list[AircraftType]] = {}
    for entry in listed:
        resolved = Path(str(entry))
        if not resolved.is_absolute():
            resolved = base / resolved
        aircraft = load_aircraft(resolved)
        by_designator.setdefault(aircraft.designator, []).append(aircraft)
    fleet = Fleet(tuple(merge(*group) for group in by_designator.values()))

    rows = manifest.get("network", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: network must be a list of aerodrome entries")
    entries: list[NetworkEntry] = []
    for index, row in enumerate(rows):
        where = f"{path}: network[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        aerodrome = str(row.get("aerodrome", "")).strip()
        if not aerodrome:
            raise ManifestError(f"{where}: aerodrome is required")
        try:
            role = Role(str(row.get("role", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: role must be one of "
                f"{', '.join(r.value for r in Role)}. It is the multiplier on "
                "every severity here, so it is never defaulted."
            ) from None
        entries.append(
            NetworkEntry(
                aerodrome=aerodrome,
                role=role,
                sole_suitable=bool(row.get("sole_suitable", False)),
                note=str(row.get("note", "")),
            )
        )

    return OperatorProfile(name=name, fleet=fleet, network=Network(tuple(entries)))


_PROFILE_TEMPLATE = {
    "name": "",
    "fleet": [],
    "network": [
        {"aerodrome": "", "role": "destination", "sole_suitable": False, "note": ""}
    ],
}


def profile_template() -> str:
    """A blank operator profile.

    ``fleet`` takes paths to aircraft manifests; ``sole_suitable`` is the
    operator's own judgement about their region and nothing else can supply it.
    """
    return json.dumps(_PROFILE_TEMPLATE, indent=2)
