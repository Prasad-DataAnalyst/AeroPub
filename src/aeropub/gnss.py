"""ENR 4.3 — GNSS, and the two questions it is the only authority for.

Every other navigation aid in ENR 4 is a transmitter on a hill: it is on or
off, it reaches so far, and a NOTAM says when that stops being true. GNSS is
not like that, and the difference is what this module exists for.

**A satellite constellation is not published as coverage.** It is published as
an *approval*. A State does not tell you where GPS reaches — it tells you which
GNSS elements it has approved for which operations in its airspace, on what
conditions, and what you must do before you use them. That is a legal
statement, not a signal-strength one, and it is the thing an operator has to
read.

**Which is why an approach minimum can be on the plate and unavailable to
you.** An LPV line is drawn from SBAS. If the State has published no approved
SBAS service for that airspace, the line exists on the chart and there is no
authority to fly it. Nothing on the plate says so; ENR 4.3 is where it says so.
:func:`view_gnss` is built around that cross-check.

What this module refuses
------------------------
**It does not compute a RAIM prediction, and it never will.** A prediction
needs the current almanac, the health of each satellite, the aeroplane's
receiver model and the exact time and place of the operation. This platform
holds none of those. What it holds is the published *requirement* to obtain a
prediction and the *service the State names* for doing so — so the answer this
module gives is "a prediction is required here, from this provider, and it is
not in this document". A number computed from anything less would be a
prediction somebody flew on.

It also does not decide substitution. Whether GNSS may be flown in lieu of a
VOR radial or a DME arc is a State approval, published or not published, and
:func:`substitutions_for` lists what is published and stops. The same rule as
:func:`aeropub.navaids.alternatives_to`, for the same reason.

Four states, not two
--------------------
An approach capability comes back as one of four things, because collapsing
them loses the distinction a dispatcher needs:

=================  ==========================================================
``PUBLISHED``      An approved service covers it. Named, with its source
``WITHDRAWN``      A service exists and the State publishes it as not
                   approved, on trial, or withdrawn. Different from silence:
                   somebody decided this
``NOT_PUBLISHED``  ENR 4.3 was read for this region and names no such
                   service. For an approach capability that is an answer —
                   the authority to fly it is the publication — but only
                   because the region was read
``UNREAD``         Nobody has read ENR 4.3 here. Not an answer at all
=================  ==========================================================

The fourth state is why the register tracks which regions an extract *covers*
as well as which ones it has rows for. Without that, one loaded row about
EGNOS would make every unmentioned element read as "the State does not approve
it", and a coverage gap would have quietly become a finding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

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
    "GNSS",
    "NO_PREDICTION_COMPUTED",
    "ApproachCapability",
    "Augmentation",
    "Availability",
    "CapabilityFinding",
    "Constellation",
    "GnssRegister",
    "GnssService",
    "GnssView",
    "PredictionNote",
    "RaimRequirement",
    "ServiceStatus",
    "gnss_template",
    "load_gnss",
    "substitutions_for",
    "view_gnss",
]

#: The parser identity written into citations read from an ENR 4.3 manifest.
GNSS_PARSER_ID = "aeropub.gnss"

#: The entity kind a GNSS statement is keyed under. A NOTAM about GNSS is
#: written against airspace or a constellation rather than against a
#: transmitter, so both ``GNSS:OTDF`` and ``GNSS:GPS`` are looked for.
GNSS = "GNSS"

#: Said in full wherever a prediction requirement is reported. The sentence is
#: a constant rather than prose at each call site so it cannot drift into
#: sounding like a prediction was attempted.
NO_PREDICTION_COMPUTED = (
    "This platform does not compute RAIM predictions. A prediction needs the "
    "current almanac, satellite health, the receiver model and the exact time "
    "and place of the operation, and none of those are held here. Obtain it "
    "from the service the State publishes."
)


class Constellation(str, Enum):
    """A core satellite constellation, as ENR 4.3 names it."""

    GPS = "gps"
    GLONASS = "glonass"
    GALILEO = "galileo"
    BEIDOU = "beidou"
    QZSS = "qzss"
    NAVIC = "navic"
    OTHER = "other"


class Augmentation(str, Enum):
    """How the core signal is made good enough to navigate on.

    The three are not degrees of the same thing. They are three different
    places the integrity comes from, and which one a State has approved decides
    which operations are available in its airspace.
    """

    ABAS = "abas"
    """Airborne — the receiver checks itself, which is what RAIM is. Needs no
    ground or space segment, and therefore needs a *prediction* instead: its
    availability depends on the satellite geometry at the time and place."""

    SBAS = "sbas"
    """Satellite-based. A published footprint, and the only thing that makes
    an LPV or LP minimum available."""

    GBAS = "gbas"
    """Ground-based, at one aerodrome. What a GLS approach is flown on."""

    @property
    def is_airborne(self) -> bool:
        """Whether it lives in the aeroplane, and so has no published area."""
        return self is Augmentation.ABAS

    @property
    def needs_prediction(self) -> bool:
        """Whether its availability has to be predicted before the flight.

        True only for ABAS. An SBAS or GBAS service either covers the place or
        does not, and the State publishes which.
        """
        return self is Augmentation.ABAS


class ServiceStatus(str, Enum):
    """What the State says about this element."""

    APPROVED = "approved"
    TRIAL = "trial"
    """Published, and published as not yet operational. Not something to plan
    on, and not the same as absent."""

    NOT_APPROVED = "not_approved"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"

    @property
    def is_approved(self) -> bool | None:
        """``None`` where the extract carried no status. Never assumed."""
        if self is ServiceStatus.UNKNOWN:
            return None
        return self is ServiceStatus.APPROVED


class RaimRequirement(str, Enum):
    """What the State requires be done before departure."""

    REQUIRED = "required"
    RECOMMENDED = "recommended"
    NOT_REQUIRED = "not_required"
    NOT_STATED = "not_stated"
    """The extract did not say. Reported as not knowing, never as not
    required — the two look identical in a table and are opposite in a
    briefing."""

    @property
    def is_binding(self) -> bool:
        return self is RaimRequirement.REQUIRED


class ApproachCapability(str, Enum):
    """A minimum line on an approach plate, and what it is flown on."""

    LNAV = "lnav"
    LNAV_VNAV = "lnav_vnav"
    LP = "lp"
    LPV = "lpv"
    GLS = "gls"

    @property
    def satisfied_by(self) -> frozenset[Augmentation]:
        """Which augmentations can provide this line.

        LNAV/VNAV is in here with ABAS because its vertical guidance may come
        from baro-VNAV rather than from the satellite signal — the aeroplane
        provides it. LP and LPV cannot be flown on anything but SBAS, and GLS
        on anything but GBAS, and that is the whole point of the distinction.
        """
        if self is ApproachCapability.GLS:
            return frozenset({Augmentation.GBAS})
        if self in (ApproachCapability.LP, ApproachCapability.LPV):
            return frozenset({Augmentation.SBAS})
        return frozenset({Augmentation.ABAS, Augmentation.SBAS})

    @property
    def has_vertical_guidance(self) -> bool:
        return self is not ApproachCapability.LNAV

    @property
    def label(self) -> str:
        return self.value.upper().replace("_", "/")


class Availability(str, Enum):
    """Whether a capability is available on the State's own authority.

    Four states because three of them would collapse silence into a decision.
    """

    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"
    NOT_PUBLISHED = "not_published"
    UNREAD = "unread"

    @property
    def is_available(self) -> bool | None:
        """``None`` only for :attr:`UNREAD`.

        :attr:`NOT_PUBLISHED` is ``False`` and not ``None`` on purpose: the
        authority to fly a satellite-based minimum *is* the publication, so a
        region that was read and does not approve it has answered. That only
        holds because :attr:`UNREAD` exists to carry the other case.
        """
        if self is Availability.UNREAD:
            return None
        return self is Availability.PUBLISHED


@dataclass(frozen=True, slots=True)
class GnssService:
    """One statement of ENR 4.3, as the State publishes it."""

    region: str
    augmentation: Augmentation
    source: SourceRef
    system: str = ""
    """The name the State gives it — ``EGNOS``, ``WAAS``, ``GAGAN``. Empty for
    a bare ABAS statement, which usually names no system at all."""

    constellations: tuple[Constellation, ...] = ()
    status: ServiceStatus = ServiceStatus.UNKNOWN
    service_area: str = ""
    """As published, in the State's own words. Never derived: a footprint
    computed from anything else is a guarantee nobody gave."""

    approved_operations: tuple[str, ...] = ()
    """The operations this element is published as approved for, in the codes
    the AIP prints — ``RNAV 5``, ``RNP APCH``, ``LPV``, ``RNP 4``."""

    capabilities: tuple[ApproachCapability, ...] = ()
    """Approach lines the State names explicitly. Where it names none, the
    augmentation still decides what it can provide — an approved SBAS service
    supports an LPV line whether or not the paragraph spells that out."""

    raim_prediction: RaimRequirement = RaimRequirement.NOT_STATED
    prediction_service: str = ""
    """Who the State says to obtain the prediction from. The pointer this
    module exists to hand over, since it computes none itself."""

    substitutes_for: tuple[str, ...] = ()
    """Aids or procedures the State publishes GNSS as usable in lieu of. A
    list of what is published, never a ruling on what may be done."""

    conditions: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(self, "system", str(self.system).strip().upper())
        object.__setattr__(
            self,
            "substitutes_for",
            tuple(normalise(s) for s in self.substitutes_for),
        )
        object.__setattr__(
            self,
            "approved_operations",
            tuple(str(o).strip() for o in self.approved_operations if str(o).strip()),
        )
        if not self.region:
            raise ValueError(
                "GnssService.region must be a non-empty string. A GNSS "
                "approval is an approval for one State's airspace, and one "
                "with no airspace attached would be read as global."
            )
        if not isinstance(self.augmentation, Augmentation):
            raise TypeError("GnssService.augmentation must be an Augmentation")
        if not isinstance(self.status, ServiceStatus):
            raise TypeError("GnssService.status must be a ServiceStatus")
        if not isinstance(self.raim_prediction, RaimRequirement):
            raise TypeError(
                "GnssService.raim_prediction must be a RaimRequirement"
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError("GnssService.source must be a SourceRef")

    @property
    def name(self) -> str:
        """What to call it in a line of output."""
        return self.system or self.augmentation.value.upper()

    def provides(self, capability: ApproachCapability) -> bool:
        """Whether this element can provide that approach line.

        An explicit list on the service wins, because a State that enumerates
        its approved lines has said something more specific than the physics.
        Otherwise the augmentation decides.
        """
        if self.capabilities:
            return capability in self.capabilities
        return self.augmentation in capability.satisfied_by

    def describe(self) -> str:
        parts = [f"{self.region} {self.name}"]
        if self.constellations:
            parts.append("/".join(c.value.upper() for c in self.constellations))
        parts.append(self.status.value.replace("_", " "))
        if self.approved_operations:
            parts.append(", ".join(self.approved_operations))
        if self.service_area:
            parts.append(self.service_area)
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class GnssRegister:
    """Every ENR 4.3 statement read so far, and where it was read.

    ``covers`` is the second half and the load-bearing one. A region in it has
    been read; a region not in it has not, and the difference decides whether
    an unmentioned capability comes back as ``NOT_PUBLISHED`` or ``UNREAD``.
    """

    services: tuple[GnssService, ...] = ()
    covers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # Publishing a row for a region is itself a claim to have read it, so
        # the two sources of coverage are unioned rather than checked against
        # each other.
        object.__setattr__(
            self,
            "covers",
            frozenset(normalise(r) for r in self.covers if normalise(r))
            | {s.region for s in self.services},
        )

    def __len__(self) -> int:
        return len(self.services)

    def __iter__(self):
        return iter(self.services)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted(self.covers))

    @property
    def systems(self) -> tuple[str, ...]:
        return tuple(sorted({s.system for s in self.services if s.system}))

    def is_read(self, region: str) -> bool:
        return normalise(region) in self.covers

    def in_region(self, region: str) -> tuple[GnssService, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(s for s in self.services if s.region == wanted)

    def approved_in(self, region: str) -> tuple[GnssService, ...]:
        return tuple(
            s for s in self.in_region(region) if s.status is ServiceStatus.APPROVED
        )


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    """One approach line, in one region, and whether the State authorises it."""

    region: str
    capability: ApproachCapability
    availability: Availability
    basis: str
    service: GnssService | None = None
    notams: tuple[tuple[RegisteredNotam, ForceState], ...] = ()
    """NOTAM in force against GNSS here — against the airspace, against the
    named system, or against a constellation the service depends on."""

    @property
    def is_available(self) -> bool | None:
        """Whether the line may be planned on, as far as anything held says.

        ``None`` where a NOTAM is in force against an otherwise published
        capability. The same rule :class:`aeropub.navaids.NavaidUse` applies to
        an aid: a NOTAM reopens the question the publication had answered, and
        returning the published value would be the AIP answering over it.
        """
        if self.notams and self.availability is Availability.PUBLISHED:
            return None
        return self.availability.is_available

    def describe(self) -> str:
        text = (
            f"{self.region} {self.capability.label}: "
            f"{self.availability.value.replace('_', ' ')} — {self.basis}"
        )
        if self.notams:
            numbers = ", ".join(n.identifier for n, _ in self.notams)
            text += f"  ·  reopened by {numbers}"
        return text


@dataclass(frozen=True, slots=True)
class PredictionNote:
    """A region where something must be obtained before the flight."""

    region: str
    requirement: RaimRequirement
    service: str = ""
    why: str = ""

    @property
    def is_binding(self) -> bool:
        return self.requirement.is_binding

    def describe(self) -> str:
        where = f" from {self.service}" if self.service else " — no provider published"
        return (
            f"{self.region}: RAIM prediction {self.requirement.value.replace('_', ' ')}"
            f"{where}" + (f" ({self.why})" if self.why else "")
        )


@dataclass(frozen=True, slots=True)
class GnssView:
    """What the crossed regions publish about GNSS, and what they do not."""

    regions: tuple[str, ...] = ()
    services: tuple[GnssService, ...] = ()
    unread_regions: tuple[str, ...] = ()
    capabilities: tuple[CapabilityFinding, ...] = ()
    predictions: tuple[PredictionNote, ...] = ()
    outages: tuple[tuple[str, RegisteredNotam, ForceState], ...] = ()

    @property
    def is_conclusive(self) -> bool:
        """False while any region is unread. Not a quality score."""
        return not self.unread_regions

    @property
    def unavailable(self) -> tuple[CapabilityFinding, ...]:
        """Capabilities the State does not authorise. Excludes the unread."""
        return tuple(f for f in self.capabilities if f.is_available is False)

    @property
    def unanswered(self) -> tuple[CapabilityFinding, ...]:
        return tuple(f for f in self.capabilities if f.is_available is None)

    @property
    def reopened(self) -> tuple[CapabilityFinding, ...]:
        """Capabilities the State publishes and a NOTAM has put back in doubt.

        A subset of :attr:`unanswered`, kept separately because it needs a
        different action: the unread ones need somebody to read an AIP, and
        these need somebody to read a NOTAM.
        """
        return tuple(
            f
            for f in self.capabilities
            if f.notams and f.availability is Availability.PUBLISHED
        )

    @property
    def must_predict(self) -> tuple[PredictionNote, ...]:
        return tuple(n for n in self.predictions if n.is_binding)

    def render(self) -> str:
        lines = [
            "GNSS — what the State approves, and what it requires first",
            f"{len(self.regions)} regions  ·  {len(self.services)} published "
            f"statements  ·  {len(self.outages)} NOTAM in force",
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 4.3 has been read for "
                f"{', '.join(self.unread_regions)}. Nothing is approved and",
                "   nothing is refused there — neither reading is available "
                "from what is held.",
            ]
        if self.capabilities:
            lines += ["", "APPROACH CAPABILITY — what the plate offers vs what "
                      "the State authorises"]
            for finding in self.capabilities:
                lines.append(f"  {finding.describe()}")
        if self.predictions:
            lines += ["", "BEFORE DEPARTURE"]
            for note in self.predictions:
                lines.append(f"  {note.describe()}")
            lines += [f"  {NO_PREDICTION_COMPUTED}"]
        if self.outages:
            lines += ["", "NOTAM AGAINST GNSS"]
            for key, notam, force in self.outages:
                lines.append(f"  {key}  {notam.identifier}  {force.value}")
        if self.services:
            lines += ["", "PUBLISHED"]
            for service in self.services:
                lines.append(f"  {service.describe()}")
        return "\n".join(lines)


def view_gnss(
    register: GnssRegister,
    *,
    regions: Iterable[str],
    capabilities: Iterable[ApproachCapability] = (),
    notams: NotamRegister | None = None,
    at: datetime | None = None,
) -> GnssView:
    """What ENR 4.3 says for these regions, and what it does not say.

    ``capabilities`` are the approach lines the operation intends to use — the
    lines printed on the plates being flown. Each is answered per region, so a
    sector that crosses one State approving LPV and one that does not gets two
    findings rather than an average.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))
    asked = tuple(dict.fromkeys(capabilities))

    services: list[GnssService] = []
    unread: list[str] = []
    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            continue
        services.extend(register.in_region(region))

    in_force: dict[str, tuple[tuple[RegisteredNotam, ForceState], ...]] = {}
    outages: list[tuple[str, RegisteredNotam, ForceState]] = []
    if notams is not None and at is not None:
        # A GNSS NOTAM is written against airspace or against a constellation,
        # never against a box on a hill, so both key spaces are searched.
        keys = [named(GNSS, r) for r in wanted]
        keys += [named(GNSS, c.value) for c in Constellation]
        keys += [named(GNSS, s) for s in register.systems]
        for key in dict.fromkeys(keys):
            against = notams.at(key, at)
            if against:
                in_force[key] = against
            for notam, force in against:
                outages.append((key, notam, force))

    findings: list[CapabilityFinding] = []
    for region in wanted:
        for capability in asked:
            findings.append(_capability_in(register, region, capability, in_force))

    predictions: list[PredictionNote] = []
    for region in wanted:
        note = _prediction_in(register, region)
        if note is not None:
            predictions.append(note)

    return GnssView(
        regions=wanted,
        services=tuple(services),
        unread_regions=tuple(unread),
        capabilities=tuple(findings),
        predictions=tuple(predictions),
        outages=tuple(outages),
    )


