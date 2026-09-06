"""ENR 1.6 — the class says you are separated; this says how.

ENR 2 publishes the class of an airspace, and a class is a promise about
service: in Class A, ATC separates every aircraft from every other. What it
does not say is *by what means*. Separation is provided on surveillance or
procedurally, those are different operations, and the difference is published
here.

The finding this section exists for
------------------------------------
A region publishes Class A up to FL660 and surveillance coverage from FL200.
Below FL200 the class is unchanged and the separation is procedural: longer
spacing, position reports, a different contingency posture, and a controller
who cannot see you. Nothing in ENR 2 says so, nothing on the flight says so,
and the two sections have to be read together for it to appear at all.

So :func:`view_surveillance` takes the planned level and reports the regions
where it falls below the published coverage floor. Never as "no service" —
that is not what happens, and a crew reading it that way would be wrong in the
other direction. The service is provided procedurally.

What a floor means, and what its absence does not
--------------------------------------------------
A published coverage floor is what the State guarantees. A region that
publishes none has not published coverage everywhere; it has published
nothing, and this reports that as not knowing. The temptation to read a blank
column as "covered throughout" is exactly the failure the rest of this
platform is built against.

Primary radar is not surveillance of an identity
-------------------------------------------------
A primary return is an echo. It has no identity and no level, so a region
whose only surveillance is PSR cannot provide a service that depends on
knowing which aircraft is which. The kinds carry that distinction rather than
being one word, because "radar" covers two things that differ in what a
controller can do with them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.airspace import read_limit
from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)

__all__ = [
    "ServiceLevel",
    "Surveillance",
    "SurveillanceGap",
    "SurveillanceKind",
    "SurveillanceRegister",
    "SurveillanceView",
    "load_surveillance",
    "surveillance_template",
    "view_surveillance",
]

#: The parser identity written into citations read from an ENR 1.6 manifest.
SURVEILLANCE_PARSER_ID = "aeropub.surveillance"


class SurveillanceKind(str, Enum):
    """What the State has, which decides what a controller can do."""

    PSR = "psr"
    """Primary radar. An echo with a position and nothing else — no identity,
    no level. Whatever it supports, it is not a service that depends on
    knowing which aircraft is which."""

    SSR = "ssr"
    """Secondary radar, Mode A/C: a code and a pressure altitude."""

    MODE_S = "mode_s"
    ADS_B = "ads_b"
    ADS_C = "ads_c"
    """Contract-based, and the oceanic answer. Position when the contract
    says, not continuously, so the picture is a sequence of reports."""

    MLAT = "mlat"
    NONE = "none"
    """Published as having none. An answer, and not the same as an unread
    region."""

    OTHER = "other"

    @property
    def identifies_aircraft(self) -> bool:
        """Whether it tells a controller which aircraft this is."""
        return self in (
            SurveillanceKind.SSR,
            SurveillanceKind.MODE_S,
            SurveillanceKind.ADS_B,
            SurveillanceKind.ADS_C,
            SurveillanceKind.MLAT,
        )

    @property
    def is_cooperative(self) -> bool:
        """Whether it needs equipment in the aeroplane to work at all."""
        return self.identifies_aircraft

    @property
    def is_continuous(self) -> bool:
        """Whether the picture updates on its own.

        False for ADS-C, which reports when a contract says it should. A
        controller working ADS-C has a sequence of positions, not a display.
        """
        if self in (SurveillanceKind.ADS_C, SurveillanceKind.NONE):
            return False
        return True

    @property
    def label(self) -> str:
        return self.value.upper().replace("_", "-")


class ServiceLevel(str, Enum):
    """What the unit provides on the back of it."""

    RADAR_CONTROL = "radar_control"
    RADAR_ADVISORY = "radar_advisory"
    """Information and advice. The controller is not separating you."""

    FLIGHT_INFORMATION = "flight_information"
    PROCEDURAL = "procedural"
    """Separation without surveillance: time, distance and position reports."""

    NOT_STATED = "not_stated"

    @property
    def separates(self) -> bool | None:
        """Whether the unit provides separation on this service.

        ``None`` where the extract did not say. Procedural is ``True``: it is
        separation, provided differently, and a reader who took it for no
        service would be wrong in the direction that matters least but wrong.
        """
        if self is ServiceLevel.NOT_STATED:
            return None
        return self in (ServiceLevel.RADAR_CONTROL, ServiceLevel.PROCEDURAL)

    @property
    def needs_surveillance(self) -> bool:
        return self in (ServiceLevel.RADAR_CONTROL, ServiceLevel.RADAR_ADVISORY)

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass(frozen=True, slots=True)
class Surveillance:
    """One region's ENR 1.6 statement, as the State publishes it."""

    region: str
    source: SourceRef
    kinds: tuple[SurveillanceKind, ...] = ()
    service: ServiceLevel = ServiceLevel.NOT_STATED
    floor_ft: float | None = None
    """The level below which the State does not publish coverage. ``None``
    means none was published — never that coverage reaches the ground."""

    ceiling_ft: float | None = None
    carriage: tuple[str, ...] = ()
    """Equipment the State mandates, in the words it uses — "Mode S EHS",
    "ADS-B OUT 1090ES"."""

    unit: str = ""
    failure_procedure: str = ""
    """What to do when the surveillance is lost, as published. The paragraph
    nobody reads until the day it applies."""

    conditions: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(
            self, "carriage", tuple(str(c).strip() for c in self.carriage if str(c).strip())
        )
        if not self.region:
            raise ValueError(
                "Surveillance.region must be a non-empty string — which "
                "airspace this service is provided in."
            )
        for kind in self.kinds:
            if not isinstance(kind, SurveillanceKind):
                raise TypeError("Surveillance.kinds must be SurveillanceKind")
        if not isinstance(self.service, ServiceLevel):
            raise TypeError("Surveillance.service must be a ServiceLevel")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Surveillance.source must be a SourceRef")
        if (
            self.floor_ft is not None
            and self.ceiling_ft is not None
            and self.floor_ft > self.ceiling_ft
        ):
            raise ValueError(
                f"{self.region}: coverage from {self.floor_ft:g} ft to "
                f"{self.ceiling_ft:g} ft never covers anything. One of the two "
                "was read from the wrong row."
            )

    @property
    def identifies(self) -> bool:
        """Whether anything held here tells a controller which aircraft this is."""
        return any(kind.identifies_aircraft for kind in self.kinds)

    @property
    def is_continuous(self) -> bool:
        """Whether any held kind updates on its own."""
        return any(kind.is_continuous for kind in self.kinds)

    @property
    def coverage_known(self) -> bool:
        return self.floor_ft is not None or self.ceiling_ft is not None

    def covers(self, level_ft: float) -> bool | None:
        """Whether the published coverage reaches that level.

        ``None`` where no coverage was published. A blank column is not
        coverage throughout — it is a column nobody filled in.
        """
        if not self.coverage_known:
            return None
        if self.floor_ft is not None and level_ft < self.floor_ft:
            return False
        if self.ceiling_ft is not None and level_ft > self.ceiling_ft:
            return False
        return True

    def describe(self) -> str:
        parts = [self.region]
        parts.append(
            "/".join(k.label for k in self.kinds) if self.kinds else "kinds not held"
        )
        if self.service is not ServiceLevel.NOT_STATED:
            parts.append(self.service.label)
        if self.floor_ft is not None and self.ceiling_ft is not None:
            parts.append(f"{self.floor_ft:.0f}–{self.ceiling_ft:.0f} ft")
        elif self.floor_ft is not None:
            parts.append(f"{self.floor_ft:.0f} ft and above, no ceiling published")
        elif self.ceiling_ft is not None:
            parts.append(f"up to {self.ceiling_ft:.0f} ft, no floor published")
        else:
            parts.append("no coverage published")
        if self.carriage:
            parts.append("carriage: " + ", ".join(self.carriage))
        if self.unit:
            parts.append(self.unit)
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class SurveillanceGap:
    """A planned level below the coverage the State publishes."""

    region: str
    planned_ft: float
    surveillance: Surveillance
    airspace_class: str = ""
    """The class ENR 2 publishes there, where the caller supplied it. It is
    what makes the finding land: Class A at a level with no surveillance is
    still Class A."""

    @property
    def short_by_ft(self) -> float:
        return (self.surveillance.floor_ft or 0.0) - self.planned_ft

    def describe(self) -> str:
        text = (
            f"{self.region}: surveillance is published from "
            f"{self.surveillance.floor_ft:.0f} ft and the plan is "
            f"{self.planned_ft:.0f} ft — {self.short_by_ft:.0f} ft below it"
        )
        if self.airspace_class:
            text += (
                f". The airspace is still Class {self.airspace_class.upper()}"
                " and separation is still provided"
            )
        return text + ", procedurally rather than on a display"


