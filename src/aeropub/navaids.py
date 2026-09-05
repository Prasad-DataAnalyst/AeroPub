"""ENR 4 — navigation aids, and the change with the widest blast radius.

A navaid is a small row in a table and the thing most of an instrument
procedure hangs from. Change its identifier and every plate that names it is
wrong. Change its frequency and every plate that tunes it is wrong. Take it off
the air for maintenance and every approach built on it is unavailable, along
with the airway segments that use it and the holding patterns that reference
it — none of which say so anywhere on their own face.

That asymmetry is why this module exists. :mod:`aeropub.charts` already maps a
``navaid`` change to an approach-plate amendment, and until now it had nothing
to source that change from. ENR 4.1 is the source.

What a navaid entry has to carry
---------------------------------
Four fields do the work, and each one breaks something different:

============  ================================================================
``ident``     What the plate names and what the crew identifies. A change here
              invalidates the plate's text, not just its numbers
``frequency`` What the crew tunes. A change here is silent in the cockpit —
              the wrong frequency is not an error message, it is no
              identification
``coverage``  How far the signal is usable. What makes a fix reachable, and
              what a route built on it quietly depends on
``hours``     When it is on. A navaid on daylight hours is not a navaid for a
              night arrival, and nothing on the approach plate says so
============  ================================================================

Substitution is a decision, not a lookup
-----------------------------------------
When a navaid is out, the question a planner asks is what else reaches the
fix. This module answers what the AIP publishes — which aids serve the same
fix or the same aerodrome, at what range and on what hours — and stops there.
Whether a particular aid may be substituted for another in a particular
procedure is an operational approval question this platform does not hold and
must not appear to answer. :func:`alternatives_to` is named for what it does:
it lists what else is published nearby, and the document says the choice is
the operator's.

What this does not do
---------------------
It does not compute coverage from transmitter power, terrain or line of sight.
The published designated operational coverage is what a State guarantees, and
a figure derived from anything else would be a range nobody promised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import aerodrome_of, named, normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)
from aeropub.notam_register import ForceState, NotamRegister, RegisteredNotam

__all__ = [
    "NAVAID",
    "Navaid",
    "NavaidKind",
    "NavaidRegister",
    "NavaidStatus",
    "NavaidUse",
    "alternatives_to",
    "load_navaids",
    "navaid_template",
    "screen_navaids",
]

#: The parser identity written into citations read from an ENR 4 manifest.
NAVAID_PARSER_ID = "aeropub.navaids"

#: The entity kind a navigation aid is keyed under. The same key space
#: :mod:`aeropub.ats` uses for a navaid significant point, so a NOTAM against
#: ``DOH`` lands on the route that names it and the register that describes it.
NAVAID = "NAVAID"


class NavaidKind(str, Enum):
    """What sort of aid this is, and therefore what it can be used for."""

    VOR = "vor"
    DVOR = "dvor"
    VOR_DME = "vor_dme"
    VORTAC = "vortac"
    DME = "dme"
    NDB = "ndb"
    TACAN = "tacan"
    ILS = "ils"
    LOCALIZER = "localizer"
    GLIDEPATH = "glidepath"
    MARKER = "marker"
    GBAS = "gbas"
    OTHER = "other"

    @property
    def gives_bearing(self) -> bool:
        """Whether it provides azimuth. A DME alone does not."""
        return self in (
            NavaidKind.VOR,
            NavaidKind.DVOR,
            NavaidKind.VOR_DME,
            NavaidKind.VORTAC,
            NavaidKind.NDB,
            NavaidKind.TACAN,
            NavaidKind.LOCALIZER,
        )

    @property
    def gives_distance(self) -> bool:
        return self in (
            NavaidKind.DME,
            NavaidKind.VOR_DME,
            NavaidKind.VORTAC,
            NavaidKind.TACAN,
        )

    @property
    def is_approach_aid(self) -> bool:
        """Whether it exists to serve one runway rather than the route.

        The ones that do are aerodrome equipment published in AD 2 as well as
        here, and an outage on one is an approach problem rather than a route
        problem.
        """
        return self in (
            NavaidKind.ILS,
            NavaidKind.LOCALIZER,
            NavaidKind.GLIDEPATH,
            NavaidKind.MARKER,
            NavaidKind.GBAS,
        )


class NavaidStatus(str, Enum):
    """What the publication says about the aid's availability."""

    OPERATIONAL = "operational"
    ON_TEST = "on_test"
    """Radiating and not to be used for navigation. The dangerous one: it is
    on the air, it identifies, and it must not be relied on."""

    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"
    """Published without stating. Reported as unknown rather than assumed
    operational — an aid assumed serviceable is one a route is planned on."""

    @property
    def usable(self) -> bool | None:
        if self is NavaidStatus.UNKNOWN:
            return None
        return self is NavaidStatus.OPERATIONAL