def _against(
    in_force: Mapping[str, tuple[tuple[RegisteredNotam, ForceState], ...]],
    region: str,
    service: GnssService | None,
) -> tuple[tuple[RegisteredNotam, ForceState], ...]:
    """Every NOTAM that reaches this capability in this region.

    Three key spaces, because a GNSS outage is filed against whichever of them
    the originator thought in: the airspace, the augmentation system by name,
    or the constellation the service depends on.
    """
    keys = [named(GNSS, region)]
    if service is not None:
        if service.system:
            keys.append(named(GNSS, service.system))
        keys += [named(GNSS, c.value) for c in service.constellations]
    found: list[tuple[RegisteredNotam, ForceState]] = []
    for key in dict.fromkeys(keys):
        found.extend(in_force.get(key, ()))
    return tuple(dict.fromkeys(found))


def _capability_in(
    register: GnssRegister,
    region: str,
    capability: ApproachCapability,
    in_force: Mapping[str, tuple[tuple[RegisteredNotam, ForceState], ...]] | None = None,
) -> CapabilityFinding:
    """Resolve one approach line against one region's published approvals."""
    in_force = in_force or {}
    if not register.is_read(region):
        return CapabilityFinding(
            region=region,
            capability=capability,
            availability=Availability.UNREAD,
            basis="no ENR 4.3 read for this region",
        )

    candidates = [s for s in register.in_region(region) if s.provides(capability)]
    approved = [s for s in candidates if s.status is ServiceStatus.APPROVED]
    if approved:
        service = approved[0]
        return CapabilityFinding(
            region=region,
            capability=capability,
            availability=Availability.PUBLISHED,
            basis=f"{service.name} approved",
            service=service,
            notams=_against(in_force, region, service),
        )
    if candidates:
        service = candidates[0]
        return CapabilityFinding(
            region=region,
            capability=capability,
            availability=Availability.WITHDRAWN,
            basis=(
                f"{service.name} is published as "
                f"{service.status.value.replace('_', ' ')}"
            ),
            service=service,
            notams=_against(in_force, region, service),
        )
    needs = ", ".join(sorted(a.value.upper() for a in capability.satisfied_by))
    return CapabilityFinding(
        region=region,
        capability=capability,
        availability=Availability.NOT_PUBLISHED,
        basis=(
            f"needs {needs}; ENR 4.3 was read here and approves no such service"
        ),
        notams=_against(in_force, region, None),
    )