@dataclass(frozen=True, slots=True)
class SurveillanceRegister:
    """Every ENR 1.6 statement read so far, and where it was read."""

    services: tuple[Surveillance, ...] = ()
    covers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
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

    def is_read(self, region: str) -> bool:
        return normalise(region) in self.covers

    def in_region(self, region: str) -> tuple[Surveillance, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(s for s in self.services if s.region == wanted)


@dataclass(frozen=True, slots=True)
class SurveillanceView:
    """What the crossed regions publish, and where the plan falls below it."""

    regions: tuple[str, ...] = ()
    services: tuple[Surveillance, ...] = ()
    unread_regions: tuple[str, ...] = ()
    gaps: tuple[SurveillanceGap, ...] = ()
    unpublished_coverage: tuple[str, ...] = ()
    """Regions read, with a service, and no coverage figure. Not covered
    throughout — a column nobody filled in."""

    planned_ft: float | None = None

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions and not self.unpublished_coverage

    @property
    def carriage(self) -> tuple[tuple[str, str], ...]:
        """Every mandated item, with the region mandating it."""
        return tuple(
            (service.region, item)
            for service in self.services
            for item in service.carriage
        )

    def render(self) -> str:
        lines = [
            "SURVEILLANCE — the class says you are separated; this says how",
            f"{len(self.regions)} regions  ·  {len(self.services)} published "
            f"statements"
            + (
                f"  ·  at {self.planned_ft:.0f} ft"
                if self.planned_ft is not None
                else "  ·  no level given, so no coverage was screened"
            ),
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 1.6 has been read for "
                f"{', '.join(self.unread_regions)}. Whether separation there "
                "is provided",
                "   on a display or procedurally is not something the held "
                "documents answer.",
            ]
        if self.gaps:
            lines += ["", "BELOW THE PUBLISHED COVERAGE"]
            for gap in self.gaps:
                lines.append(f"  {gap.describe()}")
        if self.unpublished_coverage:
            lines += [
                "",
                "NO COVERAGE FIGURE PUBLISHED",
                "  " + ", ".join(self.unpublished_coverage),
                "  Read, and the column is blank. That is not coverage "
                "throughout.",
            ]
        if self.carriage:
            lines += ["", "CARRIAGE MANDATED"]
            for region, item in self.carriage:
                lines.append(f"  {region}: {item}")
        if self.services:
            lines += ["", "PUBLISHED"]
            for service in self.services:
                lines.append(f"  {service.describe()}")
        return "\n".join(lines)


def view_surveillance(
    register: SurveillanceRegister,
    *,
    regions: Iterable[str],
    planned_ft: float | None = None,
    classes: Mapping[str, str] | None = None,
) -> SurveillanceView:
    """What ENR 1.6 says for these regions, against the planned level.

    ``classes`` is what ENR 2 publishes per region, supplied by the caller.
    It is not looked up here: this module is about surveillance, and the class
    is what makes the finding land rather than what produces it.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))
    published = {normalise(k): v for k, v in (classes or {}).items()}

    held: list[Surveillance] = []
    unread: list[str] = []
    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            continue
        held.extend(register.in_region(region))

    gaps: list[SurveillanceGap] = []
    blank: list[str] = []
    for service in held:
        if not service.coverage_known:
            if service.region not in blank:
                blank.append(service.region)
            continue
        if planned_ft is None:
            continue
        if service.covers(planned_ft) is False and service.floor_ft is not None:
            gaps.append(
                SurveillanceGap(
                    region=service.region,
                    planned_ft=planned_ft,
                    surveillance=service,
                    airspace_class=published.get(service.region, ""),
                )
            )

    return SurveillanceView(
        regions=wanted,
        services=tuple(held),
        unread_regions=tuple(unread),
        gaps=tuple(gaps),
        unpublished_coverage=tuple(blank),
        planned_ft=planned_ft,
    )


# --------------------------------------------------------------------------
# Reading an ENR 1.6 manifest
# --------------------------------------------------------------------------


def _enum(enum_type, value: object, *, where: str, field: str):
    try:
        return enum_type(
            str(value).strip().lower().replace("-", "_").replace(" ", "_")
        )
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ManifestError(f"{where}: {field} must be one of {allowed}") from None


def load_surveillance(path: Path | str) -> SurveillanceRegister:
    """Read one ENR 1.6 extract, with every statement cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=SURVEILLANCE_PARSER_ID,
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

    services: list[Surveillance] = []
    for index, row in enumerate(rows):
        where = f"{path}: services[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 1.6 "
                "this came from."
            )
        kinds = tuple(
            _enum(SurveillanceKind, k, where=where, field="kinds")
            for k in row.get("kinds", [])
        )
        try:
            services.append(
                Surveillance(
                    region=str(row.get("region", default_region)),
                    source=sub_source(document, locator),
                    kinds=kinds,
                    service=_enum(
                        ServiceLevel,
                        row.get("service", ServiceLevel.NOT_STATED.value),
                        where=where,
                        field="service",
                    ),
                    floor_ft=read_limit(row.get("floor"), where=where, field="floor"),
                    ceiling_ft=read_limit(
                        row.get("ceiling"), where=where, field="ceiling"
                    ),
                    carriage=tuple(str(c) for c in row.get("carriage", [])),
                    unit=str(row.get("unit", "")).strip(),
                    failure_procedure=str(row.get("failure_procedure", "")).strip(),
                    conditions=str(row.get("conditions", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return SurveillanceRegister(
        services=tuple(services), covers=frozenset(covers)
    )


_SURVEILLANCE_TEMPLATE = {
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
            "kinds": ["ssr", "mode_s"],
            "service": "radar_control",
            "floor": "",
            "ceiling": "",
            "carriage": [],
            "unit": "",
            "failure_procedure": "",
            "conditions": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def surveillance_template() -> str:
    """A blank ENR 1.6 extract.

    ``floor`` is the level below which the State does not publish coverage.
    Leave it out where the State publishes none — a blank column is not
    coverage to the ground, and this reports the two differently.

    ``kinds`` matter beyond bookkeeping: a primary return is an echo with no
    identity and no level, so a region whose only surveillance is PSR cannot
    provide a service that depends on knowing which aircraft is which.
    """
    return json.dumps(_SURVEILLANCE_TEMPLATE, indent=2)