@dataclass(frozen=True, slots=True)
class Navaid:
    """One aid, as ENR 4.1 publishes it."""

    ident: str
    kind: NavaidKind
    source: SourceRef
    name: str = ""
    region: str = ""
    aerodrome: str = ""
    """The aerodrome it serves, where it serves one. Empty for an en-route
    aid, and set for an approach aid so an outage reaches the right dossier."""

    frequency_mhz: float | None = None
    channel: str = ""
    """A DME or TACAN channel, where the publication gives one instead of a
    frequency."""

    coverage_nm: float | None = None
    """Designated operational coverage, as published. Never derived: a range
    computed from power or terrain is a figure nobody promised."""

    coverage_ft: float | None = None
    """The level to which that coverage is designated, where published."""

    status: NavaidStatus = NavaidStatus.UNKNOWN
    hours: str = ""
    magnetic_variation: float | None = None
    serves: tuple[str, ...] = ()
    """Fixes, airways or procedures published as depending on this aid. What
    turns an outage into a list of consequences instead of a single row."""

    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ident", normalise(self.ident))
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        object.__setattr__(self, "serves", tuple(normalise(s) for s in self.serves))
        if not self.ident:
            raise ValueError("Navaid.ident must be a non-empty string")
        if not isinstance(self.kind, NavaidKind):
            raise TypeError("Navaid.kind must be a NavaidKind")
        if not isinstance(self.status, NavaidStatus):
            raise TypeError("Navaid.status must be a NavaidStatus")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Navaid.source must be a SourceRef")

    @property
    def key(self) -> str:
        return named(NAVAID, self.ident)

    @property
    def is_usable(self) -> bool | None:
        """Whether the publication says it may be relied on.

        ``None`` where the status was not read. An aid assumed serviceable is
        an aid a route gets planned on, so the absence stays visible.
        """
        return self.status.usable

    def covers(self, distance_nm: float, level_ft: float | None = None) -> bool | None:
        """Whether the published coverage reaches that far.

        ``None`` where no coverage figure is held, which is common and must
        not read as unlimited. A route depending on an aid whose range nobody
        published is depending on a guess.
        """
        if self.coverage_nm is None:
            return None
        if distance_nm > self.coverage_nm:
            return False
        if (
            level_ft is not None
            and self.coverage_ft is not None
            and level_ft > self.coverage_ft
        ):
            return False
        return True

    def describe(self) -> str:
        parts = [f"{self.ident} {self.kind.value.upper().replace('_', '/')}"]
        if self.name:
            parts.append(self.name)
        if self.frequency_mhz is not None:
            parts.append(f"{self.frequency_mhz:.3f}")
        elif self.channel:
            parts.append(f"CH {self.channel}")
        if self.coverage_nm is not None:
            reach = f"{self.coverage_nm:.0f} NM"
            if self.coverage_ft is not None:
                reach += f"/{self.coverage_ft:.0f} ft"
            parts.append(reach)
        parts.append(self.status.value.replace("_", " "))
        if self.hours:
            parts.append(self.hours)
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class NavaidRegister:
    """Every aid read so far."""

    navaids: tuple[Navaid, ...] = ()

    def __len__(self) -> int:
        return len(self.navaids)

    def __iter__(self):
        return iter(self.navaids)

    @property
    def idents(self) -> tuple[str, ...]:
        return tuple(sorted({n.ident for n in self.navaids}))

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({n.region for n in self.navaids if n.region}))

    def navaid(self, ident: str) -> Navaid | None:
        """The aid with this identifier.

        Identifiers are not globally unique — two States may both publish a
        ``KIA`` — so this returns the first held and :meth:`all_named` returns
        every one. A caller that must be certain names the region.
        """
        wanted = normalise(ident)
        return next((n for n in self.navaids if n.ident == wanted), None)

    def all_named(self, ident: str) -> tuple[Navaid, ...]:
        wanted = normalise(ident)
        return tuple(n for n in self.navaids if n.ident == wanted)

    def in_region(self, region: str) -> tuple[Navaid, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(n for n in self.navaids if n.region == wanted)

    def at(self, aerodrome: str) -> tuple[Navaid, ...]:
        """Every aid published as serving this aerodrome.

        A blank argument returns nothing rather than every en-route aid. The
        empty string is what an aid carries when it serves *no* aerodrome, so
        matching on it would answer "which aids are at nowhere" with a list —
        a confident answer to a question nobody asked.
        """
        wanted = normalise(aerodrome)
        if not wanted:
            return ()
        return tuple(n for n in self.navaids if n.aerodrome == wanted)

    def serving(self, thing: str) -> tuple[Navaid, ...]:
        """Every aid published as serving this fix, airway or procedure."""
        wanted = normalise(thing)
        if not wanted:
            return ()
        return tuple(n for n in self.navaids if wanted in n.serves)


def alternatives_to(
    register: NavaidRegister, ident: str, *, bearing: bool | None = None
) -> tuple[Navaid, ...]:
    """What else the AIP publishes near the same place.

    **Not a substitution ruling.** Whether one aid may stand in for another in
    a particular procedure is an operational approval question this platform
    does not hold, and a function that appeared to answer it would be read as
    doing so. This lists what else is published serving the same fixes or the
    same aerodrome, and the choice stays the operator's.

    ``bearing`` narrows to aids that give azimuth, or to those that do not —
    useful because a DME cannot replace a VOR and a list that mixed them would
    have to be filtered by the reader anyway.
    """
    found = register.navaid(ident)
    if found is None:
        return ()
    wanted = set(found.serves)
    others = []
    for other in register.navaids:
        if other.ident == found.ident:
            continue
        shares = bool(wanted & set(other.serves))
        same_field = bool(found.aerodrome) and other.aerodrome == found.aerodrome
        if not (shares or same_field):
            continue
        if bearing is not None and other.kind.gives_bearing is not bearing:
            continue
        others.append(other)
    return tuple(others)


@dataclass(frozen=True, slots=True)
class NavaidUse:
    """One aid a route or procedure depends on, and what is known about it."""

    navaid: Navaid | None
    ident: str
    used_by: tuple[str, ...] = ()
    """What names it — an airway, a fix, a procedure."""

    notams: tuple[tuple[RegisteredNotam, ForceState], ...] = ()

    @property
    def is_held(self) -> bool:
        return self.navaid is not None

    @property
    def is_usable(self) -> bool | None:
        """Whether it may be relied on, as far as anything held says.

        ``None`` covers two different absences — the aid is not held at all,
        or it is held without a status — and both mean the same thing to a
        planner: nobody has confirmed it works.
        """
        if self.navaid is None:
            return None
        if self.notams:
            # A NOTAM in force against the aid overrides a published status
            # the same way it overrides any other published value.
            return None
        return self.navaid.is_usable

    def describe(self) -> str:
        if self.navaid is None:
            return f"{self.ident}: not in the held ENR 4"
        text = self.navaid.describe()
        if self.notams:
            text += f"  ·  {len(self.notams)} NOTAM in force"
        if self.used_by:
            text += f"  ·  used by {', '.join(self.used_by)}"
        return text


def screen_navaids(
    register: NavaidRegister,
    idents: Iterable[str],
    *,
    notams: NotamRegister | None = None,
    at: datetime | None = None,
    used_by: Mapping[str, Iterable[str]] | None = None,
) -> tuple[NavaidUse, ...]:
    """What is known about each aid a route or procedure names.

    An identifier not in the register comes back as a held-nothing entry
    rather than being dropped. A route that names an aid we have never read is
    not a route with fewer aids — and dropping it would make the screen get
    shorter as coverage got worse.
    """
    uses: list[NavaidUse] = []
    for ident in idents:
        wanted = normalise(ident)
        if not wanted:
            continue
        found = register.navaid(wanted)
        against = ()
        if notams is not None and at is not None:
            key = found.key if found else named(NAVAID, wanted)
            against = notams.at(key, at)
        uses.append(
            NavaidUse(
                navaid=found,
                ident=wanted,
                used_by=tuple((used_by or {}).get(wanted, ())),
                notams=against,
            )
        )
    return tuple(uses)


# --------------------------------------------------------------------------
# Reading an ENR 4 manifest
# --------------------------------------------------------------------------


def _number(value: object, *, where: str, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field} {value!r} is not a number. A frequency that "
            "cannot be read is left unread — a rounded one is a frequency "
            "nobody can tune."
        ) from None