def _prediction_in(register: GnssRegister, region: str) -> PredictionNote | None:
    """The strongest prediction requirement published for a region.

    Strongest, not first: a State that requires a prediction for RNP 4 and
    only recommends one for RNAV 5 has required one, and reporting the weaker
    sentence because it was printed earlier would lose the requirement.
    """
    if not register.is_read(region):
        return None
    order = {
        RaimRequirement.REQUIRED: 3,
        RaimRequirement.RECOMMENDED: 2,
        RaimRequirement.NOT_REQUIRED: 1,
        RaimRequirement.NOT_STATED: 0,
    }
    held = register.in_region(region)
    if not held:
        return None
    strongest = max(held, key=lambda s: order[s.raim_prediction])
    if strongest.raim_prediction in (
        RaimRequirement.NOT_STATED,
        RaimRequirement.NOT_REQUIRED,
    ):
        return None
    airborne = [s for s in held if s.augmentation.needs_prediction]
    why = (
        f"{airborne[0].name} is the airborne augmentation here"
        if airborne
        else ""
    )
    return PredictionNote(
        region=region,
        requirement=strongest.raim_prediction,
        service=strongest.prediction_service,
        why=why,
    )


def substitutions_for(
    register: GnssRegister, *, region: str, aid: str
) -> tuple[GnssService, ...]:
    """What the State publishes about GNSS in lieu of a named aid.

    **Not a ruling.** Whether GNSS may be flown in place of a particular aid
    on a particular procedure is an approval this platform does not hold. This
    returns the published statements that name the aid, and the choice stays
    the operator's — the same discipline as
    :func:`aeropub.navaids.alternatives_to`, for the same reason.
    """
    wanted = normalise(aid)
    if not wanted:
        return ()
    return tuple(
        s for s in register.in_region(region) if wanted in s.substitutes_for
    )