def load_navaids(path: Path | str) -> NavaidRegister:
    """Read one ENR 4 extract, with every aid cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=NAVAID_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    rows = manifest.get("navaids", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: navaids must be a list")

    navaids: list[Navaid] = []
    for index, row in enumerate(rows):
        where = f"{path}: navaids[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which row of ENR 4 this came "
                "from."
            )
        try:
            kind = NavaidKind(str(row.get("kind", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in NavaidKind)}. What an aid "
                "provides decides what it can be used for, so there is no "
                "safe default."
            ) from None
        try:
            status = NavaidStatus(
                str(row.get("status", NavaidStatus.UNKNOWN.value)).strip().lower()
            )
        except ValueError:
            raise ManifestError(
                f"{where}: status must be one of "
                f"{', '.join(s.value for s in NavaidStatus)}"
            ) from None

        aerodrome = str(row.get("aerodrome", "")).strip()
        if aerodrome and aerodrome_of(aerodrome) != normalise(aerodrome):
            raise ManifestError(
                f"{where}: aerodrome {aerodrome!r} names an object on an "
                "aerodrome rather than the aerodrome itself"
            )

        try:
            navaids.append(
                Navaid(
                    ident=str(row.get("ident", "")),
                    kind=kind,
                    source=sub_source(document, locator),
                    name=str(row.get("name", "")).strip(),
                    region=str(row.get("region", default_region)).strip(),
                    aerodrome=aerodrome,
                    frequency_mhz=_number(
                        row.get("frequency_mhz"), where=where, field="frequency_mhz"
                    ),
                    channel=str(row.get("channel", "")).strip(),
                    coverage_nm=_number(
                        row.get("coverage_nm"), where=where, field="coverage_nm"
                    ),
                    coverage_ft=_number(
                        row.get("coverage_ft"), where=where, field="coverage_ft"
                    ),
                    status=status,
                    hours=str(row.get("hours", "")).strip(),
                    magnetic_variation=_number(
                        row.get("magnetic_variation"),
                        where=where,
                        field="magnetic_variation",
                    ),
                    serves=tuple(str(s) for s in row.get("serves", [])),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return NavaidRegister(navaids=tuple(navaids))


_NAVAID_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "navaids": [
        {
            "ident": "",
            "kind": "vor_dme",
            "name": "",
            "region": "",
            "aerodrome": "",
            "frequency_mhz": None,
            "channel": "",
            "coverage_nm": None,
            "coverage_ft": None,
            "status": "operational",
            "hours": "",
            "magnetic_variation": None,
            "serves": [],
            "remarks": "",
            "locator": "",
        }
    ],
}


def navaid_template() -> str:
    """A blank ENR 4 extract.

    ``coverage_nm`` is the designated operational coverage as published, and
    is left out where the State does not publish one — never derived, because
    a range computed from power or terrain is a figure nobody promised.
    ``serves`` lists the fixes, airways and procedures published as depending
    on the aid, and is what turns an outage into a list of consequences rather
    than a single row. ``aerodrome`` is set only for an approach aid, so an
    outage reaches the right dossier.
    """
    return json.dumps(_NAVAID_TEMPLATE, indent=2)