# --------------------------------------------------------------------------
# Reading an ENR 4.3 manifest
# --------------------------------------------------------------------------


def load_gnss(path: Path | str) -> GnssRegister:
    """Read one ENR 4.3 extract, with every statement cited to it.

    ``covers`` in the manifest is the list of regions the extract was read
    for. It is separate from the rows because reading a State's ENR 4.3 and
    finding it approves no SBAS is a *result*, and without somewhere to record
    that the result is indistinguishable from never having looked.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=GNSS_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    covers = manifest.get("covers", [])
    if not isinstance(covers, list):
        raise ManifestError(
            f"{path}: covers must be a list of the regions this extract was "
            "read for"
        )
    if default_region:
        covers = list(covers) + [default_region]

    rows = manifest.get("services", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: services must be a list")

    services: list[GnssService] = []
    for index, row in enumerate(rows):
        where = f"{path}: services[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 4.3 "
                "this came from."
            )
        augmentation = _enum(
            Augmentation, row.get("augmentation"), where=where, field="augmentation"
        )
        status = _enum(
            ServiceStatus,
            row.get("status", ServiceStatus.UNKNOWN.value),
            where=where,
            field="status",
        )
        requirement = _enum(
            RaimRequirement,
            row.get("raim_prediction", RaimRequirement.NOT_STATED.value),
            where=where,
            field="raim_prediction",
        )
        constellations = tuple(
            _enum(Constellation, c, where=where, field="constellations")
            for c in row.get("constellations", [])
        )
        capabilities = tuple(
            _enum(ApproachCapability, c, where=where, field="capabilities")
            for c in row.get("capabilities", [])
        )
        try:
            services.append(
                GnssService(
                    region=str(row.get("region", default_region)),
                    augmentation=augmentation,
                    source=sub_source(document, locator),
                    system=str(row.get("system", "")),
                    constellations=constellations,
                    status=status,
                    service_area=str(row.get("service_area", "")).strip(),
                    approved_operations=tuple(
                        str(o) for o in row.get("approved_operations", [])
                    ),
                    capabilities=capabilities,
                    raim_prediction=requirement,
                    prediction_service=str(row.get("prediction_service", "")).strip(),
                    substitutes_for=tuple(
                        str(s) for s in row.get("substitutes_for", [])
                    ),
                    conditions=str(row.get("conditions", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return GnssRegister(services=tuple(services), covers=frozenset(covers))


def _enum(enum_type, value: object, *, where: str, field: str):
    try:
        return enum_type(str(value).strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ManifestError(
            f"{where}: {field} must be one of {allowed}. There is no safe "
            "default: what a State approves decides what may be flown."
        ) from None


_GNSS_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "covers": [],
    "services": [
        {
            "region": "",
            "augmentation": "sbas",
            "system": "",
            "constellations": ["gps"],
            "status": "approved",
            "service_area": "",
            "approved_operations": [],
            "capabilities": [],
            "raim_prediction": "not_stated",
            "prediction_service": "",
            "substitutes_for": [],
            "conditions": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def gnss_template() -> str:
    """A blank ENR 4.3 extract.

    ``covers`` lists every region this extract was read for, including any
    that turned out to approve nothing. It is what separates "read, and no
    SBAS is approved here" from "nobody has looked", and those two are opposite
    answers to the question of whether an LPV line may be flown.

    ``service_area`` is the State's own words. It is never derived: a footprint
    computed from anything else is a guarantee nobody gave. ``raim_prediction``
    records the published *requirement* — this platform computes no prediction
    and ``prediction_service`` is where the State says to obtain one.
    """
    return json.dumps(_GNSS_TEMPLATE, indent=2)
